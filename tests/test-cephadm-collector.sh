#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

fakebin="$tmpdir/fakebin"
mkdir -p "$fakebin"
cat >"$fakebin/python3" <<'EOF'
#!/usr/bin/env bash
printf 'python3 should not be used by cephadm collector\n' >&2
exit 99
EOF
chmod +x "$fakebin/python3"
PATH="$fakebin:$ROOT/tests/fixtures/bin:$PATH"

# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/collect-cluster-cephadm.sh"

make_fake_ssh() {
  local log_file=$1
  local fail_on=${2:-}
  local crash_ls_broken=${3:-0}

  export FAKE_SSH_LOG="$log_file"
  export FAKE_SSH_FAIL_ON="$fail_on"
  export FAKE_SSH_CRASH_LS_BROKEN="$crash_ls_broken"
}

assert_file_contains() {
  local file=$1 expected=$2
  [[ -f "$file" ]] || fail "missing file: $file"
  grep -qF "$expected" "$file" || fail "expected '$expected' in $file"
}

read_manifest_count() {
  local manifest=$1
  [[ -f "$manifest" ]] || fail "missing manifest: $manifest"
  wc -l <"$manifest" | tr -d ' '
}

test_collect_cluster_cephadm_happy_path_and_limit_recent_crashes() {
  local outdir="$tmpdir/out-happy"
  local manifest="$tmpdir/manifest-happy.jsonl"
  local ssh_log="$tmpdir/ssh-happy.log"
  mkdir -p "$outdir"
  touch "$tmpdir/id_ed25519"

  make_fake_ssh "$ssh_log"
  collect_cluster_cephadm "$outdir" "$manifest" "monitor01@example.invalid" "$tmpdir/id_ed25519" "7 days ago" "30"

  [[ -f "$outdir/cluster/ceph/json/status.json" ]] || fail "missing status json artifact"
  [[ -f "$outdir/cluster/ceph/text/status.txt" ]] || fail "missing status text artifact"
  [[ -f "$outdir/cluster/ceph/text/health-detail.txt" ]] || fail "missing health detail text artifact"
  [[ -f "$outdir/cluster/ceph/text/osd-tree.txt" ]] || fail "missing osd tree text artifact"
  [[ -f "$outdir/cluster/ceph/text/orch-ps.txt" ]] || fail "missing orch ps text artifact"
  [[ -f "$outdir/cluster/ceph/json/crash-ls.json" ]] || fail "missing crash ls artifact"
  [[ -f "$outdir/cluster/ceph/json/crash-info/crash-01.json" ]] || fail "missing first crash info artifact"
  [[ -f "$outdir/cluster/ceph/json/crash-info/crash_02.json" ]] || fail "missing sanitized crash info artifact"
  [[ -f "$outdir/cluster/ceph/json/crash-info/crash_02-2.json" ]] || fail "missing collision-safe crash info artifact"
  [[ -f "$outdir/cluster/ceph/json/crash-info/crash-10.json" ]] || fail "missing tenth crash info artifact"
  [[ ! -f "$outdir/cluster/ceph/json/crash-info/crash-11.json" ]] || fail "collector did not cap crash info at 10"

  assert_file_contains "$outdir/cluster/ceph/text/status.txt" "cluster is healthy"
  assert_file_contains "$outdir/cluster/ceph/text/health-detail.txt" "HEALTH_OK"
  assert_file_contains "$outdir/cluster/ceph/text/orch-ps.txt" "NAME HOST STATUS"
  assert_file_contains "$outdir/cluster/ceph/json/status.json" "\"health\":\"HEALTH_OK\""
  assert_file_contains "$outdir/cluster/ceph/json/crash-info/crash-01.json" "\"crash_id\":\"crash-01\""
  assert_file_contains "$outdir/cluster/ceph/json/crash-info/crash_02.json" "\"crash_id\":\"crash/02\""
  assert_file_contains "$outdir/cluster/ceph/json/crash-info/crash_02-2.json" "\"crash_id\":\"crash:02\""

  [[ "$(read_manifest_count "$manifest")" == "34" ]] || fail "expected 34 manifest entries"
  grep -qF 'sudo -n cephadm shell -- ceph status --format json-pretty' "$ssh_log" || fail "ssh log missing cephadm shell invocation"
  grep -qF 'sudo -n cephadm shell -- ceph status' "$ssh_log" || fail "ssh log missing text status invocation"
  grep -qF 'sudo -n cephadm shell -- ceph crash info crash-10' "$ssh_log" || fail "ssh log missing 10th crash info invocation"
  # R1: cluster SSH must carry connect/keepalive bounds (was missing -> could hang on a half-open seed)
  grep -qF 'ConnectTimeout=30' "$ssh_log" || fail "cluster ssh missing ConnectTimeout"
  grep -qF 'ServerAliveInterval=30' "$ssh_log" || fail "cluster ssh missing ServerAliveInterval"
}

