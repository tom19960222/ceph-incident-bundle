#!/usr/bin/env bash
set -euo pipefail

# Convert an /etc/hosts style file into a collect.sh inventory.
#
# The emitted grammar is the one both inventory parsers accept — load_inventory()
# in lib/bundle.sh and _read_inventory() in ceph_incident_bundle.py: quoted
# scalar assignments, a bare `HOSTS=(` line, one quoted "alias=host" entry per
# line, and a closing `)`. Alias and target are validated here against the same
# rules those parsers apply, so a generated inventory never contributes a
# "skipped unsafe host alias" / "skipped unsafe SSH target" error at collect
# time. Anything that cannot be mapped is reported on stderr and skipped —
# never guessed at, never dropped silently.

PROGRAM="${0##*/}"

# Same shapes as lib/bundle.sh (is_safe_ssh_target, is_safe_namespace) and
# ceph_incident_bundle.py (SAFE_ALIAS, SAFE_SSH_USER, SAFE_SSH_TARGET).
#
# Where the two implementations differ, the *narrower* rule is the one to copy:
# an inventory this script emits has to survive both parsers, so the bracketed
# IPv6 form is the Python candidate's hex-and-colon set, not the shell
# reference's wider one (which also admits "." and "%", i.e. v4-mapped
# addresses and zone ids the candidate then rejects as an unsafe SSH target).
ALIAS_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
SSH_USER_RE='^[A-Za-z0-9._%+-]+$'
NAMESPACE_RE='^[A-Za-z0-9][A-Za-z0-9.-]*$'
SSH_TARGET_RE='^([A-Za-z0-9._%+-]+@)?(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._:-]+)$'

usage() {
  cat <<EOF
Usage: $PROGRAM [options] [HOSTS_FILE]

Convert /etc/hosts format into an inventory that run/collect.sh accepts.
HOSTS_FILE defaults to /etc/hosts; "-" reads stdin. Output goes to stdout
unless --output is given.

Options:
  --user USER            SSH_USER value (default: \$USER)
  --seed HOST            SEED_HOST value; an alias or hostname found in the
                         input resolves to its address
  --rook-ns NS           ROOK_NAMESPACE value
  --rook-operator-ns NS  ROOK_OPERATOR_NAMESPACE value
  --match REGEX          keep only lines whose address or any hostname matches
                         this ERE
  --exclude REGEX        drop lines whose address or any hostname matches this
                         ERE (applied after --match)
  --strip-domain         use the first label of the chosen hostname as the
                         alias ("mon01.lab.local" -> "mon01")
  --keep-loopback        keep loopback, wildcard, broadcast and link-local
                         addresses (skipped by default)
  --ipv6                 keep IPv6 entries, emitted bracketed as [addr]
                         (skipped by default); a zone id ("fe80::1%eth0") or a
                         v4-mapped form is still skipped, because the collect
                         entrypoints do not accept either as an SSH target
  -o, --output FILE      write to FILE instead of stdout
  --force                overwrite an existing --output file
  -h, --help             show this help

An /etc/hosts line may carry several names ("1.2.3.4 aaa aaa.com"); the first
name that is a valid alias wins, and the rest are ignored. Comments (whole-line
and trailing) and blank lines are skipped. Exits non-zero if no entry survives.
EOF
}

die() {
  printf '%s: %s\n' "$PROGRAM" "$*" >&2
  exit 1
}

warn() {
  printf '%s: %s\n' "$PROGRAM" "$*" >&2
}

require_value() {
  [[ $# -ge 2 ]] || die "$1 requires a value"
}

# bash returns 2 (not 1) for a malformed ERE, and prints its own complaint.
# Catch that here so a typo in --match fails the run instead of silently
# matching nothing.
validate_regex() {
  local re=$1 label=$2 status=0
  set +e
  [[ x =~ $re ]]
  status=$?
  set -e
  [[ $status -le 1 ]] || die "invalid regular expression for $label"
}

input_path='/etc/hosts'
input_given=0
ssh_user="${USER:-}"
seed_request=''
rook_namespace=''
rook_operator_namespace=''
match_re=''
exclude_re=''
strip_domain=0
keep_loopback=0
allow_ipv6=0
output_path=''
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) require_value "$@"; ssh_user=$2; shift 2 ;;
    --seed) require_value "$@"; seed_request=$2; shift 2 ;;
    --rook-ns) require_value "$@"; rook_namespace=$2; shift 2 ;;
    --rook-operator-ns) require_value "$@"; rook_operator_namespace=$2; shift 2 ;;
    --match) require_value "$@"; match_re=$2; shift 2 ;;
    --exclude) require_value "$@"; exclude_re=$2; shift 2 ;;
    --strip-domain) strip_domain=1; shift ;;
    --keep-loopback) keep_loopback=1; shift ;;
    --ipv6) allow_ipv6=1; shift ;;
    -o|--output) require_value "$@"; output_path=$2; shift 2 ;;
    --force) force=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    # A bare "-" is the stdin filename, not an option, so it has to be matched
    # before the catch-all that rejects unknown flags.
    -|[!-]*)
      [[ $input_given -eq 0 ]] || die "only one HOSTS_FILE may be given"
      input_path=$1
      input_given=1
      shift
      ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

