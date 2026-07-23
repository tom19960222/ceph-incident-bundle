#!/usr/bin/env bash
set -euo pipefail

# Ceph incident bundle collection entrypoint.

COLLECT_RUN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECT_ROOT="$(cd "$COLLECT_RUN_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$COLLECT_ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$COLLECT_ROOT/lib/collect-cluster-cephadm.sh"
# shellcheck disable=SC1091
source "$COLLECT_ROOT/lib/collect-cluster-rook.sh"
# shellcheck disable=SC1091
source "$COLLECT_ROOT/lib/collect-prometheus.sh"
# shellcheck disable=SC1091
source "$COLLECT_ROOT/lib/bundle.sh"

usage() {
  cat <<'EOF'
Usage: collect.sh --inventory PATH --ssh-key PATH [options]

One command example:
  bash run/collect.sh \
    --inventory inventory/ceph-lab.example.env \
    --ssh-key .ssh/id_ed25519 \
    --mode cephadm \
    --since 24h

Required:
  --inventory PATH       shell inventory with HOSTS=( "alias=host" ... )
  --ssh-key PATH         SSH private key used to reach every node

Options:
  --seed USER@HOST       override inventory SEED_HOST
  --out DIR              output dir (default: results)
  --mode auto|cephadm|rook   auto = per-node detect, collect ceph and/or rook layer
  --kube-context CTX     kubectl context for the rook layer (default: none)
  --kube-mode MODE       where the rook layer runs kubectl: remote (on an
                         inventory node, default) or local (this jump host)
  --since DURATION       log/journal window (default: 24h)
  --prom-url URL         optional Prometheus base URL; dump metrics of scrape
                         jobs matching --prom-job-regex over the --since window
  --prom-job-regex RE    scrape-job filter for the dump (default: ceph|node)
  --prom-step SECONDS    query_range step (default: max(15, window/10000))
  --prom-timeout SECONDS overall time budget for the metrics dump (default: 600)
  --timeout SECONDS      per-command / SSH-connect timeout (default: 20)
  --node-timeout SECONDS overall timeout for one node's full collection (default: 600)
  --skip-logs            collect state but skip /var/log file collection
  --keep-original-logs   retain active/rotated source files in addition to merged logs
  --var-log-max-bytes N  per-node /var/log output cap (default: 10737418240);
                         use "unlimited" to disable the cap
  --allow-cephadm-shell  permit cephadm shell fallback (may start/pull a container);
                         disabled by default for operational read-only collection
  --allow-kubectl-exec   permit toolbox kubectl exec (starts a Pod process);
                         disabled by default for operational read-only collection
  --trust-ssh-host-key   accept new SSH host keys automatically (default)
  --no-trust-ssh-host-key
                         use OpenSSH's normal host-key checking behavior
  --redact               redact sensitive-looking lines before packaging (default)
  --no-redact            keep raw collected text without redaction
  --quiet                suppress progress output on stderr (stdout still prints bundle:)
  --keep-workdir         keep temporary extracted workdir for debugging
  --help                 print this help

Output:
  DIR/ceph-incident-YYYYMMDDTHHMMSSZ.tar.gz

Exit codes:
  0 complete, 2 partial collection failure with bundle produced, 1 usage/config/verify failure
EOF
}

detect_node_caps() {
  # echo a space-joined subset of "cephadm kubectl" present on the target node.
  # A probe that fails to ssh is NOT the same as "node has no caps" — record the
  # ssh failure to ERROR_LOG so a silently-dropped cluster source is visible.
  local target=$1 ssh_key=$2 timeout=$3
  local tbin out rc _w
  local -a ssh_cmd sopts
  while IFS= read -r _w; do sopts+=("$_w"); done < <(ssh_base_opts "$ssh_key" "$timeout")
  # SC2016: the probe script is single-quoted on purpose — it expands on the remote.
  # shellcheck disable=SC2016
  ssh_cmd=(ssh "${sopts[@]}" "$target" 'caps=""; command -v cephadm >/dev/null 2>&1 && caps="$caps cephadm"; command -v ceph >/dev/null 2>&1 && caps="$caps ceph"; command -v kubectl >/dev/null 2>&1 && caps="$caps kubectl"; printf "%s\n" "$caps"')
  tbin="$(timeout_cmd)"
  if [[ -n "$tbin" ]]; then
    ssh_cmd=("$tbin" "$timeout" "${ssh_cmd[@]}")
  fi
  out="$("${ssh_cmd[@]}" 2>/dev/null)"
  rc=$?
  if [[ $rc -ne 0 && -n "${ERROR_LOG:-}" ]]; then
    ensure_dir "$(dirname -- "$ERROR_LOG")"
    printf '%s capability probe failed for %s (ssh exit %s) — node not considered as a cluster source\n' \
      "$(date -u +%FT%TZ)" "$target" "$rc" >>"$ERROR_LOG"
    write_ssh_debug_log "$(dirname -- "$ERROR_LOG")" capability-probe "$target" "$ssh_key" "$timeout"
  fi
  printf '%s' "$out"
}

