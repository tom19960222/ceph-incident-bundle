#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_file_contains() {
  local file=$1 expected=$2
  [[ -f "$file" ]] || fail "missing file: $file"
  grep -qF "$expected" "$file" || fail "expected '$expected' in $file"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

fakebin="$tmpdir/fakebin"
mkdir -p "$fakebin"
fake_log_dir="$tmpdir/var-log-ceph"
fake_var_lib="$tmpdir/var-lib-ceph"
fake_timesyncd_conf="$tmpdir/timesyncd.conf"
fake_timesyncd_conf_d="$tmpdir/timesyncd.conf.d"
mkdir -p "$fake_log_dir" "$fake_var_lib/fsid/mon.a"
mkdir -p "$fake_timesyncd_conf_d"

printf 'current ceph log\n' >"$fake_log_dir/ceph.log"
printf 'rotated ceph log\n' >"$fake_log_dir/ceph.log.1"
printf 'rotated osd log\n' >"$fake_log_dir/ceph-osd.0.log.1"
printf 'compressed ceph log bytes\n' >"$fake_log_dir/ceph.log.2.gz"
printf '%0200d\n' 1 >"$fake_log_dir/ceph-too-large.log"

printf 'fsid = fake\n' >"$fake_var_lib/fsid/mon.a/config"
printf 'secret key material\n' >"$fake_var_lib/fsid/mon.a/keyring"
printf '[Time]\nNTP=192.168.18.1\n' >"$fake_timesyncd_conf"
printf '[Time]\nFallbackNTP=time.cloudflare.com\n' >"$fake_timesyncd_conf_d/10-lab.conf"

cat >"$fakebin/sudo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_SUDO_LOG:?}"
if [[ ${1-} == "-n" ]]; then
  shift
fi
exec "$@"
EOF

cat >"$fakebin/journalctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"--since -24h"*) ;;
  *) printf 'journalctl expected --since -24h, got: %s\n' "$*" >&2; exit 12 ;;
esac
if [[ "${FAKE_JOURNALCTL_NO_CEPH:-}" == "1" ]]; then
  printf 'no entries\n'
  exit 1
fi
if [[ ${FAKE_TIMESYNCD_MISSING:-0} == "1" && "$*" == *"-u systemd-timesyncd"* ]]; then
  printf 'No journal files were found for systemd-timesyncd\n' >&2
  exit 1
fi
printf 'fake journalctl %s\n' "$*"
EOF

cat >"$fakebin/timedatectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${FAKE_TIMESYNCD_MISSING:-0} == "1" ]]; then
  printf 'systemd-timesyncd unavailable for %s\n' "$*" >&2
  exit 1
fi
case "$*" in
  "status") printf 'System clock synchronized: yes\nNTP service: active\n' ;;
  "show-timesync --all") printf 'ServerName=192.168.18.1\nPollIntervalUSec=34min 8s\n' ;;
  "timesync-status") printf 'Server: 192.168.18.1 (192.168.18.1)\nPoll interval: 34min 8s\n' ;;
  *) printf 'fake timedatectl %s\n' "$*" ;;
esac
EOF

cat >"$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'fake podman %s\n' "$*"
EOF

cat >"$fakebin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'fake docker %s\n' "$*"
exit 1
EOF

cat >"$fakebin/cephadm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "ls --format json-pretty") printf '[{"name":"mon.a","style":"cephadm"}]\n' ;;
  *) printf 'fake cephadm %s\n' "$*" ;;
esac
EOF

cat >"$fakebin/timeout" <<'EOF'
#!/usr/bin/env bash
shift
exec "$@"
EOF

cat >"$fakebin/dmesg" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'fake kernel ring buffer\n'
EOF

cat >"$fakebin/hostname" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'monitor01\n'
EOF

cat >"$fakebin/uname" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'Linux monitor01 6.1.0 fake\n'
EOF

cat >"$fakebin/uptime" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'up 1 day\n'
EOF

cat >"$fakebin/free" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'Mem: 1Gi 512Mi 512Mi\n'
EOF

cat >"$fakebin/df" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'Filesystem Type Size Used Avail Mounted on\n'
EOF

cat >"$fakebin/lsblk" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'NAME SIZE TYPE MOUNTPOINT\nsda 100G disk\n'
EOF

cat >"$fakebin/ip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '1: lo: <LOOPBACK,UP>\n'
EOF

cat >"$fakebin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "status systemd-timesyncd --no-pager --plain")
    if [[ ${FAKE_TIMESYNCD_MISSING:-0} == "1" ]]; then
      printf 'Unit systemd-timesyncd.service could not be found.\n' >&2
      exit 3
    fi
    printf 'systemd-timesyncd.service - Network Time Synchronization\nActive: active (running)\n'
    ;;
  *) printf '0 loaded units listed.\n' ;;
