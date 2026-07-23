#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASH_BIN="$(command -v bash)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_file_contains() {
  local file=$1 expected=$2
  [[ -f "$file" ]] || fail "missing file: $file"
  grep -qF "$expected" "$file" || fail "expected '$expected' in $file"
}

run_rook_collector() {
  local outdir=$1 manifest=$2
  shift 2
  "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
    --out "$outdir" \
    --manifest "$manifest" \
    --namespace rook-ceph \
    --since 24h \
    --timeout 5 \
    "$@"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

minimal_bin="$tmpdir/minimal-bin"
mkdir -p "$minimal_bin"
ln -s "$(command -v dirname)" "$minimal_bin/dirname"
ln -s "$(command -v mkdir)" "$minimal_bin/mkdir"

out_no_kubectl="$tmpdir/out-no-kubectl"
manifest_no_kubectl="$tmpdir/manifest-no-kubectl.jsonl"
no_kubectl_rc=0
set +e
PATH="$minimal_bin" "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
  --out "$out_no_kubectl" \
  --manifest "$manifest_no_kubectl" \
  --namespace rook-ceph \
  --since 24h \
  --timeout 5
no_kubectl_rc=$?
set -e
# R4: explicit rook mode with no kubectl is a real failure (not a silent success)
[[ "$no_kubectl_rc" == "2" ]] || fail "explicit rook with no kubectl should exit 2, got $no_kubectl_rc"
assert_file_contains "$out_no_kubectl/cluster/rook/SKIPPED.txt" "kubectl command not found"

# R4: auto-mode fallback passes --allow-skip, so the same situation is a graceful skip (rc 0)
out_no_kubectl_skip="$tmpdir/out-no-kubectl-skip"
manifest_no_kubectl_skip="$tmpdir/manifest-no-kubectl-skip.jsonl"
allow_skip_rc=0
set +e
PATH="$minimal_bin" "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
  --out "$out_no_kubectl_skip" \
  --manifest "$manifest_no_kubectl_skip" \
  --namespace rook-ceph \
  --since 24h \
  --timeout 5 \
  --allow-skip
allow_skip_rc=$?
set -e
[[ "$allow_skip_rc" == "0" ]] || fail "rook with --allow-skip and no kubectl should exit 0, got $allow_skip_rc"
assert_file_contains "$out_no_kubectl_skip/cluster/rook/SKIPPED.txt" "kubectl command not found"

fakebin="$tmpdir/fakebin"
mkdir -p "$fakebin"
cat >"$fakebin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_KUBECTL_LOG:?}"

# tolerate a leading "--context CTX" (added in remote/ssh mode)
ctx=''
if [[ "${1:-}" == "--context" ]]; then
  ctx=${2:-}
  shift 2
fi

mode=${FAKE_KUBECTL_MODE:-present}
cmd="$*"

case "$cmd" in
  "get namespace rook-ceph")
    case "$mode" in
      missing-namespace)
        printf 'Error from server (NotFound): namespaces "rook-ceph" not found\n' >&2
        exit 1
        ;;
      context-missing)
        printf 'error: no context exists with the name: "%s"\n' "$ctx" >&2
        exit 1
        ;;
      connection-refused)
        printf 'The connection to the server 127.0.0.1:6443 was refused - did you specify the right host or port?\n' >&2
        exit 1
        ;;
    esac
    printf 'rook-ceph\n'
    ;;
  "get namespace rook-ceph-external")
    printf 'rook-ceph-external\n'
    ;;
  "get pods -n rook-ceph -o wide")
    printf 'NAME READY STATUS\nrook-ceph-operator-0 1/1 Running\n'
    ;;
  "get pods -n rook-ceph-external -o wide")
    printf 'No resources found in rook-ceph-external namespace.\n'
    ;;
  "get events -n rook-ceph --sort-by=.lastTimestamp")
    printf 'LAST SEEN TYPE REASON OBJECT MESSAGE\n1m Normal Started pod/osd started\n'
    ;;
  "get events -n rook-ceph-external --sort-by=.lastTimestamp")
    printf 'LAST SEEN TYPE REASON OBJECT MESSAGE\n1m Normal ClusterConnected cephcluster/rook-ceph-external connected\n'
    ;;
  "get cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io -n rook-ceph -o yaml")
    printf 'apiVersion: v1\nitems:\n- kind: CephCluster\n  metadata:\n    name: rook-ceph\n'
    ;;
  "get cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io -n rook-ceph-external -o yaml")
    printf 'apiVersion: v1\nitems:\n- kind: CephCluster\n  metadata:\n    name: rook-ceph-external\n'
    ;;
  "get pods -n rook-ceph -l app=rook-ceph-operator -o name")
    [[ "$mode" == "op-lookup-fail" ]] && exit 1
    printf 'pod/rook-ceph-operator-0\n'
    ;;
  "logs -n rook-ceph rook-ceph-operator-0 --since=24h")
    printf 'operator log line\n'
    ;;
  "get pods -n rook-ceph -l app=rook-ceph-tools -o name")
    [[ "$mode" == "with-toolbox" ]] || exit 0
    printf 'pod/rook-ceph-tools-0\n'
    ;;
  "exec -n rook-ceph rook-ceph-tools-0 -- ceph status")
    printf 'cluster is healthy from toolbox\n'
    ;;
  *)
    printf 'unexpected kubectl command: %s\n' "$cmd" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$fakebin/kubectl"

