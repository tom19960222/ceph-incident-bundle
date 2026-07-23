#!/usr/bin/env bash
set -euo pipefail

# Ceph incident bundle verification entrypoint.

usage() {
  cat >&2 <<'EOF'
Usage: verify-bundle.sh <bundle-dir|bundle.tar.gz>

Checks gzip integrity, required top-level files, cluster/node artifacts,
and obvious secret paths such as keyring, .ssh, id_ed25519, private_key.
EOF
}

verify_fail() {
  printf 'VERIFY FAIL: %s\n' "$*" >&2
  return 1
}

verify_members() {
  local root=$1 path

  # -print0 so newlines in filenames cannot smuggle a forbidden component
  # past the glob (this is a security boundary).
  while IFS= read -r -d '' path; do
    case "$path" in
      *keyring*|*.ssh*|*id_ed25519*|*private_key*|*.pem|*.key|*.crt|*.pfx|*.p12)
        verify_fail "forbidden path: ${path#./}"
        return 1
        ;;
    esac
  done < <(cd "$root" && find . -mindepth 1 -print0)
}

verify_no_secret_content() {
  # Defense-in-depth: even if redaction missed something, refuse to bless a
  # bundle whose files still contain obvious unredacted key material.
  local root=$1 hit
  hit="$(grep -rlIE -e '-----BEGIN[ A-Za-z]*PRIVATE KEY-----' -e '^[[:space:]]*key[[:space:]]*=[[:space:]]*[A-Za-z0-9+/]{20,}={0,2}' "$root" 2>/dev/null | head -n1 || true)"
  if [[ -n "$hit" ]]; then
    verify_fail "unredacted PRIVATE KEY / key material in: ${hit#"$root"/}"
    return 1
  fi
}

verify_required_files() {
  local root=$1
  local required

  for required in manifest.jsonl summary.txt README-FIRST.txt; do
    [[ -f "$root/$required" ]] || {
      verify_fail "missing required file: $required"
      return 1
    }
  done
}

verify_required_artifacts() {
  local root=$1 cluster_artifact nodes_artifact

  cluster_artifact="$(find "$root/cluster" -type f -print -quit 2>/dev/null || true)"
  [[ -n "$cluster_artifact" ]] || {
    verify_fail "missing cluster/ artifact"
    return 1
  }

  nodes_artifact="$(find "$root/nodes" -type f -print -quit 2>/dev/null || true)"
  [[ -n "$nodes_artifact" ]] || {
    verify_fail "missing nodes/ artifact"
    return 1
  }
}

verify_bundle_tree() {
  local root=$1

  verify_members "$root" || return 1
  verify_no_secret_content "$root" || return 1
  verify_required_files "$root" || return 1
  verify_required_artifacts "$root" || return 1
}

verify_bundle_path() {
  local bundle=$1 workdir extracted_root

  if [[ -d "$bundle" ]]; then
    verify_bundle_tree "$bundle" || return 1
    printf 'VERIFY PASS: %s\n' "$bundle"
    return 0
  fi

  [[ -f "$bundle" && "$bundle" == *.tar.gz ]] || { verify_fail "expected a directory or .tar.gz bundle: $bundle"; return 1; }

  workdir="$(mktemp -d)"
  if ! tar -tzf "$bundle" >/dev/null 2>/dev/null; then
    verify_fail "invalid archive: $bundle"
    rm -rf "$workdir"
    return 1
  fi
  if ! tar -xzf "$bundle" -C "$workdir" >/dev/null 2>/dev/null; then
    verify_fail "invalid archive: $bundle"
    rm -rf "$workdir"
    return 1
  fi

  extracted_root="$workdir"
  if ! verify_bundle_tree "$extracted_root"; then
    rm -rf "$workdir"
    return 1
  fi
  rm -rf "$workdir"
  printf 'VERIFY PASS: %s\n' "$bundle"
}

main() {
  if [[ $# -ne 1 ]]; then
    usage
    return 1
  fi

  verify_bundle_path "$1"
}

main "$@"
