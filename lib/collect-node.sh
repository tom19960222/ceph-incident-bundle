#!/usr/bin/env bash
set -euo pipefail

# Collect read-only node evidence for a Ceph incident bundle.

NODE_COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$NODE_COLLECTOR_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: collect-node.sh --out DIR --host-alias ALIAS [--since DURATION] [--timeout SECONDS] [--skip-logs]
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

node_file_size() {
  local source=$1 size

  if [[ $EUID -eq 0 || -r "$source" ]]; then
    size="$(wc -c <"$source" 2>/dev/null | tr -d '[:space:]')" || return 1
  elif command -v sudo >/dev/null 2>&1; then
    size="$(sudo -n wc -c "$source" 2>/dev/null | awk '{print $1}')" || return 1
  else
    return 1
  fi

  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$size"
}

node_copy_file() {
  local source=$1 dest=$2
  ensure_dir "$(dirname -- "$dest")"

  if [[ $EUID -eq 0 || -r "$source" ]]; then
    cp -p -- "$source" "$dest"
    return $?
  fi

  if command -v sudo >/dev/null 2>&1; then
    # Intentional: read the source as root, but write $dest as the calling user
    # (who owns the bundle). `sudo tee` would create $dest as root — not wanted.
    # shellcheck disable=SC2024
    sudo -n cat -- "$source" >"$dest"
    return $?
  fi

  return 1
}

node_tail_file() {
  local source=$1 nbytes=$2 dest=$3
  ensure_dir "$(dirname -- "$dest")"

  if [[ $EUID -eq 0 || -r "$source" ]]; then
    tail -c "$nbytes" "$source" >"$dest"
    return $?
  fi

  if command -v sudo >/dev/null 2>&1; then
    # Intentional: read as root, write $dest as the calling user (see node_copy_file).
    # shellcheck disable=SC2024
    sudo -n tail -c "$nbytes" "$source" >"$dest"
    return $?
  fi

  return 1
}

copy_readable_etc_files() {
  local outdir=$1
  local source dest_name

  for source in /etc/os-release /etc/hosts /etc/resolv.conf; do
    [[ -r "$source" ]] || continue
    dest_name="${source#/etc/}"
    copy_if_exists "$source" "$outdir/system/$dest_name"
  done
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

collect_ceph_log_listing() {
  local outdir=$1 manifest=$2 host_alias=$3 timeout=$4
  local log_dir=${CEPH_INCIDENT_VAR_LOG_CEPH_DIR:-/var/log/ceph}
  local listing="$outdir/logs/ceph-log-listing.txt"

  if [[ -d "$log_dir" ]]; then
    # SC2016: the sh -c body is meant to expand on the remote sh, not here.
    # shellcheck disable=SC2016
    if ! node_run_privileged "$outdir" "$manifest" "$host_alias" "$timeout" "logs/ceph-log-listing.txt" \
      find "$log_dir" -maxdepth 2 -type f -exec sh -c '
        for path do
          size=$(wc -c <"$path" 2>/dev/null || printf unknown)
          printf "%s %s bytes\n" "$path" "$size"
        done
      ' sh {} +; then
      return 2
    fi
  else
    ensure_dir "$(dirname -- "$listing")"
    printf 'SKIPPED: %s is not a readable directory on this node\n' "$log_dir" >"$listing"
  fi
}

copy_ceph_logs() {
  local outdir=$1
  local log_dir=${CEPH_INCIDENT_VAR_LOG_CEPH_DIR:-/var/log/ceph}
  local cap_bytes=${CEPH_INCIDENT_LOG_FILE_CAP_BYTES:-1048576}
  local copied_dir="$outdir/logs/ceph"
  local source rel dest size
  local failed=0

  [[ -d "$log_dir" ]] || return 0
  ensure_dir "$copied_dir"

  while IFS= read -r -d '' source; do
    if ! size="$(node_file_size "$source")"; then
      failed=1
      continue
    fi
    rel="${source#"$log_dir"/}"
    dest="$copied_dir/$rel"
    if (( size <= cap_bytes )); then
      if ! node_copy_file "$source" "$dest"; then
        failed=1
      fi
    elif [[ "$source" == *.gz ]]; then
      # A byte-tail of a gzip stream is not decompressible (and would evade
      # redaction); record it instead of shipping garbage.
      ensure_dir "$(dirname -- "$dest")"
      printf 'original_bytes=%s\nnote=oversized compressed log skipped (gzip tail is not usable)\n' \
        "$size" >"$dest.TRUNCATED"
    else
      # Oversized: keep the most recent cap_bytes (tail) instead of dropping the
      # file silently — the active large log is often exactly what's wanted —
      # and record the truncation so the omission is visible.
      if node_tail_file "$source" "$cap_bytes" "$dest"; then
        printf 'original_bytes=%s\ntail_bytes=%s\nnote=captured trailing bytes only (file exceeded cap)\n' \
          "$size" "$cap_bytes" >"$dest.TRUNCATED"
      else
        failed=1
      fi
    fi
  done < <(node_find0 "$log_dir" -maxdepth 2 -type f \( -name '*.log' -o -name '*.log.*' -o -name '*.txt' -o -name '*.gz' \) -print0 2>/dev/null || true)

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

  copy_readable_etc_files "$outdir"
  if ! collect_var_lib_ceph "$outdir" "$manifest" "$host_alias" "$timeout"; then
    failed=1
  fi

  if [[ $skip_logs -eq 1 ]]; then
    write_skip_artifact "$outdir/logs/ceph-log-listing.txt" "log collection disabled by --skip-logs"
  else
    if ! collect_ceph_log_listing "$outdir" "$manifest" "$host_alias" "$timeout"; then
      failed=1
    fi
    if ! copy_ceph_logs "$outdir"; then
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