export FAKE_KUBECTL_LOG="$tmpdir/kubectl.log"

out_missing_ns="$tmpdir/out-missing-ns"
manifest_missing_ns="$tmpdir/manifest-missing-ns.jsonl"
missing_ns_rc=0
set +e
FAKE_KUBECTL_MODE=missing-namespace PATH="$fakebin:$PATH" run_rook_collector "$out_missing_ns" "$manifest_missing_ns"
missing_ns_rc=$?
set -e
[[ "$missing_ns_rc" == "2" ]] || fail "explicit rook with missing namespace should exit 2, got $missing_ns_rc"
assert_file_contains "$out_missing_ns/cluster/rook/SKIPPED.txt" "namespace not found"
assert_file_contains "$out_missing_ns/cluster/rook/SKIPPED.txt" "rook namespace not found: rook-ceph"
assert_file_contains "$out_missing_ns/cluster/rook/SKIPPED.txt" "namespaces \"rook-ceph\" not found"

out_missing_ctx="$tmpdir/out-missing-ctx"
manifest_missing_ctx="$tmpdir/manifest-missing-ctx.jsonl"
missing_ctx_rc=0
set +e
FAKE_KUBECTL_MODE=context-missing PATH="$fakebin:$PATH" \
  "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
  --out "$out_missing_ctx" \
  --manifest "$manifest_missing_ctx" \
  --namespace rook-ceph \
  --since 24h \
  --timeout 5 \
  --kube-context lab
missing_ctx_rc=$?
set -e
[[ "$missing_ctx_rc" == "2" ]] || fail "explicit rook with missing context should exit 2, got $missing_ctx_rc"
assert_file_contains "$out_missing_ctx/cluster/rook/SKIPPED.txt" "kubectl context not found: lab"
assert_file_contains "$out_missing_ctx/cluster/rook/SKIPPED.txt" "no context exists with the name"

out_connection_refused="$tmpdir/out-connection-refused"
manifest_connection_refused="$tmpdir/manifest-connection-refused.jsonl"
connection_refused_rc=0
set +e
FAKE_KUBECTL_MODE=connection-refused PATH="$fakebin:$PATH" run_rook_collector "$out_connection_refused" "$manifest_connection_refused"
connection_refused_rc=$?
set -e
[[ "$connection_refused_rc" == "2" ]] || fail "explicit rook with connection failure should exit 2, got $connection_refused_rc"
assert_file_contains "$out_connection_refused/cluster/rook/SKIPPED.txt" "kubectl cannot connect to cluster API"
assert_file_contains "$out_connection_refused/cluster/rook/SKIPPED.txt" "connection to the server"

