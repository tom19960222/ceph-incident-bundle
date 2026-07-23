#!/usr/bin/env bash
set -euo pipefail

# Collect read-only Rook/Ceph Kubernetes evidence.

ROOK_COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$ROOK_COLLECTOR_DIR/common.sh"

# Default kubectl prefix (local); collect_cluster_rook overrides it per call.
ROOK_KUBECTL_ARGV=(kubectl)

usage() {
  cat <<'EOF'
Usage: collect-cluster-rook.sh --out DIR --manifest PATH [--namespace rook-ceph]
       [--operator-namespace rook-ceph] [--since DURATION] [--timeout SECONDS] [--allow-skip]
       [--ssh-target USER@HOST --ssh-key PATH] [--kube-context CTX]
EOF
}

rook_skip() {
  write_skip_artifact "$1/cluster/rook/SKIPPED.txt" "$2"
}

rook_run_capture() {
  local outdir=$1 manifest=$2 timeout=$3 artifact_rel=$4
  shift 4

  local artifact="$outdir/$artifact_rel"
  if ! COMMAND_TIMEOUT="$timeout" ERROR_LOG="${ERROR_LOG:-$outdir/errors.log}" \
    run_capture "$manifest" "rook" "collect-cluster-rook" "$artifact" -- "$@"; then
    return 2
  fi
  return 0
}

rook_get_first_pod() {
  local namespace=$1 label=$2
  # -o name (not jsonpath) so the arg has no braces/brackets to mangle over ssh.
  # `|| true`: a lookup failure (e.g. remote kubectl/ssh error) must yield an
  # empty result (-> SKIPPED artifact), not abort the collector under set -e.
  { "${ROOK_KUBECTL_ARGV[@]}" get pods -n "$namespace" -l "$label" -o name 2>/dev/null || true; } |
    head -n1 | sed 's#^pod/##'
}

