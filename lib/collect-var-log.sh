#!/usr/bin/env bash
set -euo pipefail

# Read-only /var/log collector.
#
# Public interface:
#   collect_var_logs ROOT OUT MAX_BYTES KEEP_ORIGINALS WINDOW_START_EPOCH
#
# ROOT is /var/log in production and may be replaced by a fixture root in tests.
# The collector never writes below ROOT. All staging and output live below OUT.
#
# WINDOW_START_EPOCH is the absolute start of the Evidence Window in seconds
# since the epoch; 0 means unbounded, because every real mtime is greater. It is
# absolute rather than a duration so that what gets collected does not depend on
# when the collector runs, and so both implementations of the window can be
# pinned by fixtures with fixed mtimes (ADR 0012).

var_log_escape_tsv() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//$'\t'/\\t}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  printf '%s' "$value"
}

var_log_stat_size() {
  local source=$1
  stat -c '%s' "$source" 2>/dev/null ||
    stat -f '%z' "$source" 2>/dev/null ||
    sudo -n stat -c '%s' "$source" 2>/dev/null
}

var_log_stat_mtime() {
  local source=$1
  stat -c '%Y' "$source" 2>/dev/null ||
    stat -f '%m' "$source" 2>/dev/null ||
    sudo -n stat -c '%Y' "$source" 2>/dev/null
}

var_log_read() {
  local source=$1

  # Fixture-only escape hatch for platforms whose dd lacks GNU iflag=noatime.
  # Production collection deliberately has no ordinary cat fallback: if a
  # no-atime read cannot be made, the file becomes a partial/opaque result.
  if [[ "${CEPH_INCIDENT_TEST_ALLOW_ATIME_READ:-0}" == "1" ]]; then
    cat -- "$source"
    return $?
  fi

  if [[ $EUID -eq 0 || -O "$source" ]]; then
    dd "if=$source" iflag=noatime,nofollow status=none
    return $?
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo -n dd "if=$source" iflag=noatime,nofollow status=none
    return $?
  fi
  return 1
}

var_log_find0() {
  local root=$1
  if [[ "${CEPH_INCIDENT_TEST_ALLOW_ATIME_READ:-0}" == "1" || $EUID -eq 0 ]]; then
    find "$root" -type f -print0
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n find "$root" -type f -print0
  else
    find "$root" -type f -print0
  fi
}

var_log_stream() {
  local source=$1 codec=$2
  case "$codec" in
    plain) var_log_read "$source" ;;
    gz) var_log_read "$source" | gzip -dc ;;
    xz) var_log_read "$source" | xz -dc ;;
    bz2) var_log_read "$source" | bzip2 -dc ;;
    zst) var_log_read "$source" | zstd -qdc ;;
    *) return 1 ;;
  esac
}

var_log_codec_command() {
  case "$1" in
    plain) printf 'cat' ;;
    gz) printf 'gzip' ;;
    xz) printf 'xz' ;;
    bz2) printf 'bzip2' ;;
    zst) printf 'zstd' ;;
    *) return 1 ;;
  esac
}

var_log_is_sensitive_path() {
  local path=$1 nocase_was_set=0
  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch
  if [[ "$path" == *keyring* || "$path" == *.ssh* || "$path" == *id_ed25519* ||
        "$path" == *private_key* || "$path" == *.pem || "$path" == *.pem.* ||
        "$path" == *.key || "$path" == *.key.* || "$path" == *.crt ||
        "$path" == *.crt.* || "$path" == *.pfx || "$path" == *.pfx.* ||
        "$path" == *.p12 || "$path" == *.p12.* ]]; then
    [[ $nocase_was_set -eq 1 ]] || shopt -u nocasematch
    return 0
  fi
  [[ $nocase_was_set -eq 1 ]] || shopt -u nocasematch
  return 1
}

var_log_is_opaque_archive() {
  local path=$1 nocase_was_set=0
  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch
  if [[ "$path" == *.zip || "$path" == *.tar || "$path" == *.tgz ||
        "$path" == *.tbz || "$path" == *.tbz2 || "$path" == *.txz ||
        "$path" == *.tzst || "$path" == *.tar.gz || "$path" == *.tar.xz ||
        "$path" == *.tar.bz2 || "$path" == *.tar.zst ]]; then
    [[ $nocase_was_set -eq 1 ]] || shopt -u nocasematch
    return 0
  fi
  [[ $nocase_was_set -eq 1 ]] || shopt -u nocasematch
  return 1
}

