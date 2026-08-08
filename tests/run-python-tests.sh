#!/usr/bin/env bash
set -euo pipefail

# Runs the Python unit tests, sharded one process per test module.
#
# The suite is ~550 small tests whose cost is dominated by per-test fake-world
# setup, so sharding by module cuts the wall time to roughly the largest single
# module -- one module is the critical path, not the job count.  Sharding by
# module (not by test) also keeps each shard's output a self-contained unittest
# run, so a failure can be reported verbatim instead of interleaved.
#
# `TEST_JOBS=1` runs the plain serial `unittest discover` this replaced.  Keep
# that path working: an isolation bug that only appears under sharding is not
# debuggable without a serial run to compare against.
#
# A shard's recorded exit status is the authority on whether it passed: each
# shard writes its interpreter's exit code to a per-module status file as its
# last act, and only a recorded 0 counts as a pass.  A shard that dies before
# recording (crash, OOM, external kill, a disk too full to write one byte)
# leaves no status and is reported as DIED -- it cannot look like a pass,
# whatever its log says.  The `Ran N tests` line is scraped for reporting only.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-python3}"
# The shards re-read PYTHON; exported so the interpreter that passed the 3.11
# gate reaches them by the script's own doing, not the caller's spelling.
export PYTHON
PATTERN='test_python_*.py'

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# Internal shard entry point: re-invoked by xargs, one call per module file.
# The sentinel is exported only by the xargs line below, so a stray TEST_JOBS
# value of `--run-one` cannot steer the public entry point in here.  Each shard
# runs the same `unittest discover` the serial path runs, narrowed to one
# module's filename, so a module keeps the same import identity under sharding
# as under `TEST_JOBS=1` -- an isolation bug can be compared across the two
# runs without the module changing its name.  Writing the status file last
# means a shard that dies mid-run leaves no record and is judged fail-closed.
if [[ "${1-}" == "--run-one" && "${RUN_PYTHON_TESTS_SHARD:-}" == "1" ]]; then
  [[ $# -eq 3 ]] || fail "--run-one needs a module filename and a log directory"
  module_file=$2
  logdir=$3
  module="${module_file%.py}"
  cd "$ROOT"
  rc=0
  # `-p` is an fnmatch pattern, so a filename containing `*`, `?` or `[` could
  # match nothing — and on Python 3.11, the gate's floor, "Ran 0 tests" exits 0.
  # The list feeding this entry point comes from `find`, so a plain filename
  # always matches itself; refuse the metacharacters instead of risking a shard
  # that silently tests nothing.
  [[ "$module_file" != *[\*\?\[]* ]] ||
    fail "test module filename contains fnmatch metacharacters: $module_file"
  "$PYTHON" -m unittest discover -s "$ROOT/tests" -p "$module_file" -v \
    >"$logdir/$module.log" 2>&1 || rc=$?
  printf '%s\n' "$rc" >"$logdir/$module.status"
  exit "$rc"
fi

# Public entry point from here on.  An inherited sentinel is dropped so only the
# xargs line below can open the shard entry for its own children.
unset RUN_PYTHON_TESTS_SHARD

# Physical CPUs only: a cgroup CPU quota (a limited CI container) is not
# consulted, so an over-subscribed environment should pass TEST_JOBS explicitly.
detect_jobs() {
  local detected
  if detected="$(sysctl -n hw.ncpu 2>/dev/null)" && [[ "$detected" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$detected"
    return
  fi
  if detected="$(nproc 2>/dev/null)" && [[ "$detected" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$detected"
    return
  fi
  printf '4\n'
}

jobs_request="${1:-auto}"
if [[ "$jobs_request" == "auto" ]]; then
  jobs="$(detect_jobs)"
else
  [[ "$jobs_request" =~ ^[0-9]+$ && "$jobs_request" -ge 1 ]] ||
    fail "TEST_JOBS must be a positive integer or 'auto', got: $jobs_request"
  jobs="$jobs_request"
fi

if [[ "$jobs" -eq 1 ]]; then
  cd "$ROOT"
  exec "$PYTHON" -m unittest discover -s "$ROOT/tests" -p "$PATTERN" -v
fi

module_files=()
while IFS= read -r path; do
  module_files+=("$(basename "$path")")
done < <(find "$ROOT/tests" -maxdepth 1 -name "$PATTERN" | sort)

[[ ${#module_files[@]} -gt 0 ]] || fail "no test modules matched $PATTERN"

# The shard list above deliberately stops at the top level, while serial
# discovery also descends into package directories. A matching file anywhere
# deeper is refused outright: either
# the two paths would run different suites, or the file sits in a non-package
# directory where neither path would run it — both are layouts to reject
# loudly, not to paper over.
stray="$(find "$ROOT/tests" -mindepth 2 -name "$PATTERN" | head -n 1)"
[[ -z "$stray" ]] || fail "a test module below tests/ top level would never be sharded: $stray"

logdir="$(mktemp -d "${TMPDIR:-/tmp}/ceph-incident-python-tests.XXXXXX")"
trap 'rm -rf "$logdir"' EXIT

started="$SECONDS"
printf 'running %s test modules across %s jobs\n' "${#module_files[@]}" "$jobs"

# xargs exits nonzero when any shard does; the recorded statuses below are what
# actually decide the outcome, so don't let `set -e` cut reporting short.
printf '%s\n' "${module_files[@]}" |
  RUN_PYTHON_TESTS_SHARD=1 xargs -P "$jobs" -I {} bash "$SELF" --run-one {} "$logdir" || true

# Collect every verdict before reporting anything: the EXIT trap removes the
# logs, so bailing out mid-loop would destroy the evidence for the failure being
# reported *and* for every module after it.
total=0
failed=()
for module_file in "${module_files[@]}"; do
  module="${module_file%.py}"
  log="$logdir/$module.log"
  ran=""
  [[ -f "$log" ]] && ran="$(sed -n 's/^Ran \([0-9]*\) test.*/\1/p' "$log" | tail -1)"
  summary="${ran:+$ran tests}"
  summary="${summary:-no unittest summary}"
  status=""
  [[ -f "$logdir/$module.status" ]] && status="$(<"$logdir/$module.status")"

  if [[ "$status" == "0" ]]; then
    total=$((total + ${ran:-0}))
    printf '  ok   %-44s %s\n' "$module" "$summary"
  elif [[ -z "$status" ]]; then
    failed+=("$module")
    printf '  DIED %-44s shard exited without recording a status\n' "$module"
  else
    failed+=("$module")
    printf '  FAIL %-44s exit %s, %s\n' "$module" "$status" "$summary"
  fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
  for module in "${failed[@]}"; do
    printf '\n===== %s =====\n' "$module" >&2
    if [[ -f "$logdir/$module.log" ]]; then
      cat "$logdir/$module.log" >&2
    else
      printf '(shard produced no output at all)\n' >&2
    fi
  done
  printf '\n%s of %s modules failed: %s\n' \
    "${#failed[@]}" "${#module_files[@]}" "${failed[*]}" >&2
  exit 1
fi

printf 'OK — %s tests in %s modules, %ss\n' "$total" "${#module_files[@]}" "$((SECONDS - started))"
