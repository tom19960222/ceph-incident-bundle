#!/usr/bin/env bash
set -euo pipefail

# Bundle/orchestration helpers used by run/collect.sh. These are pure-ish units
# (SSH target/quoting, bundle metadata, summary, error log, redaction driver)
# kept out of the entrypoint so collect.sh stays a thin orchestrator. Relies on
# common.sh (sourced first by the entrypoint) for redact_file/redact_gz_file.

ssh_target_for_host() {
  local host=$1 ssh_user=$2
  if [[ "$host" == *@* || -z "$ssh_user" ]]; then
    printf '%s' "$host"
  else
    printf '%s@%s' "$ssh_user" "$host"
  fi
}

# Quote a value for safe interpolation into a remote shell string. Returns 1 if
# the value contains a single quote (callers treat that as a hard input error).
shell_quote() {
  local value=$1
  [[ "$value" != *"'"* ]] || return 1
  printf "'%s'" "$value"
}

write_initial_metadata() {
  local workdir=$1 mode=$2 seed=$3 since=$4 timeout=$5
  local git_commit
  git_commit="$(git -C "$COLLECT_ROOT" rev-parse --short HEAD 2>/dev/null || printf unknown)"

  cat >"$workdir/README-FIRST.txt" <<'EOF'
Ceph incident bundle

Start with:
- summary.txt
- errors.log
- cluster/
- nodes/

This bundle is read-only evidence captured at incident time. Review it before sharing outside your team.
EOF

  cat >"$workdir/environment.txt" <<EOF
created_utc=$(date -u +%FT%TZ)
mode=$mode
seed=$seed
since=$since
timeout=$timeout
git_commit=$git_commit
EOF

  : >"$workdir/manifest.jsonl"
  : >"$workdir/errors.log"
}

write_summary() {
  local workdir=$1 mode=$2 seed=$3 node_ok=$4 node_failed=$5 cluster_status=$6 final_status=$7

  {
    printf 'Ceph incident bundle summary\n'
    printf 'created_utc: %s\n' "$(date -u +%FT%TZ)"
    printf 'mode: %s\n' "$mode"
    printf 'seed: %s\n' "$seed"
    printf 'cluster_status: %s\n' "$cluster_status"
    printf 'node_ok: %s\n' "$node_ok"
    printf 'node_failed: %s\n' "$node_failed"
    printf 'final_status: %s\n' "$final_status"
  } >"$workdir/summary.txt"
}

append_error() {
  local workdir=$1 message=$2
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$message" >>"$workdir/errors.log"
}

# Render manifest.jsonl rows as a markdown table (exit | file | command). $base
# is the dir the artifact paths are relative to; $prefix is prepended to the
# bundle-relative path (for per-node manifests whose paths are remote temp dirs).
# SC2016: backticks in the printf formats are literal markdown, not command subs.
# shellcheck disable=SC2016
catalog_rows() {
  local manifest=$1 base=$2 prefix=${3:-}
  [[ -f "$manifest" ]] || return 0
  local line art cmd ex rel
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    art="$(printf '%s' "$line" | sed -n 's/.*"artifact":"\([^"]*\)".*/\1/p')"
    cmd="$(printf '%s' "$line" | sed -n 's/.*"command":"\(.*\)","exit_code":.*/\1/p')"
    ex="$(printf '%s' "$line" | sed -n 's/.*"exit_code":\([0-9]*\).*/\1/p')"
    [[ -n "$art" ]] || continue
    case "$art" in
      "$base"/*) rel="${art#"$base"/}" ;;
      */out/*) rel="$prefix${art##*/out/}" ;;   # per-node manifest: /tmp/…/out/<rel>
      *) rel="$art" ;;
    esac
    cmd="${cmd//\\\"/\"}"   # unescape JSON \" and \\ for display
    cmd="${cmd//\\\\/\\}"
    cmd="${cmd//|/\\|}"     # escape markdown table pipe
    printf '| %s | `%s` | `%s` |\n' "$ex" "$rel" "$cmd"
  done <"$manifest"
}

