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
# A shard's exit status is the authority on whether it passed.  The `Ran N
# tests` line is scraped for reporting only, and a shard that never printed one
# counts as failed -- an interpreter that dies before unittest summarises
# (crash, OOM, external kill) must not be able to look like a pass.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PATTERN='test_python_*.py'

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# Internal shard entry point: re-invoked by xargs, one call per module.  A
# failure is recorded as a marker file rather than appended to a shared log, so
# concurrent shards never contend for the same file.  `tests.<module>` only
# resolves from the repository root, which is why this re-anchors rather than
# inheriting the caller's directory.
if [[ "${1-}" == "--run-one" ]]; then
  [[ $# -eq 3 ]] || fail "--run-one needs a module and a log directory"
  module=$2
  logdir=$3
  cd "$ROOT"
  if "$PYTHON" -m unittest "$module" -v >"$logdir/$module.log" 2>&1; then
    exit 0
  fi
  : >"$logdir/$module.failed"
  exit 1
fi

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

modules=()
while IFS= read -r path; do
  modules+=("tests.$(basename "$path" .py)")
done < <(find "$ROOT/tests" -maxdepth 1 -name "$PATTERN" | sort)

[[ ${#modules[@]} -gt 0 ]] || fail "no test modules matched $PATTERN"

logdir="$(mktemp -d "${TMPDIR:-/tmp}/ceph-incident-python-tests.XXXXXX")"
trap 'rm -rf "$logdir"' EXIT

started="$SECONDS"
printf 'running %s test modules across %s jobs\n' "${#modules[@]}" "$jobs"

# xargs exits nonzero when any shard does; the per-module verdicts below are
# what actually decide the outcome, so don't let `set -e` cut reporting short.
printf '%s\n' "${modules[@]}" |
  xargs -P "$jobs" -I {} bash "$SELF" --run-one {} "$logdir" || true

# Collect every verdict before reporting anything: the EXIT trap removes the
# logs, so bailing out mid-loop would destroy the evidence for the failure being
# reported *and* for every module after it.
total=0
failed=()
for module in "${modules[@]}"; do
  log="$logdir/$module.log"
  ran=""
  [[ -f "$log" ]] && ran="$(sed -n 's/^Ran \([0-9]*\) test.*/\1/p' "$log" | tail -1)"

  if [[ -f "$logdir/$module.failed" ]]; then
    failed+=("$module")
    printf '  FAIL %-44s %s\n' "$module" "${ran:+$ran tests}${ran:-no unittest summary}"
  elif [[ -z "$ran" ]]; then
    failed+=("$module")
    printf '  DIED %-44s shard exited without a unittest summary\n' "$module"
  else
    total=$((total + ran))
    printf '  ok   %-44s %s tests\n' "$module" "$ran"
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
    "${#failed[@]}" "${#modules[@]}" "${failed[*]}" >&2
  exit 1
fi

printf 'OK — %s tests in %s modules, %ss\n' "$total" "${#modules[@]}" "$((SECONDS - started))"
