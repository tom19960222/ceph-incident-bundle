#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/bundle.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/collect-prometheus.sh"

fakebin="$tmpdir/fakebin"
mkdir -p "$fakebin"
cp "$ROOT/tests/fixtures/bin/curl" "$fakebin/curl"
export FAKE_CURL_LOG="$tmpdir/curl.log"

# Invoke collect_prometheus against a fresh fake workdir. Runs in a subshell
# so the PATH override and knob exports never leak between test cases.
run_prom() {
  local wd=$1
  shift
  mkdir -p "$wd"
  : >"$wd/manifest.jsonl"
  : >"$wd/errors.log"
  : >"$wd/environment.txt"
  (
    PATH="$fakebin:$PATH"
    collect_prometheus --out "$wd" --manifest "$wd/manifest.jsonl" \
      --url http://prom.example:9090 "$@"
  )
}

test_duration_parser() {
  [[ "$(prom_duration_seconds 90)" == "90" ]] || fail "90 -> 90"
  [[ "$(prom_duration_seconds 45s)" == "45" ]] || fail "45s -> 45"
  [[ "$(prom_duration_seconds 30m)" == "1800" ]] || fail "30m -> 1800"
  [[ "$(prom_duration_seconds 24h)" == "86400" ]] || fail "24h -> 86400"
  [[ "$(prom_duration_seconds 7d)" == "604800" ]] || fail "7d -> 604800"
  [[ "$(prom_duration_seconds 2w)" == "1209600" ]] || fail "2w -> 1209600"
  prom_duration_seconds yesterday >/dev/null 2>&1 && fail "'yesterday' should be rejected" || true
  prom_duration_seconds 5x >/dev/null 2>&1 && fail "'5x' should be rejected" || true
  prom_duration_seconds '' >/dev/null 2>&1 && fail "empty should be rejected" || true
  prom_duration_seconds 0 >/dev/null 2>&1 && fail "'0' should be rejected" || true
  [[ "$(prom_duration_seconds 010h)" == "36000" ]] || fail "leading zero must be base-10 (010h -> 36000)"
  [[ "$(prom_duration_seconds 008)" == "8" ]] || fail "008 -> 8 (no octal crash)"
  prom_duration_seconds 000 >/dev/null 2>&1 && fail "'000' should be rejected" || true
}

test_auto_step() {
  [[ "$(prom_auto_step 86400)" == "15" ]] || fail "24h window -> 15s floor"
  [[ "$(prom_auto_step 604800)" == "61" ]] || fail "7d window -> ceil(604800/10000)=61"
  [[ "$(prom_auto_step 60)" == "15" ]] || fail "tiny window -> 15s floor"
}

test_mask_url() {
  [[ "$(prom_mask_url 'http://u:sekrit@h:9090')" == 'http://u:***@h:9090' ]] || fail "credentials should be masked"
  [[ "$(prom_mask_url 'http://h:9090/sub')" == 'http://h:9090/sub' ]] || fail "no-credential URL should pass through"
}

test_require_cmds() {
  local onlycurl="$tmpdir/onlycurl" out
  mkdir -p "$onlycurl"
  printf '#!/bin/sh\nexit 0\n' >"$onlycurl/curl"
  chmod +x "$onlycurl/curl"
  # command -v is a builtin, so a bare restricted PATH is enough here.
  out="$(PATH="$onlycurl" prom_require_cmds 2>&1)" && fail "should fail when python3 is missing" || true
  [[ "$out" == *python3* ]] || fail "reason should name python3, got '$out'"
}

test_happy_path() {
  local wd="$tmpdir/wd-happy" p s e rows
  : >"$FAKE_CURL_LOG"
  run_prom "$wd" --since 24h --timeout 5 || fail "happy path should return 0"
  p="$wd/cluster/prometheus"
  [[ -f "$p/buildinfo.json" ]] || fail "missing buildinfo.json"
  [[ -f "$p/targets.json" ]] || fail "missing targets.json"
  [[ -f "$p/dump-info.txt" ]] || fail "missing dump-info.txt"
  [[ -f "$p/ceph/index.txt" ]] || fail "missing ceph index.txt"
  [[ -f "$p/ceph/ceph_health_status.json.gz" ]] || fail "missing ceph_health_status dump"
  [[ -f "$p/ceph/ceph_osd_up.json.gz" ]] || fail "missing ceph_osd_up dump"
  [[ -f "$p/node-exporter/node_load1.json.gz" ]] || fail "missing node_load1 dump"
  [[ ! -d "$p/grafana" ]] || fail "grafana job must not be collected"
  gzip -dc "$p/ceph/ceph_health_status.json.gz" | grep -qF '"status":"success"' \
    || fail "metric dump is not a success response"
  grep -qF 'step=15' "$FAKE_CURL_LOG" || fail "24h window should query with step=15"
  grep -qF 'ok ceph_health_status ceph_health_status.json.gz' "$p/ceph/index.txt" \
    || fail "index missing ok row"
  s="$(sed -n 's/^window_start_epoch=//p' "$p/dump-info.txt")"
  e="$(sed -n 's/^window_end_epoch=//p' "$p/dump-info.txt")"
  [[ "$((e - s))" == "86400" ]] || fail "window should span 86400s, got $((e - s))"
  rows="$(grep -c '"collector":"collect-prometheus"' "$wd/manifest.jsonl")"
  [[ "$rows" == "4" ]] || fail "expected 4 manifest rows (buildinfo/targets/2 jobs), got $rows"
  grep -qF 'prom_url=http://prom.example:9090' "$wd/environment.txt" \
    || fail "environment.txt missing prom_url"
  grep -qF 'prom_jobs=ceph node-exporter' "$wd/environment.txt" \
    || fail "environment.txt missing prom_jobs"
}

