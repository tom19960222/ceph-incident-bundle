#!/usr/bin/env bash
set -euo pipefail

# Cephadm collection helpers live here.

CEPHADM_COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$CEPHADM_COLLECTOR_DIR/common.sh"

# The remote prefix that actually runs ceph on the source node, by runner token:
#   direct  -> ceph                          (fast: no container per command)
#   sudo    -> sudo -n ceph
#   cephadm -> sudo -n cephadm shell -- ceph (default; spins a container each call)
ceph_runner_argv() {
  case "$1" in
    direct) printf '%s\n' ceph ;;
    sudo) printf '%s\n' sudo -n ceph ;;
    *) printf '%s\n' sudo -n cephadm shell -- ceph ;;
  esac
}

collect_cephadm_command() {
  local outdir=$1 manifest=$2 seed=$3 ssh_key=$4 timeout=$5 runner=$6 artifact=$7
  shift 7

  local artifact_dir
  artifact_dir="$(dirname -- "$artifact")"
  ensure_dir "$artifact_dir"

  local -a runner_argv sopts
  local _w
  while IFS= read -r _w; do runner_argv+=("$_w"); done < <(ceph_runner_argv "$runner")
  while IFS= read -r _w; do sopts+=("$_w"); done < <(ssh_base_opts "$ssh_key" "$timeout")

  local rc=0
  COMMAND_TIMEOUT="$timeout" ERROR_LOG="${ERROR_LOG:-$outdir/errors.log}" \
    run_capture "$manifest" "$seed" "collect-cluster-cephadm" "$artifact" -- \
    ssh "${sopts[@]}" "$seed" "${runner_argv[@]}" "$@" || rc=$?
  if [[ $rc -eq 255 || $rc -eq 124 || $rc -eq 137 ]]; then
    write_ssh_debug_log "$outdir" "cluster-ceph" "$seed" "$ssh_key" "$timeout"
  fi
  return "$rc"
}

write_cephadm_crash_skip() {
  write_skip_artifact "$1" "unable to parse crash list JSON for recent crash inspection"
}

extract_cephadm_crash_ids() {
  local crash_ls_artifact=$1
  local payload compact ids

  [[ -f "$crash_ls_artifact" ]] || return 1
  payload="$(sed '/^[[:space:]]*#/d' "$crash_ls_artifact")" || return 1

  # Anchor strictly to crash_id; matching id/name too would capture unrelated
  # nested fields and feed bogus ids back into `ceph crash info`.
  ids="$(
    printf '%s\n' "$payload" |
      grep -oE '"crash_id"[[:space:]]*:[[:space:]]*"[^"]*"' |
      sed -E 's/^"crash_id"[[:space:]]*:[[:space:]]*"([^"]*)"$/\1/' |
      head -n 10
  )" || true

  if [[ -n "$ids" ]]; then
    printf '%s\n' "$ids"
    return 0
  fi

  compact="$(printf '%s' "$payload" | tr -d '[:space:]')"
  case "$compact" in
    "[]"|"{}"|"{\"crashes\":[]}"|"{\"items\":[]}"|"{\"entries\":[]}"|"{\"crash_ls\":[]}")
      return 0
      ;;
  esac

  return 1
}

cephadm_crash_artifact_name() {
  local crash_id=$1 safe_id
  safe_id="$(printf '%s' "$crash_id" | tr -c 'A-Za-z0-9._-' '_')"
  while [[ "$safe_id" == *..* ]]; do
    safe_id="${safe_id//../__}"
  done
  [[ -n "$safe_id" ]] || safe_id="crash"
  printf '%s' "$safe_id"
}

cephadm_unique_crash_artifact() {
  local crash_dir=$1 safe_id=$2
  local artifact="$crash_dir/$safe_id.json"
  local suffix=2

  while [[ -e "$artifact" ]]; do
    artifact="$crash_dir/$safe_id-$suffix.json"
    suffix=$((suffix + 1))
  done

  printf '%s' "$artifact"
}

