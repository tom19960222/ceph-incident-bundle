#!/usr/bin/env bash
set -euo pipefail

# Invoked by the Python compatibility suite; make validate includes test-python.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 1 && "$1" == /* ]] || fail "Usage: export-shell-collect-fixture.sh /absolute/output-dir"
fixture_dir=$1
[[ ! -e "$fixture_dir" ]] || fail "fixture output already exists: $fixture_dir"

tmpdir="$(mktemp -d)"
fixture_created=0
cleanup() {
  local status=$?
  trap - EXIT
  rm -rf "$tmpdir"
  if [[ $status -ne 0 && $fixture_created -eq 1 ]]; then
    rm -rf "$fixture_dir"
  fi
  exit "$status"
}
trap cleanup EXIT

fakebin='' ssh_key='' inventory=''
# shellcheck disable=SC1091
source "$ROOT/tests/fixtures/shell-collect-environment.sh"
setup_shell_collect_environment "$ROOT" "$tmpdir"

unset CEPH_INCIDENT_ALLOW_CEPHADM_SHELL CEPH_INCIDENT_ALLOW_KUBECTL_EXEC
unset FAKE_SSH_PEM_ALIAS FAKE_SSH_FAIL_ALIAS FAKE_SSH_NO_MANIFEST_ALIAS
unset FAKE_SSH_BAD_TAR_ALIAS FAKE_KUBE_NS_MISSING FAKE_PROBE_FAIL_TARGETS
unset FAKE_SSH_CONNECT_FAIL_TARGETS FAKE_SSH_SLEEP
unset FAKE_CEPH_SUDO_OK FAKE_CEPHADM_OK FAKE_KUBE_TOOLS_POD

out="$tmpdir/out"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_CEPH_BIN_TARGETS="10.0.0.1" \
FAKE_CEPH_DIRECT_OK="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
FAKE_KUBE_TOOLS_POD=1 \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --kube-context lab --out "$out" --since 24h --timeout 5 \
  --keep-workdir

bundle="$(find "$out" -maxdepth 1 -type f -name 'ceph-incident-*.tar.gz' -print -quit)"
workdir="$(find "$out" -maxdepth 1 -type d -name 'tmp.*' -print -quit)"
[[ -n "$bundle" && -n "$workdir" ]] || fail "shell Collect did not retain both fixture targets"

for required in \
  cluster/ceph/json/status.json \
  cluster/rook/pods-wide.txt \
  nodes/cephnode/system/hostname.txt; do
  [[ -f "$workdir/$required" ]] || fail "retained workdir missing real evidence: $required"
  tar -tzf "$bundle" | sed 's#^\./##' | grep -qxF "$required" \
    || fail "archive missing real evidence: $required"
done

if grep -qF 'cephadm shell' "$FAKE_SSH_LOG"; then
  fail "default-off fixture used cephadm shell"
fi
if grep -qF 'kubectl exec' "$FAKE_SSH_LOG"; then
  fail "default-off fixture used kubectl exec"
fi

mkdir "$fixture_dir"
fixture_created=1
cp -R "$workdir" "$fixture_dir/workdir"
cp "$bundle" "$fixture_dir/bundle.tar.gz"
printf 'ok: exported default-off shell Collect fixture\n'
