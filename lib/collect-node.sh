#!/usr/bin/env bash
set -euo pipefail

# Collect read-only node evidence for a Ceph incident bundle.

NODE_COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$NODE_COLLECTOR_DIR/common.sh"
# shellcheck disable=SC1091
source "$NODE_COLLECTOR_DIR/collect-var-log.sh"

usage() {
  cat <<'EOF'
Usage: collect-node.sh --out DIR --host-alias ALIAS [options]

Options:
  --since DURATION
  --timeout SECONDS
  --skip-logs
  --keep-original-logs
  --var-log-max-bytes BYTES|unlimited
EOF
}


node_run_capture() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4 artifact_rel=$5
  shift 5

  local artifact="$outdir/$artifact_rel"
  if ! COMMAND_TIMEOUT="$timeout" ERROR_LOG="${ERROR_LOG:-$outdir/errors.log}" \
    run_capture "$manifest" "$host_alias" "collect-node" "$artifact" -- "$@"; then
    return 2
  fi
  return 0
}

node_run_optional() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4 artifact_rel=$5 command_name=$6
  shift 6

  if ! command -v "$command_name" >/dev/null 2>&1; then
    write_skip_artifact "$outdir/$artifact_rel" "command not found: $command_name"
    return 0
  fi

  node_run_capture "$outdir" "$manifest" "$host_alias" "$timeout" "$artifact_rel" "$command_name" "$@" || return 0
}

node_run_privileged() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4 artifact_rel=$5 command_name=$6
  shift 6

  if [[ $EUID -eq 0 ]]; then
    node_run_capture "$outdir" "$manifest" "$host_alias" "$timeout" "$artifact_rel" "$command_name" "$@"
    return $?
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    write_skip_artifact "$outdir/$artifact_rel" "sudo command not found for privileged read: $command_name"
    return 0
  fi

  node_run_capture "$outdir" "$manifest" "$host_alias" "$timeout" "$artifact_rel" sudo -n "$command_name" "$@"
}