collect_cephadm_recent_crashes() {
  local outdir=$1 manifest=$2 seed=$3 ssh_key=$4 timeout=$5 runner=$6 crash_ls_artifact=$7

  local crash_dir="$outdir/cluster/ceph/json/crash-info"
  local skip_artifact="$outdir/cluster/ceph/text/crash-info-skip.txt"
  local crash_ids rc=0

  if ! crash_ids="$(extract_cephadm_crash_ids "$crash_ls_artifact")"; then
    write_cephadm_crash_skip "$skip_artifact"
    return 0
  fi

  [[ -n "$crash_ids" ]] || return 0

  local crash_id safe_id crash_info_artifact
  while IFS= read -r crash_id; do
    [[ -n "$crash_id" ]] || continue
    safe_id="$(cephadm_crash_artifact_name "$crash_id")"
    crash_info_artifact="$(cephadm_unique_crash_artifact "$crash_dir" "$safe_id")"
    if ! collect_cephadm_command "$outdir" "$manifest" "$seed" "$ssh_key" "$timeout" "$runner" "$crash_info_artifact" crash info "$crash_id"; then
      rc=2
    fi
  done <<<"$crash_ids"

  return "$rc"
}

collect_cluster_cephadm() {
  local outdir=$1 manifest=$2 seed=$3 ssh_key=$4 since=$5 timeout=$6 runner="${7:-cephadm}"
  local failed=0
  local json_dir="$outdir/cluster/ceph/json"
  local text_dir="$outdir/cluster/ceph/text"

  # Cluster-level ceph commands are point-in-time snapshots; node collectors apply the time window.
  : "$since"

  ensure_dir "$json_dir"
  ensure_dir "$text_dir"

  local -a json_specs=(
    "status.json::status --format json-pretty"
    "health-detail.json::health detail --format json-pretty"
    "versions.json::versions --format json-pretty"
    "df-detail.json::df detail --format json-pretty"
    "osd-tree.json::osd tree --format json-pretty"
    "osd-df.json::osd df --format json-pretty"
    "osd-dump.json::osd dump --format json-pretty"
    "osd-perf.json::osd perf --format json-pretty"
    "osd-blocked-by.json::osd blocked-by --format json-pretty"
    "pg-stat.json::pg stat --format json-pretty"
    "pg-dump.json::pg dump --format json-pretty"
    "pg-dump-stuck.json::pg dump_stuck --format json-pretty"
    "mon-dump.json::mon dump --format json-pretty"
    "quorum-status.json::quorum_status --format json-pretty"
    "mgr-dump.json::mgr dump --format json-pretty"
    "orch-host-ls.json::orch host ls --format json-pretty"
    "orch-ps.json::orch ps --format json-pretty"
    "orch-device-ls-wide.json::orch device ls --wide --format json-pretty"
    "config-dump.json::config dump --format json-pretty"
    "crash-ls.json::crash ls --format json-pretty"
  )

  local -a text_specs=(
    "status.txt::status"
    "health-detail.txt::health detail"
    "osd-tree.txt::osd tree"
    "orch-ps.txt::orch ps"
  )

  local spec artifact command
  local -a command_words
  local total=$(( ${#json_specs[@]} + ${#text_specs[@]} )) k=0
  for spec in "${json_specs[@]}"; do
    artifact=${spec%%::*}
    command=${spec#*::}
    k=$((k + 1))
    progress "[$k/$total] ceph $command"
    # shellcheck disable=SC2206
    command_words=($command)
    if ! collect_cephadm_command "$outdir" "$manifest" "$seed" "$ssh_key" "$timeout" "$runner" "$json_dir/$artifact" "${command_words[@]}"; then
      failed=1
    fi
  done

  for spec in "${text_specs[@]}"; do
    artifact=${spec%%::*}
    command=${spec#*::}
    k=$((k + 1))
    progress "[$k/$total] ceph $command"
    # shellcheck disable=SC2206
    command_words=($command)
    if ! collect_cephadm_command "$outdir" "$manifest" "$seed" "$ssh_key" "$timeout" "$runner" "$text_dir/$artifact" "${command_words[@]}"; then
      failed=1
    fi
  done

  progress "ceph crash info (recent)…"
  if ! collect_cephadm_recent_crashes "$outdir" "$manifest" "$seed" "$ssh_key" "$timeout" "$runner" "$json_dir/crash-ls.json"; then
    failed=1
  fi

  if [[ $failed -ne 0 ]]; then
    return 2
  fi

  return 0
}
