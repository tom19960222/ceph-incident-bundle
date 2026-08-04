#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/bundle.sh"

test_json_escape() {
  local got
  got="$(json_escape 'a"b\c')"
  [[ "$got" == 'a\"b\\c' ]] || fail "json_escape returned '$got'"
}

test_json_escape_is_shell_native() {
  local fakebin="$tmpdir/fakebin"
  mkdir -p "$fakebin"
  cat >"$fakebin/python3" <<'EOF'
#!/usr/bin/env bash
printf 'python3 should not be used\n' >&2
exit 99
EOF
  chmod +x "$fakebin/python3"

  local got
  got="$(PATH="$fakebin:$PATH" json_escape 'a"b\c')"
  [[ "$got" == 'a\"b\\c' ]] || fail "shell-native json_escape failed with '$got'"
}

test_manifest_add() {
  local manifest="$tmpdir/manifest.jsonl"
  local artifact="$tmpdir/artifact.txt"
  printf 'payload\n' >"$artifact"

  manifest_add \
    "$manifest" \
    "host-1" \
    "collector-1" \
    "$artifact" \
    "command with spaces" \
    7 \
    "2026-06-29T00:00:00Z" \
    "2026-06-29T00:00:01Z"

  python3 - "$manifest" <<'PY'
import json
import sys

path = sys.argv[1]
lines = [line.rstrip("\n") for line in open(path)]
if len(lines) != 1:
    raise SystemExit(f"expected 1 manifest entry, got {len(lines)}")
entry = json.loads(lines[0])
expected = {
    "host": "host-1",
    "collector": "collector-1",
    "artifact": path.replace("manifest.jsonl", "artifact.txt"),
    "command": "command with spaces",
    "exit_code": 7,
    "started": "2026-06-29T00:00:00Z",
    "ended": "2026-06-29T00:00:01Z",
}
for key, value in expected.items():
    if entry.get(key) != value:
        raise SystemExit(f"{key}={entry.get(key)!r} != {value!r}")
PY
}

test_manifest_add_rejects_non_numeric_exit_code() {
  local manifest="$tmpdir/invalid-manifest.jsonl"
  local artifact="$tmpdir/artifact-invalid.txt"
  printf 'payload\n' >"$artifact"

  local output rc
  set +e
  output="$(
    bash -c '
      set -euo pipefail
      ROOT=$1
      MANIFEST=$2
      ARTIFACT=$3
      # shellcheck disable=SC1091
      source "$ROOT/lib/common.sh"
      manifest_add \
        "$MANIFEST" \
        "host-1" \
        "collector-1" \
        "$ARTIFACT" \
        "bad command" \
        "abc" \
        "2026-06-29T00:00:00Z" \
        "2026-06-29T00:00:01Z"
    ' bash "$ROOT" "$manifest" "$artifact" 2>&1
  )"
  rc=$?
  set -e
  [[ "$rc" != "0" ]] || fail "manifest_add accepted a non-numeric exit code"
  [[ "$output" == *"exit_code"* ]] || fail "manifest_add did not explain the exit_code failure"
}

test_redact_file() {
  local source_file="$tmpdir/secret.txt"
  local redaction_log="$tmpdir/redaction.log"

  cat >"$source_file" <<'EOF'
safe line
Password=abc
SECRET=def
token: ghi
keyring: jkl
private_key=xyz
EOF

  redact_file "$source_file" "$redaction_log"

  [[ "$(sed -n '1p' "$source_file")" == "safe line" ]] || fail "safe line was modified"
  for i in 2 3 4 5 6; do
    [[ "$(sed -n "${i}p" "$source_file")" == "[REDACTED]" ]] || fail "line $i was not redacted"
  done

  [[ -s "$redaction_log" ]] || fail "redaction log is empty"
  grep -q "secret.txt" "$redaction_log" || fail "redaction log does not mention the file"
}

test_redact_file_private_key_variants() {
  local source_file="$tmpdir/private-keys.txt"
  local redaction_log="$tmpdir/private-keys.log"

  cat >"$source_file" <<'EOF'
plain
-----BEGIN OPENSSH PRIVATE KEY-----
private-key: abc
PRIVATE KEY material
EOF

  redact_file "$source_file" "$redaction_log"

  [[ "$(sed -n '1p' "$source_file")" == "plain" ]] || fail "plain line was modified"
  for i in 2 3 4; do
    [[ "$(sed -n "${i}p" "$source_file")" == "[REDACTED]" ]] || fail "private key marker on line $i was not redacted"
  done
}