out_present="$tmpdir/out-present"
manifest_present="$tmpdir/manifest-present.jsonl"
FAKE_KUBECTL_MODE=with-toolbox PATH="$fakebin:$PATH" run_rook_collector "$out_present" "$manifest_present"

assert_file_contains "$out_present/cluster/rook/pods-wide.txt" "rook-ceph-operator-0"
assert_file_contains "$out_present/cluster/rook/events.txt" "osd started"
assert_file_contains "$out_present/cluster/rook/rook-resources.yaml" "kind: CephCluster"
assert_file_contains "$out_present/cluster/rook/operator.log" "operator log line"
assert_file_contains "$out_present/cluster/rook/toolbox-status.txt" "cluster is healthy from toolbox"

grep -qF 'get namespace rook-ceph' "$FAKE_KUBECTL_LOG" || fail "namespace detection was not called"
grep -qF 'logs -n rook-ceph rook-ceph-operator-0 --since=24h' "$FAKE_KUBECTL_LOG" || fail "operator logs were not collected"

out_external="$tmpdir/out-external"
manifest_external="$tmpdir/manifest-external.jsonl"
FAKE_KUBECTL_MODE=present PATH="$fakebin:$PATH" \
  "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
  --out "$out_external" --manifest "$manifest_external" \
  --namespace rook-ceph-external --operator-namespace rook-ceph \
  --since 24h --timeout 5

assert_file_contains "$out_external/cluster/rook/rook-resources.yaml" "name: rook-ceph-external"
assert_file_contains "$out_external/cluster/rook/operator.log" "operator log line"
grep -qF 'logs -n rook-ceph rook-ceph-operator-0 --since=24h' "$FAKE_KUBECTL_LOG" || fail "external cluster operator logs were not collected from operator namespace"

# --- remote (ssh-target) mode: kubectl runs via ssh on the chosen node, with --context ---
fake_ssh_log="$tmpdir/ssh.log"
cat >"$fakebin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_SSH_LOG:?}"
# argv: -i key <opts...> target kubectl <kubectl args...>; forward everything after 'kubectl'
seen=0
argv=()
for a in "$@"; do
  if [[ $seen -eq 1 ]]; then argv+=("$a"); continue; fi
  [[ "$a" == "kubectl" ]] && seen=1
done
exec kubectl "${argv[@]}"
EOF
chmod +x "$fakebin/ssh"
printf 'k\n' >"$tmpdir/key"

out_remote="$tmpdir/out-remote"
manifest_remote="$tmpdir/manifest-remote.jsonl"
FAKE_SSH_LOG="$fake_ssh_log" FAKE_KUBECTL_MODE=with-toolbox PATH="$fakebin:$PATH" \
  "$BASH_BIN" "$ROOT/lib/collect-cluster-rook.sh" \
  --out "$out_remote" --manifest "$manifest_remote" \
  --namespace rook-ceph --since 24h --timeout 5 \
  --ssh-target tester@node2 --ssh-key "$tmpdir/key" --kube-context lab

assert_file_contains "$out_remote/cluster/rook/pods-wide.txt" "rook-ceph-operator-0"
assert_file_contains "$out_remote/cluster/rook/toolbox-status.txt" "cluster is healthy from toolbox"
grep -qF -- '--context lab' "$fake_ssh_log" || fail "remote kubectl missing --context"
grep -qF 'tester@node2 kubectl' "$fake_ssh_log" || fail "kubectl did not run via ssh on the target node"

# A2 regression: a pod-lookup failure must not abort the collector under set -e
out_oplookup="$tmpdir/out-oplookup"
manifest_oplookup="$tmpdir/manifest-oplookup.jsonl"
oplookup_rc=0
set +e
FAKE_KUBECTL_MODE=op-lookup-fail PATH="$fakebin:$PATH" run_rook_collector "$out_oplookup" "$manifest_oplookup"
oplookup_rc=$?
set -e
[[ "$oplookup_rc" == "0" ]] || fail "operator pod-lookup failure should not abort collector, got $oplookup_rc"
assert_file_contains "$out_oplookup/cluster/rook/operator-SKIPPED.txt" "operator Pod not found"