node_run_privileged_bounded() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4 artifact_rel=$5 limit=$6 command_name=$7
  shift 7

  local artifact="$outdir/$artifact_rel" payload_tmp artifact_tmp started ended
  local command_string payload_size command_rc=0 tbin
  local -a cmd exec_cmd stream_status
  if [[ $EUID -eq 0 ]]; then
    cmd=("$command_name" "$@")
  elif command -v sudo >/dev/null 2>&1; then
    cmd=(sudo -n "$command_name" "$@")
  else
    write_skip_artifact "$artifact" "sudo command not found for privileged read: $command_name"
    return 2
  fi

  ensure_dir "$(dirname -- "$artifact")"
  payload_tmp="$(mktemp "$(dirname -- "$artifact")/.${artifact##*/}.payload.XXXXXX")"
  artifact_tmp="$(mktemp "$(dirname -- "$artifact")/.${artifact##*/}.XXXXXX")"
  printf -v command_string '%q ' "${cmd[@]}"
  command_string=${command_string% }
  started="$(date -u +%FT%TZ)"
  exec_cmd=("${cmd[@]}")
  tbin="$(timeout_cmd)"
  [[ -n "$tbin" ]] && exec_cmd=("$tbin" "$timeout" "${cmd[@]}")

  set +e
  set +o pipefail
  "${exec_cmd[@]}" 2>&1 | head -c "$((limit + 1))" >"$payload_tmp"
  stream_status=("${PIPESTATUS[@]}")
  set -o pipefail
  set -e
  command_rc=${stream_status[0]}
  payload_size="$(var_log_stat_size "$payload_tmp" 2>/dev/null || printf 0)"
  ended="$(date -u +%FT%TZ)"

  if [[ "$payload_size" -gt "$limit" ]]; then
    rm -f -- "$payload_tmp" "$artifact_tmp"
    manifest_add "$manifest" "$host_alias" "collect-node" "$artifact" "$command_string" 75 "$started" "$ended"
    return 3
  fi

  {
    printf '# host: %s\n# collector: collect-node\n# started: %s\n' "$host_alias" "$started"
    [[ -n "$tbin" ]] && printf '# timeout: %ss\n' "$timeout" || printf '# timeout: unavailable\n'
    cat -- "$payload_tmp"
    if [[ $command_rc -eq 124 || $command_rc -eq 137 ]]; then
      printf '# TRUNCATED: command timed out after %ss (exit %s)\n' "$timeout" "$command_rc"
    fi
  } >"$artifact_tmp"
  rm -f -- "$payload_tmp"
  mv -f -- "$artifact_tmp" "$artifact"
  manifest_add "$manifest" "$host_alias" "collect-node" "$artifact" "$command_string" "$command_rc" "$started" "$ended"
  [[ $command_rc -eq 0 ]] || return 2
  return 0
}

journal_since_arg() {
  local since=$1
  if [[ "$since" =~ ^[0-9]+[smhdw]$ ]]; then
    printf -- '-%s' "$since"
  else
    printf '%s' "$since"
  fi
}

node_find0() {
  local root=$1
  shift

  if [[ $EUID -eq 0 ]]; then
    find "$root" "$@"
    return $?
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo -n find "$root" "$@"
    return $?
  fi

  find "$root" "$@" 2>/dev/null
}

node_copy_file() {
  local source=$1 dest=$2 tmp
  ensure_dir "$(dirname -- "$dest")"
  tmp="$dest.tmp.$$"

  if [[ "${CEPH_INCIDENT_TEST_ALLOW_ATIME_READ:-0}" == "1" ]]; then
    if ! cat -- "$source" >"$tmp"; then
      rm -f -- "$tmp"
      return 1
    fi
  elif [[ $EUID -eq 0 || -O "$source" ]]; then
    if ! dd "if=$source" iflag=noatime status=none >"$tmp"; then
      rm -f -- "$tmp"
      return 1
    fi
  elif command -v sudo >/dev/null 2>&1; then
    # Read as root with O_NOATIME, but write the collector-owned destination as
    # the calling user. This avoids mutating source atime and output ownership.
    # shellcheck disable=SC2024
    if ! sudo -n dd "if=$source" iflag=noatime status=none >"$tmp"; then
      rm -f -- "$tmp"
      return 1
    fi
  else
    return 1
  fi

  mv -f -- "$tmp" "$dest"
}

copy_readable_etc_files() {
  local outdir=$1
  local source dest_name failed=0

  for source in /etc/os-release /etc/hosts /etc/resolv.conf; do
    [[ -r "$source" ]] || continue
    dest_name="${source#/etc/}"
    node_copy_file "$source" "$outdir/system/$dest_name" || failed=1
  done
  [[ $failed -eq 0 ]] || return 2
}

collect_timesyncd_config() {
  local outdir=$1
  local conf=${CEPH_INCIDENT_TIMESYNCD_CONF:-/etc/systemd/timesyncd.conf}
  local conf_d=${CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR:-/etc/systemd/timesyncd.conf.d}
  local dest="$outdir/time/systemd-timesyncd-config"
  local source rel copied=0 failed=0

  ensure_dir "$dest"
  if [[ -f "$conf" ]]; then
    if node_copy_file "$conf" "$dest/timesyncd.conf"; then
      copied=1
    else
      failed=1
    fi
  fi

  if [[ -d "$conf_d" ]]; then
    ensure_dir "$dest/timesyncd.conf.d"
    while IFS= read -r -d '' source; do
      rel="${source#"$conf_d"/}"
      if node_copy_file "$source" "$dest/timesyncd.conf.d/$rel"; then
        copied=1
      else
        failed=1
      fi
    done < <(node_find0 "$conf_d" -maxdepth 1 -type f -name '*.conf' -print0 2>/dev/null || true)
  fi

  if [[ $copied -eq 0 ]]; then
    write_skip_artifact "$dest/SKIPPED.txt" "systemd-timesyncd config not found at $conf or $conf_d"
  fi

  [[ $failed -eq 0 ]] || return 2
}

collect_var_lib_ceph() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4
  local ceph_dir=${CEPH_INCIDENT_VAR_LIB_CEPH_DIR:-/var/lib/ceph}
  local config_dest="$outdir/cephadm/var-lib-ceph-configs"
  local source rel dest
  local failed=0

  if [[ -d "$ceph_dir" ]]; then
    # SC2016: the sh -c body is meant to expand on the remote sh, not here.
    # shellcheck disable=SC2016
    if ! node_run_privileged "$outdir" "$manifest" "$host_alias" "$timeout" "cephadm/var-lib-ceph-listing.txt" \
      find "$ceph_dir" -maxdepth 3 \
        \( -iname '*keyring*' -o -iname '*private_key*' -o -path '*/.ssh/*' \) -prune \
        -o -exec sh -c '
          for path do
            if [ -d "$path" ]; then
              type=d
            elif [ -f "$path" ]; then
              type=f
            else
              type=o
            fi
            printf "%s %s\n" "$type" "$path"
          done
        ' sh {} +; then
      return 2
    fi
  else
    write_skip_artifact "$outdir/cephadm/var-lib-ceph-listing.txt" "$ceph_dir is not a readable directory on this node"
    return 0
  fi

  ensure_dir "$config_dest"
  while IFS= read -r -d '' source; do
    rel="${source#"$ceph_dir"/}"
    dest="$config_dest/$rel"
    if ! node_copy_file "$source" "$dest"; then
      failed=1
    fi
  done < <(node_find0 "$ceph_dir" -maxdepth 4 \
    \( -iname '*keyring*' -o -iname '*private_key*' -o -path '*/.ssh/*' \) -prune \
    -o -type f \( -name 'ceph.conf' -o -name '*.conf' -o -name 'config' -o -name '*.config' \) -print0 2>/dev/null || true)

  [[ $failed -eq 0 ]] || return 2
}