esac
EOF

for tool in iostat chronyc ntpq pvs vgs lvs; do
  cat >"$fakebin/$tool" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
tool=${0##*/}
{
  printf '%s' "$tool"
  for arg in "$@"; do
    printf ' <%s>' "$arg"
  done
  printf '\n'
} >>"${FAKE_OPTIONAL_LOG:?}"
printf 'fake optional command %s\n' "$tool"
EOF
done

rm -f "$fakebin/ntpq"

chmod +x "$fakebin"/*

export FAKE_SUDO_LOG="$tmpdir/sudo.log"
export FAKE_OPTIONAL_LOG="$tmpdir/optional.log"
PATH="$fakebin:$PATH"

outdir="$tmpdir/node"
set +e
CEPH_INCIDENT_VAR_LOG_CEPH_DIR="$fake_log_dir" \
CEPH_INCIDENT_VAR_LIB_CEPH_DIR="$fake_var_lib" \
CEPH_INCIDENT_LOG_FILE_CAP_BYTES=128 \
CEPH_INCIDENT_TIMESYNCD_CONF="$fake_timesyncd_conf" \
CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR="$fake_timesyncd_conf_d" \
bash "$ROOT/lib/collect-node.sh" \
  --out "$outdir" \
  --host-alias monitor01 \
  --since "24h" \
  --timeout 5
rc=$?
set -e
if [[ "$rc" != "0" ]]; then
  [[ -f "$outdir/errors.log" ]] && sed -n '1,120p' "$outdir/errors.log" >&2
  fail "collect-node.sh exited $rc"
fi

for artifact in \
  system/hostname.txt \
  system/uname.txt \
  system/uptime.txt \
  resources/free.txt \
  storage/df.txt \
  storage/lsblk.txt \
  network/ip-addr.txt \
  kernel/dmesg.txt \
  time/timedatectl-status.txt \
  time/timedatectl-show-timesync.txt \
  time/timedatectl-timesync-status.txt \
  time/systemd-timesyncd-status.txt \
  time/systemd-timesyncd-journal.txt \
  systemd/failed-units.txt \
  cephadm/cephadm-ls.json \
  logs/ceph-log-listing.txt; do
  [[ -f "$outdir/$artifact" ]] || fail "missing artifact: $artifact"
done

assert_file_contains "$outdir/cephadm/cephadm-ls.json" '"style":"cephadm"'
assert_file_contains "$outdir/kernel/dmesg.txt" 'fake kernel ring buffer'
assert_file_contains "$outdir/time/timedatectl-status.txt" 'System clock synchronized: yes'
assert_file_contains "$outdir/time/timedatectl-show-timesync.txt" 'ServerName=192.168.18.1'
assert_file_contains "$outdir/time/timedatectl-timesync-status.txt" 'Poll interval'
assert_file_contains "$outdir/time/systemd-timesyncd-status.txt" 'Network Time Synchronization'
assert_file_contains "$outdir/time/systemd-timesyncd-journal.txt" 'systemd-timesyncd'
assert_file_contains "$outdir/containers/docker-ps.txt" 'fake docker'
assert_file_contains "$outdir/logs/ceph-log-listing.txt" "$fake_log_dir"
assert_file_contains "$outdir/time/ntpq-peers.txt" 'SKIPPED: command not found: ntpq'
assert_file_contains "$outdir/time/systemd-timesyncd-config/timesyncd.conf" 'NTP=192.168.18.1'
assert_file_contains "$outdir/time/systemd-timesyncd-config/timesyncd.conf.d/10-lab.conf" 'FallbackNTP=time.cloudflare.com'

[[ -f "$outdir/logs/ceph/ceph.log" ]] || fail "missing copied current ceph log"
[[ -f "$outdir/logs/ceph/ceph.log.1" ]] || fail "missing copied rotated ceph log"
[[ -f "$outdir/logs/ceph/ceph-osd.0.log.1" ]] || fail "missing copied rotated osd log"
[[ -f "$outdir/logs/ceph/ceph.log.2.gz" ]] || fail "missing copied gz ceph log"
# R2: oversized logs are tail-captured (not silently dropped) with a marker
[[ -f "$outdir/logs/ceph/ceph-too-large.log" ]] || fail "oversized ceph log should be tail-captured"
toobig_bytes="$(wc -c <"$outdir/logs/ceph/ceph-too-large.log" | tr -d '[:space:]')"
[[ "$toobig_bytes" -le 128 ]] || fail "oversized ceph log tail should be <= cap (got $toobig_bytes)"
[[ -f "$outdir/logs/ceph/ceph-too-large.log.TRUNCATED" ]] || fail "oversized ceph log missing .TRUNCATED marker"
assert_file_contains "$outdir/logs/ceph/ceph-too-large.log.TRUNCATED" "original_bytes="

[[ -f "$outdir/cephadm/var-lib-ceph-configs/fsid/mon.a/config" ]] || fail "missing copied var-lib ceph config"
[[ ! -e "$outdir/cephadm/var-lib-ceph-configs/fsid/mon.a/keyring" ]] || fail "keyring should not be copied from var-lib ceph"
if grep -qF 'keyring' "$outdir/cephadm/var-lib-ceph-listing.txt"; then
  fail "var-lib ceph listing should exclude keyrings"
fi

grep -qF 'iostat <-xz> <1> <3>' "$FAKE_OPTIONAL_LOG" || fail "iostat argv was not preserved"
grep -qF 'pvs <--noheadings> <--separator> < >' "$FAKE_OPTIONAL_LOG" || fail "pvs separator argv was not preserved"
grep -qF 'vgs <--noheadings> <--separator> < >' "$FAKE_OPTIONAL_LOG" || fail "vgs separator argv was not preserved"
grep -qF 'lvs <--noheadings> <--separator> < >' "$FAKE_OPTIONAL_LOG" || fail "lvs separator argv was not preserved"

grep -qF -- '-n dmesg' "$FAKE_SUDO_LOG" || fail "dmesg was not collected through sudo -n"

# C8: dmesg and ceph journal get a heavier timeout than the per-command --timeout (5),
# so large kernel ring / journals are not silently truncated.
assert_file_contains "$outdir/kernel/dmesg.txt" '# timeout: 120s'
assert_file_contains "$outdir/systemd/journal-ceph.txt" '# timeout: 120s'

outdir_no_ceph_journal="$tmpdir/node-no-ceph-journal"
set +e
FAKE_JOURNALCTL_NO_CEPH=1 \
CEPH_INCIDENT_VAR_LOG_CEPH_DIR="$fake_log_dir" \
CEPH_INCIDENT_VAR_LIB_CEPH_DIR="$fake_var_lib" \
CEPH_INCIDENT_LOG_FILE_CAP_BYTES=128 \
bash "$ROOT/lib/collect-node.sh" \
  --out "$outdir_no_ceph_journal" \
  --host-alias kubenode \
  --since "24h" \
  --timeout 5 \
  --skip-logs
rc=$?
set -e
[[ "$rc" == "0" ]] || fail "missing ceph journal should not fail non-ceph node collection (got $rc)"
assert_file_contains "$outdir_no_ceph_journal/systemd/journal-ceph.txt" 'no entries'

missing_timesyncd_outdir="$tmpdir/node-missing-timesyncd"
set +e
FAKE_TIMESYNCD_MISSING=1 \
CEPH_INCIDENT_VAR_LOG_CEPH_DIR="$fake_log_dir" \
CEPH_INCIDENT_VAR_LIB_CEPH_DIR="$fake_var_lib" \
CEPH_INCIDENT_LOG_FILE_CAP_BYTES=128 \
CEPH_INCIDENT_TIMESYNCD_CONF="$tmpdir/missing-timesyncd.conf" \
CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR="$tmpdir/missing-timesyncd.conf.d" \
bash "$ROOT/lib/collect-node.sh" \
  --out "$missing_timesyncd_outdir" \
  --host-alias monitor02 \
  --since "24h" \
  --timeout 5
missing_timesyncd_rc=$?
set -e
if [[ "$missing_timesyncd_rc" != "0" ]]; then
  [[ -f "$missing_timesyncd_outdir/errors.log" ]] && sed -n '1,120p' "$missing_timesyncd_outdir/errors.log" >&2
  fail "missing timesyncd should not fail collect-node.sh, got $missing_timesyncd_rc"
fi

assert_file_contains "$missing_timesyncd_outdir/time/timedatectl-status.txt" 'systemd-timesyncd unavailable'
assert_file_contains "$missing_timesyncd_outdir/time/systemd-timesyncd-status.txt" 'Unit systemd-timesyncd.service could not be found.'
assert_file_contains "$missing_timesyncd_outdir/time/systemd-timesyncd-journal.txt" 'No journal files were found for systemd-timesyncd'
assert_file_contains "$missing_timesyncd_outdir/time/systemd-timesyncd-config/SKIPPED.txt" 'systemd-timesyncd config not found'
