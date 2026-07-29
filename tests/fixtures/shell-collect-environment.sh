#!/usr/bin/env bash

# Shared offline fake environment for public shell Collect process tests.
# Callers own the supplied test root and are responsible for cleaning it.
# The function sets fakebin, ssh_key, and inventory in caller scope, and exports
# FAKE_SSH_LOG, FAKE_TIMEOUT_LOG, and FIXTURE_SSH for the generated commands.
setup_shell_collect_environment() {
  local repo_root=$1 test_root=$2

  fakebin="$test_root/fakebin"
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
  "get pods -n rook-ceph -l app=rook-ceph-tools -o name")
    if [[ "${FAKE_KUBE_TOOLS_POD:-}" == "1" ]]; then
      printf 'pod/rook-ceph-tools-0\n'
    fi
    ;;
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

target=''; j=0
# The target is the first argument after the -i KEY / -o OPT option pairs.
while [[ $j -lt $n ]]; do
  case "${args[$j]}" in
    -i|-o) j=$((j + 2)) ;;
    *) target="${args[$j]}"; break ;;
  esac
done

if [[ "$whole" == *"-vvv"* ]]; then
  printf 'debug1: fake ssh verbose log for %s\n' "$target" >&2
  printf 'debug3: fake ssh argv %s\n' "$whole" >&2
  exit 255
fi
for t in ${FAKE_SSH_CONNECT_FAIL_TARGETS:-}; do
  [[ "$target" == *"$t"* ]] && { printf 'ssh: connect to host %s port 22: Connection refused\n' "$target" >&2; exit 255; }
done

# Order matters: the capability probe also contains "kubectl", and the runner
# connectivity probe must be matched before the generic Ceph command branch.
case "$whole" in
  *"--connect-timeout 5 -s"*)
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
    alias_name="$(printf '%s\n' "$whole" | sed -n "s/.*--host-alias '\([^']*\)'.*/\1/p")"
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
    exec "$FIXTURE_SSH" "$@"
    ;;
  *)
    printf 'unexpected ssh remote: %s\n' "$whole" >&2
    exit 99
    ;;
esac
EOF

  chmod +x "$fakebin/kubectl" "$fakebin/ssh" "$fakebin/timeout"

  ssh_key="$test_root/id_ed25519"
  printf 'fake key\n' >"$ssh_key"
  export FAKE_SSH_LOG="$test_root/ssh.log"
  export FAKE_TIMEOUT_LOG="$test_root/timeout.log"
  export FIXTURE_SSH="$repo_root/tests/fixtures/bin/ssh"

  inventory="$test_root/inv-external.env"
  cat >"$inventory" <<'EOF'
SSH_USER="tester"
ROOK_NAMESPACE="rook-ceph"
HOSTS=(
  "cephnode=10.0.0.1"
  "kubenode=10.0.0.9"
)
EOF
}
