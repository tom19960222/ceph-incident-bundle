#!/usr/bin/env bash
set -euo pipefail

# Collect Prometheus metrics evidence (optional layer). Runs curl on the
# workstation against a user-supplied Prometheus base URL, dumping every
# metric of the matching scrape jobs over the --since window.

PROM_COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PROM_COLLECTOR_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: collect-prometheus.sh --out DIR --manifest PATH --url URL
       [--job-regex RE] [--step SECONDS] [--since DURATION]
       [--timeout SECONDS] [--budget SECONDS]
EOF
}

# Parse a --since style duration into seconds: N (seconds) or N{s,m,h,d,w}.
# Rejects 0 and anything non-matching.
prom_duration_seconds() {
  local value=$1 n unit
  local re='^([0-9]+)([smhdw]?)$'
  [[ $value =~ $re ]] || return 1
  n="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]}"
  # Normalize to base-10 immediately to avoid octal interpretation
  n=$((10#$n))
  [[ "$n" -gt 0 ]] || return 1
  case "$unit" in
    ''|s) : ;;
    m) n=$((n * 60)) ;;
    h) n=$((n * 3600)) ;;
    d) n=$((n * 86400)) ;;
    w) n=$((n * 604800)) ;;
  esac
  printf '%s' "$n"
}

# Auto query_range step: smallest step >= 15s that keeps points per series
# under Prometheus's 11,000-point query_range limit (ceil(window/10000)).
prom_auto_step() {
  local window=$1 step
  step=$(((window + 9999) / 10000))
  [[ $step -ge 15 ]] || step=15
  printf '%s' "$step"
}

# Mask embedded basic-auth credentials in a URL before it lands in artifacts.
prom_mask_url() {
  local url=$1
  local re='^([A-Za-z][A-Za-z0-9+.-]*://)([^/@]+)@(.*)$'
  if [[ $url =~ $re ]]; then
    local cred=${BASH_REMATCH[2]}
    printf '%s%s:***@%s' "${BASH_REMATCH[1]}" "${cred%%:*}" "${BASH_REMATCH[3]}"
  else
    printf '%s' "$url"
  fi
}

# Workstation dependencies for this layer. Prints the missing dependency
# (used verbatim as the SKIPPED reason) and returns 1.
prom_require_cmds() {
  local c
  for c in curl python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
      printf '%s not found on this workstation' "$c"
      return 1
    fi
  done
}

# Epoch -> UTC ISO timestamp. BSD date takes -r EPOCH; GNU date's -r means
# file-mtime (fails on a bare number) so it falls through to -d @EPOCH.
prom_epoch_utc() {
  date -u -r "$1" +%FT%TZ 2>/dev/null || date -u -d "@$1" +%FT%TZ
}

# GET BASE+PATH into OUTFILE. Extra args become --data-urlencode params (so
# PromQL matchers never need manual URL-encoding). Caller captures stderr.
prom_curl() {
  local base=$1 path=$2 outfile=$3 timeout=$4
  shift 4
  local -a cmd
  local p
  cmd=(curl -fsS -G --connect-timeout "$timeout" --max-time "$timeout" -o "$outfile" "$base$path")
  for p in "$@"; do
    cmd+=(--data-urlencode "$p")
  done
  "${cmd[@]}"
}

# Print each string in a Prometheus label-values response's data[] array,
# one per line. Fails if the file is not parseable status=success JSON.
prom_json_data_values() {
  local file=$1
  python3 - "$file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        doc = json.load(f)
except Exception:
    sys.exit(1)
if doc.get("status") != "success":
    sys.exit(1)
for value in doc.get("data", []):
    print(value)
PYEOF
}

prom_error() {
  local outdir=$1 message=$2
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$message" >>"$outdir/errors.log"
}

prom_skip() {
  local outdir=$1 reason=$2
  write_skip_artifact "$outdir/cluster/prometheus/SKIPPED.txt" "$reason"
  prom_error "$outdir" "prometheus dump skipped: $reason"
}

