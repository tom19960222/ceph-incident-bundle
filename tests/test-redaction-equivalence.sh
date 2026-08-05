#!/usr/bin/env bash
set -euo pipefail

# Equivalence net for `redact_file`'s scanning engine (#59).
#
# #59 replaced the line-at-a-time bash loop with a single `awk` scan for speed —
# 10 MiB/min against 251 MiB/min on a real-lab syslog sample. Speed is not the
# risk; a silent change in what gets redacted is. So the old loop is kept here as
# an oracle and every fixture in the corpus below is run through both, with the
# new implementation reached only through `redact_file`'s observable surface: the
# rewritten file's bytes, the `redactions.log` line, and the return code.
#
# Keeping the oracle here rather than in `lib/` means the library carries one
# engine, and keeping the check generative rather than freezing golden files
# means the net survives a rule change: both copies get edited and the
# comparison still holds.
#
# `tests/test-common.sh` owns the per-rule assertions for `redact_file` and is
# deliberately untouched by #59 — it is the first-hand evidence that observable
# behaviour did not move.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

# The pre-#59 engine. Reads stdin, writes redacted lines to stdout and
# "<redacted> <records>" to $1, where records counts newline-terminated records
# only — the latched `||` arm processes a final line with no newline but does
# not count it (#49).
#
# $2 names one of the four rules to drop, and exists only for
# test_corpus_can_tell_the_rules_apart. Left empty — which is how every
# equivalence assertion calls it — the body is the shipped code, verbatim.
reference_redact_stream() {
  local count_file=$1 skip_rule=${2-}
  local count=0 records=0 line in_pem=0 redact nocase_was_set=0 tail_done=0
  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch

  while IFS= read -r line || { [[ $tail_done -eq 0 && -n "$line" ]] && tail_done=1; }; do
    [[ $tail_done -eq 0 ]] && records=$((records + 1))
    redact=0
    if [[ "$skip_rule" != pem && "$line" =~ -----BEGIN[[:space:]].*PRIVATE[[:space:]]KEY----- ]]; then
      in_pem=1
    fi
    if [[ $in_pem -eq 1 ]]; then
      redact=1
      if [[ "$line" =~ -----END[[:space:]].*PRIVATE[[:space:]]KEY----- ]]; then
        in_pem=0
      fi
    elif [[ "$skip_rule" != keywords && "$line" =~ (password|secret|token|keyring|private([[:space:]_-]+)?key) ]]; then
      redact=1
    elif [[ "$skip_rule" != key-label && "$line" =~ (^|[^[:alnum:]])key[[:space:]]*[:=] ]]; then
      redact=1
    elif [[ "$skip_rule" != base64 && "$line" =~ [A-Za-z0-9+/]{38,}={1,2} ]]; then
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

# The oracle runs in the C locale because that is the locale the `awk` scan pins
# for itself, and this comparison is meant to isolate the engine. The bracket
# expressions in these rules — `[[:alnum:]]` above all — resolve differently in
# a UTF-8 locale, and so does a `[[ =~ ]]` handed bytes that are not valid in
# one. That is a locale difference, not an engine difference; what pinning C
# changed for real bytes is written down in docs/behavior-contract.md §13.2.
run_reference() {
  local source=$1 out=$2 counts=$3 skip_rule=${4-}
  (
    export LC_ALL=C
    cat -- "$source" | reference_redact_stream "$counts" "$skip_rule" >"$out"
  )
}

# Fixtures the two engines must agree on byte for byte.
build_corpus() {
  local corpus=$1
  mkdir -p "$corpus"
  python3 - "$corpus" <<'PY'
import pathlib
import sys

corpus = pathlib.Path(sys.argv[1])


def write(name, data):
    (corpus / name).write_bytes(data)


# Shapes of file, not of content: the record count the integrity check compares
# against `wc -l` turns on exactly these.
write("empty", b"")
write("just-a-newline", b"\n")
write("trailing-newline", b"a\nb\n")
write("no-trailing-newline", b"first\nlast line without a newline")
write("blank-lines", b"a\n\n\nb\n\n")
write("one-unterminated-line", b"key = only line, no newline")

# The four rules, positive and negative.
write(
    "rule-keywords",
    b"safe line\nPassword=abc\nSECRET=def\ntoken: ghi\nkeyring: jkl\n"
    b"private_key=xyz\nprivate-key: 1\nprivate key: 2\nprivatekey: 3\n"
    b"a passwordless day\nnothing here\n",
)
write(
    "rule-keywords-case",
    b"PASSWORD\nSecret\nToKeN\nPrivate_Key\nprivate key\nPRIVATE-KEY\n"
    b"prIVaTe \t KEY\nKeYrInG\n",
)
write(
    "rule-key-label",
    b"key = 1\nkey: 2\nkey\t=3\nKEY : 4\n.key=5\n_key=6\n"
    b"monkey = 7\nakey:8\nkeyx = 9\nkey value 10\nturkey\n",
)

BASE64 = b"A" * 38
NEAR = b"A" * 37
write(
    "rule-base64-boundary",
    b"x " + NEAR + b"=\n"
    b"x " + BASE64 + b"=\n"
    b"x " + BASE64 + b"==\n"
    b"x " + NEAR + b"==\n"
    b"x " + BASE64 + b"===\n"
    b"x " + BASE64 + b"\n"
    b"plain line\n",
)
write(
    "rule-base64-charclass",
    b"abc+def/ghi" + b"B" * 30 + b"=\n"
    + b"C" * 20 + b"-" + b"C" * 20 + b"=\n"
    + b"D" * 20 + b" " + b"D" * 20 + b"=\n",
)

# PEM blocks: the only rule with state that crosses lines.
write(
    "pem-closed",
    b"prefix\n-----BEGIN RSA PRIVATE KEY-----\nbody one\nbody two\n"
    b"-----END RSA PRIVATE KEY-----\nsuffix\n",
)
write(
    "pem-unclosed-to-eof",
    b"prefix\n-----BEGIN OPENSSH PRIVATE KEY-----\nbody one\nbody two\n",
)
write(
    "pem-two-blocks",
    b"a\n-----BEGIN A PRIVATE KEY-----\nx\n-----END A PRIVATE KEY-----\n"
    b"b\n-----BEGIN B PRIVATE KEY-----\ny\n-----END B PRIVATE KEY-----\nc\n",
)
write(
    "pem-malformed-markers",
    b"-----BEGIN PRIVATE KEY-----\nno space between BEGIN and PRIVATE\n"
    b"-----BEGINX RSA PRIVATE KEY-----\n----- BEGIN RSA PRIVATE KEY -----\n"
    b"-----begin rsa private key-----\nbody\n-----END rsa PRIVATE KEY-----\n"
    b"after\n",
)
write(
    "pem-begin-and-end-on-one-line",
    b"-----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----\nafter\n",
)
write(
    "pem-unclosed-then-unterminated",
    b"-----BEGIN RSA PRIVATE KEY-----\nbody with no closing marker or newline",
)

# Dirty bytes. Real /var/log carries all of these.
write("non-ascii", "正常的一行\nパスワード\nsecret 密碼\nkey = 值\n".encode("utf-8"))
write(
    "invalid-utf8",
    b"good\n\xff\xfe truncated multibyte\nsecret \x80\x81\nkey = \xc3\n",
)
write("latin1", b"caf\xe9\nsecret caf\xe9\nkey = caf\xe9\n")
write("crlf", b"line one\r\nsecret\r\nkey = 1\r\n")
write("backslashes-and-formats", b"a\\b\nc\\\\d\n%s %d %%\nsecret\\\n-n\n")
write("leading-whitespace", b"   \tkey = 1\n   secret\n\t\t\n")
write("long-line", b"x" * 200000 + b"\n" + b"secret" + b"y" * 200000 + b"\n")

# A real-lab syslog shape, with the identities and the key material replaced.
write(
    "syslog-shape",
    b"\n".join(
        [
            b"Aug  4 23:37:33 mon-02 ceph-mon[1234]: cluster [INF] osdmap e12345: 7 total, 7 up, 7 in",
            b"Aug  4 23:37:34 mon-02 systemd[1]: Started Ceph cluster monitor daemon.",
            b"Aug  4 23:37:35 mon-02 ceph-osd[999]: 7f0 1 osd.3 pg_epoch: 42 handle_message",
            b"Aug  4 23:37:36 mon-02 kernel: [12345.678901] EXT4-fs (sda1): mounted filesystem",
            b"Aug  4 23:37:37 mon-02 ceph-mgr[321]: auth: could not find secret_id=4242",
            b"Aug  4 23:37:38 mon-02 systemd[1]: Reloading.",
            b"",
        ]
    ),
)
PY
}

# Fixtures with NUL bytes, held to a weaker claim than byte equality. See
# assert_nul_fixture.
build_nul_corpus() {
  local corpus=$1
  mkdir -p "$corpus"
  python3 - "$corpus" <<'PY'
import pathlib
import sys

corpus = pathlib.Path(sys.argv[1])
(corpus / "nul-inside-lines").write_bytes(b"a\x00b\nsecret\x00here\nkey\x00 = 1\n\x00\n")
(corpus / "nul-at-eof").write_bytes(b"plain\nbinary tail\x00\x00")
# A macOS AppleDouble sidecar: binary, no newline anywhere, first byte NUL, and
# carrying a text-ish name that puts it squarely in the redaction target set.
# Extracting a Node Evidence Archive on macOS leaves these (named `._hostname.txt`
# beside `hostname.txt`) among the real artifacts, so this is not a hypothetical
# shape; the fixture drops the leading dot only so a plain glob still sees it.
(corpus / "apple-double-sidecar.txt").write_bytes(
    b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X" + bytes(64) + b"\x00\x00\x00\x02"
)
PY
}

# Does this awk stop a record at a NUL, the way bash's `read` does? The awk this
# repository is developed against does; gawk keeps the NUL and everything after
# it. That byte is the one place the two engines are allowed to disagree, and
# this decides which claim the NUL fixtures can be held to.
awk_truncates_records_at_nul() {
  [[ "$(printf 'a\000b\n' | LC_ALL=C awk '{ print length($0) }')" == 1 ]]
}

redaction_log_count() {
  local log=$1 display=$2 line
  line="$(grep -F "$display: " "$log" | tail -n 1)" ||
    fail "redactions.log has no line for $display: $(cat "$log")"
  [[ "$line" == *" line(s) redacted" ]] ||
    fail "redactions.log did not record a successful redaction for $display: $line"
  line="${line##*: }"
  printf '%s' "${line%% *}"
}

# run_both_engines publishes its results here rather than returning them, since
# a redacted artifact is bytes and shell functions return numbers.
ref_out=''
new_out=''
ref_count=''
new_count=''

# Runs one fixture through both engines, having first checked the properties
# every fixture shares: `redact_file` must succeed, and it must succeed for the
# same reason the oracle does — the record count matching the source's `wc -l`.
# That equality is the one thing the caller cannot see directly and the one #59
# was most likely to break, because `awk` counts an unterminated final line as a
# record and `wc -l` does not.
run_both_engines() {
  local fixture=$1
  local name work rc=0 expected_records ref_records ref_counts new_log
  name="$(basename -- "$fixture")"
  work="$tmpdir/work/$name"
  mkdir -p "$work"

  ref_out="$work/reference.out"
  ref_counts="$work/reference.counts"
  new_out="$work/candidate.out"
  new_log="$work/redactions.log"
  cp -- "$fixture" "$new_out"

  run_reference "$fixture" "$ref_out" "$ref_counts"
  read -r ref_count ref_records <"$ref_counts"

  expected_records="$(wc -l <"$fixture")"
  expected_records="${expected_records//[[:space:]]/}"
  [[ "$ref_records" == "$expected_records" ]] ||
    fail "$name: the oracle read $ref_records of $expected_records record(s); the corpus is wrong, not the engine"

  set +e
  redact_file "$new_out" "$new_log" "$name"
  rc=$?
  set -e
  [[ $rc -eq 0 ]] ||
    fail "$name: redact_file refused to redact a complete file (rc=$rc): $(cat "$new_log")"

  new_count="$(redaction_log_count "$new_log" "$name")"
}

assert_byte_identical() {
  local fixture=$1 name
  name="$(basename -- "$fixture")"
  run_both_engines "$fixture"

  cmp -s "$ref_out" "$new_out" ||
    fail "$name: the awk scan and the reference loop produced different bytes"
  [[ "$new_count" == "$ref_count" ]] ||
    fail "$name: redactions.log says $new_count line(s) redacted, the reference says $ref_count"
}

# NUL is the one byte the two engines are allowed to disagree about, so what a
# NUL fixture can claim depends on the awk in front of it. On an awk that
# truncates at NUL the engines see identical lines and the ordinary byte claim
# holds. On a NUL-clean one the rest of the line comes along, which can only add
# matches — every rule is a substring test — so the claim narrows to the two
# things that still have to be true: the file redacts at all (equal record
# counts, asserted in run_both_engines) and nothing the reference redacted stops
# being redacted.
assert_nul_fixture() {
  local fixture=$1 name
  name="$(basename -- "$fixture")"

  if awk_truncates_records_at_nul; then
    assert_byte_identical "$fixture"
    return
  fi

  run_both_engines "$fixture"
  [[ "$new_count" -ge "$ref_count" ]] ||
    fail "$name: the awk scan redacted $new_count line(s), fewer than the reference's $ref_count"

  local index=0 ref_line new_line
  while IFS= read -r ref_line <&3; do
    index=$((index + 1))
    IFS= read -r new_line <&4 || new_line=''
    [[ "$ref_line" == "[REDACTED]" ]] || continue
    [[ "$new_line" == "[REDACTED]" ]] ||
      fail "$name: line $index is redacted by the reference but not by the awk scan"
  done 3<"$ref_out" 4<"$new_out"
}

test_engines_agree_on_the_corpus() {
  local corpus="$tmpdir/corpus" fixture count=0
  build_corpus "$corpus"
  for fixture in "$corpus"/*; do
    assert_byte_identical "$fixture"
    count=$((count + 1))
  done
  [[ $count -ge 20 ]] || fail "the corpus shrank to $count fixture(s)"
}

test_engines_agree_on_nul_bearing_files() {
  local corpus="$tmpdir/nul-corpus" fixture
  build_nul_corpus "$corpus"
  for fixture in "$corpus"/*; do
    assert_nul_fixture "$fixture"
  done
}

# The net above is only worth its runtime if the corpus can distinguish engines,
# and the quiet way it stops being able to is a fixture set that no longer
# exercises a rule. So each rule is removed from the oracle in turn, and the
# corpus has to notice: an engine missing a rule must disagree with the shipped
# one somewhere. `test_engines_agree_on_the_corpus` runs first and leaves the
# reference output for each fixture behind to compare against.
test_corpus_can_tell_the_rules_apart() {
  local corpus="$tmpdir/rule-check" rule fixture name intact mutated counts diverged
  build_corpus "$corpus"
  for rule in pem keywords key-label base64; do
    diverged=0
    for fixture in "$corpus"/*; do
      name="$(basename -- "$fixture")"
      intact="$tmpdir/rule-check-$name.intact"
      mutated="$tmpdir/rule-check-$name.without-$rule"
      counts="$tmpdir/rule-check-$name.counts"
      run_reference "$fixture" "$intact" "$counts"
      run_reference "$fixture" "$mutated" "$counts" "$rule"
      if ! cmp -s "$intact" "$mutated"; then
        diverged=1
        break
      fi
    done
    [[ $diverged -eq 1 ]] ||
      fail "no fixture depends on the $rule rule: the corpus would not notice it disappearing"
  done
}

# The integrity check exists because a source can stop delivering, and it has to
# catch a stream that stopped in the middle of a line, not only one that stopped
# on a boundary. `awk` cannot see the difference by itself — `NR` counts a half
# record like any other — so if this stops failing, `redact_file` will rewrite an
# artifact with truncated contents and log it as a success. That is the exact
# outcome #49's fail-closed rule was written to prevent.
test_redaction_fails_closed_on_a_stream_cut_mid_line() {
  local work="$tmpdir/mid-line-cut"
  local source_file="$work/evidence.log"
  local fakebin="$work/bin"
  mkdir -p "$work" "$fakebin"

  # Twenty bytes is `first\nsecond\n` and then seven characters of the last
  # line. The cut has to land inside the last line rather than on a boundary:
  # dropping whole records is the easy case, and the count catches it whatever
  # the engine. What only a stream-aware count catches is a last record that
  # arrived incomplete, because it is still one record either way.
  cat >"$fakebin/cat" <<'EOF'
#!/usr/bin/env bash
shift
head -c 20 -- "$1"
EOF
  chmod +x "$fakebin/cat"

  printf 'first\nsecond\nthird line with a secret\n' >"$source_file"
  local before
  before="$(cat "$source_file")"

  local rc=0 saved_path=$PATH
  PATH="$fakebin:$PATH"
  set +e
  redact_file "$source_file" "$work/redactions.log"
  rc=$?
  set -e
  PATH=$saved_path

  [[ $rc -ne 0 ]] || fail "redact_file accepted a stream that stopped mid-line"
  [[ "$(cat "$source_file")" == "$before" ]] ||
    fail "a stream that stopped mid-line replaced the artifact with what did arrive"
  grep -q "NOT redacted" "$work/redactions.log" ||
    fail "redactions.log does not record the incomplete read: $(cat "$work/redactions.log")"
}

# The one place the engines genuinely part company, asserted rather than argued.
# `LC_ALL=C` makes the rules byte tests, which is what keeps a multibyte-aware
# awk from aborting on the invalid UTF-8 every real /var/log carries — and it
# moves two kinds of line, in both directions. The equivalence corpus cannot see
# this: it runs the oracle in C too, on purpose, so that it compares engines
# rather than locales.
test_the_c_locale_pin_is_pinned() {
  local work="$tmpdir/locale-pin"
  local source_file="$work/lines.txt"
  mkdir -p "$work"

  python3 - "$source_file" <<'PY'
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_bytes(
    # Non-alphanumeric before `key` is a byte question now, so the CJK character
    # in front of this one no longer shields it.
    "密key = 1\n".encode("utf-8")
    # A line bash's `[[ =~ ]]` could not match at all in a UTF-8 locale, because
    # the bytes are not a valid character in one.
    + b"secret \x80\x81\n"
    # And the other direction: an em space is whitespace to a UTF-8 locale but
    # three ordinary bytes to a byte one, so `private<em space>key` stops
    # matching rule 2. The Python candidate's `[\t\v\f\r _-]` agrees with C.
    + "private key\n".encode("utf-8")
    + b"ordinary line\n"
)
PY

  redact_file "$source_file" "$work/redactions.log"

  [[ "$(sed -n '1p' "$source_file")" == "[REDACTED]" ]] ||
    fail "a non-ASCII character before 'key =' still shields it from rule 3"
  [[ "$(sed -n '2p' "$source_file")" == "[REDACTED]" ]] ||
    fail "a line with invalid UTF-8 was not matched against the rules"
  [[ "$(sed -n '3p' "$source_file")" != "[REDACTED]" ]] ||
    fail "an em space now counts as whitespace: the scan is not byte-oriented"
  [[ "$(sed -n '4p' "$source_file")" == "ordinary line" ]] ||
    fail "an ordinary line was over-redacted"
}

# `awk` is not optional and there is no slower path to fall back to: the loop it
# replaced needed 12.8 hours for the evidence one real-lab collect produced,
# which is past the qualification harness's own collect timeout (#59). A quiet
# fallback would bring that failure mode back without a message. Same answer as
# ADR 0011 gives for a missing `timeout` binary — say what is missing, and stop.
test_redaction_fails_without_awk() {
  local source_file="$tmpdir/no-awk.txt"
  printf 'secret\n' >"$source_file"

  # PATH is emptied inside the child rather than in front of it, so that finding
  # `bash` is not the thing being tested.
  local output rc=0
  set +e
  output="$(
    bash -c '
      set -euo pipefail
      PATH=$4
      # shellcheck disable=SC1091
      source "$1/lib/common.sh"
      redact_file "$2" "$3"
    ' bash "$ROOT" "$source_file" "$tmpdir/no-awk.log" "$tmpdir/empty-path" 2>&1
  )"
  rc=$?
  set -e

  [[ $rc -ne 0 ]] || fail "redact_file redacted without an awk"
  [[ "$output" == *awk* ]] || fail "redact_file did not say awk was missing: $output"
  [[ "$(cat "$source_file")" == "secret" ]] ||
    fail "redact_file rewrote the artifact before checking for awk"
}

# `{38,}` is the only interval expression in the rules, and it is the one that
# catches ceph key material. A pre-POSIX awk (mawk 1.3.3) reads it as literal
# text and matches nothing, so the base64 rule would go quiet without failing.
# Under-redacting silently is the one outcome this repository cannot ship.
test_redaction_fails_without_interval_expressions() {
  local fakebin="$tmpdir/no-interval-bin"
  mkdir -p "$fakebin"
  cat >"$fakebin/awk" <<'EOF'
#!/usr/bin/env bash
# An awk that reads `{38,}` as literal text matches nothing the probe expects.
exit 0
EOF
  chmod +x "$fakebin/awk"

  local source_file="$tmpdir/no-interval.txt"
  printf 'secret\n' >"$source_file"

  local output rc=0
  set +e
  output="$(
    bash -c '
      set -euo pipefail
      PATH=$4
      # shellcheck disable=SC1091
      source "$1/lib/common.sh"
      redact_file "$2" "$3"
    ' bash "$ROOT" "$source_file" "$tmpdir/no-interval.log" "$fakebin:$PATH" 2>&1
  )"
  rc=$?
  set -e

  [[ $rc -ne 0 ]] || fail "redact_file accepted an awk without interval expressions"
  [[ "$output" == *interval* ]] ||
    fail "redact_file did not name interval expressions as the problem: $output"
  [[ "$(cat "$source_file")" == "secret" ]] ||
    fail "redact_file rewrote the artifact using an awk it had rejected"
}

mkdir -p "$tmpdir/empty-path"

test_engines_agree_on_the_corpus
test_engines_agree_on_nul_bearing_files
test_corpus_can_tell_the_rules_apart
test_the_c_locale_pin_is_pinned
test_redaction_fails_closed_on_a_stream_cut_mid_line
test_redaction_fails_without_awk
test_redaction_fails_without_interval_expressions

printf 'ok: redaction engine equivalence\n'
