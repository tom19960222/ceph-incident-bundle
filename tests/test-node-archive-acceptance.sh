#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

fakebin='' ssh_key='' inventory=''
# shellcheck disable=SC1091
source "$ROOT/tests/fixtures/shell-collect-environment.sh"
setup_shell_collect_environment "$ROOT" "$tmpdir"

# Each archive begins with a valid hostname payload. Rejection must still happen
# before extraction, including when the unsafe member occurs later in the table.
archive_cases=(
  absolute empty traversal symlink hardlink device fifo socket collision
  oversize truncated missing-manifest
)
adversarial_inventory="$tmpdir/adversarial-inventory.env"
{
  printf 'SSH_USER="tester"\nHOSTS=(\n'
  for archive_case in "${archive_cases[@]}"; do
    printf '  "%s=10.0.0.1"\n' "$archive_case"
  done
  printf ')\n'
} >"$adversarial_inventory"
archive_mappings=''
for archive_case in "${archive_cases[@]}"; do
  archive_mappings+=" $archive_case:$archive_case"
done
outside_marker="$tmpdir/outside-marker"
printf 'must remain unchanged\n' >"$outside_marker"
adversarial_out="$tmpdir/out-adversarial"
set +e
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_SSH_NODE_ARCHIVE_CASES="$archive_mappings" \
  FAKE_SSH_ARCHIVE_OUTSIDE_PATH="$outside_marker" \
  CEPH_INCIDENT_TEST_NODE_ARCHIVE_MAX_BYTES=65536 \
  PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
    --inventory "$adversarial_inventory" --ssh-key "$ssh_key" \
    --seed tester@10.0.0.1 --mode cephadm --out "$adversarial_out" \
    --since 24h --timeout 5 >/dev/null 2>&1
adversarial_status=$?
set -e
[[ "$adversarial_status" == "2" ]] \
  || fail "adversarial archives should be partial (2), got $adversarial_status"
adversarial_bundle="$(find "$adversarial_out" -maxdepth 1 -name 'ceph-incident-*.tar.gz' -print -quit)"
[[ -n "$adversarial_bundle" ]] || fail "adversarial archives should produce a partial bundle"
adversarial_members="$(tar -tzf "$adversarial_bundle" | sed 's#^\./##')"
for archive_case in "${archive_cases[@]}"; do
  node_members="$(grep "^nodes/$archive_case/" <<<"$adversarial_members" | grep -v '/$' || true)"
  [[ "$node_members" == "nodes/$archive_case/SKIPPED.txt" ]] \
    || fail "$archive_case archive wrote unaccepted members: $node_members"
done
[[ "$(cat "$outside_marker")" == "must remain unchanged" ]] \
  || fail "absolute archive changed a file outside the collector workspace"

# A complete archive is evidence even when the remote collector itself reports
# partial. The remote status remains the Collect status; evidence is not dropped.
partial_out="$tmpdir/out-valid-partial"
set +e
partial_output="$(FAKE_CEPH_TARGETS="10.0.0.1" FAKE_SSH_NODE_ARCHIVE_CASE=valid \
  FAKE_SSH_FAIL_ALIAS=cephnode PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
    --inventory "$inventory" --ssh-key "$ssh_key" \
    --seed tester@10.0.0.1 --mode cephadm --out "$partial_out" \
    --since 24h --timeout 5 2>&1)"
partial_status=$?
set -e
[[ "$partial_status" == "2" ]] || fail "valid remote partial should exit 2, got $partial_status"
partial_bundle="$(find "$partial_out" -maxdepth 1 -name 'ceph-incident-*.tar.gz' -print -quit)"
[[ -n "$partial_bundle" ]] || fail "valid remote partial should retain a bundle"
partial_members="$(tar -tzf "$partial_bundle" | sed 's#^\./##')"
grep -qx 'nodes/cephnode/system/hostname.txt' <<<"$partial_members" \
  || fail "valid remote partial dropped node evidence: $partial_members; output: $partial_output"

printf 'ok: node archive pre-extraction acceptance\n'