collect_prometheus() {
  local outdir='' manifest='' url='' job_regex='ceph|node' step='' since=24h timeout=20 budget=600

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out) outdir=${2-}; shift 2 ;;
      --manifest) manifest=${2-}; shift 2 ;;
      --url) url=${2-}; shift 2 ;;
      --job-regex) job_regex=${2-}; shift 2 ;;
      --step) step=${2-}; shift 2 ;;
      --since) since=${2-}; shift 2 ;;
      --timeout) timeout=${2-}; shift 2 ;;
      --budget) budget=${2-}; shift 2 ;;
      --help|-h) usage; return 0 ;;
      *) usage >&2; return 1 ;;
    esac
  done
  [[ -n "$outdir" && -n "$manifest" && -n "$url" ]] || { usage >&2; return 1; }

  # tolerate operator-pasted trailing slashes; path segments below add their own /
  while [[ "$url" == */ ]]; do url="${url%/}"; done

  local promdir="$outdir/cluster/prometheus"
  local masked_url window start_epoch end_epoch deadline missing
  masked_url="$(prom_mask_url "$url")"

  if ! window="$(prom_duration_seconds "$since")"; then
    log "invalid --since for prometheus dump (want N/Ns/Nm/Nh/Nd/Nw): $since"
    return 1
  fi

  if ! missing="$(prom_require_cmds)"; then
    prom_skip "$outdir" "$missing"
    return 2
  fi

  ensure_dir "$promdir"
  end_epoch="$(date -u +%s)"
  start_epoch=$((end_epoch - window))
  [[ -n "$step" ]] || step="$(prom_auto_step "$window")"
  deadline=$((SECONDS + budget))

  # Raw JSON artifacts are written by curl -o directly — NOT via run_capture,
  # whose "# host:" header lines would corrupt the JSON — so each phase does
  # its own manifest_add.
  local started ended detail rc failed=0

  # buildinfo doubles as the connectivity probe.
  started="$(date -u +%FT%TZ)"
  detail="$(prom_curl "$url" /api/v1/status/buildinfo "$promdir/buildinfo.json" "$timeout" 2>&1)" && rc=0 || rc=$?
  if [[ $rc -ne 0 ]]; then
    rm -f -- "$promdir/buildinfo.json"
    prom_skip "$outdir" "prometheus not reachable: $masked_url (curl exit $rc: $detail)"
    return 2
  fi
  ended="$(date -u +%FT%TZ)"
  manifest_add "$manifest" prometheus collect-prometheus "$promdir/buildinfo.json" \
    "GET $masked_url/api/v1/status/buildinfo" 0 "$started" "$ended"

  started="$(date -u +%FT%TZ)"
  detail="$(prom_curl "$url" /api/v1/targets "$promdir/targets.json" "$timeout" 2>&1)" && rc=0 || rc=$?
  if [[ $rc -ne 0 ]]; then
    rm -f -- "$promdir/targets.json"
    prom_error "$outdir" "prometheus targets fetch failed (curl exit $rc): $detail"
    failed=1
  fi
  ended="$(date -u +%FT%TZ)"
  manifest_add "$manifest" prometheus collect-prometheus "$promdir/targets.json" \
    "GET $masked_url/api/v1/targets" "$rc" "$started" "$ended"

  # Enumerate scrape jobs; the user-facing contract is "find metrics by
  # exporter (job) name", so filtering happens on job labels, not metric names.
  local jobs_file="$promdir/.jobs.json" job_list job
  local jobs_seen_str='' jobs_matched_str=''
  local -a jobs_matched
  jobs_matched=()
  detail="$(prom_curl "$url" /api/v1/label/job/values "$jobs_file" "$timeout" 2>&1)" && rc=0 || rc=$?
  if [[ $rc -ne 0 ]] || ! job_list="$(prom_json_data_values "$jobs_file")"; then
    rm -f -- "$jobs_file"
    prom_skip "$outdir" "prometheus job listing failed (curl exit $rc): ${detail:-unparseable JSON}"
    return 2
  fi
  rm -f -- "$jobs_file"
  while IFS= read -r job; do
    [[ -n "$job" ]] || continue
    jobs_seen_str="${jobs_seen_str:+$jobs_seen_str }$job"
    if printf '%s' "$job" | grep -qiE -- "$job_regex"; then
      # shellcheck disable=SC1003 # literal backslash-in-glob pattern below, not an escaped quote
      case "$job" in
        *'"'*|*'\'*)
          # cannot be interpolated into a PromQL matcher safely
          prom_error "$outdir" "prometheus job skipped (unsafe name): $job"
          failed=1
          ;;
        *)
          jobs_matched+=("$job")
          jobs_matched_str="${jobs_matched_str:+$jobs_matched_str }$job"
          ;;
      esac
    fi
  done <<<"$job_list"

  if [[ ${#jobs_matched[@]} -eq 0 ]]; then
    prom_skip "$outdir" "no scrape job matched regex '$job_regex' (jobs seen: ${jobs_seen_str:-<none>})"
    return 2
  fi

  local truncated=0 metrics_ok=0 metrics_failed=0
  local metric_re='^[a-zA-Z_:][a-zA-Z0-9_:]*$'
  local safe_job jobdir index names_file name_list metric file job_rc n_metrics
  for job in "${jobs_matched[@]+"${jobs_matched[@]}"}"; do
    if [[ $truncated -eq 1 ]]; then
      break
    fi
    safe_job="$(ssh_debug_safe_name "$job")"
    jobdir="$promdir/$safe_job"
    ensure_dir "$jobdir"
    index="$jobdir/index.txt"
    : >"$index"
    job_rc=0
    started="$(date -u +%FT%TZ)"

    names_file="$jobdir/.names.json"
    detail="$(prom_curl "$url" /api/v1/label/__name__/values "$names_file" "$timeout" \
      "match[]={job=\"$job\"}" "start=$start_epoch" "end=$end_epoch" 2>&1)" && rc=0 || rc=$?
    if [[ $rc -ne 0 ]] || ! name_list="$(prom_json_data_values "$names_file")"; then
      rm -f -- "$names_file"
      printf 'FAILED: metric listing for job %s\n' "$job" >>"$index"
      prom_error "$outdir" "prometheus metric listing failed for job $job (curl exit $rc): ${detail:-unparseable JSON}"
      failed=1
      ended="$(date -u +%FT%TZ)"
      manifest_add "$manifest" prometheus collect-prometheus "$index" \
        "GET $masked_url/api/v1/label/__name__/values match[]={job=\"$job\"}" 2 "$started" "$ended"
      continue
    fi
    rm -f -- "$names_file"

    n_metrics="$(printf '%s\n' "$name_list" | grep -c . || true)"
    progress "prometheus: job $job — $n_metrics metrics, step ${step}s…"

    while IFS= read -r metric; do
      [[ -n "$metric" ]] || continue
      if [[ $SECONDS -ge $deadline ]]; then
        truncated=1
        printf 'TRUNCATED: budget %ss exceeded\n' "$budget" >>"$index"
        prom_error "$outdir" "prometheus dump truncated: budget ${budget}s exceeded at job $job"
        job_rc=2
        failed=1
        break
      fi
      if ! [[ $metric =~ $metric_re ]]; then
        printf 'skipped %s unsafe-name\n' "$metric" >>"$index"
        prom_error "$outdir" "prometheus metric skipped (unsafe name) job=$job metric=$metric"
        job_rc=2
        failed=1
        continue
      fi
      file="${metric//:/__}.json"
      detail="$(prom_curl "$url" /api/v1/query_range "$jobdir/$file" "$timeout" \
        "query={__name__=\"$metric\",job=\"$job\"}" \
        "start=$start_epoch" "end=$end_epoch" "step=$step" 2>&1)" && rc=0 || rc=$?
      if [[ $rc -ne 0 ]] || ! head -c 512 "$jobdir/$file" 2>/dev/null | grep -qF '"status":"success"'; then
        rm -f -- "$jobdir/$file"
        printf 'failed %s -\n' "$metric" >>"$index"
        prom_error "$outdir" "prometheus query_range failed job=$job metric=$metric (curl exit $rc): $detail"
        metrics_failed=$((metrics_failed + 1))
        job_rc=2
        failed=1
        continue
      fi
      gzip -f -- "$jobdir/$file"
      printf 'ok %s %s.gz\n' "$metric" "$file" >>"$index"
      metrics_ok=$((metrics_ok + 1))
    done <<<"$name_list"

    ended="$(date -u +%FT%TZ)"
    manifest_add "$manifest" prometheus collect-prometheus "$index" \
      "GET $masked_url/api/v1/query_range query={__name__=\"<metric>\",job=\"$job\"} start=$start_epoch end=$end_epoch step=$step ($n_metrics metrics)" \
      "$job_rc" "$started" "$ended"
  done

  {
    printf 'url=%s\n' "$masked_url"
    printf 'since=%s\n' "$since"
    printf 'window_start_epoch=%s\n' "$start_epoch"
    printf 'window_start_utc=%s\n' "$(prom_epoch_utc "$start_epoch")"
    printf 'window_end_epoch=%s\n' "$end_epoch"
    printf 'window_end_utc=%s\n' "$(prom_epoch_utc "$end_epoch")"
    printf 'step_seconds=%s\n' "$step"
    printf 'job_regex=%s\n' "$job_regex"
    printf 'jobs_seen=%s\n' "${jobs_seen_str:-<none>}"
    printf 'jobs_matched=%s\n' "${jobs_matched_str:-<none>}"
    printf 'metrics_ok=%s\n' "$metrics_ok"
    printf 'metrics_failed=%s\n' "$metrics_failed"
    printf 'truncated=%s\n' "$truncated"
  } >"$promdir/dump-info.txt"

  {
    printf 'prom_url=%s\n' "$masked_url"
    printf 'prom_jobs=%s\n' "${jobs_matched_str:-<none>}"
  } >>"$outdir/environment.txt"

  [[ $failed -eq 0 ]] || return 2
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  collect_prometheus "$@"
fi
