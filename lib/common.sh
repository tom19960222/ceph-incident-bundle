#!/usr/bin/env bash
set -euo pipefail

# Shared helpers for the Ceph incident bundle harness.

log() {
  # stderr: stdout is reserved for the final `bundle:` line (machine-readable).
  printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

die() {
  log "FATAL: $*"
  exit 1
}

require_file() {
  [[ -f "$1" ]] || die "missing file: $1"
}

ensure_dir() {
  mkdir -p "$1"
}

# Shared SSH option vector (incl. -i KEY). Printed one argv item per line so
# callers fill an array with `while IFS= read -r w; do a+=("$w"); done` — the
# bash-3.2-safe idiom used throughout (no mapfile). Single source of truth so a
# flag like BatchMode can't drift between call sites.
ssh_base_opts() {
  local ssh_key=$1 timeout=$2
  # LogLevel=ERROR keeps ssh's own chatter (e.g. "Warning: Permanently added ...
  # to the list of known hosts", server banners) out of the captured artifacts,
  # while still surfacing real connection errors.
  printf '%s\n' \
    -i "$ssh_key" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o IdentityAgent=none \
    -o LogLevel=ERROR \
    -o "ConnectTimeout=$timeout" \
    -o "ServerAliveInterval=$timeout" \
    -o ServerAliveCountMax=1
  if [[ "${CEPH_INCIDENT_TRUST_SSH_HOST_KEY:-1}" == "1" ]]; then
    printf '%s\n' -o StrictHostKeyChecking=accept-new
    if [[ -n "${CEPH_INCIDENT_KNOWN_HOSTS_FILE:-}" ]]; then
      printf '%s\n' -o "UserKnownHostsFile=$CEPH_INCIDENT_KNOWN_HOSTS_FILE ${HOME}/.ssh/known_hosts"
    fi
  fi
}

ssh_debug_safe_name() {
  local value=$1
  value="$(printf '%s' "$value" | tr -c 'A-Za-z0-9._-' '_')"
  while [[ "$value" == *..* ]]; do
    value="${value//../__}"
  done
  [[ -n "$value" ]] || value=ssh
  printf '%s' "$value"
}

write_ssh_debug_log() {
  local workdir=$1 label=$2 target=$3 ssh_key=$4 timeout=$5
  local debug_dir="$workdir/ssh-debug"
  local safe_label safe_target artifact started ended rc tbin _w
  local -a sopts cmd

  ensure_dir "$debug_dir"
  safe_label="$(ssh_debug_safe_name "$label")"
  safe_target="$(ssh_debug_safe_name "$target")"
  artifact="$debug_dir/${safe_label}-${safe_target}.log"
  while IFS= read -r _w; do sopts+=("$_w"); done < <(ssh_base_opts "$ssh_key" "$timeout")
  cmd=(ssh "${sopts[@]}" -vvv -o LogLevel=DEBUG3 "$target" true)
  tbin="$(timeout_cmd)"
  [[ -n "$tbin" ]] && cmd=("$tbin" "$timeout" "${cmd[@]}")

  started="$(date -u +%FT%TZ)"
  {
    printf '# ssh debug log\n'
    printf '# target: %s\n' "$target"
    printf '# label: %s\n' "$label"
    printf '# started: %s\n' "$started"
    printf '# command: '
    printf '%q ' "${cmd[@]}"
    printf '\n'
  } >"$artifact"

  if "${cmd[@]}" >>"$artifact" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  ended="$(date -u +%FT%TZ)"
  {
    printf '# ended: %s\n' "$ended"
    printf '# exit_code: %s\n' "$rc"
  } >>"$artifact"
}

# Write a `SKIPPED: <reason>` artifact. `_once` does not overwrite an existing
# file (so a collector's specific reason is never clobbered by a generic one).
write_skip_artifact() {
  local artifact=$1 reason=$2
  ensure_dir "$(dirname -- "$artifact")"
  printf 'SKIPPED: %s\n' "$reason" >"$artifact"
}

write_skip_artifact_once() {
  local artifact=$1 reason=$2
  [[ -f "$artifact" ]] && return 0
  write_skip_artifact "$artifact" "$reason"
}

# Live progress to stderr (stdout stays reserved for the final `bundle:` line).
# Suppressed when CEPH_INCIDENT_QUIET is set. Call only from workstation-side
# code — NOT from the remote node collector (its stderr is multiplexed over ssh).
progress() {
  [[ -n "${CEPH_INCIDENT_QUIET:-}" ]] && return 0
  printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

# Resolve a timeout binary: GNU coreutils `timeout`, or `gtimeout` on macOS.
# Prints the binary name, or nothing if neither is installed.
timeout_cmd() {
  if command -v timeout >/dev/null 2>&1; then
    printf 'timeout'
  elif command -v gtimeout >/dev/null 2>&1; then
    printf 'gtimeout'
  fi
}

json_escape() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

manifest_add() {
  local manifest=$1 host=$2 collector=$3 artifact=$4 command=$5 exit_code=$6 started=$7 ended=$8
  [[ "$exit_code" =~ ^[0-9]+$ ]] || die "manifest_add requires numeric exit_code: $exit_code"
  ensure_dir "$(dirname -- "$manifest")"
  printf '{"host":"%s","collector":"%s","artifact":"%s","command":"%s","exit_code":%s,"started":"%s","ended":"%s"}\n' \
    "$(json_escape "$host")" \
    "$(json_escape "$collector")" \
    "$(json_escape "$artifact")" \
    "$(json_escape "$command")" \
    "$exit_code" \
    "$(json_escape "$started")" \
    "$(json_escape "$ended")" >>"$manifest"
}

# Redact stdin to stdout, writing "<redacted> <records>" to $1: how many lines
# were replaced, and how many newline-terminated records were read. The second
# number is what proves the whole stream arrived.
#
# Best-effort redaction (NOT a complete DLP): keyword lines, ceph key material
# (`key = AQB..==`, base64 blobs), and whole multi-line PEM private key blocks.
# Extensions/encodings outside this are intentionally not covered — see README
# "安全界線"; operators must self-review before sharing.
redact_stream() {
  local count_file=$1
  local count=0 records=0 line in_pem=0 redact nocase_was_set=0 tail_done=0
  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch

  # The `||` arm exists for a final line with no newline. It is latched so it
  # can fire at most once: a `read` that fails without clearing `line` would
  # otherwise keep the condition true forever — see issue #49.
  while IFS= read -r line || { [[ $tail_done -eq 0 && -n "$line" ]] && tail_done=1; }; do
    [[ $tail_done -eq 0 ]] && records=$((records + 1))
    redact=0
    if [[ "$line" =~ -----BEGIN[[:space:]].*PRIVATE[[:space:]]KEY----- ]]; then
      in_pem=1
    fi
    if [[ $in_pem -eq 1 ]]; then
      redact=1
      if [[ "$line" =~ -----END[[:space:]].*PRIVATE[[:space:]]KEY----- ]]; then
        in_pem=0
      fi
    elif [[ "$line" =~ (password|secret|token|keyring|private([[:space:]_-]+)?key) ]]; then
      redact=1
    elif [[ "$line" =~ (^|[^[:alnum:]])key[[:space:]]*[:=] ]]; then
      redact=1
    elif [[ "$line" =~ [A-Za-z0-9+/]{38,}={1,2} ]]; then
      redact=1
    fi
    if [[ $redact -eq 1 ]]; then
      printf '[REDACTED]\n'
      count=$((count + 1))
    else
      printf '%s\n' "$line"
    fi
    [[ $tail_done -eq 1 ]] && break
  done

  if [[ $nocase_was_set -eq 1 ]]; then shopt -s nocasematch; else shopt -u nocasematch; fi
  printf '%s %s\n' "$count" "$records" >"$count_file"
}

# Redact one text artifact in place.
#
# The source is streamed through a pipe rather than read with `<"$source_file"`.
# Bash keeps a file offset for a redirected regular file and can stop making
# forward progress on a large one: past 2 GiB `read` began failing without
# clearing `line`, and the loop rewrote the same line until the disk filled —
# one 3.25 GB node log produced 11.75 GB of output with a single line repeated
# 75,056,647 times (issue #49). The same content read at a lower offset, and
# the same content read through a pipe, are both fine. A pipe carries no offset.
#
# Forward progress is then checked rather than assumed: the stream must deliver
# every newline-terminated record the source holds. A stall shows up as a short
# count, and the answer is to leave the original artifact in place and report it
# as NOT redacted — silently dropping evidence is worse than refusing to redact.
redact_file() {
  local source_file=$1 redaction_log=$2
  require_file "$source_file"
  ensure_dir "$(dirname -- "$redaction_log")"

  local source_dir tmp_file count_file mode expected count='' records='' stream_ok=1
  source_dir="$(dirname -- "$source_file")"
  tmp_file="$(mktemp "$source_dir/.${source_file##*/}.XXXXXX")"
  count_file="$(mktemp "$source_dir/.${source_file##*/}.count.XXXXXX")"
  expected="$(wc -l <"$source_file")"
  expected=${expected//[[:space:]]/}

  # `if !` rather than `set +e`: toggling errexit here would hand the caller
  # back a different shell than it had, and this function is called from both
  # errexit and non-errexit contexts. A `cat` that dies mid-stream needs no
  # special case — it shows up as a short record count below.
  if ! cat -- "$source_file" | redact_stream "$count_file" >"$tmp_file"; then
    stream_ok=0
  fi
  read -r count records <"$count_file" || true

  if [[ $stream_ok -eq 0 || "$records" != "$expected" ]]; then
    rm -f -- "$tmp_file" "$count_file"
    printf '%s: read %s of %s line(s), original left as-is (NOT redacted)\n' \
      "$source_file" "$records" "$expected" >>"$redaction_log"
    return 1
  fi

  rm -f -- "$count_file"
  mode="$(stat -c '%a' "$source_file" 2>/dev/null || stat -f '%Lp' "$source_file" 2>/dev/null || printf '600')"
  chmod "$mode" "$tmp_file" 2>/dev/null || true
  mv -f -- "$tmp_file" "$source_file"
  printf '%s: %s line(s) redacted\n' "$source_file" "$count" >>"$redaction_log"
}

redact_compressed_file() {
  local source_file=$1 redaction_log=$2 codec=$3
  require_file "$source_file"
  ensure_dir "$(dirname -- "$redaction_log")"

  local dir tmp_plain tmp_encoded mode decode_rc=0 encode_rc=0
  dir="$(dirname -- "$source_file")"
  mode="$(stat -c '%a' "$source_file" 2>/dev/null || stat -f '%Lp' "$source_file" 2>/dev/null || printf '600')"
  tmp_plain="$(mktemp "$dir/.${source_file##*/}.plain.XXXXXX")"
  tmp_encoded="$(mktemp "$dir/.${source_file##*/}.encoded.XXXXXX")"
  case "$codec" in
    gz) gzip -dc -- "$source_file" >"$tmp_plain" 2>/dev/null || decode_rc=$? ;;
    xz) xz -dc -- "$source_file" >"$tmp_plain" 2>/dev/null || decode_rc=$? ;;
    bz2) bzip2 -dc -- "$source_file" >"$tmp_plain" 2>/dev/null || decode_rc=$? ;;
    zst) zstd -qdc -- "$source_file" >"$tmp_plain" 2>/dev/null || decode_rc=$? ;;
    *) rm -f -- "$tmp_plain" "$tmp_encoded"; return 1 ;;
  esac
  if [[ $decode_rc -ne 0 ]]; then
    rm -f -- "$tmp_plain" "$tmp_encoded"
    printf '%s: %s decompress failed, left as-is (NOT redacted)\n' \
      "$source_file" "$codec" >>"$redaction_log"
    return 0
  fi

  # The decompressed payload is what `redact_file` reads, so a codec whose
  # plaintext crosses the size where reading fails takes the same failure — and
  # the same fail-closed answer: leave the original encoded artifact alone.
  if ! redact_file "$tmp_plain" "$redaction_log"; then
    rm -f -- "$tmp_plain" "$tmp_encoded"
    return 1
  fi
  case "$codec" in
    gz) gzip -c -- "$tmp_plain" >"$tmp_encoded" || encode_rc=$? ;;
    xz) xz -c -- "$tmp_plain" >"$tmp_encoded" || encode_rc=$? ;;
    bz2) bzip2 -c -- "$tmp_plain" >"$tmp_encoded" || encode_rc=$? ;;
    zst) zstd -q -c -- "$tmp_plain" >"$tmp_encoded" || encode_rc=$? ;;
  esac
  if [[ $encode_rc -eq 0 ]]; then
    rm -f -- "$tmp_plain"
    chmod "$mode" "$tmp_encoded" 2>/dev/null || true
    mv -f -- "$tmp_encoded" "$source_file"
  else
    rm -f -- "$tmp_plain" "$tmp_encoded"
    printf '%s: %s recompress failed, original left as-is (NOT redacted)\n' \
      "$source_file" "$codec" >>"$redaction_log"
    return 1
  fi
}