# Human-readable index of the bundle: what each file is, and the exact command
# (with exit code) that produced each captured artifact. Sourced from the
# manifests, so it always matches what was actually collected.
# SC2016: backticks in the printf formats are literal markdown, not command subs.
# shellcheck disable=SC2016
write_catalog() {
  local workdir=$1
  local nd alias
  {
    printf '# Bundle contents\n\n'
    printf 'Read-only Ceph incident evidence. Below is what each file is, and — for captured commands — the exact command and exit code that produced it (from the manifest.jsonl files).\n\n'
    printf '## Top-level\n\n'
    printf -- '- `README-FIRST.txt` — start here\n'
    printf -- '- `summary.txt` — run summary (mode, seed, nodes ok/failed, final exit code)\n'
    printf -- '- `environment.txt` — when/mode/seed/git commit + chosen ceph_source / ceph_runner / rook_source\n'
    printf -- '- `manifest.jsonl` — machine-readable: one JSON line per captured command\n'
    printf -- '- `errors.log` — commands that returned non-zero, or nodes that failed\n'
    printf -- '- `redactions.log` — per-file count of lines redacted\n'
    printf -- '- `CONTENTS.md` — this file\n\n'
    printf '## Cluster-level commands (cluster/)\n\n'
    printf '| exit | file | command |\n|---|---|---|\n'
    catalog_rows "$workdir/manifest.jsonl" "$workdir"
    for nd in "$workdir"/nodes/*/; do
      [[ -d "$nd" ]] || continue
      alias="$(basename "$nd")"
      printf '\n## Node: %s (nodes/%s/)\n\n' "$alias" "$alias"
      if [[ -f "$nd/manifest.jsonl" ]]; then
        printf '| exit | file | command |\n|---|---|---|\n'
        catalog_rows "$nd/manifest.jsonl" "${nd%/}" "nodes/$alias/"
      else
        printf 'Not collected — see `nodes/%s/SKIPPED.txt`.\n' "$alias"
      fi
    done
  } >"$workdir/CONTENTS.md"
}

# Single cleanup point (EXIT trap). Uses globals (CLEANUP_WORKDIR/CLEANUP_KEEP)
# because it fires after main has returned and main's locals are gone.
cleanup_workdir() {
  local rc=$?
  if [[ -n "${CLEANUP_WORKDIR:-}" && -d "$CLEANUP_WORKDIR" ]]; then
    if [[ "${CLEANUP_KEEP:-0}" -eq 1 ]]; then
      printf 'kept workdir: %s\n' "$CLEANUP_WORKDIR" >&2
    else
      rm -rf -- "$CLEANUP_WORKDIR"
    fi
  fi
  return "$rc"
}

# Ctrl-C / SIGTERM: stop NOW. Without this the plain EXIT-trap handler runs
# cleanup but bash keeps executing the next node, so the run "can't be stopped".
# Drop the traps (avoid re-entry / double cleanup), clean up, and exit 130.
on_interrupt() {
  trap - INT TERM EXIT
  printf '\ninterrupted — stopping and cleaning up…\n' >&2
  cleanup_workdir
  exit 130
}

# Redact every text-ish artifact in the bundle in place (gz handled specially).
redact_bundle_text() {
  local workdir=$1
  local redaction_log="$workdir/redactions.log"
  local path

  # Per-metric Prometheus dumps (workdir/cluster/prometheus/<job>/*.json.gz) are
  # numeric time series in single multi-MB JSON lines: line-based redaction is
  # pathologically slow there and one regex false-positive would blank the
  # whole file. They are excluded; dump-info/index/buildinfo/targets in
  # cluster/prometheus/ still go through redaction like everything else.
  while IFS= read -r path; do
    case "$path" in
      *.gz) redact_gz_file "$path" "$redaction_log" ;;
      *) redact_file "$path" "$redaction_log" ;;
    esac
  done < <(find "$workdir/cluster" "$workdir/nodes" -type f \
    -not -path "$workdir/cluster/prometheus/*/*.json.gz" \
    \( -name '*.txt' -o -name '*.log' -o -name '*.log.*' -o -name '*.yaml' -o -name '*.json' -o -name '*.jsonl' -o -name '*.conf' -o -name 'config' -o -name '*.gz' \) -print 2>/dev/null || true)
}
