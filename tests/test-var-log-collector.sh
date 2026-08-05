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

# Evidence Window fixtures pin mtimes to fixed epochs, so what the window keeps
# is decided by the fixture rather than by when the suite happens to run.
WINDOW_START=1000000000
IN_WINDOW=$((WINDOW_START + 3600))
AT_WINDOW=$WINDOW_START
JUST_BEFORE=$((WINDOW_START - 3600))
LONG_BEFORE=$((WINDOW_START - 2592000))

set_source_mtime() {
  local path=$1 epoch=$2 stamp
  stamp="$(TZ=UTC0 date -u -r "$epoch" +%Y%m%d%H%M.%S 2>/dev/null ||
    TZ=UTC0 date -u -d "@$epoch" +%Y%m%d%H%M.%S)"
  TZ=UTC0 touch -t "$stamp" -- "$path"
}

index_row() {
  local out=$1 source=$2
  LC_ALL=C awk -F '\t' -v want="$source" 'NR > 1 && $1 == want {print; found = 1}
    END {exit !found}' "$out/INDEX.tsv"
}

# INDEX.tsv fields: 1 source, 2 family, 3 codec, 4 stored_bytes,
# 5 decoded_bytes, 6 mtime_epoch, 7 disposition, 8 detail.
assert_index_field() {
  local out=$1 source=$2 field=$3 want=$4 row got
  row="$(index_row "$out" "$source")" || fail "no INDEX.tsv row for $source"
  got="$(printf '%s\n' "$row" | LC_ALL=C awk -F '\t' -v f="$field" '{print $f}')"
  [[ "$got" == "$want" ]] \
    || fail "INDEX.tsv field $field for $source: want '$want', got '$got'"
}

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "1" "0"

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
    collect_var_logs "$var_log" "$out" "5" "0" "0" || rc=$?

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0" || rc=$?

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0" || rc=$?

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0"

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
    collect_var_logs "$var_log" "$out" "2097152" "0" "0"

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
    collect_var_logs "$var_log" "$out" "1048576" "0" "0" || rc=$?

  [[ "$rc" == "2" ]] || fail "second-pass decode failure should return 2, got $rc"
  cmp "$var_log/app.log.1.gz" "$out/raw/app.log.1.gz" \
    || fail "second-pass failure did not preserve compressed source"
  if [[ -f "$out/merged/tree/files/app.log.merged" ]]; then
    grep -qF 'partial decode' "$out/merged/tree/files/app.log.merged" \
      && fail "partial decode bytes leaked into merged output" || true
  fi
}

test_window_keeps_sources_at_or_after_its_start_and_indexes_the_rest() {
  local var_log="$tmpdir/window-boundary/var-log"
  local out="$tmpdir/window-boundary/out"
  mkdir -p "$var_log"
  printf 'inside the window\n' >"$var_log/syslog"
  printf 'exactly at the window start\n' >"$var_log/syslog.1"
  printf 'just before the window\n' | gzip -c >"$var_log/syslog.2.gz"
  printf 'a month before the window\n' | gzip -c >"$var_log/syslog.3.gz"
  set_source_mtime "$var_log/syslog" "$IN_WINDOW"
  set_source_mtime "$var_log/syslog.1" "$AT_WINDOW"
  set_source_mtime "$var_log/syslog.2.gz" "$JUST_BEFORE"
  set_source_mtime "$var_log/syslog.3.gz" "$LONG_BEFORE"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "$WINDOW_START"

  local merged="$out/merged/tree/files/syslog.merged"
  grep -qF 'exactly at the window start' "$merged" \
    || fail "a source stamped exactly at the window start was dropped"
  grep -qF 'inside the window' "$merged" || fail "an in-window source was dropped"
  grep -qF 'just before the window' "$merged" \
    || fail "the newest source before the window start was dropped"
  grep -qF 'a month before the window' "$merged" \
    && fail "an out-of-window source was merged" || true
  [[ ! -e "$out/raw/syslog.3.gz" ]] || fail "an out-of-window source was copied"

  assert_index_field "$out" syslog.3.gz 7 outside-window
  assert_index_field "$out" syslog.3.gz 6 "$LONG_BEFORE"
  assert_index_field "$out" syslog.3.gz 8 'older than the evidence window start'
  assert_index_field "$out" syslog.2.gz 7 merge-candidate
  assert_index_field "$out" syslog.2.gz 6 "$JUST_BEFORE"
  assert_index_field "$out" syslog.2.gz 8 'oldest-to-newest window-crossing'
  assert_index_field "$out" syslog 6 "$IN_WINDOW"
  assert_index_field "$out" syslog 8 oldest-to-newest
}

