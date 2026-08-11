#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_JOBS_REQUEST="${1:-auto}"

probe_runtime() {
  local role=$1
  local variable=$2
  local selection=$3
  local resolved_variable=$4
  local resolved output status

  if [[ -z "$selection" ]]; then
    printf 'FAIL: %s is required for the %s interpreter\n' "$variable" "$role" >&2
    return 1
  fi
  if [[ "$selection" != /* ]]; then
    printf 'FAIL: %s interpreter must be selected by absolute path: %s\n' \
      "$role" "$selection" >&2
    return 1
  fi
  if [[ ! -f "$selection" || ! -x "$selection" ]]; then
    printf 'FAIL: %s interpreter is missing or not executable: %s\n' \
      "$role" "$selection" >&2
    return 1
  fi

  output="$("$selection" -c '
import os
import sys

role = sys.argv[1]
version = tuple(sys.version_info[:5])
print(f"{role} interpreter:")
# Keep the selected virtual environment executable path intact. Resolving the
# symlink with realpath would silently replace the isolated environment with
# its base interpreter when the gates run.
print(f"  executable: {os.path.abspath(sys.executable)}")
print(f"  implementation: {sys.implementation.name}")
print(
    "  version: "
    f"major={version[0]} minor={version[1]} micro={version[2]} "
    f"releaselevel={version[3]} serial={version[4]}"
)
if version[:2] < (3, 11):
    sys.stderr.write(
        f"FAIL: {role} interpreter requires Python 3.11 or newer; "
        f"got {sys.implementation.name} {version[0]}.{version[1]}.{version[2]}\n"
    )
    raise SystemExit(1)
if role == "production" and sys.implementation.name != "cpython":
    sys.stderr.write(
        "FAIL: production interpreter must be CPython for compatibility proof; "
        f"got {sys.implementation.name}\n"
    )
    raise SystemExit(1)
print("__CEPH_INCIDENT_RUNTIME_OK__")
' "$role" 2>&1)"
  status=$?
  if [[ $status -ne 0 ]]; then
    printf '%s\n' "$output"
    return "$status"
  fi
  if [[ "$output" != *$'\n__CEPH_INCIDENT_RUNTIME_OK__' ]]; then
    printf 'FAIL: %s interpreter did not report a valid Python runtime identity\n' \
      "$role" >&2
    return 1
  fi
  output="${output%$'\n__CEPH_INCIDENT_RUNTIME_OK__'}"
  printf '%s\n' "$output"
  resolved="$(printf '%s\n' "$output" | sed -n 's/^  executable: //p')"
  if [[ "$resolved" != /* || ! -f "$resolved" || ! -x "$resolved" ]]; then
    printf 'FAIL: %s interpreter reported an unusable executable: %s\n' \
      "$role" "$resolved" >&2
    return 1
  fi
  printf -v "$resolved_variable" '%s' "$resolved"
}

PRODUCTION_PYTHON_RESOLVED=""
TOOLING_PYTHON_RESOLVED=""
preflight_failed=0
probe_runtime \
  production PRODUCTION_PYTHON "${PRODUCTION_PYTHON:-}" \
  PRODUCTION_PYTHON_RESOLVED || preflight_failed=1
probe_runtime \
  tooling TOOLING_PYTHON "${TOOLING_PYTHON:-}" \
  TOOLING_PYTHON_RESOLVED || preflight_failed=1
[[ $preflight_failed -eq 0 ]] || exit 1

runtime_shims="$(mktemp -d "${TMPDIR:-/tmp}/ceph-incident-runtime-shims.XXXXXX")" ||
  exit 1
production_shim="$runtime_shims/production"
tooling_shim="$runtime_shims/tooling"
cleanup_runtime_shims() {
  rm -f "$production_shim/python3" "$tooling_shim/python3" 2>/dev/null || true
  rmdir "$production_shim" "$tooling_shim" "$runtime_shims" 2>/dev/null || true
}
trap cleanup_runtime_shims EXIT
mkdir "$production_shim" "$tooling_shim" || exit 1
ln -s "$PRODUCTION_PYTHON_RESOLVED" "$production_shim/python3" || exit 1
ln -s "$TOOLING_PYTHON_RESOLVED" "$tooling_shim/python3" || exit 1

printf '\nproduction test gate:\n'
PATH="$production_shim:$PATH" \
  PYTHON="$PRODUCTION_PYTHON_RESOLVED" TEST_SCOPE=production \
  bash "$HERE/run-python-tests.sh" "$TEST_JOBS_REQUEST" || exit $?

printf '\ncomplete suite gate:\n'
PATH="$tooling_shim:$PATH" \
  PYTHON="$TOOLING_PYTHON_RESOLVED" TEST_SCOPE=complete \
  bash "$HERE/run-python-tests.sh" "$TEST_JOBS_REQUEST"