test_redact_file_multiline_pem_body() {
  local source_file="$tmpdir/pem.txt"
  local redaction_log="$tmpdir/pem.log"

  cat >"$source_file" <<'EOF'
prefix safe
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA1234567890abcdefghijKLMNOPqrstuv
ZZZZnormalbodyline+slashes/and+more1234567890ab==
-----END RSA PRIVATE KEY-----
suffix safe
EOF

  redact_file "$source_file" "$redaction_log"

  [[ "$(sed -n '1p' "$source_file")" == "prefix safe" ]] || fail "pem prefix modified"
  for i in 2 3 4 5; do
    [[ "$(sed -n "${i}p" "$source_file")" == "[REDACTED]" ]] || fail "pem body line $i not redacted"
  done
  [[ "$(sed -n '6p' "$source_file")" == "suffix safe" ]] || fail "pem suffix modified"
}

test_redact_file_ceph_key_material() {
  local source_file="$tmpdir/keymat.txt"
  local redaction_log="$tmpdir/keymat.log"

  cat >"$source_file" <<'EOF'
[client.admin]
	key = AQBabcdefghij0123456789KLMNOPQRSTUVWXyz==
"auth_key": "AQBZZZZ1111222233334444555566667777abcd=="
just a normal sentence with words
EOF

  redact_file "$source_file" "$redaction_log"

  [[ "$(sed -n '1p' "$source_file")" == "[client.admin]" ]] || fail "section header modified"
  [[ "$(sed -n '2p' "$source_file")" == "[REDACTED]" ]] || fail "ceph 'key =' line not redacted"
  [[ "$(sed -n '3p' "$source_file")" == "[REDACTED]" ]] || fail "base64 key blob not redacted"
  [[ "$(sed -n '4p' "$source_file")" == "just a normal sentence with words" ]] || fail "normal line over-redacted"
}

test_redact_file_keeps_unterminated_final_line_once() {
  local source_file="$tmpdir/no-trailing-newline.txt"
  local redaction_log="$tmpdir/no-trailing-newline.log"

  printf 'first\nlast line without a newline' >"$source_file"

  redact_file "$source_file" "$redaction_log"

  local lines
  lines="$(wc -l <"$source_file" | tr -d '[:space:]')"
  [[ "$lines" == "2" ]] || fail "unterminated final line produced $lines line(s), want 2"
  [[ "$(sed -n '2p' "$source_file")" == "last line without a newline" ]] ||
    fail "unterminated final line was not written through verbatim"
}

# The tail-line fallback (`|| [[ -n "$line" ]]`) assumes a failing `read` clears
# `line`. When it does not, the fallback stays true and the loop rewrites the
# same line until the disk fills — see issue #49, where one 3.25 GB source
# produced 11.75 GB of output with a single line repeated 75,056,647 times.
# A stream that stops early must fail closed instead, leaving the original
# artifact alone: silently dropping evidence is worse than refusing to redact.
test_redact_file_fails_closed_on_short_read() {
  local source_file="$tmpdir/short-read.txt"
  local redaction_log="$tmpdir/short-read.log"
  local fakebin="$tmpdir/short-read-bin"

  mkdir -p "$fakebin"
  cat >"$fakebin/cat" <<'EOF'
#!/usr/bin/env bash
# Stop after one line, the way a source that loses forward progress does.
shift
head -n 1 -- "$1"
EOF
  chmod +x "$fakebin/cat"

  printf 'one\ntwo\nthree\n' >"$source_file"

  local rc=0 saved_path=$PATH
  PATH="$fakebin:$PATH"
  set +e
  redact_file "$source_file" "$redaction_log"
  rc=$?
  set -e
  PATH=$saved_path

  [[ "$rc" != "0" ]] || fail "redact_file accepted a short read"
  [[ "$(cat "$source_file")" == "$(printf 'one\ntwo\nthree')" ]] ||
    fail "short read replaced the original artifact"
  if compgen -G "$tmpdir/.short-read.txt.*" >/dev/null; then
    fail "short read left a redaction temp file behind"
  fi
  grep -q "NOT redacted" "$redaction_log" ||
    fail "redaction log does not record the incomplete read"
}