redact_gz_file() {
  redact_compressed_file "$1" "$2" gz
}

run_capture() {
  local manifest=$1 host=$2 collector=$3 artifact=$4
  shift 4
  [[ ${1-} == -- ]] || die "run_capture requires -- before the command"
  shift

  local -a cmd
  local started ended rc command_string artifact_dir artifact_tmp

  cmd=("$@")
  [[ ${#cmd[@]} -gt 0 ]] || die "run_capture requires a command"

  started="$(date -u +%FT%TZ)"
  artifact_dir="$(dirname -- "$artifact")"
  ensure_dir "$artifact_dir"
  artifact_tmp="$(mktemp "$artifact_dir/.${artifact##*/}.XXXXXX")"

  printf '# host: %s\n# collector: %s\n# started: %s\n' "$host" "$collector" "$started" >"$artifact_tmp"
  printf -v command_string '%q ' "${cmd[@]}"
  command_string=${command_string% }

  local tbin
  tbin="$(timeout_cmd)"
  if [[ -n "$tbin" ]]; then
    printf '# timeout: %ss\n' "${COMMAND_TIMEOUT:-20}" >>"$artifact_tmp"
    if "$tbin" "${COMMAND_TIMEOUT:-20}" "${cmd[@]}" >>"$artifact_tmp" 2>&1; then
      rc=0
    else
      rc=$?
    fi
  else
    printf '# timeout: unavailable\n' >>"$artifact_tmp"
    if "${cmd[@]}" >>"$artifact_tmp" 2>&1; then
      rc=0
    else
      rc=$?
    fi
  fi

  # Make timeout-kills (124) distinguishable from ordinary command failure, and
  # mark the artifact so a truncated capture is visible to whoever reads it.
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then
    printf '# TRUNCATED: command timed out after %ss (exit %s)\n' "${COMMAND_TIMEOUT:-20}" "$rc" >>"$artifact_tmp"
  fi

  ended="$(date -u +%FT%TZ)"
  mv -f -- "$artifact_tmp" "$artifact"
  manifest_add "$manifest" "$host" "$collector" "$artifact" "$command_string" "$rc" "$started" "$ended"

  if [[ $rc -ne 0 && -n "${ERROR_LOG:-}" ]]; then
    ensure_dir "$(dirname -- "$ERROR_LOG")"
    printf '%s host=%s collector=%s artifact=%s exit=%s command=%s\n' \
      "$ended" "$host" "$collector" "$artifact" "$rc" "$command_string" >>"$ERROR_LOG"
  fi

  return "$rc"
}

copy_if_exists() {
  local source=$1 dest=$2
  [[ -e "$source" ]] || return 0
  ensure_dir "$(dirname -- "$dest")"
  cp -a -- "$source" "$dest"
}
