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

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

make_valid_bundle_dir() {
  local dir=$1
  mkdir -p \
    "$dir/cluster/ceph" \
    "$dir/nodes/monitor01/system"

  cat >"$dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
  printf 'summary\n' >"$dir/summary.txt"
  printf 'read me first\n' >"$dir/README-FIRST.txt"
  printf 'ok\n' >"$dir/cluster/ceph/status.txt"
  printf 'monitor01\n' >"$dir/nodes/monitor01/system/hostname.txt"
}

make_bundle_archive() {
  local source_dir=$1 archive_path=$2
  tar -C "$source_dir" -czf "$archive_path" .
}

assert_pass() {
  local target=$1
  local result status output

  result="$(run_and_capture "$ROOT/lib/verify-bundle.sh" "$target")"
  status="${result%%$'\n'*}"
  output="${result#*$'\n'}"
  [[ "$status" == "0" ]] || fail "expected success for $target, got status $status: $output"
  [[ "$output" == "VERIFY PASS: $target" ]] || fail "unexpected pass output: $output"
}

assert_fail() {
  local target=$1 expected=$2
  local result status output

  result="$(run_and_capture "$ROOT/lib/verify-bundle.sh" "$target")"
  status="${result%%$'\n'*}"
  output="${result#*$'\n'}"
  [[ "$status" != "0" ]] || fail "expected failure for $target"
  [[ "$output" == *"$expected"* ]] || fail "failure output did not mention '$expected': $output"
}

valid_dir="$tmpdir/valid-dir"
mkdir -p "$valid_dir"
make_valid_bundle_dir "$valid_dir"

valid_archive="$tmpdir/valid-bundle.tar.gz"
make_bundle_archive "$valid_dir" "$valid_archive"

missing_manifest_dir="$tmpdir/missing-manifest"
mkdir -p "$missing_manifest_dir"
make_valid_bundle_dir "$missing_manifest_dir"
rm -f "$missing_manifest_dir/manifest.jsonl"
missing_manifest_archive="$tmpdir/missing-manifest.tar.gz"
make_bundle_archive "$missing_manifest_dir" "$missing_manifest_archive"

keyring_dir="$tmpdir/keyring-bundle"
mkdir -p "$keyring_dir/cluster/ceph" "$keyring_dir/nodes/node01/system"
cat >"$keyring_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$keyring_dir/summary.txt"
printf 'read me first\n' >"$keyring_dir/README-FIRST.txt"
printf 'secret\n' >"$keyring_dir/cluster/ceph/keyring"
printf 'node01\n' >"$keyring_dir/nodes/node01/system/hostname.txt"
keyring_archive="$tmpdir/keyring-bundle.tar.gz"
make_bundle_archive "$keyring_dir" "$keyring_archive"

ssh_dir="$tmpdir/ssh-bundle"
mkdir -p "$ssh_dir/cluster/ceph" "$ssh_dir/nodes/node01/.ssh"
cat >"$ssh_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$ssh_dir/summary.txt"
printf 'read me first\n' >"$ssh_dir/README-FIRST.txt"
printf 'ok\n' >"$ssh_dir/cluster/ceph/status.txt"
printf 'secret\n' >"$ssh_dir/nodes/node01/.ssh/id_ed25519"
ssh_archive="$tmpdir/ssh-bundle.tar.gz"
make_bundle_archive "$ssh_dir" "$ssh_archive"

id_ed25519_dir="$tmpdir/id-ed25519-bundle"
mkdir -p "$id_ed25519_dir/cluster/ceph" "$id_ed25519_dir/nodes/node01/system"
cat >"$id_ed25519_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$id_ed25519_dir/summary.txt"
printf 'read me first\n' >"$id_ed25519_dir/README-FIRST.txt"
printf 'ok\n' >"$id_ed25519_dir/cluster/ceph/id_ed25519"
printf 'node01\n' >"$id_ed25519_dir/nodes/node01/system/hostname.txt"
id_ed25519_archive="$tmpdir/id-ed25519-bundle.tar.gz"
make_bundle_archive "$id_ed25519_dir" "$id_ed25519_archive"

