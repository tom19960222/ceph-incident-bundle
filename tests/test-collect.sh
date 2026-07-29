#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CEPH_INCIDENT_ALLOW_CEPHADM_SHELL=1
export CEPH_INCIDENT_ALLOW_KUBECTL_EXEC=1

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

run_and_capture() {
  local output status
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  printf '%s\n%s' "$status" "$output"
}

find_bundle() {
  local outdir=$1 bundle
  bundle="$(find "$outdir" -maxdepth 1 -name 'ceph-incident-*.tar.gz' -print -quit)"
  [[ -n "$bundle" ]] || fail "missing generated bundle in $outdir"
  printf '%s' "$bundle"
}

assert_archive_contains() {
  local bundle=$1 expected=$2
  tar -tzf "$bundle" | sed 's#^\./##' | grep -qF "$expected" || fail "archive missing $expected"
}

assert_archive_file_contains() {
  local bundle=$1 path=$2 expected=$3
  tar -xOzf "$bundle" "./$path" 2>/dev/null | grep -qF "$expected" || fail "archive file $path missing $expected"
}

assert_archive_has_debug_log_for() {
  local bundle=$1 target=$2 expected=${3-} member content matched=0
  while IFS= read -r member; do
    content="$(tar -xOzf "$bundle" "./$member" 2>/dev/null)"
    [[ "$content" == *"$target"* ]] || continue
    [[ "$content" == *"debug1:"* ]] || continue
    [[ -z "$expected" || "$content" == *"$expected"* ]] || continue
    matched=1
    break
  done < <(tar -tzf "$bundle" | sed 's#^\./##' | grep '^ssh-debug/.*[.]log$')
  [[ "$matched" == "1" ]] || fail "archive missing ssh-debug log for $target"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
results_before="$(find "$ROOT/results" -mindepth 1 -print 2>/dev/null | sort || true)"

# ---------------------------------------------------------------------------
# usage / arg validation
# ---------------------------------------------------------------------------
help_result="$(run_and_capture "$ROOT/run/collect.sh" --help)"
help_status="${help_result%%$'\n'*}"
help_output="${help_result#*$'\n'}"
[[ "$help_status" == "0" ]] || fail "collect.sh --help exited $help_status"
[[ "$help_output" == *"Usage:"* ]] || fail "collect.sh --help did not print usage"
[[ "$help_output" == *"--kube-context"* ]] || fail "help should document --kube-context"
[[ "$help_output" == *"--no-trust-ssh-host-key"* ]] || fail "help should document --no-trust-ssh-host-key"
[[ "$help_output" == *"--no-redact"* ]] || fail "help should document --no-redact"
[[ "$help_output" == *"--prom-url"* ]] || fail "help should document --prom-url"
[[ "$help_output" == *"--keep-original-logs"* ]] || fail "help should document --keep-original-logs"
[[ "$help_output" == *"--var-log-max-bytes"* ]] || fail "help should document --var-log-max-bytes"
[[ "$help_output" == *"--allow-cephadm-shell"* ]] || fail "help should document --allow-cephadm-shell"
[[ "$help_output" == *"--allow-kubectl-exec"* ]] || fail "help should document --allow-kubectl-exec"

missing_result="$(run_and_capture "$ROOT/run/collect.sh" --inventory "$tmpdir/missing.env")"
missing_status="${missing_result%%$'\n'*}"
[[ "$missing_status" == "1" ]] || fail "missing inventory should exit 1, got $missing_status"

# Shared by the full shell suite and the focused Python compatibility fixture.
fakebin='' ssh_key='' inventory=''
# shellcheck disable=SC1091
source "$ROOT/tests/fixtures/shell-collect-environment.sh"
setup_shell_collect_environment "$ROOT" "$tmpdir"

# Inventory is declarative input, never executable shell.
inventory_marker="$tmpdir/inventory-command-ran"
malicious_inventory="$tmpdir/inv-malicious.env"
# shellcheck disable=SC2016
printf 'SSH_USER="$(touch %s)"\nHOSTS=(\n  "safe=10.0.0.1"\n)\n' \
  "$inventory_marker" >"$malicious_inventory"
malicious_result="$(run_and_capture "$ROOT/run/collect.sh" \
  --inventory "$malicious_inventory" --ssh-key "$ssh_key")"
malicious_status="${malicious_result%%$'\n'*}"
[[ "$malicious_status" == "1" ]] || fail "executable inventory should be rejected"
[[ ! -e "$inventory_marker" ]] || fail "inventory command substitution was executed"

unsafe_alias_inventory="$tmpdir/inv-unsafe-alias.env"
printf 'SSH_USER="tester"\nHOSTS=(\n  "../escape=10.0.0.1"\n)\n' >"$unsafe_alias_inventory"
unsafe_alias_result="$(run_and_capture "$ROOT/run/collect.sh" \
  --inventory "$unsafe_alias_inventory" --ssh-key "$ssh_key" --mode cephadm \
  --out "$tmpdir/out-unsafe-alias")"
unsafe_alias_status="${unsafe_alias_result%%$'\n'*}"
[[ "$unsafe_alias_status" == "1" ]] || fail "unsafe alias should fail before collection"
unsafe_alias_escape="$(find "$tmpdir" -name escape -print -quit 2>/dev/null || true)"
[[ -z "$unsafe_alias_escape" ]] || fail "unsafe alias escaped its output root: $unsafe_alias_escape"

unsafe_target_inventory="$tmpdir/inv-unsafe-target.env"
printf 'SSH_USER="tester"\nHOSTS=(\n  "node=--ProxyCommand=touch-bad"\n)\n' >"$unsafe_target_inventory"
: >"$FAKE_SSH_LOG"
unsafe_target_result="$(PATH="$fakebin:$PATH" run_and_capture "$ROOT/run/collect.sh" \
  --inventory "$unsafe_target_inventory" --ssh-key "$ssh_key" --mode cephadm \
  --out "$tmpdir/out-unsafe-target")"
unsafe_target_status="${unsafe_target_result%%$'\n'*}"
[[ "$unsafe_target_status" != "0" ]] || fail "unsafe SSH target should fail collection"
[[ ! -s "$FAKE_SSH_LOG" ]] || fail "unsafe SSH target reached ssh"

# ---------------------------------------------------------------------------
# auto: dual-layer collection (ceph from cephnode, rook from kubenode), --context
# ---------------------------------------------------------------------------
out_auto="$tmpdir/out-auto"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --kube-context lab --out "$out_auto" --since 24h --timeout 5 --node-timeout 90 \
  --keep-original-logs --var-log-max-bytes 123456
bundle_auto="$(find_bundle "$out_auto")"
auto_workdirs="$(find "$out_auto" -maxdepth 1 -type d -name 'tmp.*' -print | wc -l | tr -d '[:space:]')"
[[ "$auto_workdirs" == "0" ]] || fail "successful default Collect left $auto_workdirs workdir(s)"
assert_archive_contains "$bundle_auto" "cluster/ceph/json/status.json"
assert_archive_contains "$bundle_auto" "cluster/rook/pods-wide.txt"
assert_archive_contains "$bundle_auto" "nodes/cephnode/system/hostname.txt"
assert_archive_contains "$bundle_auto" "nodes/kubenode/system/hostname.txt"
assert_archive_file_contains "$bundle_auto" "nodes/cephnode/cephadm/var-lib-ceph-configs/fsid/mon.a/config" "[REDACTED]"
grep -qF -- '--context lab' "$FAKE_SSH_LOG" || fail "rook kubectl missing --context in auto mode"
grep -qF '10.0.0.9 kubectl' "$FAKE_SSH_LOG" || fail "rook kubectl did not run on the kube node"
grep -qF 'StrictHostKeyChecking=accept-new' "$FAKE_SSH_LOG" || fail "ssh host key trust should be enabled by default"
grep -qx '90' "$FAKE_TIMEOUT_LOG" || fail "node wrapper should use --node-timeout 90"
grep -qF -- '--keep-original-logs' "$FAKE_SSH_LOG" || fail "remote node missing --keep-original-logs"
grep -qF -- '--var-log-max-bytes' "$FAKE_SSH_LOG" || fail "remote node missing var-log cap flag"
grep -qF -- '123456' "$FAKE_SSH_LOG" || fail "remote node missing var-log cap value"
# A4: the chosen cluster sources are recorded in environment.txt
env_txt="$(tar -xOzf "$bundle_auto" ./environment.txt 2>/dev/null)"
[[ "$env_txt" == *"ceph_source=tester@10.0.0.1"* ]] || fail "environment.txt missing ceph_source"
[[ "$env_txt" == *"rook_source=tester@10.0.0.9"* ]] || fail "environment.txt missing rook_source"
# #3: CONTENTS.md catalogs each artifact and the command that produced it
assert_archive_contains "$bundle_auto" "CONTENTS.md"
contents="$(tar -xOzf "$bundle_auto" ./CONTENTS.md 2>/dev/null)"
[[ "$contents" == *"cluster/ceph/json/status.json"* ]] || fail "CONTENTS.md missing a cluster artifact row"
[[ "$contents" == *"ceph status --format json-pretty"* ]] || fail "CONTENTS.md missing the producing command"
[[ "$contents" == *"nodes/cephnode/system/hostname.txt"* ]] || fail "CONTENTS.md missing a per-node artifact row"
# no --prom-url: the bundle must not contain any prometheus layer at all
tar -tzf "$bundle_auto" | grep -q 'cluster/prometheus' && fail "prometheus dir must not exist without --prom-url" || true

# safety toggles: defaults are on; each off-switch must be independent.
out_no_trust="$tmpdir/out-no-trust"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --out "$out_no_trust" --since 24h --timeout 5 \
  --no-trust-ssh-host-key
bundle_no_trust="$(find_bundle "$out_no_trust")"
assert_archive_file_contains "$bundle_no_trust" "nodes/cephnode/cephadm/var-lib-ceph-configs/fsid/mon.a/config" "[REDACTED]"
grep -qF 'StrictHostKeyChecking=accept-new' "$FAKE_SSH_LOG" && fail "--no-trust-ssh-host-key should not add StrictHostKeyChecking=accept-new" || true

out_no_redact="$tmpdir/out-no-redact"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --out "$out_no_redact" --since 24h --timeout 5 \
  --no-redact
bundle_no_redact="$(find_bundle "$out_no_redact")"
assert_archive_file_contains "$bundle_no_redact" "nodes/cephnode/cephadm/var-lib-ceph-configs/fsid/mon.a/config" "secret = should-redact"
grep -qF 'StrictHostKeyChecking=accept-new' "$FAKE_SSH_LOG" || fail "--no-redact should not disable default ssh host key trust"

out_explicit_on="$tmpdir/out-explicit-on"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --out "$out_explicit_on" --since 24h --timeout 5 \
  --trust-ssh-host-key --redact
bundle_explicit_on="$(find_bundle "$out_explicit_on")"
assert_archive_file_contains "$bundle_explicit_on" "nodes/cephnode/cephadm/var-lib-ceph-configs/fsid/mon.a/config" "[REDACTED]"
grep -qF 'StrictHostKeyChecking=accept-new' "$FAKE_SSH_LOG" || fail "--trust-ssh-host-key should add StrictHostKeyChecking=accept-new"

# ---------------------------------------------------------------------------
# auto with NO capable nodes: both layers SKIPPED, nodes still collected, exit 2
# ---------------------------------------------------------------------------
out_nocap="$tmpdir/out-nocap"
nocap_status=0
set +e
FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --out "$out_nocap" --since 24h --timeout 5
nocap_status=$?
set -e
[[ "$nocap_status" == "2" ]] || fail "auto with no capable node should exit 2, got $nocap_status"
bundle_nocap="$(find_bundle "$out_nocap")"
assert_archive_contains "$bundle_nocap" "cluster/ceph/SKIPPED.txt"
assert_archive_contains "$bundle_nocap" "cluster/rook/SKIPPED.txt"
assert_archive_contains "$bundle_nocap" "nodes/cephnode/system/hostname.txt"

# ---------------------------------------------------------------------------
# explicit --mode cephadm --seed: only ceph layer, no kubectl probing/collection
# ---------------------------------------------------------------------------
out_ceph="$tmpdir/out-ceph"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --seed tester@10.0.0.1 --mode cephadm --out "$out_ceph" --since 24h --timeout 5
bundle_ceph="$(find_bundle "$out_ceph")"
assert_archive_contains "$bundle_ceph" "cluster/ceph/json/status.json"
grep -qF 'kubectl' "$FAKE_SSH_LOG" && fail "cephadm mode should not run kubectl" || true

# An explicit seed with no direct/sudo Ceph runner must remain skipped when the
# potentially state-changing cephadm-shell fallback is disabled.
out_seed_no_runner="$tmpdir/out-seed-no-runner"
: >"$FAKE_SSH_LOG"
seed_no_runner_status=0
set +e
(
  unset CEPH_INCIDENT_ALLOW_CEPHADM_SHELL
  FAKE_CEPH_TARGETS="10.0.0.1" FAKE_CEPH_DIRECT_OK="" FAKE_CEPH_SUDO_OK="" \
  PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
    --inventory "$inventory" --ssh-key "$ssh_key" \
    --seed tester@10.0.0.1 --mode cephadm --out "$out_seed_no_runner" --since 24h --timeout 5
)
seed_no_runner_status=$?
set -e
[[ "$seed_no_runner_status" == "2" ]] || fail "unusable explicit seed should exit 2, got $seed_no_runner_status"
bundle_seed_no_runner="$(find_bundle "$out_seed_no_runner")"
assert_archive_contains "$bundle_seed_no_runner" "cluster/ceph/SKIPPED.txt"
grep -qF 'cephadm shell -- ceph status --format' "$FAKE_SSH_LOG" \
  && fail "disabled cephadm shell was used for explicit seed" || true

# ---------------------------------------------------------------------------
# two cephadm nodes: cluster ceph collected from the FIRST only
# ---------------------------------------------------------------------------
inv_two="$tmpdir/inv-two-ceph.env"
cat >"$inv_two" <<'EOF'
SSH_USER="tester"
HOSTS=(
  "c1=10.0.0.1"
  "c2=10.0.0.2"
)
EOF
out_two="$tmpdir/out-two"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1 10.0.0.2" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inv_two" --ssh-key "$ssh_key" \
  --mode auto --out "$out_two" --since 24h --timeout 5
grep -qF '10.0.0.1 sudo -n cephadm shell -- ceph status --format json-pretty' "$FAKE_SSH_LOG" \
  || fail "cluster ceph should be collected from first cephadm node"
grep -qF '10.0.0.2 sudo -n cephadm shell -- ceph status' "$FAKE_SSH_LOG" \
  && fail "cluster ceph must not be collected twice" || true

# ---------------------------------------------------------------------------
# node-level orchestration (use cephadm --seed to keep the cluster layer simple)
# ---------------------------------------------------------------------------
run_nodecase() {
  # $1=outdir ; remaining env set by caller
  FAKE_CEPH_TARGETS="10.0.0.1" PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
    --inventory "$inventory" --ssh-key "$ssh_key" \
    --seed tester@10.0.0.1 --mode cephadm --out "$1" --since 24h --timeout 5
}

# C4: truncated node (no manifest) -> SKIPPED, exit 2
out_nomani="$tmpdir/out-nomani"
st=0; set +e
FAKE_SSH_NO_MANIFEST_ALIAS=kubenode run_nodecase "$out_nomani"
st=$?; set -e
[[ "$st" == "2" ]] || fail "missing node manifest should exit 2, got $st"
assert_archive_contains "$(find_bundle "$out_nomani")" "nodes/kubenode/SKIPPED.txt"

# bad tar -> SKIPPED, exit 2
out_badtar="$tmpdir/out-badtar"
st=0; set +e
FAKE_SSH_BAD_TAR_ALIAS=kubenode run_nodecase "$out_badtar"
st=$?; set -e
[[ "$st" == "2" ]] || fail "bad node tar should exit 2, got $st"
assert_archive_contains "$(find_bundle "$out_badtar")" "nodes/kubenode/SKIPPED.txt"

# one failed host -> exit 2, errors.log present
out_fail="$tmpdir/out-fail"
st=0; set +e
FAKE_SSH_FAIL_ALIAS=kubenode run_nodecase "$out_fail"
st=$?; set -e
[[ "$st" == "2" ]] || fail "one failed host should exit 2, got $st"
assert_archive_contains "$(find_bundle "$out_fail")" "errors.log"

# C2: abort mid-run -> trap cleans workdir (no tmp.* left)
out_abort="$tmpdir/out-abort"
set +e
COLLECT_TEST_ABORT_AFTER_NODES=1 FAKE_CEPH_TARGETS="10.0.0.1" PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --seed tester@10.0.0.1 --mode cephadm --out "$out_abort" --since 24h --timeout 5 >/dev/null 2>&1
abort_status=$?
set -e
[[ "$abort_status" != "0" ]] || fail "abort hook should exit non-zero"
leftover="$(find "$out_abort" -maxdepth 1 -name 'tmp.*' 2>/dev/null | wc -l | tr -d '[:space:]')"
[[ "$leftover" == "0" ]] || fail "abort left $leftover tmp workdir(s)"

# C3: verify failure (forbidden secret path) -> exit 1, workdir kept, no bundle
out_verify="$tmpdir/out-verify"
set +e
FAKE_SSH_PEM_ALIAS=kubenode FAKE_CEPH_TARGETS="10.0.0.1" PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --seed tester@10.0.0.1 --mode cephadm --out "$out_verify" --since 24h --timeout 5 >/dev/null 2>&1
verify_status=$?
set -e
[[ "$verify_status" == "1" ]] || fail "verify failure should exit 1, got $verify_status"
produced="$(find "$out_verify" -maxdepth 1 -name 'ceph-incident-*.tar.gz' 2>/dev/null | wc -l | tr -d '[:space:]')"
[[ "$produced" == "0" ]] || fail "verify failure must not package a bundle"
kept="$(find "$out_verify" -maxdepth 1 -name 'tmp.*' -type d 2>/dev/null | wc -l | tr -d '[:space:]')"
[[ "$kept" == "1" ]] || fail "verify failure should keep the workdir (found $kept)"

# A1: auto with a kubectl node but missing namespace AND no ceph node -> nothing
# actually collected -> exit 2 (must NOT be a green exit-0).
inv_kubeonly="$tmpdir/inv-kubeonly.env"
cat >"$inv_kubeonly" <<'EOF'
SSH_USER="tester"
HOSTS=(
  "kubenode=10.0.0.9"
)
EOF
out_nsmiss="$tmpdir/out-nsmiss"
st=0; set +e
FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS="10.0.0.9" FAKE_KUBE_NS_MISSING=1 \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inv_kubeonly" --ssh-key "$ssh_key" \
  --mode auto --out "$out_nsmiss" --since 24h --timeout 5
st=$?; set -e
[[ "$st" == "2" ]] || fail "auto with rook allow-skip and no ceph should exit 2, got $st"
# the specific collector reason must survive (not be overwritten by the generic auto skip)
tar -xOzf "$(find_bundle "$out_nsmiss")" ./cluster/rook/SKIPPED.txt 2>/dev/null | grep -qF 'namespace not found' \
  || fail "auto skip overwrote the specific rook SKIPPED reason"

# A3: a node whose capability probe ssh fails is recorded in errors.log
out_probefail="$tmpdir/out-probefail"
set +e
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="" FAKE_PROBE_FAIL_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --out "$out_probefail" --since 24h --timeout 5 >/dev/null 2>&1
set -e
assert_archive_contains "$(find_bundle "$out_probefail")" "errors.log"
assert_archive_has_debug_log_for "$(find_bundle "$out_probefail")" "tester@10.0.0.9"
tar -xOzf "$(find_bundle "$out_probefail")" ./errors.log 2>/dev/null | grep -qF 'capability probe failed for tester@10.0.0.9' \
  || fail "probe ssh failure was not recorded in errors.log"

# A3b: a node SSH transport failure preserves a verbose ssh debug log in bundle
out_node_sshfail="$tmpdir/out-node-sshfail"
set +e
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="" FAKE_SSH_CONNECT_FAIL_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode cephadm --seed tester@10.0.0.1 --out "$out_node_sshfail" --since 24h --timeout 5 >/dev/null 2>&1
node_sshfail_status=$?
set -e
[[ "$node_sshfail_status" == "2" ]] || fail "node ssh transport failure should exit 2, got $node_sshfail_status"
assert_archive_has_debug_log_for "$(find_bundle "$out_node_sshfail")" "tester@10.0.0.9"

# A3c: a cluster Ceph SSH transport failure also preserves a verbose ssh debug log
out_cluster_sshfail="$tmpdir/out-cluster-sshfail"
set +e
FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS="" FAKE_SSH_CONNECT_FAIL_TARGETS="10.0.0.1" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode cephadm --seed tester@10.0.0.1 --out "$out_cluster_sshfail" --since 24h --timeout 5 >/dev/null 2>&1
cluster_sshfail_status=$?
set -e
[[ "$cluster_sshfail_status" == "2" ]] || fail "cluster ssh transport failure should exit 2, got $cluster_sshfail_status"
assert_archive_has_debug_log_for "$(find_bundle "$out_cluster_sshfail")" "tester@10.0.0.1" "label: cluster-ceph"

# A5: empty HOSTS=() -> exit 1 with a clear message (no bash-3.2 unbound error)
inv_empty="$tmpdir/inv-empty.env"
printf 'SSH_USER="t"\nHOSTS=()\n' >"$inv_empty"
empty_result="$(run_and_capture "$ROOT/run/collect.sh" --inventory "$inv_empty" --ssh-key "$ssh_key" --mode cephadm --seed t@1.2.3.4 --out "$tmpdir/out-empty")"
empty_status="${empty_result%%$'\n'*}"
empty_output="${empty_result#*$'\n'}"
[[ "$empty_status" == "1" ]] || fail "empty HOSTS should exit 1, got $empty_status"
[[ "$empty_output" == *"HOSTS is empty"* ]] || fail "empty HOSTS should explain the failure"

# A6: --kube-context with shell metacharacters is rejected (exit 1)...
ctx_bad="$(run_and_capture "$ROOT/run/collect.sh" --kube-context 'bad;ctx' --inventory "$inventory" --ssh-key "$ssh_key")"
ctx_bad_status="${ctx_bad%%$'\n'*}"
ctx_bad_output="${ctx_bad#*$'\n'}"
[[ "$ctx_bad_status" == "1" ]] || fail "invalid --kube-context should exit 1, got $ctx_bad_status"
[[ "$ctx_bad_output" == *"invalid --kube-context"* ]] || fail "bad context should explain failure"
# ...but a real context (kubernetes-admin@kubernetes / EKS ARN chars @ : /) is accepted:
# it passes validation and fails later on the missing inventory instead.
ctx_ok="$(run_and_capture "$ROOT/run/collect.sh" --kube-context 'arn:aws:eks:us-east-1:1/x@k8s' --inventory /nope.env --ssh-key "$ssh_key")"
ctx_ok_output="${ctx_ok#*$'\n'}"
[[ "$ctx_ok_output" == *"missing inventory"* ]] || fail "valid kube-context wrongly rejected: $ctx_ok_output"

# prefer direct ceph: a node where `ceph -s` connects uses plain `ceph` (no cephadm shell)
inv_direct="$tmpdir/inv-direct.env"
cat >"$inv_direct" <<'EOF'
SSH_USER="tester"
HOSTS=(
  "cephnode=10.0.0.1"
)
EOF
out_direct="$tmpdir/out-direct"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_BIN_TARGETS="10.0.0.1" FAKE_CEPH_DIRECT_OK="10.0.0.1" FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inv_direct" --ssh-key "$ssh_key" \
  --mode cephadm --out "$out_direct" --since 24h --timeout 5
bundle_direct="$(find_bundle "$out_direct")"
assert_archive_contains "$bundle_direct" "cluster/ceph/json/status.json"
grep -qF '10.0.0.1 ceph status --format json-pretty' "$FAKE_SSH_LOG" || fail "direct runner should use plain ceph"
grep -qF 'cephadm shell' "$FAKE_SSH_LOG" && fail "direct runner must not use cephadm shell" || true
tar -xOzf "$bundle_direct" ./environment.txt 2>/dev/null | grep -qF 'ceph_runner=direct' || fail "environment.txt should record ceph_runner=direct"

# fallback: direct/sudo don't connect but cephadm does -> cephadm shell runner
inv_fb="$tmpdir/inv-fb.env"
cat >"$inv_fb" <<'EOF'
SSH_USER="tester"
HOSTS=(
  "c1=10.0.0.1"
)
EOF
out_fb="$tmpdir/out-fb"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_CEPH_DIRECT_OK="" FAKE_CEPH_SUDO_OK="" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inv_fb" --ssh-key "$ssh_key" \
  --mode cephadm --out "$out_fb" --since 24h --timeout 5
bundle_fb="$(find_bundle "$out_fb")"
grep -qF '10.0.0.1 sudo -n cephadm shell -- ceph status --format json-pretty' "$FAKE_SSH_LOG" || fail "fallback should use cephadm shell"
tar -xOzf "$bundle_fb" ./environment.txt 2>/dev/null | grep -qF 'ceph_runner=cephadm' || fail "environment.txt should record ceph_runner=cephadm"

# --kube-mode local: rook layer uses the jump host's local kubectl (no ssh), not a node
out_klocal="$tmpdir/out-klocal"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode rook --kube-mode local --kube-context lab --out "$out_klocal" --since 24h --timeout 5
bundle_klocal="$(find_bundle "$out_klocal")"
assert_archive_contains "$bundle_klocal" "cluster/rook/pods-wide.txt"
tar -xOzf "$bundle_klocal" ./environment.txt 2>/dev/null | grep -qF 'rook_source=local' || fail "kube-mode local should record rook_source=local"
grep -qF 'kubectl' "$FAKE_SSH_LOG" && fail "kube-mode local must not run kubectl over ssh" || true

# --kube-mode invalid -> exit 1
km_bad="$(run_and_capture "$ROOT/run/collect.sh" --kube-mode bogus --inventory "$inventory" --ssh-key "$ssh_key")"
km_bad_status="${km_bad%%$'\n'*}"
km_bad_out="${km_bad#*$'\n'}"
[[ "$km_bad_status" == "1" ]] || fail "invalid --kube-mode should exit 1, got $km_bad_status"
[[ "$km_bad_out" == *"invalid --kube-mode"* ]] || fail "bad --kube-mode should explain failure"

# --prom-url with an unparseable --since is rejected up front (exit 1)
prom_bad_since="$(run_and_capture "$ROOT/run/collect.sh" --prom-url http://prom.example:9090 --since yesterday --inventory "$inventory" --ssh-key "$ssh_key")"
prom_bad_since_status="${prom_bad_since%%$'\n'*}"
prom_bad_since_out="${prom_bad_since#*$'\n'}"
[[ "$prom_bad_since_status" == "1" ]] || fail "--prom-url with bad --since should exit 1, got $prom_bad_since_status"
[[ "$prom_bad_since_out" == *"--since must be"* ]] || fail "bad since should explain the failure"

prom_bad_timeout="$(run_and_capture "$ROOT/run/collect.sh" --prom-url http://prom.example:9090 --prom-timeout abc --inventory "$inventory" --ssh-key "$ssh_key")"
prom_bad_timeout_status="${prom_bad_timeout%%$'\n'*}"
[[ "$prom_bad_timeout_status" == "1" ]] || fail "non-numeric --prom-timeout should exit 1, got $prom_bad_timeout_status"

prom_zero_step="$(run_and_capture "$ROOT/run/collect.sh" --prom-url http://prom.example:9090 --prom-step 0 --inventory "$inventory" --ssh-key "$ssh_key")"
prom_zero_step_status="${prom_zero_step%%$'\n'*}"
[[ "$prom_zero_step_status" == "1" ]] || fail "--prom-step 0 should exit 1, got $prom_zero_step_status"

# ---------------------------------------------------------------------------
# --prom-url: metrics dump lands inside the bundle; only matching jobs dumped
# ---------------------------------------------------------------------------
cp "$ROOT/tests/fixtures/bin/curl" "$fakebin/curl"
export FAKE_CURL_LOG="$tmpdir/curl.log"
out_prom="$tmpdir/out-prom"
: >"$FAKE_CURL_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --seed tester@10.0.0.1 --mode cephadm --out "$out_prom" --since 24h --timeout 5 \
  --prom-url http://prom.example:9090
bundle_prom="$(find_bundle "$out_prom")"
assert_archive_contains "$bundle_prom" "cluster/prometheus/dump-info.txt"
assert_archive_contains "$bundle_prom" "cluster/prometheus/buildinfo.json"
assert_archive_contains "$bundle_prom" "cluster/prometheus/ceph/ceph_health_status.json.gz"
assert_archive_contains "$bundle_prom" "cluster/prometheus/node-exporter/node_load1.json.gz"
tar -tzf "$bundle_prom" | grep -q 'cluster/prometheus/grafana/' && fail "non-matching job must not be dumped" || true
assert_archive_file_contains "$bundle_prom" "environment.txt" "prom_url=http://prom.example:9090"
grep -qF 'step=15' "$FAKE_CURL_LOG" || fail "24h window should query with step=15"

# Progress: default-on goes to stderr; stdout stays just `bundle:`; --quiet silences it.
prog_out="$tmpdir/prog.out"; prog_err="$tmpdir/prog.err"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --kube-context lab --out "$tmpdir/out-prog" --since 24h --timeout 5 \
  >"$prog_out" 2>"$prog_err"
grep -qF 'bundle:' "$prog_out" || fail "stdout must carry the bundle: line"
grep -qE 'node (cephnode|kubenode)' "$prog_err" || fail "stderr should show node progress"
grep -qiE 'probing|collecting ceph' "$prog_err" || fail "stderr should show probe/ceph progress"
grep -qF 'bundle:' "$prog_err" && fail "bundle: must not be on stderr" || true

q_out="$tmpdir/q.out"; q_err="$tmpdir/q.err"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --kube-context lab --out "$tmpdir/out-quiet" --since 24h --timeout 5 --quiet \
  >"$q_out" 2>"$q_err"
grep -qF 'bundle:' "$q_out" || fail "--quiet must still print bundle: to stdout"
grep -qE 'probing|node cephnode|collecting ceph' "$q_err" && fail "--quiet must suppress progress" || true

# #1: the interrupt handler (Ctrl-C path) stops with exit 130 and cleans the
# workdir. (Real signal delivery isn't reliably testable in every CI sandbox, so
# we unit-test the handler contract that the trap invokes.)
int_wd="$tmpdir/int-workdir"
mkdir -p "$int_wd"
int_rc=0
set +e
int_out="$( ( set -uo pipefail
  # shellcheck disable=SC1091
  source "$ROOT/lib/common.sh"
  # shellcheck disable=SC1091
  source "$ROOT/lib/bundle.sh"
  # Used by the sourced on_interrupt/cleanup_workdir trap helpers.
  # shellcheck disable=SC2030
  CLEANUP_WORKDIR="$int_wd"
  # shellcheck disable=SC2030
  CLEANUP_KEEP=0
  on_interrupt ) 2>&1 )"
