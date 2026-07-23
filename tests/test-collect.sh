#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

missing_result="$(run_and_capture "$ROOT/run/collect.sh" --inventory "$tmpdir/missing.env")"
missing_status="${missing_result%%$'\n'*}"
[[ "$missing_status" == "1" ]] || fail "missing inventory should exit 1, got $missing_status"

# ---------------------------------------------------------------------------
# fake bins: capability-aware ssh, fake kubectl, passthrough timeout
#   ssh dispatches by the remote command:
#     - "command -v cephadm" (capability probe) -> emit caps based on target
#     - "cephadm shell -- ceph"                 -> delegate to the ceph fixture ssh
#     - "kubectl"                               -> forward to the fake kubectl
#     - "collect-node.sh"                       -> fabricate a node bundle tar
#   Caps per target come from FAKE_CEPH_TARGETS / FAKE_KUBE_TARGETS (substrings).
# ---------------------------------------------------------------------------
fakebin="$tmpdir/fakebin"
mkdir -p "$fakebin"

cat >"$fakebin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--context" ]] && shift 2
cmd="$*"
case "$cmd" in
  "get namespace rook-ceph")
    if [[ "${FAKE_KUBE_NS_MISSING:-}" == "1" ]]; then
      printf 'Error from server (NotFound): namespaces "rook-ceph" not found\n' >&2
      exit 1
    fi
    printf 'rook-ceph\n'
    ;;
  "get pods -n rook-ceph -o wide") printf 'NAME READY STATUS\nrook-ceph-operator-0 1/1 Running\n' ;;
  "get events -n rook-ceph --sort-by=.lastTimestamp") printf 'LAST SEEN TYPE\n1m Normal\n' ;;
  *"-n rook-ceph -o yaml") printf 'apiVersion: v1\nitems:\n- kind: CephCluster\n' ;;
  "get pods -n rook-ceph -l app=rook-ceph-operator -o name") printf 'pod/rook-ceph-operator-0\n' ;;
  "logs -n rook-ceph rook-ceph-operator-0 --since="*) printf 'operator log line\n' ;;
  "get pods -n rook-ceph -l app=rook-ceph-tools -o name") exit 0 ;;
  *) printf 'unexpected kubectl: %s\n' "$cmd" >&2; exit 99 ;;
esac
EOF

cat >"$fakebin/timeout" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >>"${FAKE_TIMEOUT_LOG:?}"
shift
exec "$@"
EOF

cat >"$fakebin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_SSH_LOG:?}"
whole="$*"
args=("$@")
n=${#args[@]}

# target = first arg after the -i KEY / -o OPT option pairs
target=''; j=0
while [[ $j -lt $n ]]; do
  case "${args[$j]}" in
    -i|-o) j=$((j + 2)) ;;
    *) target="${args[$j]}"; break ;;
  esac
done

# Order matters: the capability-probe script also contains "kubectl", and the
# runner connectivity probe ("--connect-timeout 5 -s") must be matched before the
# generic ceph/cephadm command branches.
if [[ "$whole" == *"-vvv"* ]]; then
  printf 'debug1: fake ssh verbose log for %s\n' "$target" >&2
  printf 'debug3: fake ssh argv %s\n' "$whole" >&2
  exit 255
fi
for t in ${FAKE_SSH_CONNECT_FAIL_TARGETS:-}; do
  [[ "$target" == *"$t"* ]] && { printf 'ssh: connect to host %s port 22: Connection refused\n' "$target" >&2; exit 255; }
done

case "$whole" in
  *"--connect-timeout 5 -s"*)
    # ceph runner connectivity probe; succeed per method+target env
    case "$whole" in
      *"cephadm shell"*) method=cephadm ;;
      *"sudo -n ceph"*) method=sudo ;;
      *) method=direct ;;
    esac
    ok=0
    case "$method" in
      direct) for t in ${FAKE_CEPH_DIRECT_OK:-}; do [[ "$target" == *"$t"* ]] && ok=1; done ;;
      sudo) for t in ${FAKE_CEPH_SUDO_OK:-}; do [[ "$target" == *"$t"* ]] && ok=1; done ;;
      cephadm) for t in ${FAKE_CEPHADM_OK:-${FAKE_CEPH_TARGETS:-}}; do [[ "$target" == *"$t"* ]] && ok=1; done ;;
    esac
    exit $(( ok == 1 ? 0 : 1 ))
    ;;
  *"command -v cephadm"*)
    for t in ${FAKE_PROBE_FAIL_TARGETS:-}; do [[ "$target" == *"$t"* ]] && exit 255; done
    caps=""
    for t in ${FAKE_CEPH_TARGETS:-}; do [[ "$target" == *"$t"* ]] && caps="$caps cephadm"; done
    for t in ${FAKE_CEPH_BIN_TARGETS:-}; do [[ "$target" == *"$t"* ]] && caps="$caps ceph"; done
    for t in ${FAKE_KUBE_TARGETS:-}; do [[ "$target" == *"$t"* ]] && caps="$caps kubectl"; done
    printf '%s\n' "$caps"
    exit 0
    ;;
  *"cephadm shell -- ceph"*)
    exec "$FIXTURE_SSH" "$@"
    ;;
  *collect-node.sh*)
    alias_name="$(printf '%s\n' "$whole" | sed -n "s/.*--host-alias '\\([^']*\\)'.*/\\1/p")"
    cat >/dev/null
    [[ -n "$alias_name" ]] || { printf 'no alias\n' >&2; exit 99; }
    if [[ "${FAKE_SSH_BAD_TAR_ALIAS:-}" == "$alias_name" ]]; then
      printf 'not a tar archive\n'; exit 0
    fi
    sleep "${FAKE_SSH_SLEEP:-0}"
    t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
    mkdir -p "$t/system"
    mkdir -p "$t/cephadm/var-lib-ceph-configs/fsid/mon.a"
    printf 'node %s\n' "$alias_name" >"$t/system/hostname.txt"
    printf 'secret = should-redact\n' >"$t/cephadm/var-lib-ceph-configs/fsid/mon.a/config"
    if [[ "${FAKE_SSH_NO_MANIFEST_ALIAS:-}" != "$alias_name" ]]; then
      printf '{"host":"%s","collector":"collect-node","artifact":"/rmt/out/system/hostname.txt","command":"hostname","exit_code":0,"started":"t0","ended":"t1"}\n' "$alias_name" >"$t/manifest.jsonl"
    fi
    [[ "${FAKE_SSH_PEM_ALIAS:-}" == "$alias_name" ]] && printf 'cert\n' >"$t/system/leak.pem"
    tar -czf - -C "$t" .
    [[ "${FAKE_SSH_FAIL_ALIAS:-}" == "$alias_name" ]] && exit 2
    exit 0
    ;;
  *kubectl*)
    seen=0; kargs=()
    for a in "$@"; do
      if [[ $seen -eq 1 ]]; then kargs+=("$a"); continue; fi
      [[ "$a" == "kubectl" ]] && seen=1
    done
    exec kubectl "${kargs[@]}"
    ;;
  *" ceph "*)
    # direct/sudo `ceph <args>` cluster commands — same responses as cephadm shell
    exec "$FIXTURE_SSH" "$@"
    ;;
  *)
    printf 'unexpected ssh remote: %s\n' "$whole" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$fakebin/kubectl" "$fakebin/ssh" "$fakebin/timeout"

