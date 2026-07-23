#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_before() {
  local file=$1 first=$2 second=$3 first_line second_line
  first_line="$(grep -nF "$first" "$file" | head -n1 | cut -d: -f1)"
  second_line="$(grep -nF "$second" "$file" | head -n1 | cut -d: -f1)"
  [[ -n "$first_line" && -n "$second_line" && "$first_line" -lt "$second_line" ]] \
    || fail "expected '$first' before '$second' in $file"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/collect-var-log.sh"

test_numbered_rotations_are_merged_oldest_to_newest() {
  local var_log="$tmpdir/numbered/var-log"
  local out="$tmpdir/numbered/out"
  mkdir -p "$var_log"
  printf 'current line\n' >"$var_log/syslog"
  printf 'middle line\n' >"$var_log/syslog.1"
  printf 'oldest line\n' | gzip -c >"$var_log/syslog.2.gz"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  local merged="$out/merged/tree/files/syslog.merged"
  [[ -f "$merged" ]] || fail "missing merged syslog"
  assert_before "$merged" "oldest line" "middle line"
  assert_before "$merged" "middle line" "current line"
  [[ -f "$out/INDEX.tsv" ]] || fail "missing INDEX.tsv"
  [[ ! -e "$out/original" ]] || fail "original logs should not be retained by default"
}

test_supported_codecs_and_dated_rotations_are_merged() {
  local var_log="$tmpdir/codecs/var-log"
  local out="$tmpdir/codecs/out"
  mkdir -p "$var_log/app"
  printf 'gz oldest\n' | gzip -c >"$var_log/app/service.log-20260720.gz"
  printf 'xz older\n' | xz -c >"$var_log/app/service.log-20260721.xz"
  printf 'bz2 recent\n' | bzip2 -c >"$var_log/app/service.log-20260722.bz2"
  printf 'zst newest rotation\n' | zstd -q -c >"$var_log/app/service.log-20260723.zst"
  printf 'active\n' >"$var_log/app/service.log"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  local merged="$out/merged/tree/dirs/app/files/service.log.merged"
  [[ -f "$merged" ]] || fail "missing dated merged log"
  assert_before "$merged" "gz oldest" "xz older"
  assert_before "$merged" "xz older" "bz2 recent"
  assert_before "$merged" "bz2 recent" "zst newest rotation"
  assert_before "$merged" "zst newest rotation" "active"
}

test_opaque_files_are_preserved_without_merging() {
  local var_log="$tmpdir/opaque/var-log"
  local out="$tmpdir/opaque/out"
  mkdir -p "$var_log/journal"
  printf 'PK\003\004zip bytes\n' >"$var_log/support.zip"
  printf 'tar-like bytes\n' >"$var_log/support.tar.gz"
  printf '\000\001\002binary\n' >"$var_log/wtmp"
  printf '\000journal bytes\n' >"$var_log/journal/system.journal"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  cmp "$var_log/support.zip" "$out/raw/support.zip" || fail "zip was not preserved byte-for-byte"
  cmp "$var_log/support.tar.gz" "$out/raw/support.tar.gz" || fail "tar.gz was not preserved byte-for-byte"
  cmp "$var_log/wtmp" "$out/raw/wtmp" || fail "binary file was not preserved byte-for-byte"
  cmp "$var_log/journal/system.journal" "$out/raw/journal/system.journal" \
    || fail "journal was not preserved byte-for-byte"
  [[ ! -e "$out/merged/tree/files/support.merged" ]] || fail "zip should never be merged"
  grep -qF 'support.zip' "$out/UNREDACTED-OPAQUE.txt" || fail "zip missing opaque warning"
  grep -qF 'wtmp' "$out/UNREDACTED-OPAQUE.txt" || fail "binary missing opaque warning"
}

test_keep_originals_is_opt_in() {
  local var_log="$tmpdir/originals/var-log"
  local out="$tmpdir/originals/out"
  mkdir -p "$var_log"
  printf 'old\n' | gzip -c >"$var_log/messages.1.gz"
  printf 'new\n' >"$var_log/messages"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "1"

  cmp "$var_log/messages" "$out/original/messages" || fail "active original not retained"
  cmp "$var_log/messages.1.gz" "$out/original/messages.1.gz" || fail "compressed original not retained"
}

test_over_limit_returns_partial_without_log_payloads() {
  local var_log="$tmpdir/limit/var-log"
  local out="$tmpdir/limit/out"
  local rc=0
  mkdir -p "$var_log"
  printf '1234567890\n' >"$var_log/messages"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "5" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "over-limit collection should return 2, got $rc"
  [[ -f "$out/OVER-LIMIT.txt" ]] || fail "over-limit marker missing"
  [[ ! -e "$out/merged" && ! -e "$out/raw" && ! -e "$out/original" ]] \
    || fail "over-limit collection left payload files"
}