# Does a given runner actually connect to the cluster on this node? "usable" is
# defined as `ceph -s` succeeding, not merely the binary existing.
ceph_runner_probe() {
  local target=$1 ssh_key=$2 timeout=$3 method=$4
  local tbin w rc
  local -a pfx ssh_cmd sopts
  while IFS= read -r w; do pfx+=("$w"); done < <(ceph_runner_argv "$method")
  while IFS= read -r w; do sopts+=("$w"); done < <(ssh_base_opts "$ssh_key" "$timeout")
  ssh_cmd=(ssh "${sopts[@]}" "$target" "${pfx[@]}" --connect-timeout 5 -s)
  tbin="$(timeout_cmd)"
  [[ -n "$tbin" ]] && ssh_cmd=("$tbin" "$timeout" "${ssh_cmd[@]}")
  if "${ssh_cmd[@]}" >/dev/null 2>&1; then
    return 0
  else
    rc=$?
  fi
  if [[ $rc -eq 255 && -n "${ERROR_LOG:-}" ]]; then
    write_ssh_debug_log "$(dirname -- "$ERROR_LOG")" "cluster-ceph-${method}" "$target" "$ssh_key" "$timeout"
  fi
  return "$rc"
}

# Pick the fastest runner that connects on $target: direct ceph, sudo ceph, then
# cephadm shell. Echoes the runner token, or nothing if none connects.
ceph_runner_for() {
  local target=$1 ssh_key=$2 timeout=$3 m
  local methods="direct sudo"
  [[ "${CEPH_INCIDENT_ALLOW_CEPHADM_SHELL:-0}" == "1" ]] && methods="$methods cephadm"
  for m in $methods; do
    if ceph_runner_probe "$target" "$ssh_key" "$timeout" "$m"; then
      printf '%s' "$m"
      return 0
    fi
  done
  # Contract: success with EMPTY stdout = no runner connected. Callers must test
  # for empty output, not the exit status (which is always 0).
  return 0
}