test_collect_cluster_cephadm_returns_partial_failure_and_keeps_collecting() {
  local outdir="$tmpdir/out-partial"
  local manifest="$tmpdir/manifest-partial.jsonl"
  local ssh_log="$tmpdir/ssh-partial.log"
  local rc=0
  mkdir -p "$outdir"
  touch "$tmpdir/id_ed25519"

  make_fake_ssh "$ssh_log" "osd perf"
  set +e
  collect_cluster_cephadm "$outdir" "$manifest" "monitor01@example.invalid" "$tmpdir/id_ed25519" "7 days ago" "30"
  rc=$?
  set -e

  [[ "$rc" == "2" ]] || fail "expected partial failure exit 2, got $rc"
  [[ -f "$outdir/cluster/ceph/json/osd-perf.json" ]] || fail "missing failed osd perf artifact"
  [[ -f "$outdir/cluster/ceph/json/pg-stat.json" ]] || fail "collector stopped before later artifacts"
  assert_file_contains "$outdir/cluster/ceph/json/osd-perf.json" "simulated failure"
  grep -qF 'ceph pg stat --format json-pretty' "$ssh_log" || fail "collector did not continue after failure"
  grep -qF 'exit_code":17' "$manifest" || fail "manifest did not record the failing command"
}

test_collect_cluster_cephadm_records_skip_text_when_crash_list_is_invalid() {
  local outdir="$tmpdir/out-broken"
  local manifest="$tmpdir/manifest-broken.jsonl"
  local ssh_log="$tmpdir/ssh-broken.log"
  mkdir -p "$outdir"
  touch "$tmpdir/id_ed25519"

  make_fake_ssh "$ssh_log" "" 1
  collect_cluster_cephadm "$outdir" "$manifest" "monitor01@example.invalid" "$tmpdir/id_ed25519" "7 days ago" "30"

  [[ -f "$outdir/cluster/ceph/text/crash-info-skip.txt" ]] || fail "missing crash parse skip artifact"
  assert_file_contains "$outdir/cluster/ceph/text/crash-info-skip.txt" "SKIPPED"
  [[ ! -d "$outdir/cluster/ceph/json/crash-info" ]] || fail "collector should not create crash info artifacts when parsing fails"
  if grep -qF 'crash info' "$ssh_log"; then
    fail "collector tried to inspect crash ids after parse failure"
  fi
}

test_collect_cluster_cephadm_runner_direct() {
  local outdir="$tmpdir/out-direct"
  local manifest="$tmpdir/manifest-direct.jsonl"
  local ssh_log="$tmpdir/ssh-direct.log"
  mkdir -p "$outdir"
  touch "$tmpdir/id_ed25519"

  make_fake_ssh "$ssh_log"
  collect_cluster_cephadm "$outdir" "$manifest" "monitor01@example.invalid" "$tmpdir/id_ed25519" "7 days ago" "30" "direct"

  [[ -f "$outdir/cluster/ceph/json/status.json" ]] || fail "direct runner produced no status artifact"
  grep -qF 'monitor01@example.invalid ceph status --format json-pretty' "$ssh_log" || fail "direct runner should run plain 'ceph', got: $(grep -F 'ceph status --format json-pretty' "$ssh_log" | head -1)"
  if grep -qF 'cephadm shell' "$ssh_log"; then
    fail "direct runner must NOT use cephadm shell"
  fi
  if grep -qF 'sudo -n ceph' "$ssh_log"; then
    fail "direct runner must NOT use sudo"
  fi
}

test_collect_cluster_cephadm_runner_sudo() {
  local outdir="$tmpdir/out-sudo"
  local manifest="$tmpdir/manifest-sudo.jsonl"
  local ssh_log="$tmpdir/ssh-sudo.log"
  mkdir -p "$outdir"
  touch "$tmpdir/id_ed25519"

  make_fake_ssh "$ssh_log"
  collect_cluster_cephadm "$outdir" "$manifest" "monitor01@example.invalid" "$tmpdir/id_ed25519" "7 days ago" "30" "sudo"

  grep -qF 'monitor01@example.invalid sudo -n ceph status --format json-pretty' "$ssh_log" || fail "sudo runner should run 'sudo -n ceph'"
  if grep -qF 'cephadm shell' "$ssh_log"; then
    fail "sudo runner must NOT use cephadm shell"
  fi
}

test_collect_cluster_cephadm_happy_path_and_limit_recent_crashes
test_collect_cluster_cephadm_returns_partial_failure_and_keeps_collecting
test_collect_cluster_cephadm_records_skip_text_when_crash_list_is_invalid
test_collect_cluster_cephadm_runner_direct
test_collect_cluster_cephadm_runner_sudo