test_window_crossing_applies_per_family_and_leaves_no_empty_output() {
  local var_log="$tmpdir/window-families/var-log"
  local out="$tmpdir/window-families/out"
  mkdir -p "$var_log/slow"
  # A family that rotated moments before the collect: the current file is nearly
  # empty and every line inside the window sits in the newest rotation.
  printf 'post-rotation line\n' >"$var_log/fast.log"
  printf 'the incident is in here\n' >"$var_log/fast.log.1"
  set_source_mtime "$var_log/fast.log" "$IN_WINDOW"
  set_source_mtime "$var_log/fast.log.1" "$JUST_BEFORE"
  # A family that rotates far more slowly: nothing it has is inside the window.
  printf 'slow current\n' >"$var_log/slow/slow.log"
  printf 'slow rotation\n' >"$var_log/slow/slow.log.1"
  printf 'slow ancient\n' >"$var_log/slow/slow.log.2"
  set_source_mtime "$var_log/slow/slow.log" "$JUST_BEFORE"
  set_source_mtime "$var_log/slow/slow.log.1" "$LONG_BEFORE"
  set_source_mtime "$var_log/slow/slow.log.2" $((LONG_BEFORE - 2592000))

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "$WINDOW_START"

  local fast="$out/merged/tree/files/fast.log.merged"
  local slow="$out/merged/tree/dirs/slow/files/slow.log.merged"
  grep -qF 'the incident is in here' "$fast" \
    || fail "a rotation that happened just before the collect was dropped"
  grep -qF 'post-rotation line' "$fast" || fail "the active log was dropped"
  # The slow family keeps its own newest source: one family's rotation rhythm
  # must not decide another's.
  grep -qF 'slow current' "$slow" \
    || fail "a family with nothing inside the window kept no source at all"
  grep -qF 'slow rotation' "$slow" && fail "a superseded rotation was merged" || true
  assert_index_field "$out" slow/slow.log 7 merge-candidate
  assert_index_field "$out" slow/slow.log 8 'oldest-to-newest window-crossing'
  assert_index_field "$out" slow/slow.log.1 7 outside-window
  assert_index_field "$out" slow/slow.log.2 7 outside-window

  local empty
  while IFS= read -r empty; do
    fail "window left an empty output file: $empty"
  done < <(find "$out/merged" -type f -empty 2>/dev/null || true)
  while IFS= read -r empty; do
    fail "window left staging behind: $empty"
  done < <(find "$out/merged" -name '*.tmp.*' 2>/dev/null || true)
}

test_window_groups_binary_journals_by_machine_and_stream() {
  local var_log="$tmpdir/window-journal/var-log"
  local out="$tmpdir/window-journal/out"
  local machine_a="$var_log/journal/1111111111111111111111111111aaaa"
  local machine_b="$var_log/journal/2222222222222222222222222222bbbb"
  mkdir -p "$machine_a" "$machine_b"
  printf 'system current\0\n' >"$machine_a/system.journal"
  printf 'system newest archive\0\n' >"$machine_a/system@0001.journal"
  printf 'system older archive\0\n' >"$machine_a/system@0002.journal"
  printf 'user newest archive\0\n' >"$machine_a/user-1000@0001.journal"
  printf 'other machine archive\0\n' >"$machine_b/system@0003.journal"
  set_source_mtime "$machine_a/system.journal" "$IN_WINDOW"
  set_source_mtime "$machine_a/system@0001.journal" "$JUST_BEFORE"
  set_source_mtime "$machine_a/system@0002.journal" "$LONG_BEFORE"
  set_source_mtime "$machine_a/user-1000@0001.journal" "$LONG_BEFORE"
  set_source_mtime "$machine_b/system@0003.journal" "$LONG_BEFORE"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "$WINDOW_START"

  local raw="$out/raw/journal"
  local rel_a="journal/1111111111111111111111111111aaaa"
  local rel_b="journal/2222222222222222222222222222bbbb"
  [[ -f "$raw/1111111111111111111111111111aaaa/system.journal" ]] \
    || fail "the in-window journal was dropped"
  [[ -f "$raw/1111111111111111111111111111aaaa/system@0001.journal" ]] \
    || fail "the newest archived journal before the window was dropped"
  [[ ! -e "$raw/1111111111111111111111111111aaaa/system@0002.journal" ]] \
    || fail "a superseded archived journal was collected"
  # A different stream on the same machine, and the same stream on another
  # machine, each get their own window-crossing file.
  [[ -f "$raw/1111111111111111111111111111aaaa/user-1000@0001.journal" ]] \
    || fail "the user journal stream was decided by the system stream"
  [[ -f "$raw/2222222222222222222222222222bbbb/system@0003.journal" ]] \
    || fail "one machine-id decided another machine-id's journals"
  assert_index_field "$out" "$rel_a/system@0002.journal" 7 outside-window
  assert_index_field "$out" "$rel_a/system@0001.journal" 8 'binary or unknown window-crossing'
  assert_index_field "$out" "$rel_b/system@0003.journal" 7 raw
}