# A compressed artifact is redacted through a temp plaintext file that is then
# removed, so naming that temp in the log would point an operator at a path that
# no longer exists. The line has to name the bundle artifact left unredacted.
test_compressed_short_read_names_the_original_artifact() {
  local plain="$tmpdir/rotated-short.log"
  local gz="$tmpdir/rotated-short.log.gz"
  local redaction_log="$tmpdir/gz-short.log"
  local fakebin="$tmpdir/gz-short-bin"

  mkdir -p "$fakebin"
  cat >"$fakebin/cat" <<'EOF'
#!/usr/bin/env bash
shift
head -n 1 -- "$1"
EOF
  chmod +x "$fakebin/cat"

  printf 'one\ntwo\nthree\n' >"$plain"
  gzip -c "$plain" >"$gz"
  rm -f -- "$plain"

  local rc=0 saved_path=$PATH
  PATH="$fakebin:$PATH"
  set +e
  redact_gz_file "$gz" "$redaction_log"
  rc=$?
  set -e
  PATH=$saved_path

  [[ "$rc" != "0" ]] || fail "redact_gz_file accepted a short read"
  grep -q "rotated-short.log.gz: read " "$redaction_log" ||
    fail "redaction log does not name the original artifact: $(cat "$redaction_log")"
  [[ "$(gzip -dc "$gz")" == "$(printf 'one\ntwo\nthree')" ]] ||
    fail "short read modified the compressed original"
}

test_redact_file_preserves_errexit_state() {
  local source_file="$tmpdir/errexit.txt"
  local redaction_log="$tmpdir/errexit.log"
  printf 'token: leak\nplain\n' >"$source_file"

  set +e
  redact_file "$source_file" "$redaction_log"
  local status="$-"
  set -e

  [[ "$status" != *e* ]] || fail "redact_file re-enabled errexit for its caller"
}

test_redact_file_preserves_mode() {
  local source_file="$tmpdir/mode.txt"
  local redaction_log="$tmpdir/mode.log"
  printf 'token: leak\nplain\n' >"$source_file"
  chmod 640 "$source_file"

  redact_file "$source_file" "$redaction_log"

  local got
  got="$(stat -c '%a' "$source_file" 2>/dev/null || stat -f '%Lp' "$source_file" 2>/dev/null)"
  [[ "$got" == "640" ]] || fail "redaction did not preserve file mode (got $got)"
}

test_redact_gz_file() {
  local plain="$tmpdir/rotated.log"
  local gz="$tmpdir/rotated.log.gz"
  local redaction_log="$tmpdir/gz.log"

  printf 'normal rotated line\n\tkey = AQBsecretkeymaterial0123456789abcdefghij==\nanother line\n' >"$plain"
  gzip -c "$plain" >"$gz"
  rm -f "$plain"
  chmod 640 "$gz"

  redact_gz_file "$gz" "$redaction_log"

  local decoded
  decoded="$(gzip -dc "$gz")"
  [[ "$decoded" == *"normal rotated line"* ]] || fail "gz lost normal content"
  [[ "$decoded" == *"[REDACTED]"* ]] || fail "gz secret not redacted"
  [[ "$decoded" != *"AQBsecretkeymaterial"* ]] || fail "gz secret leaked"
  local got_mode
  got_mode="$(stat -c '%a' "$gz" 2>/dev/null || stat -f '%Lp' "$gz" 2>/dev/null)"
  [[ "$got_mode" == "640" ]] || fail "gz redaction did not preserve mode"
}

test_redact_supported_compressed_files() {
  local codec extension file decoded redaction_log="$tmpdir/compressed-redaction.log"
  for codec in xz bz2 zst; do
    extension=$codec
    file="$tmpdir/secret.$extension"
    case "$codec" in
      xz) printf 'safe\ntoken: secret\n' | xz -c >"$file" ;;
      bz2) printf 'safe\ntoken: secret\n' | bzip2 -c >"$file" ;;
      zst) printf 'safe\ntoken: secret\n' | zstd -q -c >"$file" ;;
    esac

    redact_compressed_file "$file" "$redaction_log" "$codec"
    case "$codec" in
      xz) decoded="$(xz -dc "$file")" ;;
      bz2) decoded="$(bzip2 -dc "$file")" ;;
      zst) decoded="$(zstd -qdc "$file")" ;;
    esac
    [[ "$decoded" == *"safe"* && "$decoded" == *"[REDACTED]"* ]] \
      || fail "$codec compressed redaction produced unexpected content"
    [[ "$decoded" != *"token: secret"* ]] || fail "$codec compressed secret leaked"
  done
}