test_corrupt_archive_is_preserved_and_other_families_continue() {
  local var_log="$tmpdir/corrupt/var-log"
  local out="$tmpdir/corrupt/out"
  local rc=0
  mkdir -p "$var_log"
  printf 'not gzip\n' >"$var_log/broken.log.1.gz"
  printf 'healthy\n' >"$var_log/healthy.log"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "corrupt archive should return 2, got $rc"
  cmp "$var_log/broken.log.1.gz" "$out/raw/broken.log.1.gz" \
    || fail "corrupt archive was not preserved"
  grep -qF 'healthy' "$out/merged/tree/files/healthy.log.merged" || fail "healthy family was not collected"
  grep -qF 'decode-failed' "$out/ERRORS.tsv" || fail "decode failure was not recorded"
}

test_symlinks_and_sensitive_paths_are_not_read() {
  local var_log="$tmpdir/exclusions/var-log"
  local out="$tmpdir/exclusions/out"
  local outside="$tmpdir/exclusions/outside-secret"
  mkdir -p "$var_log"
  printf 'outside sentinel\n' >"$outside"
  ln -s "$outside" "$var_log/follow-me.log"
  printf 'private material\n' >"$var_log/server.pem"
  printf 'compressed private material\n' | gzip -c >"$var_log/server.pem.gz"
  printf 'rotated private material\n' >"$var_log/server.key.1"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  [[ ! -e "$out/merged/tree/files/follow-me.log.merged" && ! -e "$out/raw/follow-me.log" ]] \
    || fail "collector followed a symlink"
  [[ ! -e "$out/merged/tree/files/server.pem.merged" && ! -e "$out/raw/server.pem" ]] \
    || fail "collector copied a forbidden path"
  [[ ! -e "$out/merged/tree/files/server.pem.merged" && ! -e "$out/raw/server.pem.gz" ]] \
    || fail "collector copied a compressed forbidden path"
  [[ ! -e "$out/merged/tree/files/server.key.merged" && ! -e "$out/raw/server.key.1" ]] \
    || fail "collector copied a rotated forbidden path"
  grep -qF 'server.pem' "$out/SKIPPED-sensitive.txt" || fail "sensitive skip was not recorded"
}

test_source_content_and_metadata_are_unchanged() {
  local var_log="$tmpdir/immutable/var-log"
  local out="$tmpdir/immutable/out"
  local source before_hash after_hash before_mode after_mode before_mtime after_mtime
  mkdir -p "$var_log"
  source="$var_log/messages"
  printf 'immutable source\n' >"$source"
  chmod 640 "$source"
  touch -t 202607200101 "$source"
  before_hash="$(shasum -a 256 "$source" | awk '{print $1}')"
  before_mode="$(stat -f '%Lp' "$source" 2>/dev/null || stat -c '%a' "$source")"
  before_mtime="$(stat -f '%m' "$source" 2>/dev/null || stat -c '%Y' "$source")"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  after_hash="$(shasum -a 256 "$source" | awk '{print $1}')"
  after_mode="$(stat -f '%Lp' "$source" 2>/dev/null || stat -c '%a' "$source")"
  after_mtime="$(stat -f '%m' "$source" 2>/dev/null || stat -c '%Y' "$source")"
  [[ "$before_hash" == "$after_hash" ]] || fail "source content changed"
  [[ "$before_mode" == "$after_mode" ]] || fail "source mode changed"
  [[ "$before_mtime" == "$after_mtime" ]] || fail "source mtime changed"
}

test_missing_codec_preserves_archive_and_returns_partial() {
  local var_log="$tmpdir/missing-codec/var-log"
  local out="$tmpdir/missing-codec/out"
  local rc=0
  mkdir -p "$var_log"
  printf 'zstd payload\n' | zstd -q -c >"$var_log/service.log.1.zst"

  PATH="/usr/bin:/bin" CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "missing codec should return 2, got $rc"
  cmp "$var_log/service.log.1.zst" "$out/raw/service.log.1.zst" \
    || fail "archive with missing codec was not preserved"
  grep -qF 'missing-codec:zstd' "$out/ERRORS.tsv" || fail "missing codec was not recorded"
}