# Probe each node once; pick cluster-ceph source (first cephadm node, or --seed)
# and cluster-rook source (first kubectl node); collect each requested layer once.
# Uses globals HOST_TARGETS (set by main).
collect_clusters() {
  local mode='' workdir='' manifest='' seed='' ssh_key='' since=24h timeout=20
  local rook_namespace=rook-ceph kube_context='' kube_mode=remote rook_operator_namespace=''
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode) mode=${2-}; shift 2 ;;
      --workdir) workdir=${2-}; shift 2 ;;
      --manifest) manifest=${2-}; shift 2 ;;
      --seed) seed=${2-}; shift 2 ;;
      --ssh-key) ssh_key=${2-}; shift 2 ;;
      --since) since=${2-}; shift 2 ;;
      --timeout) timeout=${2-}; shift 2 ;;
      --namespace) rook_namespace=${2-}; shift 2 ;;
      --operator-namespace) rook_operator_namespace=${2-}; shift 2 ;;
      --kube-context) kube_context=${2-}; shift 2 ;;
      --kube-mode) kube_mode=${2-}; shift 2 ;;
      *) return 1 ;;
    esac
  done
  local ceph_source='' ceph_runner='' rook_source='' i caps rc=0
  local want_ceph=0 want_rook=0 ceph_done=0 rook_done=0
  # so detect_node_caps can record probe ssh failures
  local ERROR_LOG="$workdir/errors.log"
  case "$mode" in
    cephadm) want_ceph=1 ;;
    rook) want_rook=1 ;;
    auto) want_ceph=1; want_rook=1 ;;
    *) return 1 ;;
  esac

  # explicit --seed pins the cluster-ceph source; the runner is still chosen by
  # connectivity (so direct ceph is preferred on the seed too).
  if [[ $want_ceph -eq 1 && -n "$seed" ]]; then
    ceph_source="$seed"
    ceph_runner="$(ceph_runner_for "$seed" "$ssh_key" "$timeout")"
  fi

  # Only probe nodes for kubectl when the rook layer runs remotely.
  local probe_rook=0
  [[ $want_rook -eq 1 && "$kube_mode" == remote ]] && probe_rook=1

  # probe nodes only if a source we need is still unknown
  if { [[ $want_ceph -eq 1 && -z "$ceph_source" ]]; } || [[ $probe_rook -eq 1 ]]; then
    if [[ ${#HOST_TARGETS[@]} -gt 0 ]]; then
      progress "probing ${#HOST_TARGETS[@]} nodes for capabilities…"
      for i in "${!HOST_TARGETS[@]}"; do
        caps="$(detect_node_caps "${HOST_TARGETS[$i]}" "$ssh_key" "$timeout")"
        progress "[$((i + 1))/${#HOST_TARGETS[@]}] probe ${HOST_TARGETS[$i]}: ${caps:-none}"
        # ceph source = first candidate (has ceph or cephadm binary) whose runner
        # actually connects to the cluster.
        if [[ $want_ceph -eq 1 && -z "$ceph_source" ]]; then
          case " $caps " in
            *" ceph "*|*" cephadm "*)
              ceph_runner="$(ceph_runner_for "${HOST_TARGETS[$i]}" "$ssh_key" "$timeout")"
              [[ -n "$ceph_runner" ]] && ceph_source="${HOST_TARGETS[$i]}"
              ;;
          esac
        fi
        if [[ $probe_rook -eq 1 && -z "$rook_source" ]]; then
          case " $caps " in *" kubectl "*) rook_source="${HOST_TARGETS[$i]}" ;; esac
        fi
        if { [[ $want_ceph -eq 0 || -n "$ceph_source" ]]; } && { [[ $probe_rook -eq 0 || -n "$rook_source" ]]; }; then
          break
        fi
      done
    fi
  fi

  # cluster-ceph layer
  if [[ $want_ceph -eq 1 && -n "$ceph_source" && -n "$ceph_runner" ]]; then
    progress "collecting ceph cluster from $ceph_source via ${ceph_runner}…"
    collect_cluster_cephadm "$workdir" "$manifest" "$ceph_source" "$ssh_key" "$since" "$timeout" "$ceph_runner" || rc=2
    ceph_done=1
  fi

  # cluster-rook layer: local (this jump host) or remote (on a kubectl node)
  if [[ $want_rook -eq 1 && ( "$kube_mode" == local || -n "$rook_source" ) ]]; then
    local -a rook_args
    rook_args=(--out "$workdir" --manifest "$manifest" --namespace "$rook_namespace" --since "$since" --timeout "$timeout")
    [[ -n "$rook_operator_namespace" ]] && rook_args+=(--operator-namespace "$rook_operator_namespace")
    if [[ "$kube_mode" == local ]]; then
      rook_source=local
      progress "collecting rook from local kubectl (ns=$rook_namespace)…"
    else
      rook_args+=(--ssh-target "$rook_source" --ssh-key "$ssh_key")
      progress "collecting rook from $rook_source (ns=$rook_namespace)…"
    fi
    [[ -n "$kube_context" ]] && rook_args+=(--kube-context "$kube_context")
    [[ "$mode" == auto ]] && rook_args+=(--allow-skip)
    collect_cluster_rook "${rook_args[@]}" || rc=2
    # "done" means real rook evidence was collected — NOT just an --allow-skip
    # (e.g. namespace missing) which returns 0 but only writes SKIPPED.txt.
    [[ -f "$workdir/cluster/rook/pods-wide.txt" ]] && rook_done=1
  fi

  # missing-source handling
  # When a layer wasn't collected, leave a SKIPPED.txt — but never clobber a more
  # specific reason the collector already wrote (e.g. "namespace not found").
  if [[ "$mode" == cephadm && $ceph_done -eq 0 ]]; then
    write_skip_artifact_once "$workdir/cluster/ceph/SKIPPED.txt" "no cephadm-capable node found (or --seed unreachable)"
    rc=2
  elif [[ "$mode" == rook && $rook_done -eq 0 ]]; then
    write_skip_artifact_once "$workdir/cluster/rook/SKIPPED.txt" "no kubectl-capable node found"
    rc=2
  elif [[ "$mode" == auto ]]; then
    # auto = collect whatever exists; only a hard failure if NEITHER layer found
    [[ $ceph_done -eq 0 ]] && write_skip_artifact_once "$workdir/cluster/ceph/SKIPPED.txt" "no cephadm-capable node in inventory (auto)"
    [[ $rook_done -eq 0 ]] && write_skip_artifact_once "$workdir/cluster/rook/SKIPPED.txt" "no kubectl-capable node in inventory (auto)"
    [[ $ceph_done -eq 0 && $rook_done -eq 0 ]] && rc=2
  fi

  # Record which node each cluster layer was collected from (observability:
  # "which host did we trust for ceph status?").
  {
    printf 'ceph_source=%s\n' "${ceph_source:-<none>}"
    printf 'ceph_runner=%s\n' "${ceph_runner:-<none>}"
    printf 'rook_source=%s\n' "${rook_source:-<none>}"
  } >>"$workdir/environment.txt"

  return "$rc"
}