rook_compact_error() {
  local value=$1
  value=${value//$'\r'/}
  value=${value//$'\n'/ | }
  printf '%s' "$value"
}

rook_namespace_probe_failure_reason() {
  local namespace=$1 kube_context=$2 target=$3 detail=$4
  local compact nocase_was_set=0
  compact="$(rook_compact_error "$detail")"

  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch
  if [[ "$detail" =~ (kubectl:?[[:space:]]+command[[:space:]]+not[[:space:]]+found|command[[:space:]]+not[[:space:]]+found:?[[:space:]]+kubectl) ]]; then
    printf 'kubectl command not found on %s' "$target"
  elif [[ "$detail" =~ (no[[:space:]]+context[[:space:]]+exists|context.*(not[[:space:]]+found|does[[:space:]]+not[[:space:]]+exist)) ]]; then
    printf 'kubectl context not found: %s on %s' "${kube_context:-<current-context>}" "$target"
  elif [[ "$detail" =~ (connection[[:space:]]+to[[:space:]]+the[[:space:]]+server|unable[[:space:]]+to[[:space:]]+connect[[:space:]]+to[[:space:]]+the[[:space:]]+server|connection[[:space:]]+refused|i/o[[:space:]]+timeout|context[[:space:]]+deadline[[:space:]]+exceeded|no[[:space:]]+route[[:space:]]+to[[:space:]]+host|tls[[:space:]]+handshake[[:space:]]+timeout) ]]; then
    printf 'kubectl cannot connect to cluster API from %s' "$target"
  elif [[ "$detail" =~ (namespace.*not[[:space:]]+found|namespaces.*not[[:space:]]+found|notfound) ]]; then
    printf 'rook namespace not found: %s' "$namespace"
  elif [[ "$detail" =~ (forbidden|unauthorized|permission[[:space:]]+denied) ]]; then
    printf 'kubectl cannot read rook namespace due to authorization failure: %s on %s' "$namespace" "$target"
  else
    printf 'kubectl namespace probe failed for rook namespace %s on %s' "$namespace" "$target"
  fi
  if [[ $nocase_was_set -eq 1 ]]; then shopt -s nocasematch; else shopt -u nocasematch; fi

  [[ -n "$compact" ]] && printf ': %s' "$compact"
}

collect_cluster_rook() {
  local outdir='' manifest='' namespace=rook-ceph since=24h timeout=20 allow_skip=0
  local operator_namespace='' ssh_target='' ssh_key='' kube_context=''

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out)
        outdir=${2-}
        shift 2
        ;;
      --manifest)
        manifest=${2-}
        shift 2
        ;;
      --namespace)
        namespace=${2-}
        shift 2
        ;;
      --operator-namespace)
        operator_namespace=${2-}
        shift 2
        ;;
      --since)
        since=${2-}
        shift 2
        ;;
      --timeout)
        timeout=${2-}
        shift 2
        ;;
      --allow-skip)
        allow_skip=1
        shift
        ;;
      --ssh-target)
        ssh_target=${2-}
        shift 2
        ;;
      --ssh-key)
        ssh_key=${2-}
        shift 2
        ;;
      --kube-context)
        kube_context=${2-}
        shift 2
        ;;
      --help|-h)
        usage
        return 0
        ;;
      *)
        usage >&2
        return 1
        ;;
    esac
  done

  [[ -n "$outdir" && -n "$manifest" ]] || {
    usage >&2
    return 1
  }

  ensure_dir "$outdir/cluster/rook"
  [[ -n "$operator_namespace" ]] || operator_namespace="$namespace"

  # Build the kubectl prefix once. With --ssh-target, kubectl runs ON that node
  # over ssh (the node where kubectl/kubeconfig lives); otherwise locally.
  # ROOK_KUBECTL_ARGV is global so rook_get_first_pod can use the same prefix.
  if [[ -n "$ssh_target" ]]; then
    local -a sopts; local _w
    while IFS= read -r _w; do sopts+=("$_w"); done < <(ssh_base_opts "$ssh_key" "$timeout")
    ROOK_KUBECTL_ARGV=(ssh "${sopts[@]}" "$ssh_target" kubectl)
  else
    ROOK_KUBECTL_ARGV=(kubectl)
  fi
  [[ -n "$kube_context" ]] && ROOK_KUBECTL_ARGV+=(--context "$kube_context")

  # Missing kubectl / namespace means we collected NO cluster evidence. In
  # explicit rook mode that is a partial failure (exit 2) so the bundle does not
  # falsely look complete; auto-mode fallback passes --allow-skip to tolerate it.
  # (When kubectl is remote we already probed it exists, so skip the local check.)
  if [[ -z "$ssh_target" ]] && ! command -v kubectl >/dev/null 2>&1; then
    rook_skip "$outdir" "kubectl command not found"
    [[ "$allow_skip" == "1" ]] && return 0 || return 2
  fi

  local namespace_probe_output namespace_probe_reason
  if ! namespace_probe_output="$("${ROOK_KUBECTL_ARGV[@]}" get namespace "$namespace" 2>&1)"; then
    namespace_probe_reason="$(rook_namespace_probe_failure_reason "$namespace" "$kube_context" "${ssh_target:-local}" "$namespace_probe_output")"
    rook_skip "$outdir" "$namespace_probe_reason"
    [[ "$allow_skip" == "1" ]] && return 0 || return 2
  fi

  progress "rook: pods/events/resources/operator-log/toolbox…"
  local failed=0
  if ! rook_run_capture "$outdir" "$manifest" "$timeout" "cluster/rook/pods-wide.txt" \
    "${ROOK_KUBECTL_ARGV[@]}" get pods -n "$namespace" -o wide; then
    failed=1
  fi
  if ! rook_run_capture "$outdir" "$manifest" "$timeout" "cluster/rook/events.txt" \
    "${ROOK_KUBECTL_ARGV[@]}" get events -n "$namespace" --sort-by=.lastTimestamp; then
    failed=1
  fi
  if ! rook_run_capture "$outdir" "$manifest" "$timeout" "cluster/rook/rook-resources.yaml" \
    "${ROOK_KUBECTL_ARGV[@]}" get cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io -n "$namespace" -o yaml; then
    failed=1
  fi

  local operator_pod toolbox_pod
  operator_pod="$(rook_get_first_pod "$operator_namespace" "app=rook-ceph-operator")"
  if [[ -n "$operator_pod" ]]; then
    if ! rook_run_capture "$outdir" "$manifest" "$timeout" "cluster/rook/operator.log" \
      "${ROOK_KUBECTL_ARGV[@]}" logs -n "$operator_namespace" "$operator_pod" --since="$since"; then
      failed=1
    fi
  else
    write_skip_artifact "$outdir/cluster/rook/operator-SKIPPED.txt" "rook operator Pod not found in namespace: $operator_namespace"
  fi

  toolbox_pod="$(rook_get_first_pod "$namespace" "app=rook-ceph-tools")"
  if [[ -n "$toolbox_pod" ]]; then
    if ! rook_run_capture "$outdir" "$manifest" "$timeout" "cluster/rook/toolbox-status.txt" \
      "${ROOK_KUBECTL_ARGV[@]}" exec -n "$namespace" "$toolbox_pod" -- ceph status; then
      failed=1
    fi
  else
    write_skip_artifact "$outdir/cluster/rook/toolbox-SKIPPED.txt" "rook toolbox Pod not found"
  fi

  [[ $failed -eq 0 ]] || return 2
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  collect_cluster_rook "$@"
fi