test_unreachable() {
  local wd="$tmpdir/wd-down" rc=0
  ( export FAKE_CURL_DOWN=1; run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "unreachable prometheus should return 2, got $rc"
  grep -qF 'not reachable' "$wd/cluster/prometheus/SKIPPED.txt" || fail "SKIPPED should say not reachable"
  grep -qF 'prometheus' "$wd/errors.log" || fail "errors.log should record the skip"
}

test_no_matching_jobs() {
  local wd="$tmpdir/wd-nojobs" rc=0
  run_prom "$wd" --since 24h --job-regex 'zzz' || rc=$?
  [[ "$rc" == "2" ]] || fail "no matching jobs should return 2, got $rc"
  grep -qF 'no scrape job matched' "$wd/cluster/prometheus/SKIPPED.txt" || fail "SKIPPED reason wrong"
  grep -qF 'grafana' "$wd/cluster/prometheus/SKIPPED.txt" || fail "SKIPPED should list the jobs seen"
}

test_missing_python3() {
  # Restricted PATH that still provides what the pre-check code path needs
  # (mkdir/date/dirname for prom_skip + errors.log), but no python3.
  local wd="$tmpdir/wd-nopy" bin="$tmpdir/nopybin" rc=0 c
  mkdir -p "$bin" "$wd"
  : >"$wd/manifest.jsonl"
  : >"$wd/errors.log"
  printf '#!/bin/sh\nexit 0\n' >"$bin/curl"
  chmod +x "$bin/curl"
  for c in mkdir date dirname; do
    ln -s "$(command -v "$c")" "$bin/$c"
  done
  # shellcheck disable=SC2030,SC2031 # export intentionally scoped to subshell
  ( PATH="$bin"
    collect_prometheus --out "$wd" --manifest "$wd/manifest.jsonl" \
      --url http://prom.example:9090 --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "missing python3 should return 2, got $rc"
  grep -qF 'python3 not found' "$wd/cluster/prometheus/SKIPPED.txt" || fail "SKIPPED should name python3"
}

test_single_metric_failure() {
  local wd="$tmpdir/wd-onefail" rc=0 p
  ( export FAKE_CURL_FAIL_METRICS='ceph_osd_up'; run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "metric failure should return 2, got $rc"
  p="$wd/cluster/prometheus"
  [[ -f "$p/ceph/ceph_health_status.json.gz" ]] || fail "other metrics should still be dumped"
  [[ ! -f "$p/ceph/ceph_osd_up.json.gz" ]] || fail "failed metric must not leave a dump"
  grep -qF 'failed ceph_osd_up' "$p/ceph/index.txt" || fail "index should mark the failure"
  grep -qF 'ceph_osd_up' "$wd/errors.log" || fail "errors.log should record the metric failure"
}

test_budget_truncation() {
  local wd="$tmpdir/wd-budget" rc=0
  run_prom "$wd" --since 24h --budget 0 || rc=$?
  [[ "$rc" == "2" ]] || fail "budget truncation should return 2, got $rc"
  grep -qF 'TRUNCATED' "$wd/cluster/prometheus/ceph/index.txt" || fail "index should mark TRUNCATED"
  grep -qF 'truncated=1' "$wd/cluster/prometheus/dump-info.txt" || fail "dump-info should mark truncated"
  grep -qF 'truncated' "$wd/errors.log" || fail "errors.log should record the truncation"
}

test_unsafe_job_name() {
  local wd="$tmpdir/wd-badjob" rc=0
  ( export FAKE_CURL_JOBS_JSON='{"status":"success","data":["ceph","node\"x"]}'
    run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "unsafe job name should be partial (2), got $rc"
  grep -qF 'unsafe name' "$wd/errors.log" || fail "errors.log should record the unsafe job"
  [[ -f "$wd/cluster/prometheus/ceph/index.txt" ]] || fail "safe job should still be collected"
}

test_long_window_step() {
  local wd="$tmpdir/wd-7d"
  : >"$FAKE_CURL_LOG"
  run_prom "$wd" --since 7d || fail "7d dump should succeed"
  grep -qF 'step=61' "$FAKE_CURL_LOG" || fail "7d window should query with step=61"
}

test_redaction_excludes_metric_dumps() {
  local wd="$tmpdir/wd-redact"
  local pdir="$wd/cluster/prometheus/ceph"
  mkdir -p "$pdir" "$wd/nodes"
  # this line WOULD be redacted (key = base64-ish) if the redactor visited it
  printf 'key = AQBSOMETHINGLONGENOUGHTOTRIGGERBASE64REDACTIONXX==\n' | gzip -c >"$pdir/ceph_fake.json.gz"
  printf 'password = hunter2\n' >"$wd/cluster/prometheus/dump-info.txt"
  redact_bundle_text "$wd"
  gzip -dc "$pdir/ceph_fake.json.gz" | grep -qF 'AQBSOMETHING' \
    || fail "per-metric dump must NOT be redacted"
  grep -qF '[REDACTED]' "$wd/cluster/prometheus/dump-info.txt" \
    || fail "dump-info.txt must still be redacted"
  # anchoring: a look-alike path under nodes/ must NOT be excluded
  local ndir="$wd/nodes/hostA/deep/cluster/prometheus/jobx"
  mkdir -p "$ndir"
  printf 'key = AQBSOMETHINGLONGENOUGHTOTRIGGERBASE64REDACTIONXX==\n' | gzip -c >"$ndir/evil.json.gz"
  redact_bundle_text "$wd"
  gzip -dc "$ndir/evil.json.gz" | grep -qF '[REDACTED]' \
    || fail "look-alike path under nodes/ must still be redacted"
}

test_targets_failure() {
  local wd="$tmpdir/wd-targets" rc=0 p
  # shellcheck disable=SC2030,SC2031 # export intentionally scoped to subshell
  ( export FAKE_CURL_FAIL_PATHS='/api/v1/targets'; run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "targets failure should return 2, got $rc"
  p="$wd/cluster/prometheus"
  [[ ! -f "$p/targets.json" ]] || fail "failed targets fetch must not leave targets.json"
  [[ -f "$p/buildinfo.json" ]] || fail "buildinfo should still be collected"
  [[ -f "$p/ceph/ceph_health_status.json.gz" ]] || fail "metric dumps should still proceed"
  grep -qF 'targets fetch failed' "$wd/errors.log" || fail "errors.log should record targets failure"
}

test_job_listing_failure() {
  local wd="$tmpdir/wd-joblist" rc=0
  # shellcheck disable=SC2030,SC2031 # export intentionally scoped to subshell
  ( export FAKE_CURL_FAIL_PATHS='/api/v1/label/job/values'; run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "job listing failure should return 2, got $rc"
  grep -qF 'job listing failed' "$wd/cluster/prometheus/SKIPPED.txt" || fail "SKIPPED should say job listing failed"
}

test_metric_listing_failure() {
  local wd="$tmpdir/wd-namelist" rc=0 p
  # shellcheck disable=SC2030,SC2031 # export intentionally scoped to subshell
  ( export FAKE_CURL_FAIL_PATHS='/api/v1/label/__name__/values'; run_prom "$wd" --since 24h ) || rc=$?
  [[ "$rc" == "2" ]] || fail "metric listing failure should return 2, got $rc"
  p="$wd/cluster/prometheus"
  grep -qF 'FAILED: metric listing for job ceph' "$p/ceph/index.txt" || fail "index should record listing failure"
  grep -qF 'metric listing failed for job ceph' "$wd/errors.log" || fail "errors.log should record listing failure"
}

test_trailing_slash_url() {
  local wd="$tmpdir/wd-slash"
  : >"$FAKE_CURL_LOG"
  mkdir -p "$wd"
  : >"$wd/manifest.jsonl"
  : >"$wd/errors.log"
  : >"$wd/environment.txt"
  # shellcheck disable=SC2030,SC2031 # export intentionally scoped to subshell
  ( PATH="$fakebin:$PATH"
    collect_prometheus --out "$wd" --manifest "$wd/manifest.jsonl" \
      --url http://prom.example:9090/ --since 24h ) || fail "trailing-slash URL should still work"
  grep -qF '9090//api' "$FAKE_CURL_LOG" && fail "double slash must not appear in request URLs" || true
}

test_dash_leading_job_regex() {
  local wd="$tmpdir/wd-dashre" rc=0 err
  err="$( run_prom "$wd" --since 24h --job-regex '-zzz' 2>&1 >/dev/null )" || rc=$?
  [[ "$rc" == "2" ]] || fail "dash-leading regex with no match should return 2, got $rc"
  case "$err" in *"grep:"*) fail "grep must not treat the regex as options: $err" ;; esac
}

test_duration_parser
test_auto_step
test_mask_url
test_require_cmds
test_happy_path
test_unreachable
test_no_matching_jobs
test_missing_python3
test_single_metric_failure
test_budget_truncation
test_unsafe_job_name
test_long_window_step
test_redaction_excludes_metric_dumps
test_targets_failure
test_job_listing_failure
test_metric_listing_failure
test_trailing_slash_url
test_dash_leading_job_regex

printf 'ok: prom collector\n'