test_family_file_does_not_collide_with_nested_directory() {
  local var_log="$tmpdir/collision/var-log"
  local out="$tmpdir/collision/out"
  mkdir -p "$var_log/app"
  mkdir -p "$var_log/app.merged"
  printf 'top-level rotation\n' >"$var_log/app.1"
  printf 'nested log\n' >"$var_log/app/service.log"
  printf 'suffix collision log\n' >"$var_log/app.merged/service.log"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  grep -qF 'top-level rotation' "$out/merged/tree/files/app.merged" \
    || fail "top-level family missing after path collision"
  grep -qF 'nested log' "$out/merged/tree/dirs/app/files/service.log.merged" \
    || fail "nested family missing after path collision"
  grep -qF 'suffix collision log' "$out/merged/tree/dirs/app.merged/files/service.log.merged" \
    || fail "suffix-named directory collided with a merged family"
}

test_zero_padded_numbered_rotations_are_base10() {
  local var_log="$tmpdir/zero-padded/var-log"
  local out="$tmpdir/zero-padded/out"
  mkdir -p "$var_log"
  printf 'rotation ten\n' >"$var_log/messages.010"
  printf 'rotation nine\n' >"$var_log/messages.09"
  printf 'rotation eight\n' >"$var_log/messages.08"
  printf 'active\n' >"$var_log/messages"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0"

  local merged="$out/merged/tree/files/messages.merged"
  assert_before "$merged" "rotation ten" "rotation nine"
  assert_before "$merged" "rotation nine" "rotation eight"
  assert_before "$merged" "rotation eight" "active"
}

test_late_nul_is_preserved_as_binary() {
  local var_log="$tmpdir/late-nul/var-log"
  local out="$tmpdir/late-nul/out"
  mkdir -p "$var_log"
  dd if=/dev/zero bs=1 count=1048576 2>/dev/null | tr '\000' A >"$var_log/mixed.log"
  printf '\000tail\n' >>"$var_log/mixed.log"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "2097152" "0"

  cmp "$var_log/mixed.log" "$out/raw/mixed.log" || fail "late-NUL file was not preserved raw"
  [[ ! -e "$out/merged/tree/files/mixed.log.merged" ]] || fail "late-NUL file was merged as text"
}

test_second_pass_decode_failure_rolls_back_and_preserves_raw() {
  local var_log="$tmpdir/second-pass/var-log"
  local out="$tmpdir/second-pass/out"
  local fakebin="$tmpdir/second-pass/fakebin"
  local counter="$tmpdir/second-pass/gzip-count"
  local real_gzip rc=0
  mkdir -p "$var_log" "$fakebin"
  real_gzip="$(command -v gzip)"
  printf 'archive text\n' | "$real_gzip" -c >"$var_log/app.log.1.gz"
  cat >"$fakebin/gzip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
count=0
[[ -f "$GZIP_COUNTER" ]] && count="$(cat "$GZIP_COUNTER")"
count=$((count + 1))
printf '%s\n' "$count" >"$GZIP_COUNTER"
if [[ $count -ge 3 ]]; then
  printf 'partial decode\n'
  exit 7
fi
exec "$REAL_GZIP" "$@"
EOF
  chmod +x "$fakebin/gzip"

  PATH="$fakebin:$PATH" REAL_GZIP="$real_gzip" GZIP_COUNTER="$counter" \
  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "second-pass decode failure should return 2, got $rc"
  cmp "$var_log/app.log.1.gz" "$out/raw/app.log.1.gz" \
    || fail "second-pass failure did not preserve compressed source"
  if [[ -f "$out/merged/tree/files/app.log.merged" ]]; then
    grep -qF 'partial decode' "$out/merged/tree/files/app.log.merged" \
      && fail "partial decode bytes leaked into merged output" || true
  fi
}

test_scan_metadata_is_bounded() {
  local var_log="$tmpdir/scan-limit/var-log"
  local out="$tmpdir/scan-limit/out"
  local rc=0
  mkdir -p "$var_log"
  printf 'line\n' >"$var_log/a-very-long-log-name.log"

  CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES=5 \
  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "scan metadata limit should return 2, got $rc"
  [[ -f "$out/SCAN-LIMIT.txt" ]] || fail "scan metadata limit marker missing"
  [[ ! -e "$out/merged" && ! -e "$out/raw" ]] || fail "scan metadata limit left payload"
}

test_numbered_rotations_are_merged_oldest_to_newest
test_supported_codecs_and_dated_rotations_are_merged
test_opaque_files_are_preserved_without_merging
test_keep_originals_is_opt_in
test_over_limit_returns_partial_without_log_payloads
test_corrupt_archive_is_preserved_and_other_families_continue
test_symlinks_and_sensitive_paths_are_not_read
test_source_content_and_metadata_are_unchanged
test_missing_codec_preserves_archive_and_returns_partial
test_family_file_does_not_collide_with_nested_directory
test_zero_padded_numbered_rotations_are_base10
test_late_nul_is_preserved_as_binary
test_second_pass_decode_failure_rolls_back_and_preserves_raw
test_scan_metadata_is_bounded
printf 'ok: var-log collector\n'