test_compressed_redaction_failure_preserves_original() {
  local work="$tmpdir/redaction-encode-failure"
  local fakebin="$work/fakebin"
  local file="$work/secret.log.gz"
  local log="$work/redactions.log"
  local real_gzip before after rc=0
  mkdir -p "$fakebin"
  real_gzip="$(command -v gzip)"
  printf 'safe\ntoken: secret\n' | "$real_gzip" -c >"$file"
  chmod 640 "$file"
  before="$(shasum -a 256 "$file" | awk '{print $1}')"
  cat >"$fakebin/gzip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${1-} == "-dc" ]]; then
  exec "$REAL_GZIP" "$@"
fi
printf 'simulated encoder failure\n' >&2
exit 9
EOF
  chmod +x "$fakebin/gzip"

  PATH="$fakebin:$PATH" REAL_GZIP="$real_gzip" \
    redact_compressed_file "$file" "$log" gz || rc=$?

  [[ "$rc" != "0" ]] || fail "simulated compressor failure should return non-zero"
  after="$(shasum -a 256 "$file" | awk '{print $1}')"
  [[ "$before" == "$after" ]] || fail "compressor failure destroyed the original artifact"
  "$real_gzip" -t "$file" || fail "original compressed artifact became invalid"
  local got_mode
  got_mode="$(stat -c '%a' "$file" 2>/dev/null || stat -f '%Lp' "$file" 2>/dev/null)"
  [[ "$got_mode" == "640" ]] || fail "compressor failure changed original mode"
}

test_bundle_redaction_propagates_early_failure() {
  local work="$tmpdir/bundle-redaction-failure"
  local fakebin="$work/fakebin"
  local compressed="$work/cluster/a.gz"
  local later="$work/nodes/node1/z.txt"
  local real_gzip rc=0
  mkdir -p "$fakebin" "$(dirname -- "$compressed")" "$(dirname -- "$later")"
  real_gzip="$(command -v gzip)"
  printf 'token: compressed-secret\n' | "$real_gzip" -c >"$compressed"
  printf 'token: later-secret\n' >"$later"
  cat >"$fakebin/gzip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${1-} == "-dc" ]]; then
  exec "$REAL_GZIP" "$@"
fi
exit 9
EOF
  chmod +x "$fakebin/gzip"

  PATH="$fakebin:$PATH" REAL_GZIP="$real_gzip" \
    redact_bundle_text "$work" || rc=$?

  [[ "$rc" == "2" ]] || fail "bundle redaction masked an early compressor failure (rc=$rc)"
  grep -qF '[REDACTED]' "$later" || fail "bundle redaction stopped instead of continuing"
  grep -qF 'NOT redacted' "$work/redactions.log" || fail "redaction failure warning missing"
}

test_bundle_redaction_redacts_merged_but_preserves_opaque_raw() {
  local work="$tmpdir/bundle-redaction"
  local raw_dir="$work/nodes/node1/logs/var-log/raw"
  local merged_dir="$work/nodes/node1/logs/var-log/merged"
  local tar_source="$tmpdir/tar-source"
  local before_hash after_hash
  mkdir -p "$work/cluster" "$raw_dir" "$merged_dir" "$tar_source"
  printf 'archive member\n' >"$tar_source/member.log"
  tar -czf "$raw_dir/support.tar.gz" -C "$tar_source" member.log
  printf 'safe\ntoken: merged-secret\n' >"$merged_dir/syslog.merged"
  before_hash="$(shasum -a 256 "$raw_dir/support.tar.gz" | awk '{print $1}')"

  redact_bundle_text "$work"

  after_hash="$(shasum -a 256 "$raw_dir/support.tar.gz" | awk '{print $1}')"
  [[ "$before_hash" == "$after_hash" ]] || fail "opaque tar.gz was modified by redaction"
  tar -tzf "$raw_dir/support.tar.gz" | grep -qF 'member.log' || fail "opaque tar.gz was corrupted"
  grep -qF '[REDACTED]' "$merged_dir/syslog.merged" || fail "merged log was not redacted"
  grep -qF 'merged-secret' "$merged_dir/syslog.merged" && fail "merged secret leaked" || true
}

