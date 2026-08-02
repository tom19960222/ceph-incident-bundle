"""Self-contained collector streamed to a supported node over SSH stdin."""

from __future__ import annotations

import base64
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


NODE_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("system/hostname.txt", ("hostname",)),
    ("system/uname.txt", ("uname", "-a")),
    ("system/uptime.txt", ("uptime",)),
    ("resources/free.txt", ("free", "-h")),
    ("storage/df.txt", ("df", "-hT")),
    ("network/ip-addr.txt", ("ip", "addr", "show")),
    (
        "systemd/failed-units.txt",
        ("systemctl", "--failed", "--no-pager", "--plain"),
    ),
)
# Optional tools: collected when installed, skipped with a marker when not, and
# never able to turn the node partial (shell `node_run_optional`).  The two
# groups are named for where they sit in the shell reference's collection order:
# the timesyncd journal and its config files land between them.
OPTIONAL_COMMANDS_BEFORE_TIMESYNCD_CONFIG: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("resources/iostat.txt", ("iostat", "-xz", "1", "3")),
    ("time/chronyc-tracking.txt", ("chronyc", "tracking")),
    ("time/chronyc-sources.txt", ("chronyc", "sources", "-v")),
    ("time/ntpq-peers.txt", ("ntpq", "-pn")),
    ("time/timedatectl-status.txt", ("timedatectl", "status")),
    (
        "time/timedatectl-show-timesync.txt",
        ("timedatectl", "show-timesync", "--all"),
    ),
    ("time/timedatectl-timesync-status.txt", ("timedatectl", "timesync-status")),
    (
        "time/systemd-timesyncd-status.txt",
        ("systemctl", "status", "systemd-timesyncd", "--no-pager", "--plain"),
    ),
)
OPTIONAL_COMMANDS_AFTER_TIMESYNCD_CONFIG: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("storage/pvs.txt", ("pvs", "--noheadings", "--separator", " ")),
    ("storage/vgs.txt", ("vgs", "--noheadings", "--separator", " ")),
    ("storage/lvs.txt", ("lvs", "--noheadings", "--separator", " ")),
    ("containers/podman-ps.txt", ("podman", "ps", "-a")),
    ("containers/docker-ps.txt", ("docker", "ps", "-a")),
)
LSBLK_COMMAND = (
    "lsblk",
    "-a",
    "-o",
    "NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
)
CEPHADM_LS_COMMAND = ("cephadm", "ls", "--format", "json-pretty")
ETC_EVIDENCE_FILES = ("os-release", "hosts", "resolv.conf")
# Credential material is pruned before `find` descends into it, so a keyring
# never reaches the listing, let alone the copy loop.
CREDENTIAL_PRUNE: tuple[str, ...] = (
    "(",
    "-iname",
    "*keyring*",
    "-o",
    "-iname",
    "*private_key*",
    "-o",
    "-path",
    "*/.ssh/*",
    ")",
    "-prune",
)
VAR_LIB_CEPH_CONFIG_NAMES: tuple[str, ...] = (
    "(",
    "-name",
    "ceph.conf",
    "-o",
    "-name",
    "*.conf",
    "-o",
    "-name",
    "config",
    "-o",
    "-name",
    "*.config",
    ")",
)
VAR_LIB_CEPH_LISTING_SCRIPT = (
    'for path do\n'
    '  if [ -d "$path" ]; then type=d\n'
    '  elif [ -f "$path" ]; then type=f\n'
    '  else type=o\n'
    '  fi\n'
    '  printf "%s %s\\n" "$type" "$path"\n'
    'done\n'
)
SAFE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SAFE_INVOCATION_ID = re.compile(r"[a-f0-9]{32}\Z")
DEFAULT_VAR_LOG_MAX_BYTES = 10 * 1024**3
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
INDEX_HEADER = (
    "source\tfamily\tcodec\tstored_bytes\tdecoded_bytes\tdisposition\tdetail\n"
)
CODEC_COMMANDS: dict[str, tuple[str, ...]] = {
    "gz": ("gzip", "-dc"),
    "xz": ("xz", "-dc"),
    "bz2": ("bzip2", "-dc"),
    "zst": ("zstd", "-qdc"),
}
# ADR 0010: a manifest entry's exit_code describes whether that one artifact is
# complete evidence, not how the collector group fared. Entries synthesized for
# the /var/log tree have no command exit status of their own, so an incomplete
# one records this instead.
INCOMPLETE_ARTIFACT_EXIT_CODE = 2
# A command that was never reachable, recorded the same way a failed exec is.
MISSING_COMMAND_EXIT_CODE = 127
# Markers that stand in for evidence that was never collected or was discarded:
# the marker file is written in full, but the evidence it accounts for is not.
INCOMPLETE_EVIDENCE_MARKERS = frozenset(
    {
        "INSUFFICIENT-SPACE.txt",
        "MANIFEST-LIMIT.txt",
        "OVER-LIMIT.txt",
        "SCAN-LIMIT.txt",
    }
)
OPAQUE_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tgz",
    ".tbz",
    ".tbz2",
    ".txz",
    ".tzst",
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
)


@dataclass(frozen=True)
class NodeConfig:
    host_alias: str
    invocation_id: str
    command_timeout: int
    skip_logs: bool
    keep_original_logs: bool
    var_log_max_bytes: int | None
    since: str


@dataclass(frozen=True)
class LogCandidate:
    source: Path
    relative: str
    codec: str
    family: str
    order_key: str
    stored_bytes: int
    decoded_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class RawCandidate:
    source: Path
    relative: str


@dataclass(frozen=True)
class CaptureResult:
    status: str
    exit_code: int
    started: str
    ended: str


@dataclass(frozen=True)
class VarLogResult:
    exit_code: int
    # Output-relative paths of artifacts that did land but are not complete
    # evidence. Artifacts that failed outright leave no file, and therefore no
    # manifest entry, so they are not listed here.
    incomplete_artifacts: frozenset[str]


@dataclass(frozen=True)
class JournalResult:
    node_exit_code: int
    manifest_exit_code: int
    command: str | None
    started: str | None
    ended: str | None