if [[ $# -gt 0 ]]; then
  [[ $input_given -eq 0 ]] || die "only one HOSTS_FILE may be given"
  input_path=$1
  input_given=1
  shift
  [[ $# -eq 0 ]] || die "only one HOSTS_FILE may be given"
fi

[[ -n "$ssh_user" ]] || die "no SSH user: \$USER is unset, pass --user USER"
# The character class alone would admit a leading "-": both entrypoints reject
# that separately (run/collect.sh's SSH_USER check and _validated_ssh_target in
# ceph_incident_bundle.py), because these strings end up in an ssh argv.
if [[ "$ssh_user" == -* || ! "$ssh_user" =~ $SSH_USER_RE ]]; then
  die "invalid SSH user: $ssh_user"
fi
if [[ -n "$rook_namespace" && ! "$rook_namespace" =~ $NAMESPACE_RE ]]; then
  die "invalid ROOK_NAMESPACE: $rook_namespace"
fi
if [[ -n "$rook_operator_namespace" && ! "$rook_operator_namespace" =~ $NAMESPACE_RE ]]; then
  die "invalid ROOK_OPERATOR_NAMESPACE: $rook_operator_namespace"
fi
[[ -z "$match_re" ]] || validate_regex "$match_re" --match
[[ -z "$exclude_re" ]] || validate_regex "$exclude_re" --exclude

if [[ -n "$output_path" && -e "$output_path" && $force -eq 0 ]]; then
  die "refusing to overwrite $output_path (pass --force)"
fi

source_label=$input_path
if [[ "$input_path" == '-' ]]; then
  input_path=/dev/stdin
  source_label='stdin'
elif [[ ! -r "$input_path" ]]; then
  die "cannot read $input_path"
fi

is_ipv4() {
  local address=$1 octet
  [[ "$address" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
  local saved_ifs=$IFS
  IFS='.'
  for octet in $address; do
    if [[ ${#octet} -gt 1 && "${octet:0:1}" == '0' ]]; then
      IFS=$saved_ifs
      return 1
    fi
    if [[ $((10#$octet)) -gt 255 ]]; then
      IFS=$saved_ifs
      return 1
    fi
  done
  IFS=$saved_ifs
  return 0
}

is_ipv6() {
  local address=$1
  # The trailing group is a zone id ("fe80::1%eth0"), which /etc/hosts on both
  # macOS and Linux does carry.
  [[ "$address" == *:* && "$address" =~ ^[0-9A-Fa-f:.]+(%[A-Za-z0-9._-]+)?$ ]]
}

# Addresses that are never a collectable node: loopback, wildcard, broadcast,
# link-local (both families — 169.254/16 is what an interface autoconfigures
# when DHCP failed) and IPv6 multicast (macOS ships several of these by
# default).
is_uninteresting_address() {
  local address=$1 lowered
  lowered=$(printf '%s' "$address" | tr '[:upper:]' '[:lower:]')
  case "$lowered" in
    127.*|0.0.0.0|255.255.255.255|169.254.*) return 0 ;;
    ::1|::|fe80:*|ff[0-9a-f][0-9a-f]:*) return 0 ;;
  esac
  return 1
}

# A target must also survive the guards both entrypoints apply outside their
# character class: non-empty, and never something ssh would read as an option.
is_safe_target() {
  local target=$1
  [[ -n "$target" && "$target" != -* ]] || return 1
  [[ "$target" =~ $SSH_TARGET_RE ]]
}

entries=''

entry_host_for_alias() {
  local want=$1 entry
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    if [[ "${entry%%=*}" == "$want" ]]; then
      printf '%s' "${entry#*=}"
      return 0
    fi
  done <<<"$entries"
  return 1
}

entry_alias_for_host() {
  local want=$1 entry
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    if [[ "${entry#*=}" == "$want" ]]; then
      printf '%s' "${entry%%=*}"
      return 0
    fi
  done <<<"$entries"
  return 1
}

choose_alias() {
  local name candidate
  for name in "$@"; do
    candidate=$name
    if [[ $strip_domain -eq 1 ]]; then
      candidate=${candidate%%.*}
    fi
    if [[ "$candidate" =~ $ALIAS_RE ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

any_field_matches() {
  local re=$1 field
  shift
  for field in "$@"; do
    if [[ "$field" =~ $re ]]; then
      return 0
    fi
  done
  return 1
}

# Word splitting on whitespace is exactly the /etc/hosts field grammar, but it
# must not glob: a hosts file may legitimately contain "*" in a comment we have
# already stripped, and an unquoted "*" elsewhere would expand to filenames.
set -f

# Names in an /etc/hosts file, keyed by name, so --seed can take an alias or any
# other name on the line. Stored as "name=address" lines like `entries`.
name_index=''
line_no=0
kept=0

while IFS= read -r line || [[ -n "$line" ]]; do
  line_no=$((line_no + 1))
  line=${line%%#*}
  # shellcheck disable=SC2086 # deliberate whitespace field splitting; glob is off
  set -- $line
  [[ $# -gt 0 ]] || continue

  address=$1
  shift

  if [[ $# -eq 0 ]]; then
    warn "line $line_no: address with no hostname, skipped: $address"
    continue
  fi

  if is_ipv4 "$address"; then
    :
  elif is_ipv6 "$address"; then
    if [[ $allow_ipv6 -eq 0 ]]; then
      continue
    fi
  else
    warn "line $line_no: not an IP address, skipped: $address"
    continue
  fi

  if [[ $keep_loopback -eq 0 ]] && is_uninteresting_address "$address"; then
    continue
  fi

  if [[ -n "$match_re" ]] && ! any_field_matches "$match_re" "$address" "$@"; then
    continue
  fi
  if [[ -n "$exclude_re" ]] && any_field_matches "$exclude_re" "$address" "$@"; then
    continue
  fi

  if ! alias_name=$(choose_alias "$@"); then
    warn "line $line_no: no usable alias among: $*"
    continue
  fi

  # The SSH target the collector will build is user@host; IPv6 has to be
  # bracketed to stay inside the accepted target grammar. A zone id or a
  # v4-mapped form does not fit that grammar on both sides, so it is skipped
  # here rather than emitted for one implementation to reject later.
  target=$address
  if is_ipv6 "$address"; then
    target="[$address]"
  fi
  if ! is_safe_target "$ssh_user@$target"; then
    warn "line $line_no: unusable SSH target, skipped: $target"
    continue
  fi

  if existing_host=$(entry_host_for_alias "$alias_name"); then
    if [[ "$existing_host" == "$target" ]]; then
      warn "line $line_no: duplicate entry, skipped: $alias_name=$target"
    else
      die "line $line_no: alias $alias_name maps to both $existing_host and $target; use --exclude or --strip-domain to disambiguate"
    fi
    continue
  fi
  if existing_alias=$(entry_alias_for_host "$target"); then
    warn "line $line_no: $target already collected as alias $existing_alias, skipped alias $alias_name"
    continue
  fi

  entries="$entries$alias_name=$target"$'\n'
  kept=$((kept + 1))
  for name in "$@"; do
    name_index="$name_index$name=$target"$'\n'
  done
done <"$input_path"

set +f

[[ $kept -gt 0 ]] || die "no usable host entries in $source_label"

seed_value=''
if [[ -n "$seed_request" ]]; then
  if seed_value=$(entry_host_for_alias "$seed_request"); then
    :
  elif entry_alias_for_host "$seed_request" >/dev/null; then
    # Already an address in HOSTS; keep it as written.
    seed_value=$seed_request
  else
    seed_value=''
    while IFS= read -r indexed; do
      [[ -n "$indexed" ]] || continue
      if [[ "${indexed%%=*}" == "$seed_request" ]]; then
        seed_value=${indexed#*=}
        break
      fi
    done <<<"$name_index"
  fi
  if [[ -z "$seed_value" ]]; then
    seed_value=$seed_request
    warn "seed $seed_request is not in the generated HOSTS; emitting it verbatim"
  fi
  is_safe_target "$seed_value" || die "invalid seed target: $seed_value"
fi

render_inventory() {
  local entry provenance
  # The provenance comment is the only place unvalidated text reaches the file,
  # and the inventory grammar is line-based: a newline in the input path would
  # turn the rest of that comment into inventory statements (a SEED_HOST the
  # caller never asked for, say). Collapse them instead of emitting them.
  provenance=$(printf '%s' "$source_label" | tr '\n\r' '  ')
  printf '# generated by %s from %s\n' "$PROGRAM" "$provenance"
  printf 'SSH_USER="%s"\n' "$ssh_user"
  if [[ -n "$seed_value" ]]; then
    printf 'SEED_HOST="%s"\n' "$seed_value"
  fi
  if [[ -n "$rook_namespace" ]]; then
    printf 'ROOK_NAMESPACE="%s"\n' "$rook_namespace"
  fi
  if [[ -n "$rook_operator_namespace" ]]; then
    printf 'ROOK_OPERATOR_NAMESPACE="%s"\n' "$rook_operator_namespace"
  fi
  printf 'HOSTS=(\n'
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    printf '  "%s"\n' "$entry"
  done <<<"$entries"
  printf ')\n'
}

if [[ -n "$output_path" ]]; then
  render_inventory >"$output_path"
  warn "wrote $kept host(s) to $output_path"
else
  render_inventory
fi