test_post_redaction_cap_discards_payload() {
  local work="$tmpdir/post-redaction-cap"
  local var_dir="$work/nodes/node1/logs/var-log"
  mkdir -p "$work/cluster" "$var_dir/merged/tree/files"
  printf 'key=x\n' >"$var_dir/merged/tree/files/app.log.merged"
  printf '6\n' >"$var_dir/PAYLOAD-BYTES.txt"
  : >"$work/errors.log"

  redact_bundle_text "$work"
  local rc=0
  enforce_node_log_caps "$work" 8 || rc=$?

  [[ "$rc" == "2" ]] || fail "post-redaction cap should return 2, got $rc"
  [[ ! -e "$var_dir/merged" ]] || fail "post-redaction cap retained merged payload"
  [[ -f "$var_dir/OVER-LIMIT.txt" ]] || fail "post-redaction cap marker missing"
  grep -qF 'post-redaction' "$var_dir/OVER-LIMIT.txt" || fail "post-redaction status missing"
}

test_progress_respects_quiet() {
  local out
  out="$(progress "hello-progress" 2>&1)"
  [[ "$out" == *"hello-progress"* ]] || fail "progress should print when not quiet"
  out="$(CEPH_INCIDENT_QUIET=1 progress "hello-progress" 2>&1)"
  [[ -z "$out" ]] || fail "progress should be silent when CEPH_INCIDENT_QUIET set, got '$out'"
}

test_progress_goes_to_stderr() {
  local on_stdout
  on_stdout="$(progress "stderr-check" 2>/dev/null)"
  [[ -z "$on_stdout" ]] || fail "progress must not write to stdout, got '$on_stdout'"
}

test_run_capture_success() {
  local manifest="$tmpdir/run-manifest.jsonl"
  local artifact="$tmpdir/run-artifact.txt"

  run_capture "$manifest" "host-a" "collector-a" "$artifact" -- printf 'hello world\n'

  [[ "$(sed -n '1p' "$artifact")" == "# host: host-a" ]] || fail "artifact header missing host"
  grep -q 'hello world' "$artifact" || fail "artifact output missing"
  python3 - "$manifest" "$artifact" <<'PY'
import json
import sys

manifest_path, artifact_path = sys.argv[1:3]
lines = [line.rstrip("\n") for line in open(manifest_path)]
if len(lines) != 1:
    raise SystemExit(f"expected 1 manifest entry, got {len(lines)}")
entry = json.loads(lines[0])
if entry["artifact"] != artifact_path:
    raise SystemExit("artifact path mismatch")
if entry["exit_code"] != 0:
    raise SystemExit(f"unexpected exit code {entry['exit_code']}")
PY
}

# A captured command must not be able to read the caller's stdin. Several
# collectors drive `run_capture` from a `while IFS= read -r … done <<<"$list"`
# loop, and the captured command is usually `ssh`, which reads stdin to EOF: it
# would swallow the rest of the list and the loop would stop after one or two
# items. The crash-info loop lost seven of nine ids that way (#52).
test_run_capture_isolates_the_callers_stdin() {
  local manifest="$tmpdir/run-manifest-stdin.jsonl"
  local artifact="$tmpdir/run-artifact-stdin.txt"
  local seen=''
  local item

  while IFS= read -r item; do
    seen="${seen:+$seen,}$item"
    run_capture "$manifest" "host-a" "collector-a" "$artifact-$item" -- cat
  done <<<"$(printf 'one\ntwo\nthree\n')"

  [[ "$seen" == "one,two,three" ]] ||
    fail "captured command drained the loop's stdin, saw '$seen'"
  [[ "$(sed -n '5p' "$artifact-one")" == "" ]] ||
    fail "captured command read the loop's stdin as its own: $(sed -n '5,$p' "$artifact-one")"
}

test_run_capture_non_zero_writes_error_log_and_returns_code() {
  local manifest="$tmpdir/run-manifest-fail.jsonl"
  local artifact="$tmpdir/run-artifact-fail.txt"
  local error_log="$tmpdir/errors.log"
  local rc=0

  ERROR_LOG="$error_log" run_capture "$manifest" "host-b" "collector-b" "$artifact" -- bash -c 'printf fail-output; exit 7' || rc=$?
  [[ "$rc" == "7" ]] || fail "run_capture returned $rc instead of 7"
  grep -q 'fail-output' "$artifact" || fail "failure output missing from artifact"
  grep -q 'exit=7' "$error_log" || fail "error log missing exit code"
  python3 - "$manifest" <<'PY'
import json
import sys

entry = json.loads(open(sys.argv[1]).readline())
if entry["exit_code"] != 7:
    raise SystemExit(f"unexpected exit code {entry['exit_code']}")
PY
}