test_window_collects_and_marks_a_source_whose_mtime_is_unreadable() {
  local var_log="$tmpdir/window-unknown/var-log"
  local out="$tmpdir/window-unknown/out"
  local fakebin="$tmpdir/window-unknown/bin"
  mkdir -p "$var_log" "$fakebin"
  printf 'undatable evidence\n' >"$var_log/messages"
  set_source_mtime "$var_log/messages" "$LONG_BEFORE"
  # `stat` answers for size but not for mtime, which is the shape of a source
  # whose metadata the collector cannot fully read.
  cat >"$fakebin/stat" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    *%Y*|*%m*) exit 1 ;;
  esac
done
exec /usr/bin/stat "$@"
EOF
  chmod +x "$fakebin/stat"

  PATH="$fakebin:$PATH" CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "$WINDOW_START"

  grep -qF 'undatable evidence' "$out/merged/tree/files/messages.merged" \
    || fail "a source with an unreadable mtime was dropped by the window"
  assert_index_field "$out" messages 6 unknown
  assert_index_field "$out" messages 8 'oldest-to-newest mtime-unknown'
}

test_window_is_applied_before_the_byte_cap_is_estimated() {
  local var_log="$tmpdir/window-cap/var-log"
  local out="$tmpdir/window-cap/out"
  mkdir -p "$var_log"
  printf 'current\n' >"$var_log/big.log"
  printf 'crossing\n' >"$var_log/big.log.1"
  head -c 300000 /dev/zero | tr '\0' 'a' >"$var_log/big.log.2"
  head -c 300000 /dev/zero | tr '\0' 'b' >"$var_log/big.log.3"
  set_source_mtime "$var_log/big.log" "$IN_WINDOW"
  set_source_mtime "$var_log/big.log.1" "$JUST_BEFORE"
  set_source_mtime "$var_log/big.log.2" "$LONG_BEFORE"
  set_source_mtime "$var_log/big.log.3" $((LONG_BEFORE - 86400))

  # The cap is far below the out-of-window bytes and far above what the window
  # keeps: it can only pass if the window was applied first.
  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "65536" "0" "$WINDOW_START"

  [[ ! -e "$out/OVER-LIMIT.txt" ]] \
    || fail "the byte cap judged bytes the window had already excluded"
  grep -qF 'crossing' "$out/merged/tree/files/big.log.merged" \
    || fail "the window-crossing source was dropped"
}

test_window_start_must_be_an_epoch() {
  local var_log="$tmpdir/window-invalid/var-log"
  local out="$tmpdir/window-invalid/out"
  local rc=0
  mkdir -p "$var_log"
  printf 'line\n' >"$var_log/syslog"

  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "24h" || rc=$?

  [[ "$rc" == "1" ]] || fail "a non-epoch window start should be a usage error, got $rc"
  [[ ! -e "$out/INDEX.tsv" ]] || fail "a usage error should not start collecting"
}

test_scan_metadata_is_bounded() {
  local var_log="$tmpdir/scan-limit/var-log"
  local out="$tmpdir/scan-limit/out"
  local rc=0
  mkdir -p "$var_log"
  printf 'line\n' >"$var_log/a-very-long-log-name.log"

  CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES=5 \
  CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1 \
    collect_var_logs "$var_log" "$out" "1048576" "0" "0" || rc=$?

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
test_window_keeps_sources_at_or_after_its_start_and_indexes_the_rest
test_window_crossing_applies_per_family_and_leaves_no_empty_output
test_window_groups_binary_journals_by_machine_and_stream
test_window_collects_and_marks_a_source_whose_mtime_is_unreadable
test_window_is_applied_before_the_byte_cap_is_estimated
test_window_start_must_be_an_epoch
printf 'ok: var-log collector\n'