ssh_key="$tmpdir/id_ed25519"
printf 'fake key\n' >"$ssh_key"
export FAKE_SSH_LOG="$tmpdir/ssh.log"
export FAKE_TIMEOUT_LOG="$tmpdir/timeout.log"
export FIXTURE_SSH="$ROOT/tests/fixtures/bin/ssh"

# external topology: a ceph node + a kube node
inventory="$tmpdir/inv-external.env"
cat >"$inventory" <<'EOF'
SSH_USER="tester"
ROOK_NAMESPACE="rook-ceph"
HOSTS=(
  "cephnode=10.0.0.1"
  "kubenode=10.0.0.9"
)
EOF

# ---------------------------------------------------------------------------
# auto: dual-layer collection (ceph from cephnode, rook from kubenode), --context
# ---------------------------------------------------------------------------
out_auto="$tmpdir/out-auto"
: >"$FAKE_SSH_LOG"
FAKE_CEPH_TARGETS="10.0.0.1" FAKE_KUBE_TARGETS="10.0.0.9" \
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --mode auto --kube-context lab --out "$out_auto" --since 24h --timeout 5 --node-timeout 90
bundle_auto="$(find_bundle "$out_auto")"
assert_archive_contains "$bundle_auto" "cluster/ceph/json/status.json"
assert_archive_contains "$bundle_auto" "cluster/rook/pods-wide.txt"
assert_archive_contains "$bundle_auto" "nodes/cephnode/system/hostname.txt"
assert_archive_contains "$bundle_auto" "nodes/kubenode/system/hostname.txt"
assert_archive_file_contains "$bundle_auto" "nodes/cephnode/cephadm/var-lib-ceph-configs/fsid/mon.a/config" "[REDACTED]"
grep -qF -- '--context lab' "$FAKE_SSH_LOG" || fail "rook kubectl missing --context in auto mode"
grep -qF '10.0.0.9 kubectl' "$FAKE_SSH_LOG" || fail "rook kubectl did not run on the kube node"
grep -qF 'StrictHostKeyChecking=accept-new' "$FAKE_SSH_LOG" || fail "ssh host key trust should be enabled by default"
grep -qx '90' "$FAKE_TIMEOUT_LOG" || fail "node wrapper should use --node-timeout 90"
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
PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
  --inventory "$inventory" --ssh-key "$ssh_key" \
  --seed tester@10.0.0.1 --mode cephadm --out "$out_ceph" --since 24h --timeout 5
bundle_ceph="$(find_bundle "$out_ceph")"
assert_archive_contains "$bundle_ceph" "cluster/ceph/json/status.json"
grep -qF 'kubectl' "$FAKE_SSH_LOG" && fail "cephadm mode should not run kubectl" || true

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
  PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
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
COLLECT_TEST_ABORT_AFTER_NODES=1 PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
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
FAKE_SSH_PEM_ALIAS=kubenode PATH="$fakebin:$PATH" "$ROOT/run/collect.sh" \
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
empty_result="$(run_and_capture "$ROOT/run/collect.sh" --inventory "$inv_empty" --ssh-key "$ssh_key" --mode cephadm --seed t@1.2.3.4)"
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
  # shellcheck disable=SC2030
  CLEANUP_WORKDIR="$int_wd2"
  # shellcheck disable=SC2030
  CLEANUP_KEEP=1
  on_interrupt ) >/dev/null 2>&1 || true
[[ -d "$int_wd2" ]] || fail "on_interrupt must honor CLEANUP_KEEP=1"

printf 'ok: collect orchestration\n'