private_key_dir="$tmpdir/private-key-bundle"
mkdir -p "$private_key_dir/cluster/ceph" "$private_key_dir/nodes/node01/system"
cat >"$private_key_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$private_key_dir/summary.txt"
printf 'read me first\n' >"$private_key_dir/README-FIRST.txt"
printf 'secret\n' >"$private_key_dir/cluster/ceph/private_key"
printf 'node01\n' >"$private_key_dir/nodes/node01/system/hostname.txt"
private_key_archive="$tmpdir/private-key-bundle.tar.gz"
make_bundle_archive "$private_key_dir" "$private_key_archive"

pem_dir="$tmpdir/pem-bundle"
mkdir -p "$pem_dir/cluster/ceph" "$pem_dir/nodes/node01/system"
cat >"$pem_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$pem_dir/summary.txt"
printf 'read me first\n' >"$pem_dir/README-FIRST.txt"
printf 'ok\n' >"$pem_dir/cluster/ceph/status.txt"
printf 'node01\n' >"$pem_dir/nodes/node01/system/hostname.txt"
printf 'cert material\n' >"$pem_dir/nodes/node01/system/tls.pem"
pem_archive="$tmpdir/pem-bundle.tar.gz"
make_bundle_archive "$pem_dir" "$pem_archive"

content_key_dir="$tmpdir/content-key-bundle"
mkdir -p "$content_key_dir/cluster/ceph" "$content_key_dir/nodes/node01/system"
cat >"$content_key_dir/manifest.jsonl" <<'EOF'
{"bundle":"ceph-incident"}
EOF
printf 'summary\n' >"$content_key_dir/summary.txt"
printf 'read me first\n' >"$content_key_dir/README-FIRST.txt"
printf 'node01\n' >"$content_key_dir/nodes/node01/system/hostname.txt"
# an un-redacted PEM body slipped into an allowed extension
{
  printf 'some log preamble\n'
  printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\n'
  printf 'b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n'
  printf -- '-----END OPENSSH PRIVATE KEY-----\n'
} >"$content_key_dir/cluster/ceph/leak.txt"
content_key_archive="$tmpdir/content-key-bundle.tar.gz"
make_bundle_archive "$content_key_dir" "$content_key_archive"

corrupt_archive="$tmpdir/corrupt-bundle.tar.gz"
printf 'not a tar.gz\n' >"$corrupt_archive"

assert_pass "$valid_dir"
assert_pass "$valid_archive"
assert_fail "$missing_manifest_dir" "manifest.jsonl"
assert_fail "$missing_manifest_archive" "manifest.jsonl"
assert_fail "$keyring_dir" "keyring"
assert_fail "$keyring_archive" "keyring"
assert_fail "$ssh_dir" ".ssh"
assert_fail "$ssh_archive" ".ssh"
assert_fail "$id_ed25519_dir" "id_ed25519"
assert_fail "$id_ed25519_archive" "id_ed25519"
assert_fail "$private_key_dir" "private_key"
assert_fail "$private_key_archive" "private_key"
assert_fail "$pem_dir" "tls.pem"
assert_fail "$pem_archive" "tls.pem"
assert_fail "$content_key_dir" "PRIVATE KEY"
assert_fail "$content_key_archive" "PRIVATE KEY"
assert_fail "$corrupt_archive" "invalid archive"

extra_args_result="$(run_and_capture "$ROOT/lib/verify-bundle.sh" "$valid_dir" extra)"
extra_args_status="${extra_args_result%%$'\n'*}"
extra_args_output="${extra_args_result#*$'\n'}"
[[ "$extra_args_status" != "0" ]] || fail "expected failure for extra args"
[[ "$extra_args_output" == *"Usage:"* ]] || fail "extra args should print usage: $extra_args_output"