collect_remote_node() {
  local workdir=$1 alias=$2 target=$3 ssh_key=$4 since=$5 timeout=$6 skip_logs=$7 node_timeout=$8
  local keep_original_logs=$9 var_log_max_bytes=${10}
  local node_dir="$workdir/nodes/$alias"
  local node_tar="$workdir/.node-$alias.tar.gz"
  local remote_cmd rc=0 tbin _w
  local q_alias q_since q_timeout q_var_log_max_bytes
  local -a ssh_cmd sopts

  q_alias="$(shell_quote "$alias")" || return 1
  q_since="$(shell_quote "$since")" || return 1
  q_timeout="$(shell_quote "$timeout")" || return 1
  q_var_log_max_bytes="$(shell_quote "$var_log_max_bytes")" || return 1

  # Remote side uses a gzip pipe (not `tar -z`) so minimal/BusyBox tar still works,
  # and traps its own temp dir so an interrupted/timed-out run leaves nothing behind.
  remote_cmd="set -u; tmp=\$(mktemp -d \"\${TMPDIR:-/tmp}/ceph-incident-node.XXXXXXXX\") || { printf 'SKIPPED: remote tmp not writable\n' >&2; exit 75; }; trap 'case \"\$tmp\" in \"\${TMPDIR:-/tmp}\"/ceph-incident-node.*) rm -rf -- \"\$tmp\" ;; esac' EXIT INT TERM; gzip -dc | tar -xf - -C \"\$tmp\"; out=\"\$tmp/out\"; set +e; bash \"\$tmp/lib/collect-node.sh\" --out \"\$out\" --host-alias $q_alias --since $q_since --timeout $q_timeout --var-log-max-bytes $q_var_log_max_bytes"
  if [[ "$skip_logs" == "1" ]]; then
    remote_cmd+=" --skip-logs"
  fi
  if [[ "$keep_original_logs" == "1" ]]; then
    remote_cmd+=" --keep-original-logs"
  fi
  remote_cmd+="; rc=\$?; set -e; if [ -d \"\$out\" ]; then set -o pipefail; tar -cf - -C \"\$out\" . | gzip -c || exit 74; else mkdir -p \"\$out\"; printf 'SKIPPED: remote collect-node did not create output\n' >\"\$out/SKIPPED.txt\"; set -o pipefail; tar -cf - -C \"\$out\" . | gzip -c || exit 74; fi; exit \"\$rc\""

  while IFS= read -r _w; do sopts+=("$_w"); done < <(ssh_base_opts "$ssh_key" "$timeout")
  ssh_cmd=(ssh "${sopts[@]}" "$target" "$remote_cmd")
  tbin="$(timeout_cmd)"
  if [[ -n "$tbin" ]]; then
    # Outer wrapper bounds the WHOLE node collection — must be the generous
    # node timeout, never the small per-command timeout (which would kill a
    # slow/large node mid-collection).
    ssh_cmd=("$tbin" "$node_timeout" "${ssh_cmd[@]}")
  fi

  # macOS bsdtar embeds com.apple.* xattr headers that make the remote GNU tar
  # print "Ignoring unknown extended header keyword" noise; strip them at source.
  local noxattrs=''
  if tar --version 2>&1 | grep -qi 'bsdtar'; then
    noxattrs='--no-xattrs'
  fi

  set +e
  # shellcheck disable=SC2086
  COPYFILE_DISABLE=1 tar $noxattrs -cf - -C "$COLLECT_ROOT" \
    lib/common.sh lib/collect-node.sh lib/collect-var-log.sh | gzip -c |
    "${ssh_cmd[@]}" >"$node_tar"
  rc=$?
  set -e
  if [[ $rc -eq 255 || $rc -eq 124 || $rc -eq 137 ]]; then
    write_ssh_debug_log "$workdir" "node-${alias}" "$target" "$ssh_key" "$timeout"
  fi

  ensure_dir "$node_dir"
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then
    printf 'SKIPPED: node collection timed out after %ss (exit %s) from %s\n' "$node_timeout" "$rc" "$target" >"$node_dir/SKIPPED.txt"
    rm -f "$node_tar"
    return 2
  fi

  if [[ -s "$node_tar" ]] && tar -xzf "$node_tar" -C "$node_dir" >/dev/null 2>/dev/null; then
    # A node that streamed a valid archive but is missing its own manifest.jsonl
    # was truncated (partial/interrupted transfer) — do not count it as ok.
    if [[ ! -f "$node_dir/manifest.jsonl" ]]; then
      printf 'SKIPPED: node archive from %s is incomplete (no manifest.jsonl); treated as failure\n' "$target" >"$node_dir/SKIPPED.txt"
      rc=2
    fi
  else
    rm -rf "$node_dir"
    ensure_dir "$node_dir"
    printf 'SKIPPED: no usable node archive returned from %s (ssh exit %s)\n' "$target" "$rc" >"$node_dir/SKIPPED.txt"
    [[ $rc -ne 0 ]] || rc=2
  fi
  rm -f "$node_tar"

  return "$rc"
}

