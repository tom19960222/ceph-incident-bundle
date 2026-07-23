#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok: %s\n' "$*"; }

run_and_capture() {
  local output status
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  printf '%s\n%s' "$status" "$output"
}

for path in \
  "$ROOT/run/collect.sh" \
  "$ROOT/lib/common.sh" \
  "$ROOT/lib/collect-cluster-cephadm.sh" \
  "$ROOT/lib/collect-cluster-rook.sh" \
  "$ROOT/lib/collect-node.sh" \
  "$ROOT/lib/collect-prometheus.sh" \
  "$ROOT/lib/verify-bundle.sh" \
  "$ROOT/tests/test-collect.sh" \
  "$ROOT/tests/test-cephadm-collector.sh" \
  "$ROOT/tests/test-node-collector.sh" \
  "$ROOT/tests/test-rook-collector.sh" \
  "$ROOT/tests/test-prom-collector.sh" \
  "$ROOT/tests/test-verify-bundle.sh"; do
  [[ -f "$path" ]] || fail "missing $path"
done

for path in "$ROOT/run/collect.sh" "$ROOT/lib/verify-bundle.sh"; do
  [[ -x "$path" ]] || fail "not executable $path"
done

collect_no_args="$(run_and_capture "$ROOT/run/collect.sh")"
collect_no_args_status="${collect_no_args%%$'\n'*}"
collect_no_args_output="${collect_no_args#*$'\n'}"
[[ "$collect_no_args_status" == "1" ]] || fail "collect.sh no args should exit 1, got $collect_no_args_status"
[[ "$collect_no_args_output" == *"Usage:"* ]] || fail "collect.sh no args should print usage"

verify_no_args="$(run_and_capture "$ROOT/lib/verify-bundle.sh")"
verify_no_args_status="${verify_no_args%%$'\n'*}"
verify_no_args_output="${verify_no_args#*$'\n'}"
[[ "$verify_no_args_status" == "1" ]] || fail "verify-bundle.sh no args should exit 1, got $verify_no_args_status"
[[ "$verify_no_args_output" == *"Usage:"* ]] || fail "verify-bundle.sh no args should print usage"

verify_placeholder_args="$(run_and_capture "$ROOT/lib/verify-bundle.sh" /tmp/definitely-not-a-bundle)"
verify_placeholder_status="${verify_placeholder_args%%$'\n'*}"
verify_placeholder_output="${verify_placeholder_args#*$'\n'}"
[[ "$verify_placeholder_status" != "0" ]] || fail "verify-bundle.sh placeholder args should not exit 0"
[[ "$verify_placeholder_output" == *"VERIFY FAIL:"* || "$verify_placeholder_output" == *"Usage:"* || "$verify_placeholder_output" == *"error"* ]] || fail "verify-bundle.sh placeholder args should explain failure"

collect_placeholder_args="$(run_and_capture "$ROOT/run/collect.sh" --inventory /tmp/example.env --ssh-key /tmp/id_ed25519 --seed 192.168.18.166)"
collect_placeholder_status="${collect_placeholder_args%%$'\n'*}"
collect_placeholder_output="${collect_placeholder_args#*$'\n'}"
[[ "$collect_placeholder_status" != "0" ]] || fail "collect.sh placeholder args should not exit 0"
[[ "$collect_placeholder_output" == *"missing inventory"* || "$collect_placeholder_output" == *"Usage:"* || "$collect_placeholder_output" == *"error"* ]] || fail "collect.sh placeholder args should explain failure"

common_helpers_args="$(run_and_capture "$ROOT/tests/test-common.sh")"
common_helpers_status="${common_helpers_args%%$'\n'*}"
common_helpers_output="${common_helpers_args#*$'\n'}"
[[ "$common_helpers_status" == "0" ]] || fail "test-common.sh failed: $common_helpers_output"

verify_bundle_args="$(run_and_capture "$ROOT/tests/test-verify-bundle.sh")"
verify_bundle_status="${verify_bundle_args%%$'\n'*}"
verify_bundle_output="${verify_bundle_args#*$'\n'}"
[[ "$verify_bundle_status" == "0" ]] || fail "test-verify-bundle.sh failed: $verify_bundle_output"

cephadm_collector_args="$(run_and_capture "$ROOT/tests/test-cephadm-collector.sh")"
cephadm_collector_status="${cephadm_collector_args%%$'\n'*}"
cephadm_collector_output="${cephadm_collector_args#*$'\n'}"
[[ "$cephadm_collector_status" == "0" ]] || fail "test-cephadm-collector.sh failed: $cephadm_collector_output"

node_collector_args="$(run_and_capture "$ROOT/tests/test-node-collector.sh")"
node_collector_status="${node_collector_args%%$'\n'*}"
node_collector_output="${node_collector_args#*$'\n'}"
[[ "$node_collector_status" == "0" ]] || fail "test-node-collector.sh failed: $node_collector_output"

rook_collector_args="$(run_and_capture "$ROOT/tests/test-rook-collector.sh")"
rook_collector_status="${rook_collector_args%%$'\n'*}"
rook_collector_output="${rook_collector_args#*$'\n'}"
[[ "$rook_collector_status" == "0" ]] || fail "test-rook-collector.sh failed: $rook_collector_output"

prom_collector_args="$(run_and_capture "$ROOT/tests/test-prom-collector.sh")"
prom_collector_status="${prom_collector_args%%$'\n'*}"
prom_collector_output="${prom_collector_args#*$'\n'}"
[[ "$prom_collector_status" == "0" ]] || fail "test-prom-collector.sh failed: $prom_collector_output"

collect_args="$(run_and_capture "$ROOT/tests/test-collect.sh")"
collect_status="${collect_args%%$'\n'*}"
collect_output="${collect_args#*$'\n'}"
[[ "$collect_status" == "0" ]] || fail "test-collect.sh failed: $collect_output"

ok "required files exist"