var_log_codec_for() {
  local path=$1 nocase_was_set=0
  shopt -q nocasematch && nocase_was_set=1
  shopt -s nocasematch
  case "$path" in
    *.gz) printf 'gz' ;;
    *.xz) printf 'xz' ;;
    *.bz2) printf 'bz2' ;;
    *.zst) printf 'zst' ;;
    *) printf 'plain' ;;
  esac
  [[ $nocase_was_set -eq 1 ]] || shopt -u nocasematch
}

var_log_family_and_order() {
  local rel=$1 codec=$2 dir name stem family key number raw_number date_digits
  dir=${rel%/*}
  [[ "$dir" == "$rel" ]] && dir=''
  name=${rel##*/}
  stem=$name
  case "$codec" in
    gz) stem=${stem%.gz} ;;
    xz) stem=${stem%.xz} ;;
    bz2) stem=${stem%.bz2} ;;
    zst) stem=${stem%.zst} ;;
  esac

  family=$stem
  key=900000000000
  if [[ "$stem" =~ ^(.+)\.([0-9]+)$ ]]; then
    family=${BASH_REMATCH[1]}
    raw_number=${BASH_REMATCH[2]}
    number=$raw_number
    while [[ ${#number} -gt 1 && "$number" == 0* ]]; do number=${number#0}; done
    if [[ ${#number} -le 9 ]]; then
      number=$((10#$number))
      key="$(printf '1%09d' "$((999999999 - number))")"
    else
      key="0-oversize-$number"
    fi
  elif [[ "$stem" =~ ^(.+)-([0-9]{8})$ ]]; then
    family=${BASH_REMATCH[1]}
    key="0${BASH_REMATCH[2]}"
  elif [[ "$stem" =~ ^(.+)-([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]]; then
    family=${BASH_REMATCH[1]}
    date_digits="${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
    key="0$date_digits"
  fi

  if [[ -n "$dir" ]]; then
    printf '%s\t%s/%s' "$key" "$dir" "$family"
  else
    printf '%s\t%s' "$key" "$family"
  fi
}

# The group one source belongs to while the Evidence Window picks sources.
#
# Text logs reuse the merge family, so a whole rotation set is decided as one
# unit. Binary systemd journals are never merged, and each of their file names
# is its own family, so the window would keep every one of them as its own
# newest-before-the-window file; group them the way journald names them instead
# — by machine-id directory and stream (`system`, `user-1000`) — so that one
# archived journal per stream crosses the boundary rather than all of them.
var_log_window_group() {
  local rel=$1 codec=$2 dir name stem family_and_order
  name=${rel##*/}
  stem=${name%'~'}
  if [[ "$stem" == *.journal ]]; then
    dir=${rel%/*}
    [[ "$dir" == "$rel" ]] && dir=''
    stem=${stem%.journal}
    stem=${stem%%@*}
    if [[ -n "$dir" ]]; then
      printf 'journal:%s/%s' "$dir" "$stem"
    else
      printf 'journal:%s' "$stem"
    fi
    return 0
  fi
  family_and_order="$(var_log_family_and_order "$rel" "$codec")"
  printf 'family:%s' "${family_and_order#*$'\t'}"
}

# Decide which sources the Evidence Window keeps, one log family at a time.
#
# Reads TAB-separated `index<TAB>group<TAB>mtime<TAB>source` records — group and
# source are already TSV-escaped, so neither can hold a TAB — and writes one
# `index<TAB>disposition` line per record. Rotation mtimes say when the rotation
# happened, not what the file spans, so the newest source older than the window
# start is kept as well: if logrotate ran shortly before the collect, everything
# inside the window is in that file (ADR 0012).
var_log_window_decisions() {
  local table=$1 start=$2
  LC_ALL=C awk -F '\t' -v start="$start" '
    {
      key[NR] = $1
      group[NR] = $2
      mtime[NR] = $3
      name[NR] = $4
      if ($3 == "unknown") {
        # Collect it and say so, rather than silently dropping evidence over a
        # metadata read the collector could not make.
        decision[NR] = "mtime-unknown"
        next
      }
      decision[NR] = "outside-window"
      if ($3 + 0 >= start + 0) {
        decision[NR] = "in-window"
        next
      }
      if (!($2 in newest)) {
        newest[$2] = NR
        next
      }
      before = newest[$2]
      # Same mtime in one group is possible (a rotation and its copy); break the
      # tie on the source name so both implementations pick the same file.
      if ($3 + 0 > mtime[before] + 0 ||
          ($3 + 0 == mtime[before] + 0 && $4 < name[before])) {
        newest[$2] = NR
      }
    }
    END {
      for (candidate in newest) {
        decision[newest[candidate]] = "window-crossing"
      }
      for (record = 1; record <= NR; record++) {
        printf "%s\t%s\n", key[record], decision[record]
      }
    }
  ' "$table"
}

var_log_is_text_stream() {
  local source=$1 codec=$2 scratch=$3
  local fifo="$scratch/.nul-fifo.$$.$RANDOM"
  local total_file="$scratch/.nul-total.$$.$RANDOM"
  local non_nul_file="$scratch/.nul-non.$$.$RANDOM"
  local counter_pid total_size non_nul_size wait_rc=0
  local -a stream_status
  # Consume the entire stream. A sampling/early-exit test can miss a NUL late
  # in a mixed text/binary file and wrongly merge it. The FIFO lets one stream
  # feed both byte counters without od-style hex expansion or large staging.
  mkfifo "$fifo" || return 1
  wc -c <"$fifo" >"$total_file" &
  counter_pid=$!
  set +e
  set +o pipefail
  var_log_stream "$source" "$codec" 2>/dev/null |
    tee "$fifo" |
    LC_ALL=C tr -d '\000' |
    wc -c >"$non_nul_file"
  stream_status=("${PIPESTATUS[@]}")
  wait "$counter_pid" || wait_rc=$?
  set -o pipefail
  set -e
  total_size="$(tr -d '[:space:]' <"$total_file")"
  non_nul_size="$(tr -d '[:space:]' <"$non_nul_file")"
  rm -f -- "$fifo" "$total_file" "$non_nul_file"
  [[ "${stream_status[0]}" -eq 0 && "${stream_status[1]}" -eq 0 &&
     "${stream_status[2]}" -eq 0 && "${stream_status[3]}" -eq 0 &&
     $wait_rc -eq 0 && "$total_size" == "$non_nul_size" ]]
}

var_log_merged_destination() {
  local out=$1 family=$2 dest component
  dest="$out/merged/tree"
  while [[ "$family" == */* ]]; do
    component=${family%%/*}
    dest="$dest/dirs/$component"
    family=${family#*/}
  done
  printf '%s/files/%s.merged' "$dest" "$family"
}

var_log_copy_raw() {
  local source=$1 dest=$2 limit=${3:-unlimited} tmp copied rc=0
  ensure_dir "$(dirname -- "$dest")"
  tmp="$dest.tmp.$$"
  if [[ "$limit" == "unlimited" ]]; then
    if ! var_log_read "$source" >"$tmp"; then
      rm -f -- "$tmp"
      return 1
    fi
  else
    set +e
    set +o pipefail
    var_log_read "$source" | head -c "$((limit + 1))" >"$tmp"
    local -a stream_status=("${PIPESTATUS[@]}")
    set -o pipefail
    set -e
    copied="$(var_log_stat_size "$tmp" 2>/dev/null || printf 0)"
    if [[ "$copied" -gt "$limit" ]]; then
      rm -f -- "$tmp"
      return 3
    fi
    [[ "${stream_status[0]}" -eq 0 ]] || rc=1
    if [[ $rc -ne 0 ]]; then
      rm -f -- "$tmp"
      return 1
    fi
  fi
  if [[ -f "$tmp" ]]; then
    mv -f -- "$tmp" "$dest"
    return 0
  fi
  return 1
}

var_log_measure_bounded() {
  local source=$1 codec=$2 limit=$3 scratch=$4 count rc=0
  local count_file="$scratch/.measure.$$.$RANDOM"
  local -a stream_status
  set +e
  set +o pipefail
  var_log_stream "$source" "$codec" 2>/dev/null |
    head -c "$((limit + 1))" |
    wc -c >"$count_file"
  stream_status=("${PIPESTATUS[@]}")
  set -o pipefail
  set -e
  count="$(tr -d '[:space:]' <"$count_file")"
  rm -f -- "$count_file"
  printf '%s' "$count"
  if [[ "$count" -gt "$limit" ]]; then
    return 3
  fi
  [[ "${stream_status[0]}" -eq 0 ]] || rc=2
  return "$rc"
}

var_log_append_bounded() {
  local source=$1 codec=$2 dest=$3 limit=$4 before after appended rc=0
  if [[ "$limit" == "unlimited" ]]; then
    var_log_stream "$source" "$codec" >>"$dest"
    return $?
  fi
  before="$(var_log_stat_size "$dest" 2>/dev/null || printf 0)"
  set +e
  set +o pipefail
  var_log_stream "$source" "$codec" |
    head -c "$((limit + 1))" >>"$dest"
  local -a stream_status=("${PIPESTATUS[@]}")
  set -o pipefail
  set -e
  after="$(var_log_stat_size "$dest" 2>/dev/null || printf 0)"
  appended=$((after - before))
  if [[ $appended -gt $limit ]]; then
    return 3
  fi
  [[ "${stream_status[0]}" -eq 0 ]] || rc=1
  return "$rc"
}

collect_var_logs() {
  local root=$1 out=$2 max_bytes=$3 keep_originals=$4 window_start=$5
  local index="$out/INDEX.tsv" errors="$out/ERRORS.tsv"
  local warnings="$out/WARNINGS.tsv"
  local opaque="$out/UNREDACTED-OPAQUE.txt"
  local skipped="$out/SKIPPED-sensitive.txt"
  local order_file="$out/.order.$$"
  local window_file="$out/.window.$$"
  local scan_file="$out/.scan.$$" scan_errors="$out/.scan-errors.$$"
  local total=0 partial=0 count=0 raw_count=0 seen_count=0 source rel codec command size decoded_size
  local over_limit=0 measure_rc=0 budget_used=0 hard_cap_hit=0
  local copy_limit copy_rc append_rc before_bytes after_bytes segment_start header header_bytes
  local family_and_order order_key family gid i existing raw_dest group_count=0
  local final_size final_mtime
  local max_enabled=1 window_enabled=1
  local scanned=0 n mtime note decision
  local scan_limit=${CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES:-67108864}
  local entry_limit=${CEPH_INCIDENT_VAR_LOG_MAX_ENTRIES:-100000}
  local scan_size scan_rc=0 scan_over=0
  local reserve_bytes=${CEPH_INCIDENT_VAR_LOG_FREE_RESERVE_BYTES:-1073741824}
  local available_kib available_bytes required_bytes
  local -a scan_status
  local -a sources rels codecs groups order_keys group_names source_sizes source_mtimes
  local -a raw_sources raw_rels
  local -a scan_sources scan_rels scan_codecs scan_sizes scan_mtimes scan_decisions

  [[ "$max_bytes" == "unlimited" ]] && max_enabled=0
  if [[ $max_enabled -eq 1 && ! "$max_bytes" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  [[ "$keep_originals" == "0" || "$keep_originals" == "1" ]] || return 1
  [[ "$window_start" =~ ^[0-9]+$ ]] || return 1
  [[ "$window_start" -gt 0 ]] || window_enabled=0
  [[ -d "$root" ]] || {
    write_skip_artifact "$out/SKIPPED.txt" "$root is not a directory"
    return 0
  }

  ensure_dir "$out"
  printf 'source\tfamily\tcodec\tstored_bytes\tdecoded_bytes\tmtime_epoch\tdisposition\tdetail\n' >"$index"
  : >"$errors"
  : >"$warnings"
  : >"$opaque"
  : >"$skipped"
  : >"$order_file"
  available_kib="$(df -Pk "$out" 2>/dev/null | awk 'NR == 2 {print $4}')"
  if [[ "$reserve_bytes" =~ ^[0-9]+$ && "$available_kib" =~ ^[0-9]+$ ]]; then
    available_bytes=$((available_kib * 1024))
    required_bytes=$((reserve_bytes + scan_limit))
    if [[ $available_bytes -lt $required_bytes ]]; then
      printf 'available_bytes=%s\nreserve_bytes=%s\nscan_staging_bytes=%s\nstatus=not-collected\n' \
        "$available_bytes" "$reserve_bytes" "$scan_limit" >"$out/INSUFFICIENT-SPACE.txt"
      rm -f -- "$order_file"
      return 2
    fi
  fi

  set +e
  set +o pipefail
  var_log_find0 "$root" 2>"$scan_errors" |
    head -c "$((scan_limit + 1))" >"$scan_file"
  scan_status=("${PIPESTATUS[@]}")
  set -o pipefail
  set -e
  scan_rc=${scan_status[0]}
  scan_size="$(var_log_stat_size "$scan_file" 2>/dev/null || printf 0)"
  if [[ $scan_size -gt $scan_limit ]]; then
    printf 'scan_max_bytes=%s\nstatus=not-collected-metadata-limit\n' \
      "$scan_limit" >"$out/SCAN-LIMIT.txt"
    rm -f -- "$scan_file" "$scan_errors" "$order_file"
    return 2
  fi
  if [[ $scan_rc -ne 0 ]]; then
    partial=1
    printf 'scan\t%s\n' "$(var_log_escape_tsv "$(sed -n '1p' "$scan_errors")")" >>"$errors"
  fi

  # Metadata pass. Nothing is read here: the Evidence Window decides which
  # sources are in scope from stat alone, and it decides before the byte cap is
  # estimated so that the cap never judges data that is about to be dropped.
  while IFS= read -r -d '' source; do
    seen_count=$((seen_count + 1))
    if [[ $seen_count -gt $entry_limit ]]; then
      scan_over=1
      break
    fi
    rel=${source#"$root"/}
    scan_sources[scanned]=$source
    scan_rels[scanned]=$rel
    scan_codecs[scanned]="$(var_log_codec_for "$rel")"
    scan_sizes[scanned]="$(var_log_stat_size "$source" 2>/dev/null || printf 0)"
    scan_mtimes[scanned]="$(var_log_stat_mtime "$source" 2>/dev/null || printf unknown)"
    scan_decisions[scanned]=in-window
    scanned=$((scanned + 1))
  done <"$scan_file"
  rm -f -- "$scan_file" "$scan_errors"

  if [[ $scan_over -eq 1 ]]; then
    rm -f -- "$order_file"
    printf 'max_entries=%s\nstatus=not-collected-metadata-limit\n' \
      "$entry_limit" >"$out/SCAN-LIMIT.txt"
    return 2
  fi

  if [[ $window_enabled -eq 1 && $scanned -gt 0 ]]; then
    : >"$window_file"
    for ((n = 0; n < scanned; n++)); do
      # Sensitive paths are never collected under any window, so leaving them
      # out keeps them from taking a family's window-crossing slot.
      var_log_is_sensitive_path "${scan_rels[$n]}" && continue
      printf '%s\t%s\t%s\t%s\n' \
        "$n" \
        "$(var_log_escape_tsv "$(var_log_window_group "${scan_rels[$n]}" "${scan_codecs[$n]}")")" \
        "${scan_mtimes[$n]}" \
        "$(var_log_escape_tsv "${scan_rels[$n]}")" >>"$window_file"
    done
    while IFS=$'\t' read -r n decision; do
      scan_decisions[n]=$decision
    done < <(var_log_window_decisions "$window_file" "$window_start")
    rm -f -- "$window_file"
  fi

  for ((n = 0; n < scanned; n++)); do
    source=${scan_sources[$n]}
    rel=${scan_rels[$n]}
    codec=${scan_codecs[$n]}
    size=${scan_sizes[$n]}
    mtime=${scan_mtimes[$n]}
    # The window annotation rides along on `detail` so that a reader can tell
    # why coverage reaches further back than --since asked for, and which
    # sources were kept despite unreadable metadata.
    note=''
    case "${scan_decisions[$n]}" in
      window-crossing) note=' window-crossing' ;;
      mtime-unknown) note=' mtime-unknown' ;;
    esac

    if var_log_is_sensitive_path "$rel"; then
      printf '%s\n' "$(var_log_escape_tsv "$rel")" >>"$skipped"
      printf '%s\t-\t%s\t%s\t0\t%s\tskipped-sensitive\tforbidden path\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$mtime" >>"$index"
      continue
    fi

    if [[ "${scan_decisions[$n]}" == "outside-window" ]]; then
      printf '%s\t-\t%s\t%s\t0\t%s\toutside-window\tolder than the evidence window start\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$mtime" >>"$index"
      continue
    fi

    if [[ $over_limit -eq 1 ]]; then
      printf '%s\t-\t%s\t%s\t0\t%s\tnot-inspected\tearlier file exceeded cap%s\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$mtime" "$note" >>"$index"
      continue
    fi

    if var_log_is_opaque_archive "$rel"; then
      raw_sources[raw_count]=$source
      raw_rels[raw_count]=$rel
      raw_count=$((raw_count + 1))
      total=$((total + size))
      printf '%s\n' "$(var_log_escape_tsv "$rel")" >>"$opaque"
      printf '%s\t-\topaque\t%s\t0\t%s\traw\tnot auto-merged%s\n' \
        "$(var_log_escape_tsv "$rel")" "$size" "$mtime" "$note" >>"$index"
      continue
    fi

    command="$(var_log_codec_command "$codec")"
    if [[ "$codec" != "plain" ]] && ! command -v "$command" >/dev/null 2>&1; then
      partial=1
      raw_sources[raw_count]=$source
      raw_rels[raw_count]=$rel
      raw_count=$((raw_count + 1))
      total=$((total + size))
      printf '%s\tmissing-codec:%s\n' "$(var_log_escape_tsv "$rel")" "$command" >>"$errors"
      printf '%s\t-\t%s\t%s\t0\t%s\traw-partial\tmissing codec%s\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$mtime" "$note" >>"$index"
      continue
    fi

    measure_rc=0
    if [[ $max_enabled -eq 1 ]]; then
      decoded_size="$(var_log_measure_bounded "$source" "$codec" "$max_bytes" "$out")" || measure_rc=$?
    else
      decoded_size="$(var_log_stream "$source" "$codec" 2>/dev/null | wc -c | tr -d '[:space:]')" || measure_rc=$?
    fi
    if [[ $measure_rc -eq 3 ]]; then
      over_limit=1
      printf '%s\t-\t%s\t%s\t>%s\t%s\tover-limit\tdecoded stream exceeded cap%s\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$max_bytes" "$mtime" "$note" >>"$index"
      continue
    elif [[ $measure_rc -ne 0 ]]; then
      partial=1
      raw_sources[raw_count]=$source
      raw_rels[raw_count]=$rel
      raw_count=$((raw_count + 1))
      total=$((total + size))
      printf '%s\tdecode-failed\n' "$(var_log_escape_tsv "$rel")" >>"$errors"
      printf '%s\t-\t%s\t%s\t0\t%s\traw-partial\tdecode failed%s\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$mtime" "$note" >>"$index"
      continue
    fi

    if ! var_log_is_text_stream "$source" "$codec" "$out"; then
      raw_sources[raw_count]=$source
      raw_rels[raw_count]=$rel
      raw_count=$((raw_count + 1))
      total=$((total + size))
      printf '%s\n' "$(var_log_escape_tsv "$rel")" >>"$opaque"
      printf '%s\t-\t%s\t%s\t%s\t%s\traw\tbinary or unknown%s\n' \
        "$(var_log_escape_tsv "$rel")" "$codec" "$size" "$decoded_size" "$mtime" "$note" >>"$index"
      continue
    fi

    family_and_order="$(var_log_family_and_order "$rel" "$codec")"
    order_key=${family_and_order%%$'\t'*}
    family=${family_and_order#*$'\t'}
    sources[count]=$source
    rels[count]=$rel
    codecs[count]=$codec
    groups[count]=$family
    order_keys[count]=$order_key
    source_sizes[count]=$size
    source_mtimes[count]=$mtime
    printf '%s\t%s\t%s\t%s\t%s\t%s\tmerge-candidate\toldest-to-newest%s\n' \
      "$(var_log_escape_tsv "$rel")" \
      "$(var_log_escape_tsv "$family")" \
      "$codec" "$size" "$decoded_size" "$mtime" "$note" >>"$index"
    total=$((total + decoded_size + 256))
    if [[ "$keep_originals" == "1" ]]; then
      total=$((total + size))
    fi
    count=$((count + 1))
  done

  if [[ $over_limit -eq 1 || ( $max_enabled -eq 1 && $total -gt $max_bytes ) ]]; then
    rm -rf -- "$out/merged" "$out/original" "$out/raw"
    rm -f -- "$order_file"
    printf 'estimated_output_bytes=%s\nmax_bytes=%s\nstatus=not-collected\n' \
      "$total" "$max_bytes" >"$out/OVER-LIMIT.txt"
    return 2
  fi

  available_kib="$(df -Pk "$out" 2>/dev/null | awk 'NR == 2 {print $4}')"
  if [[ "$reserve_bytes" =~ ^[0-9]+$ && "$available_kib" =~ ^[0-9]+$ ]]; then
    available_bytes=$((available_kib * 1024))
    required_bytes=$((total + reserve_bytes))
    if [[ $available_bytes -lt $required_bytes ]]; then
      printf 'estimated_output_bytes=%s\navailable_bytes=%s\nreserve_bytes=%s\nstatus=not-collected\n' \
        "$total" "$available_bytes" "$reserve_bytes" >"$out/INSUFFICIENT-SPACE.txt"
      rm -f -- "$order_file" "$order_file.sorted"
      return 2
    fi
  fi

  if [[ $raw_count -gt 0 ]]; then
    for ((i = 0; i < raw_count; i++)); do
      raw_dest="$out/raw/${raw_rels[$i]}"
      copy_limit=unlimited
      [[ $max_enabled -eq 1 ]] && copy_limit=$((max_bytes - budget_used))
      copy_rc=0
      var_log_copy_raw "${raw_sources[$i]}" "$raw_dest" "$copy_limit" || copy_rc=$?
      if [[ $copy_rc -eq 3 ]]; then
        hard_cap_hit=1
        break
      elif [[ $copy_rc -ne 0 ]]; then
        partial=1
        printf '%s\tread-failed\n' "$(var_log_escape_tsv "${raw_rels[$i]}")" >>"$errors"
      else
        budget_used=$((budget_used + $(var_log_stat_size "$raw_dest" 2>/dev/null || printf 0)))
      fi
    done
  fi

  if [[ $count -gt 0 && $hard_cap_hit -eq 0 ]]; then
    for ((i = 0; i < count; i++)); do
      gid=-1
      if [[ $group_count -gt 0 ]]; then
        for existing in "${!group_names[@]}"; do
          if [[ "${group_names[$existing]}" == "${groups[$i]}" ]]; then
            gid=$existing
            break
          fi
        done
      fi
      if [[ $gid -lt 0 ]]; then
        gid=$group_count
        group_names[gid]=${groups[$i]}
        group_count=$((group_count + 1))
      fi
      printf '%s\t%s\t%s\n' "$gid" "${order_keys[$i]}" "$i" >>"$order_file"
    done

    LC_ALL=C sort -t $'\t' -k1,1n -k2,2 -k3,3n "$order_file" >"$order_file.sorted"
    local current_gid=-1 merged_tmp='' merged_dest='' idx
    while IFS=$'\t' read -r gid order_key idx; do
      if [[ "$gid" != "$current_gid" ]]; then
        if [[ -n "$merged_tmp" ]]; then
          mv -f -- "$merged_tmp" "$merged_dest"
        fi
        current_gid=$gid
        # Keep file and directory namespaces separate at every level so source
        # names such as foo.1 and foo.merged/bar.log cannot collide.
        merged_dest="$(var_log_merged_destination "$out" "${group_names[$gid]}")"
        ensure_dir "$(dirname -- "$merged_dest")"
        merged_tmp="$merged_dest.tmp.$$"
        : >"$merged_tmp"
      fi
      printf -v header '===== source=%s mtime_epoch=%s stored_bytes=%s codec=%s =====\n' \
        "$(var_log_escape_tsv "${rels[$idx]}")" \
        "$(var_log_stat_mtime "${sources[$idx]}" 2>/dev/null || printf unknown)" \
        "$(var_log_stat_size "${sources[$idx]}" 2>/dev/null || printf unknown)" \
        "${codecs[$idx]}"
      header_bytes="$(printf '%s' "$header" | wc -c | tr -d '[:space:]')"
      if [[ $max_enabled -eq 1 && $((budget_used + header_bytes + 1)) -gt $max_bytes ]]; then
        rm -f -- "$merged_tmp"
        merged_tmp=''
        hard_cap_hit=1
        break
      fi
      segment_start="$(var_log_stat_size "$merged_tmp" 2>/dev/null || printf 0)"
      printf '%s' "$header" >>"$merged_tmp"
      budget_used=$((budget_used + header_bytes))
      copy_limit=unlimited
      [[ $max_enabled -eq 1 ]] && copy_limit=$((max_bytes - budget_used - 1))
      before_bytes="$(var_log_stat_size "$merged_tmp" 2>/dev/null || printf 0)"
      append_rc=0
      var_log_append_bounded "${sources[$idx]}" "${codecs[$idx]}" "$merged_tmp" "$copy_limit" || append_rc=$?
      after_bytes="$(var_log_stat_size "$merged_tmp" 2>/dev/null || printf 0)"
      budget_used=$((budget_used + after_bytes - before_bytes))
      if [[ $append_rc -eq 3 ]]; then
        rm -f -- "$merged_tmp"
        merged_tmp=''
        hard_cap_hit=1
        break
      elif [[ $append_rc -ne 0 ]]; then
        partial=1
        truncate -s "$segment_start" "$merged_tmp"
        budget_used=$((budget_used - header_bytes - after_bytes + before_bytes))
        printf '%s\tmerge-read-failed\n' "$(var_log_escape_tsv "${rels[$idx]}")" >>"$errors"
        copy_limit=unlimited
        [[ $max_enabled -eq 1 ]] && copy_limit=$((max_bytes - budget_used))
        copy_rc=0
        var_log_copy_raw "${sources[$idx]}" "$out/raw/${rels[$idx]}" "$copy_limit" || copy_rc=$?
        if [[ $copy_rc -eq 3 ]]; then
          rm -f -- "$merged_tmp"
          merged_tmp=''
          hard_cap_hit=1
          break
        elif [[ $copy_rc -ne 0 ]]; then
          printf '%s\traw-preserve-failed-after-merge-read\n' \
            "$(var_log_escape_tsv "${rels[$idx]}")" >>"$errors"
        else
          budget_used=$((budget_used + $(var_log_stat_size "$out/raw/${rels[$idx]}" 2>/dev/null || printf 0)))
        fi
        continue
      fi
      printf '\n' >>"$merged_tmp"
      budget_used=$((budget_used + 1))
      final_size="$(var_log_stat_size "${sources[$idx]}" 2>/dev/null || printf unknown)"
      final_mtime="$(var_log_stat_mtime "${sources[$idx]}" 2>/dev/null || printf unknown)"
      if [[ "$final_size" != "${source_sizes[$idx]}" || "$final_mtime" != "${source_mtimes[$idx]}" ]]; then
        if [[ "${codecs[$idx]}" == "plain" && "${order_keys[$idx]}" == "900000000000" ]]; then
          printf '%s\tactive-log-changed-during-collection\n' \
            "$(var_log_escape_tsv "${rels[$idx]}")" >>"$warnings"
        else
          partial=1
          printf '%s\tchanged-during-collection\n' "$(var_log_escape_tsv "${rels[$idx]}")" >>"$errors"
        fi
      fi
      if [[ "$keep_originals" == "1" ]]; then
        copy_limit=unlimited
        [[ $max_enabled -eq 1 ]] && copy_limit=$((max_bytes - budget_used))
        copy_rc=0
        var_log_copy_raw "${sources[$idx]}" "$out/original/${rels[$idx]}" "$copy_limit" || copy_rc=$?
        if [[ $copy_rc -eq 3 ]]; then
          rm -f -- "$merged_tmp"
          merged_tmp=''
          hard_cap_hit=1
          break
        elif [[ $copy_rc -ne 0 ]]; then
          partial=1
          printf '%s\toriginal-copy-failed\n' "$(var_log_escape_tsv "${rels[$idx]}")" >>"$errors"
        else
          budget_used=$((budget_used + $(var_log_stat_size "$out/original/${rels[$idx]}" 2>/dev/null || printf 0)))
        fi
      fi
    done <"$order_file.sorted"
    if [[ -n "$merged_tmp" ]]; then
      mv -f -- "$merged_tmp" "$merged_dest"
    fi
  fi

  if [[ $hard_cap_hit -eq 1 ]]; then
    rm -rf -- "$out/merged" "$out/raw" "$out/original"
    printf 'max_bytes=%s\nstatus=discarded-after-streaming-hard-cap\n' \
      "$max_bytes" >"$out/OVER-LIMIT.txt"
    rm -f -- "$order_file" "$order_file.sorted"
    return 2
  fi

  local actual_payload_bytes=0 payload_path payload_size
  while IFS= read -r -d '' payload_path; do
    payload_size="$(var_log_stat_size "$payload_path" 2>/dev/null || printf 0)"
    actual_payload_bytes=$((actual_payload_bytes + payload_size))
  done < <(find "$out/merged" "$out/raw" "$out/original" -type f -print0 2>/dev/null || true)
  if [[ $max_enabled -eq 1 ]]; then
    if [[ $actual_payload_bytes -gt $max_bytes ]]; then
      rm -rf -- "$out/merged" "$out/raw" "$out/original"
      printf 'actual_payload_bytes=%s\nmax_bytes=%s\nstatus=discarded-after-hard-cap\n' \
        "$actual_payload_bytes" "$max_bytes" >"$out/OVER-LIMIT.txt"
      rm -f -- "$order_file" "$order_file.sorted"
      return 2
    fi
  fi
  printf '%s\n' "$actual_payload_bytes" >"$out/PAYLOAD-BYTES.txt"

  rm -f -- "$order_file" "$order_file.sorted"
  [[ -s "$errors" ]] || rm -f -- "$errors"
  [[ -s "$warnings" ]] || rm -f -- "$warnings"
  [[ -s "$opaque" ]] || rm -f -- "$opaque"
  [[ -s "$skipped" ]] || rm -f -- "$skipped"
  [[ $partial -eq 0 ]] || return 2
  return 0
}