# Lifecycle globals: CLEANUP_WORKDIR / CLEANUP_KEEP (used by the trap handlers in
# lib/bundle.sh), and the parsed inventory arrays filled by main.
CLEANUP_WORKDIR=
CLEANUP_KEEP=0
HOST_ALIASES=()
HOST_TARGETS=()

main() {
  local inventory='' ssh_key='' seed_override='' out_dir="$COLLECT_ROOT/results"
  local mode=auto since=24h timeout=20 node_timeout=600 skip_logs=0 keep_workdir=0
  local keep_original_logs=0 var_log_max_bytes=10737418240
  local allow_cephadm_shell=${CEPH_INCIDENT_ALLOW_CEPHADM_SHELL:-0}
  local allow_kubectl_exec=${CEPH_INCIDENT_ALLOW_KUBECTL_EXEC:-0}
  local trust_ssh_host_key=1 redact_enabled=1
  local prom_url='' prom_job_regex='ceph|node' prom_step='' prom_timeout=600
  local seed='' ssh_user='' seed_host='' rook_namespace=rook-ceph rook_operator_namespace=rook-ceph kube_context='' kube_mode=remote
  local timestamp workdir manifest bundle rc=0 cluster_rc=0 node_ok=0 node_failed=0

  if [[ $# -eq 0 ]]; then
    usage >&2
    return 1
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --inventory)
        inventory=${2-}
        shift 2
        ;;
      --ssh-key)
        ssh_key=${2-}
        shift 2
        ;;
      --seed)
        seed_override=${2-}
        shift 2
        ;;
      --out)
        out_dir=${2-}
        shift 2
        ;;
      --mode)
        mode=${2-}
        shift 2
        ;;
      --kube-context)
        kube_context=${2-}
        shift 2
        ;;
      --kube-mode)
        kube_mode=${2-}
        shift 2
        ;;
      --since)
        since=${2-}
        shift 2
        ;;
      --prom-url)
        prom_url=${2-}
        shift 2
        ;;
      --prom-job-regex)
        prom_job_regex=${2-}
        shift 2
        ;;
      --prom-step)
        prom_step=${2-}
        shift 2
        ;;
      --prom-timeout)
        prom_timeout=${2-}
        shift 2
        ;;
      --timeout)
        timeout=${2-}
        shift 2
        ;;
      --node-timeout)
        node_timeout=${2-}
        shift 2
        ;;
      --skip-logs)
        skip_logs=1
        shift
        ;;
      --keep-original-logs)
        keep_original_logs=1
        shift
        ;;
      --var-log-max-bytes)
        var_log_max_bytes=${2-}
        shift 2
        ;;
      --allow-cephadm-shell)
        allow_cephadm_shell=1
        shift
        ;;
      --allow-kubectl-exec)
        allow_kubectl_exec=1
        shift
        ;;
      --trust-ssh-host-key)
        trust_ssh_host_key=1
        shift
        ;;
      --no-trust-ssh-host-key)
        trust_ssh_host_key=0
        shift
        ;;
      --redact)
        redact_enabled=1
        shift
        ;;
      --no-redact)
        redact_enabled=0
        shift
        ;;
      --quiet)
        export CEPH_INCIDENT_QUIET=1
        shift
        ;;
      --keep-workdir)
        keep_workdir=1
        shift
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

  [[ "$mode" == "auto" || "$mode" == "cephadm" || "$mode" == "rook" ]] || die "unsupported mode: $mode"
  [[ "$var_log_max_bytes" == "unlimited" || "$var_log_max_bytes" =~ ^[0-9]+$ ]] \
    || die "invalid --var-log-max-bytes: $var_log_max_bytes"
  export CEPH_INCIDENT_ALLOW_CEPHADM_SHELL=$allow_cephadm_shell
  export CEPH_INCIDENT_ALLOW_KUBECTL_EXEC=$allow_kubectl_exec
  # kube-context runs through a remote shell (ssh kubectl --context ...). Block the
  # actual shell metacharacters but allow the chars real contexts use (@ : / for
  # e.g. kubernetes-admin@kubernetes and EKS ARNs).
  if [[ -n "$kube_context" && "$kube_context" == *[!A-Za-z0-9._@:/-]* ]]; then
    die "invalid --kube-context (allowed: A-Za-z0-9._@:/-): $kube_context"
  fi
  [[ "$kube_mode" == "local" || "$kube_mode" == "remote" ]] || die "invalid --kube-mode (local|remote): $kube_mode"
  if [[ -n "$prom_url" ]]; then
    local num_re='^[0-9]+$' step_re='^[1-9][0-9]*$'
    prom_duration_seconds "$since" >/dev/null \
      || die "--since must be N/Ns/Nm/Nh/Nd/Nw when using --prom-url: $since"
    [[ -z "$prom_step" || "$prom_step" =~ $step_re ]] || die "invalid --prom-step (positive seconds): $prom_step"
    [[ "$prom_timeout" =~ $num_re ]] || die "invalid --prom-timeout (seconds): $prom_timeout"
  fi
  [[ -n "$inventory" && -f "$inventory" ]] || die "missing inventory: ${inventory:-<unset>}"
  [[ -n "$ssh_key" && -f "$ssh_key" ]] || die "missing ssh key: ${ssh_key:-<unset>}"
  export CEPH_INCIDENT_TRUST_SSH_HOST_KEY=$trust_ssh_host_key

  load_inventory "$inventory" || die "inventory must contain only supported quoted assignments and HOSTS entries"

  ssh_user=${SSH_USER:-}
  seed_host=${SEED_HOST:-}
  rook_namespace=${ROOK_NAMESPACE:-rook-ceph}
  rook_operator_namespace=${ROOK_OPERATOR_NAMESPACE:-rook-ceph}
  if [[ -n "$ssh_user" && ( "$ssh_user" == -* || ! "$ssh_user" =~ ^[A-Za-z0-9._%+-]+$ ) ]]; then
    die "invalid SSH_USER in inventory"
  fi
  is_safe_namespace "$rook_namespace" || die "invalid ROOK_NAMESPACE in inventory"
  is_safe_namespace "$rook_operator_namespace" || die "invalid ROOK_OPERATOR_NAMESPACE in inventory"
  if [[ -n "$seed_override" ]]; then
    seed=$seed_override
  elif [[ -n "$seed_host" ]]; then
    seed="$(ssh_target_for_host "$seed_host" "$ssh_user")"
  fi
  [[ -z "$seed" ]] || is_safe_ssh_target "$seed" || die "invalid SSH seed target"

  if [[ -z "$(timeout_cmd)" ]]; then
    log "WARNING: no 'timeout'/'gtimeout' found on this workstation; outer timeouts are disabled — relying on SSH ConnectTimeout/ServerAlive only (install coreutils for full bounding)"
  fi

  ensure_dir "$out_dir"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  workdir="$out_dir/tmp.$timestamp.$$"
  manifest="$workdir/manifest.jsonl"
  ensure_dir "$workdir"
  : >"$workdir/.runtime-known-hosts"
  export CEPH_INCIDENT_KNOWN_HOSTS_FILE="$workdir/.runtime-known-hosts"
  # Used by cleanup_workdir/on_interrupt trap handlers from lib/bundle.sh.
  # shellcheck disable=SC2034
  CLEANUP_WORKDIR="$workdir"
  # shellcheck disable=SC2034
  CLEANUP_KEEP=$keep_workdir
  trap cleanup_workdir EXIT
  trap on_interrupt INT TERM
  write_initial_metadata "$workdir" "$mode" "$seed" "$since" "$timeout"

  # Parse HOSTS once into globals (used by the cluster capability probe AND the
  # node loop). A malformed entry is recorded but must not abort collection.
  local entry
  HOST_ALIASES=()
  HOST_TARGETS=()
  # bash 3.2 + set -u: expanding an empty array errors, so guard it.
  if [[ ${#HOSTS[@]} -eq 0 ]]; then
    die "inventory HOSTS is empty"
  fi
  for entry in "${HOSTS[@]}"; do
    if [[ "$entry" != *=* || -z "${entry%%=*}" || -z "${entry#*=}" ]]; then
      append_error "$workdir" "skipped malformed HOSTS entry: $entry"
      rc=2
      continue
    fi
    if [[ ! "${entry%%=*}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ||
          "${entry%%=*}" == "." || "${entry%%=*}" == ".." ]]; then
      append_error "$workdir" "skipped unsafe host alias: ${entry%%=*}"
      rc=2
      continue
    fi
    local parsed_target
    parsed_target="$(ssh_target_for_host "${entry#*=}" "$ssh_user")"
    if ! is_safe_ssh_target "$parsed_target"; then
      append_error "$workdir" "skipped unsafe SSH target for alias ${entry%%=*}"
      rc=2
      continue
    fi
    HOST_ALIASES+=("${entry%%=*}")
    HOST_TARGETS+=("$parsed_target")
  done

  progress "starting: mode=$mode, ${#HOST_TARGETS[@]} hosts"

  set +e
  collect_clusters \
    --mode "$mode" --workdir "$workdir" --manifest "$manifest" \
    --seed "$seed" --ssh-key "$ssh_key" --since "$since" --timeout "$timeout" \
    --namespace "$rook_namespace" --operator-namespace "$rook_operator_namespace" \
    --kube-context "$kube_context" --kube-mode "$kube_mode"
  cluster_rc=$?
  set -e
  if [[ $cluster_rc -ne 0 ]]; then
    append_error "$workdir" "cluster collection exited $cluster_rc"
    rc=2
  fi

  local prom_rc=0
  if [[ -n "$prom_url" ]]; then
    progress "collecting prometheus metrics from $(prom_mask_url "$prom_url")…"
    set +e
    collect_prometheus --out "$workdir" --manifest "$manifest" --url "$prom_url" \
      --job-regex "$prom_job_regex" --step "$prom_step" --since "$since" \
      --timeout "$timeout" --budget "$prom_timeout"
    prom_rc=$?
    set -e
    if [[ $prom_rc -ne 0 ]]; then
      append_error "$workdir" "prometheus collection exited $prom_rc"
      rc=2
    fi
  fi

  local i alias target node_rc ntotal
  ntotal=${#HOST_ALIASES[@]}
  if [[ $ntotal -gt 0 ]]; then
    for i in "${!HOST_ALIASES[@]}"; do
      alias="${HOST_ALIASES[$i]}"
      target="${HOST_TARGETS[$i]}"
      progress "[$((i + 1))/$ntotal] node ${alias}…"
      if collect_remote_node "$workdir" "$alias" "$target" "$ssh_key" "$since" "$timeout" \
        "$skip_logs" "$node_timeout" "$keep_original_logs" "$var_log_max_bytes"; then
        node_ok=$((node_ok + 1))
        progress "[$((i + 1))/$ntotal] node $alias: ok"
      else
        node_rc=$?
        node_failed=$((node_failed + 1))
        append_error "$workdir" "node $alias ($target) collector exited $node_rc"
        progress "[$((i + 1))/$ntotal] node $alias: SKIPPED (exit $node_rc)"
        rc=2
      fi
    done
  fi

  # Test-only hook: simulate a mid-run abort to exercise trap cleanup. Inert in production.
  if [[ -n "${COLLECT_TEST_ABORT_AFTER_NODES:-}" ]]; then
    die "test abort after nodes"
  fi

  if [[ "$redact_enabled" == "1" ]]; then
    progress "redacting…"
    if ! redact_bundle_text "$workdir"; then
      append_error "$workdir" "redaction failed; original collected artifact was preserved"
      rc=2
    fi
  else
    : >"$workdir/redactions.log"
  fi
  if ! enforce_node_log_caps "$workdir" "$var_log_max_bytes"; then
    rc=2
  fi
  rm -f -- "$workdir/.runtime-known-hosts"
  write_summary "$workdir" "$mode" "$seed" "$node_ok" "$node_failed" "$cluster_rc" "$rc"
  write_catalog "$workdir"

  progress "verifying…"
  # Verify BEFORE packaging, but never let verification destroy collected
  # evidence: capture its result instead of aborting under set -e. On failure,
  # keep the workdir for inspection and do not produce a shareable bundle.
  local verify_rc=0
  set +e
  "$COLLECT_ROOT/lib/verify-bundle.sh" "$workdir" >/dev/null 2>>"$workdir/errors.log"
  verify_rc=$?
  set -e
  if [[ $verify_rc -ne 0 ]]; then
    # Used by cleanup_workdir trap handler from lib/bundle.sh.
    # shellcheck disable=SC2034
    CLEANUP_KEEP=1
    append_error "$workdir" "bundle verification failed (rc=$verify_rc); workdir kept, NOT packaged for sharing"
    write_summary "$workdir" "$mode" "$seed" "$node_ok" "$node_failed" "$cluster_rc" "1"
    printf 'VERIFY FAILED: workdir kept at %s (not packaged) — review errors.log\n' "$workdir" >&2
    return 1
  fi

  progress "packaging…"
  bundle="$out_dir/ceph-incident-$timestamp.tar.gz"
  COPYFILE_DISABLE=1 tar -czf "$bundle" -C "$workdir" .
  set +e
  "$COLLECT_ROOT/lib/verify-bundle.sh" "$bundle" >/dev/null 2>>"$workdir/errors.log"
  verify_rc=$?
  set -e
  if [[ $verify_rc -ne 0 ]]; then
    # Used by cleanup_workdir trap handler from lib/bundle.sh.
    # shellcheck disable=SC2034
    CLEANUP_KEEP=1
    rm -f -- "$bundle"
    printf 'VERIFY FAILED on packaged bundle; removed it, workdir kept at %s\n' "$workdir" >&2
    return 1
  fi

  printf 'bundle: %s\n' "$bundle"
  return "$rc"
}

main "$@"