int_rc=$?
set -e
[[ "$int_rc" == "130" ]] || fail "on_interrupt must exit 130, got $int_rc"
[[ "$int_out" == *"interrupted"* ]] || fail "on_interrupt should announce the interruption"
[[ ! -d "$int_wd" ]] || fail "on_interrupt should remove the workdir"
# with --keep-workdir the interrupt handler preserves it
int_wd2="$tmpdir/int-workdir-keep"
mkdir -p "$int_wd2"
( set -euo pipefail
  # shellcheck disable=SC1091
  source "$ROOT/lib/common.sh"
  # shellcheck disable=SC1091
  source "$ROOT/lib/bundle.sh"
  # Used by the sourced on_interrupt/cleanup_workdir trap helpers.
  # shellcheck disable=SC2030,SC2034
  CLEANUP_WORKDIR="$int_wd2"
  # shellcheck disable=SC2030,SC2034
  CLEANUP_KEEP=1
  on_interrupt ) >/dev/null 2>&1 || true
[[ -d "$int_wd2" ]] || fail "on_interrupt must honor CLEANUP_KEEP=1"

results_after="$(find "$ROOT/results" -mindepth 1 -print 2>/dev/null | sort || true)"
[[ "$results_after" == "$results_before" ]] || fail "test-collect.sh changed repository results/"

printf 'ok: collect orchestration\n'