test_run_capture_missing_double_dash_is_fatal() {
  local manifest="$tmpdir/run-manifest-missing-dash.jsonl"
  local artifact="$tmpdir/run-artifact-missing-dash.txt"
  local rc output

  set +e
  output="$(
    bash -c '
      set -euo pipefail
      ROOT=$1
      MANIFEST=$2
      ARTIFACT=$3
      # shellcheck disable=SC1091
      source "$ROOT/lib/common.sh"
      run_capture "$MANIFEST" "host-c" "collector-c" "$ARTIFACT" printf "missing-dash\n"
    ' bash "$ROOT" "$manifest" "$artifact" 2>&1
  )"
  rc=$?
  set -e
  [[ "$rc" != "0" ]] || fail "run_capture accepted missing --"
  [[ "$output" == *"-- before the command"* ]] || fail "missing -- failure was not explained"
}

test_run_capture_timeout_branch() {
  local fakebin="$tmpdir/timeout-bin"
  local timeout_log="$tmpdir/timeout.log"
  mkdir -p "$fakebin"
  cat >"$fakebin/timeout" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TIMEOUT_LOG"
shift
"$@"
EOF
  chmod +x "$fakebin/timeout"

  local manifest="$tmpdir/run-manifest-timeout.jsonl"
  local artifact="$tmpdir/run-artifact-timeout.txt"
  PATH="$fakebin:$PATH" TIMEOUT_LOG="$timeout_log" run_capture "$manifest" "host-d" "collector-d" "$artifact" -- printf 'timeout-path\n'

  grep -q 'timeout-path' "$artifact" || fail "timeout branch did not execute command"
  grep -q '^20 printf timeout-path\\n$' "$timeout_log" || fail "fake timeout was not used"
  grep -q '^# timeout: 20s$' "$artifact" || fail "timeout header missing"
}

test_run_capture_handles_leading_dash_artifact() {
  local manifest="$tmpdir/run-manifest-leading-dash.jsonl"
  local cwd="$tmpdir/leading-dash"
  mkdir -p "$cwd"

  (
    cd "$cwd"
    run_capture "$manifest" "host-dash" "collector-dash" "-leading-dash.txt" -- printf 'dash-safe\n'
  )

  [[ -f "$cwd/-leading-dash.txt" ]] || fail "leading-dash artifact was not created"
  grep -q 'dash-safe' "$cwd/-leading-dash.txt" || fail "leading-dash artifact output missing"
}

test_run_capture_preserves_errexit_state() {
  local manifest="$tmpdir/run-manifest-state.jsonl"
  local artifact="$tmpdir/run-artifact-state.txt"
  local status

  set +e
  run_capture "$manifest" "host-e" "collector-e" "$artifact" -- bash -c 'exit 3'
  status=$?
  false
  status=$?
  set -e

  [[ "$status" == "1" ]] || fail "run_capture changed errexit state"
}

test_json_escape
test_json_escape_is_shell_native
test_manifest_add
test_manifest_add_rejects_non_numeric_exit_code
test_redact_file
test_redact_file_private_key_variants
test_redact_file_multiline_pem_body
test_redact_file_ceph_key_material
test_redact_file_keeps_unterminated_final_line_once
test_redact_file_fails_closed_on_short_read
test_redact_file_preserves_errexit_state
test_compressed_short_read_names_the_original_artifact
test_redact_file_preserves_mode
test_redact_gz_file
test_redact_supported_compressed_files
test_compressed_redaction_failure_preserves_original
test_bundle_redaction_propagates_early_failure
test_bundle_redaction_redacts_merged_but_preserves_opaque_raw
test_post_redaction_cap_discards_payload
test_progress_respects_quiet
test_progress_goes_to_stderr
test_run_capture_success
test_run_capture_isolates_the_callers_stdin
test_run_capture_non_zero_writes_error_log_and_returns_code
test_run_capture_missing_double_dash_is_fatal
test_run_capture_timeout_branch
test_run_capture_handles_leading_dash_artifact
test_run_capture_preserves_errexit_state

printf 'ok: common helpers\n'