collect_node_main() {
  local outdir='' host_alias='' since="24h" timeout=20 skip_logs=0
  local keep_original_logs=0 var_log_max_bytes=10737418240

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out)
        outdir=${2-}
        shift 2
        ;;
      --host-alias)
        host_alias=${2-}
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

  [[ -n "$outdir" && -n "$host_alias" ]] || {
    usage >&2
    return 1
  }
  [[ "$var_log_max_bytes" == "unlimited" || "$var_log_max_bytes" =~ ^[0-9]+$ ]] || {
    printf 'invalid --var-log-max-bytes: %s\n' "$var_log_max_bytes" >&2
    return 1
  }

  # The Evidence Window start is computed here, on the node, because it is
  # compared against this node's file mtimes and this node's clock is what those
  # were stamped with. Collecting `/var/log` with a --since the window cannot
  # parse fails closed: falling back to an unbounded collection would answer a
  # bounded request with months of evidence and only a warning to say so.
  local window_start=0 window_seconds
  if [[ $skip_logs -eq 0 ]]; then
    if ! window_seconds="$(evidence_window_seconds "$since")"; then
      printf '--since must be N/Ns/Nm/Nh/Nd/Nw when collecting /var/log: %s\n' \
        "$since" >&2
      return 1
    fi
    window_start=$(($(date -u +%s) - window_seconds))
    [[ $window_start -gt 0 ]] || window_start=1
  fi

  ensure_dir "$outdir"
  local manifest="$outdir/manifest.jsonl"
  local failed=0
  local journal_since
  journal_since="$(journal_since_arg "$since")"

  # dmesg and the ceph journal can be large under load; give them a heavier
  # timeout than the per-command one so they are not silently truncated.
  local heavy_timeout=$timeout
  if [[ "$heavy_timeout" =~ ^[0-9]+$ ]] && (( heavy_timeout < 120 )); then
    heavy_timeout=120
  fi

  local -a basic_specs=(
    "system/hostname.txt::hostname"
    "system/uname.txt::uname -a"
    "system/uptime.txt::uptime"
    "resources/free.txt::free -h"
    "storage/df.txt::df -hT"
    "network/ip-addr.txt::ip addr show"
    "systemd/failed-units.txt::systemctl --failed --no-pager --plain"
  )

  local spec artifact command
  local -a command_words
  for spec in "${basic_specs[@]}"; do
    artifact=${spec%%::*}
    command=${spec#*::}
    # shellcheck disable=SC2206
    command_words=($command)
    if ! node_run_capture "$outdir" "$manifest" "$host_alias" "$timeout" "$artifact" "${command_words[@]}"; then
      failed=1
    fi
  done

  if ! node_run_privileged "$outdir" "$manifest" "$host_alias" "$timeout" "storage/lsblk.txt" lsblk -a -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL; then
    failed=1
  fi
  if ! node_run_privileged "$outdir" "$manifest" "$host_alias" "$heavy_timeout" "kernel/dmesg.txt" dmesg -T; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$heavy_timeout" "systemd/journal-ceph.txt" sudo -n journalctl --since "$journal_since" -u 'ceph*' --no-pager; then
    failed=1
  fi

  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "resources/iostat.txt" iostat -xz 1 3; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/chronyc-tracking.txt" chronyc tracking; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/chronyc-sources.txt" chronyc sources -v; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/ntpq-peers.txt" ntpq -pn; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/timedatectl-status.txt" timedatectl status; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/timedatectl-show-timesync.txt" timedatectl show-timesync --all; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/timedatectl-timesync-status.txt" timedatectl timesync-status; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "time/systemd-timesyncd-status.txt" systemctl status systemd-timesyncd --no-pager --plain; then
    failed=1
  fi
  node_run_privileged "$outdir" "$manifest" "$host_alias" "$timeout" "time/systemd-timesyncd-journal.txt" journalctl --since "$journal_since" -u systemd-timesyncd --no-pager || true
  if ! collect_timesyncd_config "$outdir"; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "storage/pvs.txt" pvs --noheadings --separator ' '; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "storage/vgs.txt" vgs --noheadings --separator ' '; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "storage/lvs.txt" lvs --noheadings --separator ' '; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "containers/podman-ps.txt" podman ps -a; then
    failed=1
  fi
  if ! node_run_optional "$outdir" "$manifest" "$host_alias" "$timeout" "containers/docker-ps.txt" docker ps -a; then
    failed=1
  fi

  if command -v cephadm >/dev/null 2>&1; then
    node_run_privileged "$outdir" "$manifest" "$host_alias" "$timeout" "cephadm/cephadm-ls.json" cephadm ls --format json-pretty || true
  else
    write_skip_artifact "$outdir/cephadm/cephadm-ls.json" "command not found: cephadm"
  fi

  if ! copy_readable_etc_files "$outdir"; then
    failed=1
  fi
  if ! collect_var_lib_ceph "$outdir" "$manifest" "$host_alias" "$timeout"; then
    failed=1
  fi

  if [[ $skip_logs -eq 1 ]]; then
    write_skip_artifact "$outdir/logs/var-log/SKIPPED.txt" "log collection disabled by --skip-logs"
  else
    local var_log_rc=0 journal_rc=0 payload_bytes=0 journal_limit
    collect_var_logs \
      "${CEPH_INCIDENT_VAR_LOG_DIR:-/var/log}" \
      "$outdir/logs/var-log" \
      "$var_log_max_bytes" \
      "$keep_original_logs" \
      "$window_start" || var_log_rc=$?
    if [[ $var_log_rc -ne 0 ]]; then
      failed=1
    fi
    if [[ "$var_log_max_bytes" == "unlimited" ]]; then
      if ! node_run_privileged "$outdir" "$manifest" "$host_alias" "$heavy_timeout" \
        "logs/var-log/journal-all-since.txt" journalctl --since "$journal_since" --no-pager; then
        failed=1
      fi
    elif [[ -f "$outdir/logs/var-log/OVER-LIMIT.txt" ]]; then
      write_skip_artifact "$outdir/logs/var-log/journal-all-since.txt" \
        "not collected because /var/log payload exceeded the per-node cap"
      failed=1
    elif [[ -f "$outdir/logs/var-log/PAYLOAD-BYTES.txt" ]]; then
      payload_bytes="$(tr -d '[:space:]' <"$outdir/logs/var-log/PAYLOAD-BYTES.txt")"
      [[ "$payload_bytes" =~ ^[0-9]+$ ]] || payload_bytes=$var_log_max_bytes
      journal_limit=$((var_log_max_bytes - payload_bytes))
      [[ $journal_limit -ge 0 ]] || journal_limit=0
      node_run_privileged_bounded "$outdir" "$manifest" "$host_alias" "$heavy_timeout" \
        "logs/var-log/journal-all-since.txt" "$journal_limit" \
        journalctl --since "$journal_since" --no-pager || journal_rc=$?
      if [[ $journal_rc -eq 3 ]]; then
        rm -rf -- "$outdir/logs/var-log/merged" "$outdir/logs/var-log/raw" "$outdir/logs/var-log/original"
        rm -f -- "$outdir/logs/var-log/PAYLOAD-BYTES.txt"
        printf 'max_bytes=%s\nstatus=not-collected-journal-exceeded-remaining-cap\n' \
          "$var_log_max_bytes" >"$outdir/logs/var-log/OVER-LIMIT.txt"
        write_skip_artifact "$outdir/logs/var-log/journal-all-since.txt" \
          "not collected because combined /var/log and journal text exceeded the per-node cap"
        failed=1
      elif [[ $journal_rc -ne 0 ]]; then
        failed=1
      else
        payload_bytes=$((payload_bytes + $(var_log_stat_size "$outdir/logs/var-log/journal-all-since.txt" 2>/dev/null || printf 0)))
        if [[ $payload_bytes -gt $var_log_max_bytes ]]; then
          rm -rf -- "$outdir/logs/var-log/merged" "$outdir/logs/var-log/raw" "$outdir/logs/var-log/original"
          rm -f -- "$outdir/logs/var-log/PAYLOAD-BYTES.txt"
          printf 'max_bytes=%s\nstatus=not-collected-combined-payload-exceeded-cap\n' \
            "$var_log_max_bytes" >"$outdir/logs/var-log/OVER-LIMIT.txt"
          write_skip_artifact "$outdir/logs/var-log/journal-all-since.txt" \
            "not collected because combined /var/log and journal text exceeded the per-node cap"
          failed=1
        else
          printf '%s\n' "$payload_bytes" >"$outdir/logs/var-log/PAYLOAD-BYTES.txt"
        fi
      fi
    elif [[ $var_log_rc -ne 0 ]]; then
      write_skip_artifact "$outdir/logs/var-log/journal-all-since.txt" \
        "not collected because /var/log payload accounting was unavailable"
      failed=1
    fi
  fi

  if [[ $failed -ne 0 ]]; then
    return 2
  fi
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  collect_node_main "$@"
fi