class NodeInterrupted(Exception):
    """The node payload received a recoverable termination signal."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_config(encoded: str) -> NodeConfig:
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
        config = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid node collector configuration") from error
    if not isinstance(config, dict):
        raise ValueError("invalid node collector configuration")
    alias = config.get("host_alias")
    invocation_id = config.get("invocation_id")
    timeout = config.get("command_timeout")
    skip_logs = config.get("skip_logs", False)
    keep_original_logs = config.get("keep_original_logs", False)
    var_log_max_bytes = config.get("var_log_max_bytes", DEFAULT_VAR_LOG_MAX_BYTES)
    since = config.get("since", "24h")
    if not isinstance(alias, str) or not SAFE_ALIAS.fullmatch(alias):
        raise ValueError("invalid host alias")
    if not isinstance(invocation_id, str) or not SAFE_INVOCATION_ID.fullmatch(
        invocation_id
    ):
        raise ValueError("invalid invocation identifier")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("invalid command timeout")
    if not isinstance(skip_logs, bool):
        raise ValueError("invalid skip-logs setting")
    if not isinstance(keep_original_logs, bool):
        raise ValueError("invalid keep-original-logs setting")
    if var_log_max_bytes != "unlimited" and (
        isinstance(var_log_max_bytes, bool)
        or not isinstance(var_log_max_bytes, int)
        or var_log_max_bytes < 0
    ):
        raise ValueError("invalid var-log payload cap")
    if not isinstance(since, str) or not since:
        raise ValueError("invalid journal time range")
    return NodeConfig(
        host_alias=alias,
        invocation_id=invocation_id,
        command_timeout=timeout,
        skip_logs=skip_logs,
        keep_original_logs=keep_original_logs,
        var_log_max_bytes=None if var_log_max_bytes == "unlimited" else var_log_max_bytes,
        since=since,
    )


def _manifest_entry_payload(
    host_alias: str,
    artifact: Path,
    command: str,
    exit_code: int,
    started: str,
    ended: str,
) -> bytes:
    entry = {
        "host": host_alias,
        "collector": "collect-node",
        "artifact": str(artifact),
        "command": command,
        "exit_code": exit_code,
        "started": started,
        "ended": ended,
    }
    return (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")


def _append_manifest_entry(
    manifest: Path,
    host_alias: str,
    artifact: Path,
    command: str,
    exit_code: int,
    started: str,
    ended: str,
) -> None:
    payload = _manifest_entry_payload(
        host_alias, artifact, command, exit_code, started, ended
    )
    if manifest.stat().st_size + len(payload) > MANIFEST_MAX_BYTES:
        raise OSError("manifest.jsonl exceeded its receiver safety cap")
    with manifest.open("ab") as output_stream:
        output_stream.write(payload)


def _write_artifact(
    output: Path,
    manifest: Path,
    host_alias: str,
    timeout: int,
    relative_artifact: str,
    command: Sequence[str],
    command_text: str | None = None,
) -> bool:
    """Capture one command into an artifact and index it in the manifest.

    `command_text` overrides what the manifest records.  Only the collector's own
    stable verbs use it (ADR 0010): an expression that names credential patterns
    would be redacted out of the manifest by the content-safety layer, which
    would leave the artifact with no index entry at all.
    """

    artifact = output / relative_artifact
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = _utc_now()
    exit_code = 0
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except FileNotFoundError:
        exit_code = 127
        stdout = b""
        stderr = f"SKIPPED: command not found: {command[0]}\n".encode()
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        stdout = error.stdout or b""
        stderr = (error.stderr or b"") + b"command timed out\n"
    ended = _utc_now()
    header = (
        f"# host: {host_alias}\n"
        "# collector: collect-node\n"
        f"# started: {started}\n"
        f"# timeout: {timeout}s\n"
    ).encode()
    timeout_marker = b""
    if exit_code in (124, 137):
        timeout_marker = (
            f"# TRUNCATED: command timed out after {timeout}s (exit {exit_code})\n"
        ).encode()
    artifact.write_bytes(header + stdout + stderr + timeout_marker)
    recorded_command = shlex.join(command) if command_text is None else command_text
    _append_manifest_entry(
        manifest,
        host_alias,
        artifact,
        recorded_command,
        exit_code,
        started,
        ended,
    )
    if exit_code != 0:
        with (manifest.parent / "errors.log").open("a", encoding="utf-8") as errors:
            errors.write(
                f"{ended} host={host_alias} collector=collect-node "
                f"artifact={artifact} exit={exit_code} "
                f"command={recorded_command}\n"
            )
    return exit_code == 0


def _sudo_path() -> str | None:
    """Where `sudo` is, honouring the offline knob that takes it away."""

    if os.environ.get("CEPH_INCIDENT_TEST_FORCE_NO_SUDO") == "1":
        return None
    return shutil.which("sudo")


def _privileged_argv(command: Sequence[str]) -> tuple[str, ...] | None:
    """Shell `node_run_privileged`: root reads directly, everyone else via sudo.

    Returns None when the read needs sudo and sudo is not installed.  The caller
    records a skip marker rather than retrying the read unprivileged, so a
    partial read can never masquerade as complete evidence.
    """

    if os.geteuid() == 0:
        return tuple(command)
    if _sudo_path() is None:
        return None
    return ("sudo", "-n", *command)


def _find_argv(arguments: Sequence[str]) -> tuple[str, ...]:
    """Shell `node_find0`: privileged scan where possible, plain scan otherwise.

    Unlike a privileged capture, a scan that cannot use sudo still runs: it just
    sees less, and the caller reports only the files it actually copied.
    """

    command: tuple[str, ...] = ("find", *arguments)
    if os.geteuid() == 0 or _sudo_path() is None:
        return command
    return ("sudo", "-n", *command)


def _node_path(variable: str, default: str) -> Path:
    """A node path an offline test may relocate, gated on the same read knob.

    Production always reads the real path: the override only exists so the fake
    world can own `/etc`, `/var/lib/ceph` and the timesyncd config.
    """

    if os.environ.get("CEPH_INCIDENT_TEST_ALLOW_ATIME_READ") == "1":
        return Path(os.environ.get(variable, default))
    return Path(default)


def _write_skipped_evidence(
    artifact: Path,
    manifest: Path,
    host_alias: str,
    reason: str,
    command: Sequence[str] | str,
    exit_code: int,
) -> None:
    """Record evidence that was never collected, and index it in the manifest.

    The shell reference writes the marker file and nothing else.  ADR 0010 makes
    the manifest the index of everything inside the archive, so the marker needs
    an entry too — naming the command that would have produced the evidence and
    a non-zero `exit_code`, because the artifact is not complete evidence.
    """

    started = _utc_now()
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifact.write_text(f"SKIPPED: {reason}\n", encoding="utf-8")
    _append_manifest_entry(
        manifest,
        host_alias,
        artifact,
        command if isinstance(command, str) else shlex.join(command),
        exit_code,
        started,
        _utc_now(),
    )


def _run_optional(
    output: Path,
    manifest: Path,
    config: NodeConfig,
    timeout: int,
    relative_artifact: str,
    command: Sequence[str],
) -> None:
    """Shell `node_run_optional`: an absent tool is a skip, a failure is evidence.

    Neither outcome makes the node partial — an incident node is not required to
    run LVM, containers or any particular time daemon.
    """

    if shutil.which(command[0]) is None:
        _write_skipped_evidence(
            output / relative_artifact,
            manifest,
            config.host_alias,
            f"command not found: {command[0]}",
            command,
            MISSING_COMMAND_EXIT_CODE,
        )
        return
    _write_artifact(
        output, manifest, config.host_alias, timeout, relative_artifact, command
    )


def _run_privileged(
    output: Path,
    manifest: Path,
    config: NodeConfig,
    timeout: int,
    relative_artifact: str,
    command: Sequence[str],
    command_text: str | None = None,
) -> bool:
    """Capture a privileged read, or record why it could not be attempted."""

    argv = _privileged_argv(command)
    if argv is None:
        _write_skipped_evidence(
            output / relative_artifact,
            manifest,
            config.host_alias,
            f"sudo command not found for privileged read: {command[0]}",
            command if command_text is None else command_text,
            MISSING_COMMAND_EXIT_CODE,
        )
        # A node that never granted the collector sudo is not a failed
        # collection, exactly as in the shell reference.
        return True
    return _write_artifact(
        output,
        manifest,
        config.host_alias,
        timeout,
        relative_artifact,
        argv,
        command_text,
    )


def _stat_or_none(path: Path, *, follow_symlinks: bool = True) -> os.stat_result | None:
    """Stat a node path, answering "cannot tell" instead of raising.

    A daemon directory under `/var/lib/ceph` is 0750, so a collector running as
    an ordinary user meets paths it may not stat on every real host. Before
    Python 3.13 `pathlib`'s predicates only swallow ENOENT-shaped errors, so
    EACCES escaped `is_symlink()` and took every artifact for the host with it
    (#50) — on a lab node running 3.12 it still does. The shell reference has no
    such cliff: `[[ -d ]]` on an untraversable path is simply false. Callers get
    the same answer here, and a copy still falls through to its privileged read.
    """

    try:
        return path.stat() if follow_symlinks else path.lstat()
    except OSError:
        return None


def _is_symlink(path: Path) -> bool:
    info = _stat_or_none(path, follow_symlinks=False)
    return info is not None and stat.S_ISLNK(info.st_mode)


def _is_regular_file(path: Path) -> bool:
    info = _stat_or_none(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def _is_directory(path: Path) -> bool:
    info = _stat_or_none(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _copy_evidence(
    source: Path, destination: Path, manifest: Path, host_alias: str
) -> bool:
    """Copy one node file into the archive and index the copy (ADR 0010).

    `command` records the absolute source path rather than the read itself: the
    safe read has three shapes depending on EUID and sudo, so it is not a stable
    contract, while the source path always lets an operator trace the evidence.
    """

    started = _utc_now()
    command = f"collect-node copy {source}"
    read_source = source
    if _is_symlink(source):
        # `/etc/resolv.conf` is a symlink on most systemd hosts and the shell
        # reference follows it.  Resolving first keeps the read itself
        # no-follow, so only this deliberate indirection is honoured.  The
        # source path an operator asked for is still what gets reported.
        read_source = source.resolve()
    status, _ = _decode_to_file(read_source, "plain", destination, None)
    if status != "ok":
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(
            f"SKIPPED: copy failed: {source}\n", encoding="utf-8"
        )
        _append_manifest_entry(
            manifest,
            host_alias,
            destination,
            command,
            INCOMPLETE_ARTIFACT_EXIT_CODE,
            started,
            _utc_now(),
        )
        return False
    _append_manifest_entry(
        manifest, host_alias, destination, command, 0, started, _utc_now()
    )
    return True


def _find_files(root: Path, expression: Sequence[str], timeout: int) -> list[Path]:
    """Enumerate files under `root` with `find -print0`.

    `timeout` is the heavy bound, not the per-command one: the shell reference
    leaves these scans unbounded, so a busy `/var/lib/ceph` must not be cut
    short at the 20s a single capture gets.  A scan that fails or times out
    yields nothing rather than raising — the shell reference also lets its
    `find` pipeline fail open and reports only what it managed to copy.
    """

    command = _find_argv((str(root), *expression, "-print0"))
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    paths: list[Path] = []
    for encoded_path in result.stdout.split(b"\0"):
        if not encoded_path:
            continue
        path = Path(os.fsdecode(encoded_path))
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        # `relative_to` is lexical, so a `..` inside the scan root would still
        # look contained while the copy destination escaped the archive.
        if ".." in relative.parts:
            continue
        paths.append(path)
    return sorted(paths)


def _collect_timesyncd_config(
    output: Path, manifest: Path, config: NodeConfig, scan_timeout: int
) -> bool:
    """Copy the timesyncd config and every `conf.d` drop-in, file by file."""

    conf = _node_path(
        "CEPH_INCIDENT_TIMESYNCD_CONF", "/etc/systemd/timesyncd.conf"
    )
    conf_directory = _node_path(
        "CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR", "/etc/systemd/timesyncd.conf.d"
    )
    destination = output / "time" / "systemd-timesyncd-config"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    copied = False
    complete = True
    if _is_regular_file(conf):
        if _copy_evidence(
            conf, destination / "timesyncd.conf", manifest, config.host_alias
        ):
            copied = True
        else:
            complete = False
    if _is_directory(conf_directory):
        for source in _find_files(
            conf_directory,
            ("-maxdepth", "1", "-type", "f", "-name", "*.conf"),
            scan_timeout,
        ):
            relative = source.relative_to(conf_directory)
            if _copy_evidence(
                source,
                destination / "timesyncd.conf.d" / relative,
                manifest,
                config.host_alias,
            ):
                copied = True
            else:
                complete = False
    if not copied:
        _write_skipped_evidence(
            destination / "SKIPPED.txt",
            manifest,
            config.host_alias,
            f"systemd-timesyncd config not found at {conf} or {conf_directory}",
            f"collect-node copy {conf} {conf_directory}",
            INCOMPLETE_ARTIFACT_EXIT_CODE,
        )
    return complete


def _copy_etc_evidence(output: Path, manifest: Path, config: NodeConfig) -> bool:
    """Copy the readable `/etc` identity files the shell reference collects."""

    etc = _node_path("CEPH_INCIDENT_ETC_DIR", "/etc")
    complete = True
    for name in ETC_EVIDENCE_FILES:
        source = etc / name
        if not os.access(source, os.R_OK):
            continue
        if not _copy_evidence(
            source, output / "system" / name, manifest, config.host_alias
        ):
            complete = False
    return complete


def _collect_var_lib_ceph(
    output: Path,
    manifest: Path,
    config: NodeConfig,
    timeout: int,
    scan_timeout: int,
) -> bool:
    """List `/var/lib/ceph` and copy its configs, never its credentials."""

    ceph_directory = _node_path("CEPH_INCIDENT_VAR_LIB_CEPH_DIR", "/var/lib/ceph")
    listing_artifact = "cephadm/var-lib-ceph-listing.txt"
    listing_command = (
        "find",
        str(ceph_directory),
        "-maxdepth",
        "3",
        *CREDENTIAL_PRUNE,
        "-o",
        "-exec",
        "sh",
        "-c",
        VAR_LIB_CEPH_LISTING_SCRIPT,
        "sh",
        "{}",
        "+",
    )
    # The find expression names the credential patterns it prunes, so recording
    # it verbatim would make the content-safety layer redact this manifest line
    # away and leave the listing unindexed.  A stable collector verb keeps the
    # manifest an index, exactly as ADR 0010 does for copied evidence.
    listing_command_text = f"collect-node list {ceph_directory}"
    if not _is_directory(ceph_directory):
        _write_skipped_evidence(
            output / listing_artifact,
            manifest,
            config.host_alias,
            f"{ceph_directory} is not a readable directory on this node",
            listing_command_text,
            INCOMPLETE_ARTIFACT_EXIT_CODE,
        )
        return True
    if not _run_privileged(
        output,
        manifest,
        config,
        timeout,
        listing_artifact,
        listing_command,
        listing_command_text,
    ):
        # A listing the collector could not produce leaves no way to tell which
        # configs were missed, so the copy loop is not started either.  A
        # listing skipped for want of sudo is a different case: it reports
        # success, and the copy loop still gathers what it can read.
        return False
    destination = output / "cephadm" / "var-lib-ceph-configs"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    complete = True
    for source in _find_files(
        ceph_directory,
        (
            "-maxdepth",
            "4",
            *CREDENTIAL_PRUNE,
            "-o",
            "-type",
            "f",
            *VAR_LIB_CEPH_CONFIG_NAMES,
        ),
        scan_timeout,
    ):
        relative = source.relative_to(ceph_directory)
        if not _copy_evidence(
            source, destination / relative, manifest, config.host_alias
        ):
            complete = False
    return complete


def _collect_node_evidence(
    output: Path, manifest: Path, config: NodeConfig
) -> bool:
    """Collect the node evidence surface, in the shell reference's order.

    Returns False when a required capture or copy did not land.  Optional tools
    and best-effort privileged reads never make the node partial: a node without
    LVM, containers, cephadm or a time daemon is a normal node, not a failure.
    """

    timeout = config.command_timeout
    # dmesg and the ceph journal can be large under load, so the shell reference
    # gives them a heavier bound than the per-command timeout.
    heavy_timeout = max(timeout, 120)
    since = _journal_since(config.since)
    complete = True

    if not _run_privileged(
        output, manifest, config, timeout, "storage/lsblk.txt", LSBLK_COMMAND
    ):
        complete = False
    if not _run_privileged(
        output, manifest, config, heavy_timeout, "kernel/dmesg.txt", ("dmesg", "-T")
    ):
        complete = False

    # The ceph journal is optional on sudo itself: the shell reference always
    # asks for it through `sudo -n`, and simply skips when sudo is absent.
    ceph_journal = (
        "sudo",
        "-n",
        "journalctl",
        "--since",
        since,
        "-u",
        "ceph*",
        "--no-pager",
    )
    if _sudo_path() is None:
        _write_skipped_evidence(
            output / "systemd" / "journal-ceph.txt",
            manifest,
            config.host_alias,
            "command not found: sudo",
            ceph_journal,
            MISSING_COMMAND_EXIT_CODE,
        )
    else:
        _write_artifact(
            output,
            manifest,
            config.host_alias,
            heavy_timeout,
            "systemd/journal-ceph.txt",
            ceph_journal,
        )

    for relative_artifact, command in OPTIONAL_COMMANDS_BEFORE_TIMESYNCD_CONFIG:
        _run_optional(output, manifest, config, timeout, relative_artifact, command)
    _run_privileged(
        output,
        manifest,
        config,
        timeout,
        "time/systemd-timesyncd-journal.txt",
        ("journalctl", "--since", since, "-u", "systemd-timesyncd", "--no-pager"),
    )
    if not _collect_timesyncd_config(output, manifest, config, heavy_timeout):
        complete = False
    for relative_artifact, command in OPTIONAL_COMMANDS_AFTER_TIMESYNCD_CONFIG:
        _run_optional(output, manifest, config, timeout, relative_artifact, command)

    if shutil.which(CEPHADM_LS_COMMAND[0]) is None:
        _write_skipped_evidence(
            output / "cephadm" / "cephadm-ls.json",
            manifest,
            config.host_alias,
            f"command not found: {CEPHADM_LS_COMMAND[0]}",
            CEPHADM_LS_COMMAND,
            MISSING_COMMAND_EXIT_CODE,
        )
    else:
        _run_privileged(
            output,
            manifest,
            config,
            timeout,
            "cephadm/cephadm-ls.json",
            CEPHADM_LS_COMMAND,
        )

    if not _copy_etc_evidence(output, manifest, config):
        complete = False
    if not _collect_var_lib_ceph(output, manifest, config, timeout, heavy_timeout):
        complete = False
    return complete


def _codec_for(path: str) -> str:
    lowered = path.lower()
    for suffix, codec in ((".gz", "gz"), (".xz", "xz"), (".bz2", "bz2"), (".zst", "zst")):
        if lowered.endswith(suffix):
            return codec
    return "plain"


def _is_sensitive_path(path: str) -> bool:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    return (
        "keyring" in lowered
        or ".ssh" in lowered
        or "id_ed25519" in lowered
        or "private_key" in lowered
        or any(
            name.endswith(suffix) or f"{suffix}." in name
            for suffix in (".pem", ".key", ".crt", ".pfx", ".p12")
        )
    )


def _is_opaque_archive(path: str) -> bool:
    return path.lower().endswith(OPAQUE_ARCHIVE_SUFFIXES)


def _family_and_order(relative: str, codec: str) -> tuple[str, str]:
    directory, separator, name = relative.rpartition("/")
    stem = name
    suffix = {"gz": ".gz", "xz": ".xz", "bz2": ".bz2", "zst": ".zst"}.get(codec)
    if suffix is not None and stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    family = stem
    order_key = "900000000000"
    numbered = re.fullmatch(r"(.+)\.([0-9]+)", stem)
    compact_date = re.fullmatch(r"(.+)-([0-9]{8})", stem)
    dashed_date = re.fullmatch(r"(.+)-([0-9]{4})-([0-9]{2})-([0-9]{2})", stem)
    if numbered is not None:
        family = numbered.group(1)
        number_text = numbered.group(2).lstrip("0") or "0"
        if len(number_text) <= 9:
            order_key = f"1{999_999_999 - int(number_text):09d}"
        else:
            order_key = f"0-oversize-{number_text}"
    elif compact_date is not None:
        family = compact_date.group(1)
        order_key = f"0{compact_date.group(2)}"
    elif dashed_date is not None:
        family = dashed_date.group(1)
        order_key = "0" + "".join(dashed_date.groups()[1:])
    if separator:
        family = f"{directory}/{family}"
    return family, order_key


def _merged_destination(output: Path, family: str) -> Path:
    parts = family.split("/")
    destination = output / "merged" / "tree"
    for directory in parts[:-1]:
        destination = destination / "dirs" / directory
    return destination / "files" / f"{parts[-1]}.merged"


def _safe_read_command(source: Path) -> tuple[str, ...] | None:
    command = ("dd", f"if={source}", "iflag=noatime,nofollow", "status=none")
    if os.environ.get("CEPH_INCIDENT_TEST_FORCE_SUDO") == "1":
        return ("sudo", "-n", *command)
    try:
        source_stat = source.lstat()
    except PermissionError:
        # Not being allowed to stat a path says nothing about whether it can be
        # read as root, and the privileged scan that found it already proved it
        # exists. The shell reference never stats either: `-O` is simply false
        # on a directory it cannot traverse, so it falls through to `sudo -n dd`
        # and comes back with the evidence (#50). `nofollow` still refuses a
        # symlink, which is the guarantee the stat was there to make.
        return None if _sudo_path() is None else ("sudo", "-n", *command)
    except OSError:
        return None
    if not stat.S_ISREG(source_stat.st_mode):
        return None
    if os.geteuid() == 0 or source_stat.st_uid == os.geteuid():
        return command
    if _sudo_path() is None:
        return None
    return ("sudo", "-n", *command)


def _stop_stream_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _decode_to_file(
    source: Path,
    codec: str,
    destination: Path,
    limit: int | None,
) -> tuple[str, int]:
    read_command = _safe_read_command(source)
    if read_command is None:
        return "read-failed", 0
    codec_command = CODEC_COMMANDS.get(codec)
    if codec_command is not None and shutil.which(codec_command[0]) is None:
        return "missing-codec", 0
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reader_error = tempfile.TemporaryFile(dir=destination.parent)
    decoder_error = tempfile.TemporaryFile(dir=destination.parent)
    reader: subprocess.Popen[bytes] | None = None
    decoder: subprocess.Popen[bytes] | None = None
    written = 0
    status = "ok"
    try:
        reader = subprocess.Popen(
            list(read_command), stdout=subprocess.PIPE, stderr=reader_error
        )
        stream = reader.stdout
        if codec_command is not None:
            decoder = subprocess.Popen(
                list(codec_command),
                stdin=reader.stdout,
                stdout=subprocess.PIPE,
                stderr=decoder_error,
            )
            assert reader.stdout is not None
            reader.stdout.close()
            stream = decoder.stdout
        assert stream is not None
        with destination.open("xb") as output:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if limit is not None and written > limit:
                    status = "over-limit"
                    break
                output.write(chunk)
        if status == "over-limit":
            if decoder is not None:
                _stop_stream_process(decoder)
            _stop_stream_process(reader)
        else:
            if decoder is not None and decoder.wait() != 0:
                status = "decode-failed"
            if reader.wait() != 0 and status == "ok":
                status = "read-failed"
    except (FileNotFoundError, OSError):
        status = "read-failed"
    finally:
        if decoder is not None:
            _stop_stream_process(decoder)
        if reader is not None:
            _stop_stream_process(reader)
        reader_error.close()
        decoder_error.close()
    if status != "ok":
        destination.unlink(missing_ok=True)
    return status, written


def _discover_log_files(
    root: Path, scan_limit: int, entry_limit: int, scratch: Path
) -> tuple[list[Path], list[str], str | None]:
    command: tuple[str, ...] = ("find", str(root), "-type", "f", "-print0")
    if os.environ.get("CEPH_INCIDENT_TEST_FORCE_SUDO") == "1":
        command = ("sudo", "-n", *command)
    elif (
        os.environ.get("CEPH_INCIDENT_TEST_ALLOW_ATIME_READ") != "1"
        and os.geteuid() != 0
        and _sudo_path() is not None
    ):
        command = ("sudo", "-n", *command)
    errors_file = tempfile.TemporaryFile(dir=scratch)
    process: subprocess.Popen[bytes] | None = None
    scan_payload = bytearray()
    try:
        process = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=errors_file
        )
        assert process.stdout is not None
        while chunk := process.stdout.read(1024 * 1024):
            scan_payload.extend(chunk)
            if len(scan_payload) > scan_limit:
                _stop_stream_process(process)
                errors_file.close()
                return [], [], "bytes"
        return_code = process.wait()
    except (FileNotFoundError, OSError) as error:
        errors_file.close()
        return [], [str(error)], None
    finally:
        if process is not None:
            _stop_stream_process(process)

    errors_file.seek(0)
    diagnostic = errors_file.read(4096).decode(errors="replace").splitlines()
    errors_file.close()
    errors: list[str] = []
    if return_code != 0:
        errors = diagnostic[:1] or [f"find exited {return_code}"]
    sources: list[Path] = []
    for encoded_path in scan_payload.split(b"\0"):
        if not encoded_path:
            continue
        if len(sources) >= entry_limit:
            return [], errors, "entries"
        source = Path(os.fsdecode(bytes(encoded_path)))
        try:
            source.relative_to(root)
        except ValueError:
            errors.append(f"find returned path outside root: {source}")
            continue
        sources.append(source)
    return sources, errors, None


def _available_bytes(output: Path) -> int | None:
    try:
        result = subprocess.run(
            ["df", "-Pk", str(output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    if len(fields) < 4 or not fields[3].isdecimal():
        return None
    return int(fields[3]) * 1024


def _remove_owned_tree(path: Path, boundary: Path) -> None:
    if not os.path.lexists(path):
        return
    boundary_resolved = boundary.resolve(strict=True)
    candidate = path.parent.resolve(strict=True) / path.name
    try:
        candidate.relative_to(boundary_resolved)
    except ValueError as error:
        raise OSError(f"cleanup target escaped owned workspace: {path}") from error
    if candidate == boundary_resolved or candidate.is_symlink():
        raise OSError(f"cleanup target is not an owned child directory: {path}")
    shutil.rmtree(candidate)


def _escape_tsv(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _contains_nul(path: Path) -> bool:
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if b"\0" in chunk:
                return True
    return False


def _manifest_generated_tree(
    output: Path,
    manifest: Path,
    host_alias: str,
    collector_exit_code: int,
    started: str,
    journal_result: JournalResult,
    incomplete_artifacts: frozenset[str],
) -> int:
    """Append one manifest entry per file in the generated /var/log tree.

    Each entry's `exit_code` describes that one artifact (ADR 0010), so it is
    unrelated to `collector_exit_code`, which is only carried through so the
    manifest-cap degradation below can turn the whole collection partial.
    """
    try:
        requested_limit = int(
            os.environ.get(
                "CEPH_INCIDENT_TEST_MANIFEST_MAX_BYTES", str(MANIFEST_MAX_BYTES)
            )
        )
    except ValueError:
        requested_limit = MANIFEST_MAX_BYTES
    manifest_limit = min(max(requested_limit, 0), MANIFEST_MAX_BYTES)

    def build_payload() -> bytes | None:
        available = manifest_limit - manifest.stat().st_size
        pending = bytearray()
        ended = _utc_now()
        for root, directories, filenames in os.walk(output):
            directories.sort()
            for filename in sorted(filenames):
                artifact = Path(root, filename)
                relative = artifact.relative_to(output).as_posix()
                command = "collect-var-log /var/log"
                artifact_started = started
                artifact_ended = ended
                if relative == "journal-all-since.txt":
                    artifact_exit_code = journal_result.manifest_exit_code
                    if journal_result.command:
                        command = journal_result.command
                        artifact_started = journal_result.started or started
                        artifact_ended = journal_result.ended or ended
                elif (
                    relative in INCOMPLETE_EVIDENCE_MARKERS
                    or relative in incomplete_artifacts
                ):
                    artifact_exit_code = INCOMPLETE_ARTIFACT_EXIT_CODE
                else:
                    artifact_exit_code = 0
                entry = _manifest_entry_payload(
                    host_alias,
                    artifact,
                    command,
                    artifact_exit_code,
                    artifact_started,
                    artifact_ended,
                )
                if len(pending) + len(entry) > available:
                    return None
                pending.extend(entry)
        return bytes(pending)

    payload = build_payload()
    if payload is None:
        _discard_log_payload(output)
        (output / "MANIFEST-LIMIT.txt").write_text(
            f"manifest_max_bytes={manifest_limit}\n"
            "status=partial-var-log-payload-discarded\n",
            encoding="utf-8",
        )
        collector_exit_code = 2
        payload = build_payload()
        if payload is None:
            raise OSError("minimal /var/log manifest exceeded its receiver safety cap")
    with manifest.open("ab") as output_stream:
        output_stream.write(payload)
    return collector_exit_code


def _collect_var_logs(
    root: Path,
    output: Path,
    config: NodeConfig,
) -> VarLogResult:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _is_directory(root):
        (output / "SKIPPED.txt").write_text(
            f"SKIPPED: {root} is not a directory\n", encoding="utf-8"
        )
        # The marker is written in full, but the evidence it stands for is not
        # here, so ADR 0010 forbids indexing it as complete.  Like the reference,
        # a node without `/var/log` is not partial.
        return VarLogResult(0, frozenset({"SKIPPED.txt"}))

    incomplete: set[str] = set()
    index_lines = [INDEX_HEADER]
    errors: list[str] = []
    warnings: list[str] = []
    opaque: list[str] = []
    sensitive: list[str] = []
    candidates: list[LogCandidate] = []
    raw_candidates: list[RawCandidate] = []
    scratch = output / ".scratch"
    scratch.mkdir(mode=0o700)
    try:
        scan_limit = int(
            os.environ.get("CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES", str(64 * 1024**2))
        )
    except ValueError:
        scan_limit = 64 * 1024**2
    try:
        entry_limit = int(
            os.environ.get("CEPH_INCIDENT_VAR_LOG_MAX_ENTRIES", "100000")
        )
    except ValueError:
        entry_limit = 100000
    try:
        reserve_bytes = int(
            os.environ.get(
                "CEPH_INCIDENT_VAR_LOG_FREE_RESERVE_BYTES", str(1024**3)
            )
        )
    except ValueError:
        reserve_bytes = -1
    available_bytes = _available_bytes(output)
    if (
        reserve_bytes >= 0
        and available_bytes is not None
        and available_bytes < reserve_bytes + scan_limit
    ):
        (output / "INDEX.tsv").write_text(INDEX_HEADER, encoding="utf-8")
        (output / "INSUFFICIENT-SPACE.txt").write_text(
            f"available_bytes={available_bytes}\n"
            f"reserve_bytes={reserve_bytes}\n"
            f"scan_staging_bytes={scan_limit}\n"
            "status=not-collected\n",
            encoding="utf-8",
        )
        _remove_owned_tree(scratch, output)
        # Nothing was scanned, so the header-only index is not a full listing.
        return VarLogResult(2, frozenset({"INDEX.tsv"}))
    sources, scan_errors, scan_limit_reason = _discover_log_files(
        root, scan_limit, entry_limit, scratch
    )
    if scan_limit_reason is not None:
        (output / "INDEX.tsv").write_text(INDEX_HEADER, encoding="utf-8")
        marker = output / "SCAN-LIMIT.txt"
        if scan_limit_reason == "bytes":
            marker.write_text(
                f"scan_max_bytes={scan_limit}\n"
                "status=not-collected-metadata-limit\n",
                encoding="utf-8",
            )
        else:
            marker.write_text(
                f"max_entries={entry_limit}\n"
                "status=not-collected-metadata-limit\n",
                encoding="utf-8",
            )
        _remove_owned_tree(scratch, output)
        # The scan was cut short, so the header-only index is not a full listing.
        return VarLogResult(2, frozenset({"INDEX.tsv"}))
    errors.extend(f"scan\t{_escape_tsv(error)}\n" for error in scan_errors)
    partial = bool(scan_errors)
    estimated = 0
    over_limit = False
    for sequence, source in enumerate(sources):
        relative = source.relative_to(root).as_posix()
        try:
            source_stat = source.lstat()
        except OSError:
            partial = True
            errors.append(f"{_escape_tsv(relative)}\tstat-failed\n")
            continue
        codec = _codec_for(relative)
        if _is_sensitive_path(relative):
            sensitive.append(f"{_escape_tsv(relative)}\n")
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t0\t"
                "skipped-sensitive\tforbidden path\n"
            )
            continue
        if over_limit:
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t0\t"
                "not-inspected\tearlier file exceeded cap\n"
            )
            continue
        if _is_opaque_archive(relative):
            raw_candidates.append(
                RawCandidate(source, relative)
            )
            opaque.append(f"{_escape_tsv(relative)}\n")
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\topaque\t{source_stat.st_size}\t0\t"
                "raw\tnot auto-merged\n"
            )
            estimated += source_stat.st_size
            continue
        measure = scratch / f"measure-{sequence}"
        status, decoded_size = _decode_to_file(
            source, codec, measure, config.var_log_max_bytes
        )
        measure.unlink(missing_ok=True)
        if status == "over-limit":
            over_limit = True
            cap = config.var_log_max_bytes
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t>{cap}\t"
                "over-limit\tdecoded stream exceeded cap\n"
            )
            continue
        if status != "ok":
            partial = True
            if status == "missing-codec":
                codec_command = CODEC_COMMANDS[codec][0]
                errors.append(
                    f"{_escape_tsv(relative)}\tmissing-codec:{codec_command}\n"
                )
            else:
                errors.append(f"{_escape_tsv(relative)}\tdecode-failed\n")
            raw_candidates.append(
                RawCandidate(source, relative)
            )
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t0\t"
                f"raw-partial\t{'missing codec' if status == 'missing-codec' else 'decode failed'}\n"
            )
            estimated += source_stat.st_size
            continue
        inspection = scratch / f"inspect-{sequence}"
        status, _ = _decode_to_file(source, codec, inspection, config.var_log_max_bytes)
        if status != "ok":
            inspection.unlink(missing_ok=True)
            partial = True
            errors.append(f"{_escape_tsv(relative)}\tdecode-failed\n")
            raw_candidates.append(RawCandidate(source, relative))
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t0\t"
                "raw-partial\tdecode failed\n"
            )
            estimated += source_stat.st_size
            continue
        binary = _contains_nul(inspection)
        inspection.unlink(missing_ok=True)
        if binary:
            raw_candidates.append(
                RawCandidate(source, relative)
            )
            opaque.append(f"{_escape_tsv(relative)}\n")
            index_lines.append(
                f"{_escape_tsv(relative)}\t-\t{codec}\t{source_stat.st_size}\t"
                f"{decoded_size}\traw\tbinary or unknown\n"
            )
            estimated += source_stat.st_size
            continue
        family, order_key = _family_and_order(relative, codec)
        candidates.append(
            LogCandidate(
                source=source,
                relative=relative,
                codec=codec,
                family=family,
                order_key=order_key,
                stored_bytes=source_stat.st_size,
                decoded_bytes=decoded_size,
                mtime_ns=source_stat.st_mtime_ns,
            )
        )
        index_lines.append(
            f"{_escape_tsv(relative)}\t{_escape_tsv(family)}\t{codec}\t"
            f"{source_stat.st_size}\t{decoded_size}\tmerge-candidate\toldest-to-newest\n"
        )
        estimated += decoded_size + 256
        if config.keep_original_logs:
            estimated += source_stat.st_size

    if over_limit or (
        config.var_log_max_bytes is not None and estimated > config.var_log_max_bytes
    ):
        (output / "INDEX.tsv").write_text("".join(index_lines), encoding="utf-8")
        (output / "OVER-LIMIT.txt").write_text(
            f"estimated_output_bytes={estimated}\n"
            f"max_bytes={config.var_log_max_bytes}\n"
            "status=not-collected\n",
            encoding="utf-8",
        )
        _remove_owned_tree(scratch, output)
        return VarLogResult(2, frozenset())

    available_bytes = _available_bytes(output)
    if (
        reserve_bytes >= 0
        and available_bytes is not None
        and available_bytes < estimated + reserve_bytes
    ):
        (output / "INDEX.tsv").write_text("".join(index_lines), encoding="utf-8")
        (output / "INSUFFICIENT-SPACE.txt").write_text(
            f"estimated_output_bytes={estimated}\n"
            f"available_bytes={available_bytes}\n"
            f"reserve_bytes={reserve_bytes}\n"
            "status=not-collected\n",
            encoding="utf-8",
        )
        _remove_owned_tree(scratch, output)
        return VarLogResult(2, frozenset())

    payload_bytes = 0
    hard_cap_hit = False
    for sequence, candidate in enumerate(raw_candidates):
        destination = output / "raw" / candidate.relative
        temporary = scratch / f"raw-{sequence}"
        remaining = None
        if config.var_log_max_bytes is not None:
            remaining = config.var_log_max_bytes - payload_bytes
        status, _ = _decode_to_file(candidate.source, "plain", temporary, remaining)
        if status == "over-limit":
            hard_cap_hit = True
            break
        if status != "ok":
            partial = True
            errors.append(f"{_escape_tsv(candidate.relative)}\tread-failed\n")
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(temporary, destination)
        payload_bytes += destination.stat().st_size

    for sequence, candidate in (() if hard_cap_hit else sorted(
        enumerate(candidates), key=lambda item: (item[1].family, item[1].order_key, item[0])
    )):
        destination = _merged_destination(output, candidate.family)
        merged_artifact = destination.relative_to(output).as_posix()
        decoded = scratch / f"merge-{sequence}"
        remaining = None
        if config.var_log_max_bytes is not None:
            remaining = config.var_log_max_bytes - payload_bytes
        status, _ = _decode_to_file(candidate.source, candidate.codec, decoded, remaining)
        if status == "over-limit":
            hard_cap_hit = True
            break
        if status != "ok":
            partial = True
            incomplete.add(merged_artifact)
            errors.append(f"{_escape_tsv(candidate.relative)}\tmerge-read-failed\n")
            raw_temporary = scratch / f"merge-raw-{sequence}"
            raw_status, _ = _decode_to_file(
                candidate.source, "plain", raw_temporary, remaining
            )
            if raw_status == "over-limit":
                hard_cap_hit = True
                break
            if raw_status == "ok":
                raw_destination = output / "raw" / candidate.relative
                raw_destination.parent.mkdir(
                    mode=0o700, parents=True, exist_ok=True
                )
                os.replace(raw_temporary, raw_destination)
                payload_bytes += raw_destination.stat().st_size
            else:
                errors.append(
                    f"{_escape_tsv(candidate.relative)}\t"
                    "raw-preserve-failed-after-merge-read\n"
                )
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        header = (
            f"===== source={_escape_tsv(candidate.relative)} "
            f"mtime_epoch={candidate.mtime_ns // 1_000_000_000} "
            f"stored_bytes={candidate.stored_bytes} codec={candidate.codec} =====\n"
        ).encode()
        segment_size = len(header) + decoded.stat().st_size + 1
        if config.var_log_max_bytes is not None and payload_bytes + segment_size > config.var_log_max_bytes:
            decoded.unlink(missing_ok=True)
            hard_cap_hit = True
            break
        segment_start = destination.stat().st_size if destination.exists() else 0
        try:
            with destination.open("ab") as merged, decoded.open("rb") as decoded_stream:
                merged.write(header)
                shutil.copyfileobj(decoded_stream, merged, length=1024 * 1024)
                merged.write(b"\n")
        except OSError:
            if destination.exists():
                with destination.open("r+b") as merged:
                    merged.truncate(segment_start)
            partial = True
            incomplete.add(merged_artifact)
            errors.append(
                f"{_escape_tsv(candidate.relative)}\tmerge-write-failed\n"
            )
            decoded.unlink(missing_ok=True)
            continue
        decoded.unlink(missing_ok=True)
        payload_bytes += segment_size
        try:
            final_stat = candidate.source.lstat()
        except OSError:
            final_stat = None
        if final_stat is None or (
            final_stat.st_size != candidate.stored_bytes
            or final_stat.st_mtime_ns != candidate.mtime_ns
        ):
            if candidate.codec == "plain" and candidate.order_key == "900000000000":
                warnings.append(
                    f"{_escape_tsv(candidate.relative)}\tactive-log-changed-during-collection\n"
                )
            else:
                partial = True
                incomplete.add(merged_artifact)
                errors.append(f"{_escape_tsv(candidate.relative)}\tchanged-during-collection\n")
        if config.keep_original_logs:
            original = output / "original" / candidate.relative
            temporary = scratch / f"original-{sequence}"
            remaining = None
            if config.var_log_max_bytes is not None:
                remaining = config.var_log_max_bytes - payload_bytes
            status, _ = _decode_to_file(
                candidate.source, "plain", temporary, remaining
            )
            if status == "over-limit":
                hard_cap_hit = True
                break
            if status != "ok":
                partial = True
                errors.append(
                    f"{_escape_tsv(candidate.relative)}\toriginal-copy-failed\n"
                )
            else:
                original.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(temporary, original)
                payload_bytes += original.stat().st_size

    _remove_owned_tree(scratch, output)
    (output / "INDEX.tsv").write_text("".join(index_lines), encoding="utf-8")
    if hard_cap_hit:
        _discard_log_payload(output)
        (output / "OVER-LIMIT.txt").write_text(
            f"max_bytes={config.var_log_max_bytes}\n"
            "status=discarded-after-streaming-hard-cap\n",
            encoding="utf-8",
        )
        if errors:
            (output / "ERRORS.tsv").write_text("".join(errors), encoding="utf-8")
        if opaque:
            (output / "UNREDACTED-OPAQUE.txt").write_text("".join(opaque), encoding="utf-8")
        if sensitive:
            (output / "SKIPPED-sensitive.txt").write_text("".join(sensitive), encoding="utf-8")
        return VarLogResult(2, frozenset())
    (output / "PAYLOAD-BYTES.txt").write_text(f"{payload_bytes}\n", encoding="utf-8")
    if errors:
        (output / "ERRORS.tsv").write_text("".join(errors), encoding="utf-8")
    if warnings:
        (output / "WARNINGS.tsv").write_text("".join(warnings), encoding="utf-8")
    if opaque:
        (output / "UNREDACTED-OPAQUE.txt").write_text("".join(opaque), encoding="utf-8")
    if sensitive:
        (output / "SKIPPED-sensitive.txt").write_text("".join(sensitive), encoding="utf-8")
    return VarLogResult(2 if partial else 0, frozenset(incomplete))


def _journal_command() -> tuple[str, ...] | None:
    command = ("journalctl",)
    if os.environ.get("CEPH_INCIDENT_TEST_FORCE_NO_SUDO") == "1":
        return None
    if os.environ.get("CEPH_INCIDENT_TEST_FORCE_SUDO") == "1":
        return ("sudo", "-n", *command)
    if os.environ.get("CEPH_INCIDENT_TEST_ALLOW_ATIME_READ") == "1":
        return command
    if os.geteuid() == 0:
        return command
    if _sudo_path() is None:
        return None
    return ("sudo", "-n", *command)


def _journal_since(value: str) -> str:
    if re.fullmatch(r"[0-9]+[smhdw]", value):
        return f"-{value}"
    return value


def _capture_command_bounded(
    command: Sequence[str],
    destination: Path,
    host_alias: str,
    timeout: int,
    limit: int | None,
) -> CaptureResult:
    started = _utc_now()
    header = (
        f"# host: {host_alias}\n"
        "# collector: collect-node\n"
        f"# started: {started}\n"
        f"# timeout: {timeout}s\n"
    ).encode()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp")
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    status = "ok"
    try:
        process = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        payload_bytes = 0
        deadline = time.monotonic() + timeout
        with temporary.open("xb") as output:
            output.write(header)
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    status = "timeout"
                    break
                events = selector.select(min(remaining_time, 0.1))
                if not events:
                    if process.poll() is not None:
                        try:
                            chunk = os.read(descriptor, 1024 * 1024)
                        except BlockingIOError:
                            chunk = b""
                        if not chunk:
                            break
                    continue
                try:
                    chunk = os.read(descriptor, 1024 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                if limit is not None and payload_bytes + len(chunk) > limit:
                    status = "over-limit"
                    break
                output.write(chunk)
                payload_bytes += len(chunk)
        if status != "ok":
            _stop_stream_process(process)
        else:
            return_code = process.wait()
            if return_code in (124, 137):
                status = "timeout"
            elif return_code != 0:
                status = "failed"
    except FileNotFoundError:
        status = "missing-command"
    finally:
        selector.close()
        if process is not None:
            _stop_stream_process(process)
    if status == "timeout" and temporary.exists():
        timeout_exit = (
            process.returncode
            if process is not None and process.returncode in (124, 137)
            else 124
        )
        with temporary.open("ab") as output:
            output.write(
                f"# TRUNCATED: command timed out after {timeout}s "
                f"(exit {timeout_exit})\n".encode()
            )
    if status in ("ok", "failed", "timeout"):
        os.replace(temporary, destination)
    else:
        temporary.unlink(missing_ok=True)
    exit_code = {
        "ok": 0,
        "over-limit": 75,
        "timeout": (
            process.returncode
            if process is not None and process.returncode in (124, 137)
            else 124
        ),
        "missing-command": 127,
    }.get(status)
    if exit_code is None:
        exit_code = process.returncode if process is not None else 1
        if exit_code is None or exit_code < 0:
            exit_code = 1
    return CaptureResult(status, exit_code, started, _utc_now())


def _write_log_skip(path: Path, reason: str) -> None:
    path.write_text(f"SKIPPED: {reason}\n", encoding="utf-8")


def _discard_log_payload(output: Path) -> None:
    for name in ("merged", "raw", "original"):
        _remove_owned_tree(output / name, output)
    (output / "PAYLOAD-BYTES.txt").unlink(missing_ok=True)


def _collect_journal(
    output: Path, config: NodeConfig, var_log_exit_code: int
) -> JournalResult:
    journal = output / "journal-all-since.txt"
    if (output / "OVER-LIMIT.txt").exists():
        _write_log_skip(
            journal,
            "not collected because /var/log payload exceeded the per-node cap",
        )
        return JournalResult(2, INCOMPLETE_ARTIFACT_EXIT_CODE, None, None, None)
    payload_path = output / "PAYLOAD-BYTES.txt"
    if config.var_log_max_bytes is not None and not payload_path.is_file():
        if var_log_exit_code == 0:
            # No accounting because there was no `/var/log` to account for: the
            # reference spends no journal budget here, collects no journal and
            # stays complete (`lib/collect-node.sh`, the final `elif`).  Failing
            # closed instead would report a partial node the reference calls
            # complete.
            return JournalResult(0, 0, None, None, None)
        _write_log_skip(journal, "/var/log payload accounting was unavailable")
        return JournalResult(2, INCOMPLETE_ARTIFACT_EXIT_CODE, None, None, None)
    payload_bytes = 0
    if payload_path.is_file():
        try:
            payload_bytes = int(payload_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            _write_log_skip(journal, "/var/log payload accounting was unavailable")
            return JournalResult(2, INCOMPLETE_ARTIFACT_EXIT_CODE, None, None, None)
    command_prefix = _journal_command()
    if command_prefix is None:
        _write_log_skip(
            journal, "sudo command not found for privileged read: journalctl"
        )
        return JournalResult(
            2 if config.var_log_max_bytes is not None else 0,
            INCOMPLETE_ARTIFACT_EXIT_CODE,
            None,
            None,
            None,
        )
    command = (*command_prefix, "--since", _journal_since(config.since), "--no-pager")
    command_text = shlex.join(command)
    remaining = None
    if config.var_log_max_bytes is not None:
        remaining = max(config.var_log_max_bytes - payload_bytes, 0)
    capture = _capture_command_bounded(
        command,
        journal,
        config.host_alias,
        max(config.command_timeout, 120),
        remaining,
    )
    if capture.status == "over-limit":
        _discard_log_payload(output)
        (output / "OVER-LIMIT.txt").write_text(
            f"max_bytes={config.var_log_max_bytes}\n"
            "status=not-collected-journal-exceeded-remaining-cap\n",
            encoding="utf-8",
        )
        _write_log_skip(
            journal,
            "combined /var/log and journal text exceeded the per-node cap",
        )
        return JournalResult(
            2,
            capture.exit_code,
            command_text,
            capture.started,
            capture.ended,
        )
    if capture.status == "missing-command":
        _write_log_skip(journal, "command not found: journalctl")
        return JournalResult(
            2,
            capture.exit_code,
            command_text,
            capture.started,
            capture.ended,
        )
    if capture.status != "ok":
        return JournalResult(
            2,
            capture.exit_code,
            command_text,
            capture.started,
            capture.ended,
        )
    if (
        config.var_log_max_bytes is not None
        and journal.is_file()
        and payload_path.is_file()
    ):
        combined = payload_bytes + journal.stat().st_size
        if config.var_log_max_bytes is not None and combined > config.var_log_max_bytes:
            _discard_log_payload(output)
            (output / "OVER-LIMIT.txt").write_text(
                f"actual_payload_bytes={combined}\n"
                f"max_bytes={config.var_log_max_bytes}\n"
                "status=not-collected-combined-payload-exceeded-cap\n",
                encoding="utf-8",
            )
            _write_log_skip(
                journal,
                "combined /var/log and journal text exceeded the per-node cap",
            )
            # The capture succeeded, but its payload has just been replaced by
            # a skip marker, so the artifact is no longer complete evidence.
            return JournalResult(
                2,
                INCOMPLETE_ARTIFACT_EXIT_CODE,
                command_text,
                capture.started,
                capture.ended,
            )
        payload_path.write_text(f"{combined}\n", encoding="utf-8")
    return JournalResult(
        0,
        capture.exit_code,
        command_text,
        capture.started,
        capture.ended,
    )


def _write_archive(output: Path, workspace: Path) -> bool:
    uncompressed = workspace / "node-evidence.tar"
    tar_result = subprocess.run(
        ["tar", "-cf", str(uncompressed), "-C", str(output), "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
        check=False,
    )
    if tar_result.returncode != 0:
        sys.stderr.buffer.write(tar_result.stderr)
        return False
    gzip_result = subprocess.run(
        ["gzip", "-c", str(uncompressed)],
        stdout=sys.stdout.buffer,
        stderr=subprocess.PIPE,
        check=False,
    )
    if gzip_result.returncode != 0:
        sys.stderr.buffer.write(gzip_result.stderr)
        return False
    sys.stdout.buffer.flush()
    return True


def _raise_interrupted(signum: int, _frame: object) -> None:
    raise NodeInterrupted(f"signal {signum}")


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 1:
        print("node collector configuration is required", file=sys.stderr)
        return 1
    try:
        config = _decode_config(args[0])
    except ValueError as error:
        print(f"node collector configuration rejected: {error}", file=sys.stderr)
        return 1

    workspace: Path | None = None
    previous_handlers: dict[int, object] = {}
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    for signal_number in handled_signals:
        previous_handlers[signal_number] = signal.signal(
            signal_number, _raise_interrupted
        )
    try:
        workspace = Path(
            tempfile.mkdtemp(prefix=f"ceph-incident-node-{config.invocation_id}-")
        ).resolve(strict=True)
        workspace.chmod(0o700)
        output = workspace / "out"
        output.mkdir(mode=0o700)
        manifest = output / "manifest.jsonl"
        manifest.touch(mode=0o600)
        failed = False
        for relative_artifact, command in NODE_COMMANDS:
            if not _write_artifact(
                output,
                manifest,
                config.host_alias,
                config.command_timeout,
                relative_artifact,
                command,
            ):
                failed = True
        if not _collect_node_evidence(output, manifest, config):
            failed = True
        log_output = output / "logs" / "var-log"
        if config.skip_logs:
            log_output.mkdir(mode=0o700, parents=True)
            started = _utc_now()
            skipped = log_output / "SKIPPED.txt"
            skipped.write_text(
                "SKIPPED: log collection disabled by --skip-logs\n",
                encoding="utf-8",
            )
            _append_manifest_entry(
                manifest,
                config.host_alias,
                skipped,
                "collect-var-log /var/log",
                # Left at 0 on purpose: unlike an absent `/var/log`, this is the
                # operator declining the evidence, and ADR 0010 does not
                # enumerate that case.  Raised with #8; do not change it here.
                0,
                started,
                _utc_now(),
            )
        else:
            root = _node_path("CEPH_INCIDENT_VAR_LOG_DIR", "/var/log")
            log_started = _utc_now()
            log_result = _collect_var_logs(root, log_output, config)
            journal_result = _collect_journal(
                log_output, config, log_result.exit_code
            )
            log_exit = log_result.exit_code
            if journal_result.node_exit_code != 0:
                log_exit = 2
            log_exit = _manifest_generated_tree(
                log_output,
                manifest,
                config.host_alias,
                log_exit,
                log_started,
                journal_result,
                log_result.incomplete_artifacts,
            )
            if log_exit != 0:
                failed = True
        if not _write_archive(output, workspace):
            return 74
        return 2 if failed else 0
    except NodeInterrupted as error:
        print(f"node collector interrupted ({error})", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"node collector failed: {error}", file=sys.stderr)
        return 1
    finally:
        if workspace is not None:
            shutil.rmtree(workspace)
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
