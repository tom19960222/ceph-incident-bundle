"""Workstation command, transport, capture, and manifest policies."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


NODE_ARCHIVE_OVERHEAD_BYTES = 1024**3
NODE_ARCHIVE_SAFETY_CEILING_BYTES = 1024**4
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
CEPH_COLLECTOR = "collect-cluster-cephadm"
# The remote prefix that runs ceph on the source node, by runner token.  Both
# runners are direct, read-only Ceph CLI invocations; `sudo -n cephadm shell`
# can start containers, so it stays default-off and has no entry here.  Runner
# selection and capability probing belong to multi-source orchestration (#16).
CEPH_RUNNER_ARGV: dict[str, tuple[str, ...]] = {
    "direct": ("ceph",),
    "sudo": ("sudo", "-n", "ceph"),
}
DEFAULT_CEPH_RUNNER = "direct"
_CEPH_JSON_QUERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status.json", ("status",)),
    ("health-detail.json", ("health", "detail")),
    ("versions.json", ("versions",)),
    ("df-detail.json", ("df", "detail")),
    ("osd-tree.json", ("osd", "tree")),
    ("osd-df.json", ("osd", "df")),
    ("osd-dump.json", ("osd", "dump")),
    ("osd-perf.json", ("osd", "perf")),
    ("osd-blocked-by.json", ("osd", "blocked-by")),
    ("pg-stat.json", ("pg", "stat")),
    ("pg-dump.json", ("pg", "dump")),
    ("pg-dump-stuck.json", ("pg", "dump_stuck")),
    ("mon-dump.json", ("mon", "dump")),
    ("quorum-status.json", ("quorum_status",)),
    ("mgr-dump.json", ("mgr", "dump")),
    ("orch-host-ls.json", ("orch", "host", "ls")),
    ("orch-ps.json", ("orch", "ps")),
    ("orch-device-ls-wide.json", ("orch", "device", "ls", "--wide")),
    ("config-dump.json", ("config", "dump")),
    ("crash-ls.json", ("crash", "ls")),
)
CEPH_JSON_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (artifact, (*words, "--format", "json-pretty"))
    for artifact, words in _CEPH_JSON_QUERIES
)
CEPH_TEXT_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status.txt", ("status",)),
    ("health-detail.txt", ("health", "detail")),
    ("osd-tree.txt", ("osd", "tree")),
    ("orch-ps.txt", ("orch", "ps")),
)
CEPH_CRASH_INFO_LIMIT = 10
ROOK_COLLECTOR = "collect-cluster-rook"
# The manifest host column for cluster-level Rook evidence: it belongs to the
# Kubernetes cluster, not to whichever workstation or node ran kubectl.
ROOK_HOST = "rook"
ROOK_RESOURCE_KINDS = (
    "cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,"
    "cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io"
)
ROOK_OPERATOR_LABEL = "app=rook-ceph-operator"
ROOK_TOOLBOX_LABEL = "app=rook-ceph-tools"
# A Pod name read back from the cluster becomes an argv word, so it must not be
# able to look like an option to the next kubectl invocation.
SAFE_POD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
# `kubectl exec` starts a process inside a running Pod, so this candidate has no
# toolbox execution path at all and no opt-in that could reach one.
ROOK_TOOLBOX_SKIP_REASON = (
    "kubectl exec disabled by default for operational read-only collection"
)
# Anchored to crash_id: matching id/name too would feed unrelated nested fields
# back into `ceph crash info`.
CRASH_ID_PATTERN = re.compile(r'"crash_id"\s*:\s*"([^"]*)"')
EMPTY_CRASH_LISTS = frozenset(
    (
        "[]",
        "{}",
        '{"crashes":[]}',
        '{"items":[]}',
        '{"entries":[]}',
        '{"crash_ls":[]}',
    )
)
PROMETHEUS_COLLECTOR = "collect-prometheus"
# The manifest host column for metrics evidence: it belongs to the Prometheus
# server, not to the workstation that ran curl against it.
PROMETHEUS_HOST = "prometheus"
PROMETHEUS_DEFAULT_JOB_REGEX = "ceph|node"
PROMETHEUS_DEFAULT_BUDGET_SECONDS = 600
# The --since grammar the metrics dump accepts: N seconds, or N{s,m,h,d,w}.
PROMETHEUS_DURATION = re.compile(r"([0-9]+)([smhdw]?)\Z")
PROMETHEUS_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# Prometheus refuses a query_range with more than 11,000 points per series, so
# the auto step keeps every series under 10,000 points but never below 15s.
PROMETHEUS_MAX_POINTS = 10000
PROMETHEUS_MIN_STEP_SECONDS = 15
# curl bounds each request with --max-time; this grace period only stops a
# transport that ignored its own deadline from hanging the whole collect.
PROMETHEUS_TRANSPORT_GRACE_SECONDS = 5
PROMETHEUS_FILTER_TIMEOUT_SECONDS = 5
PROMETHEUS_COMPONENT_MAX_LENGTH = 200
# Server-controlled job labels share this directory with collector-owned files.
# Reserve the full namespace up front, using the same case-folding as untrusted
# components so behavior is safe on both case-sensitive and insensitive hosts.
PROMETHEUS_RESERVED_TOP_LEVEL_COMPONENTS = frozenset(
    name.casefold()
    for name in (
        ".jobs.json",
        "buildinfo.json",
        "targets.json",
        "dump-info.txt",
        "SKIPPED.txt",
    )
)
# A metric name read back from the server becomes both a PromQL matcher and an
# artifact filename, so anything outside Prometheus's own grammar is skipped.
SAFE_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
# A job name is interpolated into a PromQL matcher string; a quote or backslash
# would escape it, so such a job is recorded and skipped instead.
UNSAFE_JOB_CHARACTERS = ('"', "\\")
# curl writes the raw JSON itself, so a success is confirmed from the response
# body rather than from a capture header the collector would have to add.
PROMETHEUS_SUCCESS_MARKER = b'"status":"success"'
PROMETHEUS_SUCCESS_PROBE_BYTES = 512
REMOTE_BOOTSTRAP = """\
import sys
if sys.version_info < (3, 11):
    sys.stderr.write("SKIPPED: Python 3.11 or newer is required\\n")
    raise SystemExit(75)
source = sys.stdin.buffer.read()
code = compile(source, "ceph_incident_node.py", "exec")
namespace = {"__name__": "__main__", "__file__": "ceph_incident_node.py"}
sys.argv = ["ceph_incident_node.py", sys.argv[1]]
exec(code, namespace, namespace)
"""


class ArchiveRejected(Exception):
    """A Node Evidence Archive failed before-extraction acceptance."""


class ManifestMissing(ArchiveRejected):
    """A Node Evidence Archive lacks its required manifest."""


class CollectionInterrupted(Exception):
    """The workstation collect was interrupted and cleaned up."""


@dataclass(frozen=True)
class NodeCollectionResult:
    exit_code: int
    remote_exit_code: int
    # The code this node collection reports to the run, which is what the
    # reference records in `errors.log`.  It is neither of the two above: the
    # reference passes the remote or transport code straight through, except
    # that a timeout and an unusable archive from a successful remote both
    # become 2 (`run/collect.sh`, `collect_remote_node`).
    reported_exit_code: int
    accepted: bool
    reason: str | None
    invocation_id: str


def _contained(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def _normalise_member(member: tarfile.TarInfo) -> str:
    name = member.name
    if not name:
        raise ArchiveRejected("empty archive member name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ArchiveRejected(f"unsafe archive member: {name}")
    normalised = path.as_posix()
    if normalised == "." and not (member.isdir() and name in (".", "./")):
        raise ArchiveRejected(f"unsafe archive member: {name}")
    return normalised


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk():
        return "device"
    if member.isfifo():
        return "FIFO"
    return "special"


def _validate_node_manifest(
    payload: bytes, files: set[str], expected_host: str
) -> None:
    if len(payload) > MANIFEST_MAX_BYTES:
        raise ArchiveRejected("manifest.jsonl exceeds its safety cap")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ArchiveRejected("manifest.jsonl is not valid UTF-8") from error
    if not lines:
        raise ArchiveRejected("manifest.jsonl is empty")
    string_fields = ("host", "collector", "artifact", "command", "started", "ended")
    mapped_artifacts: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ArchiveRejected(f"manifest.jsonl line {line_number} is empty")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} is invalid JSON"
            ) from error
        if not isinstance(entry, dict):
            raise ArchiveRejected(f"manifest.jsonl line {line_number} is not an object")
        for field in string_fields:
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise ArchiveRejected(
                    f"manifest.jsonl line {line_number} has invalid {field}"
                )
        exit_code = entry.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} has invalid exit_code"
            )
        if entry["host"] != expected_host or entry["collector"] != "collect-node":
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} has invalid node identity"
            )
        artifact = PurePosixPath(entry["artifact"])
        if not artifact.is_absolute() or ".." in artifact.parts:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} has unsafe artifact path"
            )
        out_indexes = [index for index, part in enumerate(artifact.parts) if part == "out"]
        if not out_indexes or out_indexes[-1] == len(artifact.parts) - 1:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} artifact is outside node output"
            )
        relative = PurePosixPath(*artifact.parts[out_indexes[-1] + 1 :]).as_posix()
        if relative not in files:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} references a missing artifact"
            )
        if relative in mapped_artifacts:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} duplicates an artifact mapping"
            )
        mapped_artifacts.add(relative)
    evidence_files = files - {"manifest.jsonl", "errors.log"}
    if mapped_artifacts != evidence_files:
        raise ArchiveRejected("archive contains evidence without a manifest mapping")


def accept_node_archive(
    candidate: Path,
    destination_argument: Path,
    workspace_argument: Path,
    expected_host: str,
    max_archive_bytes: int = NODE_ARCHIVE_OVERHEAD_BYTES,
) -> None:
    """Validate all archive bytes and metadata before creating destination."""

    workspace = workspace_argument.resolve(strict=True)
    destination_parent = destination_argument.parent.resolve(strict=True)
    destination = destination_parent / destination_argument.name
    candidate_lstat = candidate.lstat()
    candidate_resolved = candidate.resolve(strict=True)
    if candidate_resolved.parent != workspace or not _contained(
        candidate_resolved, workspace
    ):
        raise ArchiveRejected("candidate is outside its owned workspace")
    if stat.S_ISLNK(candidate_lstat.st_mode) or not stat.S_ISREG(
        candidate_lstat.st_mode
    ):
        raise ArchiveRejected("candidate is not a regular file")
    if not _contained(destination, workspace) or os.path.lexists(destination):
        raise ArchiveRejected("extraction directory is not fresh and owned")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(candidate, flags)
    try:
        snapshot = tempfile.TemporaryFile(mode="w+b", dir=workspace)
    except OSError:
        os.close(descriptor)
        raise
    members: list[tuple[tarfile.TarInfo, str]] = []
    files: dict[str, tarfile.TarInfo] = {}
    directories: set[str] = set()
    seen: dict[str, str] = {}
    destination_created = False
    tar_payload_end = 0
    try:
        candidate_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate_stat.st_size > max_archive_bytes
        ):
            raise ArchiveRejected("compressed archive exceeds payload cap")
        compressed_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            while chunk := source.read(1024 * 1024):
                compressed_bytes += len(chunk)
                if compressed_bytes > max_archive_bytes:
                    raise ArchiveRejected("compressed archive exceeds payload cap")
                snapshot.write(chunk)
        snapshot.flush()

        snapshot.seek(0)
        expanded_bytes = 0
        with gzip.GzipFile(fileobj=snapshot, mode="rb") as compressed:
            while chunk := compressed.read(1024 * 1024):
                expanded_bytes += len(chunk)
                if expanded_bytes > max_archive_bytes:
                    raise ArchiveRejected("expanded archive exceeds payload cap")

        snapshot.seek(0)
        manifest_payload: bytes | None = None
        with tarfile.open(fileobj=snapshot, mode="r:gz") as archive:
            for member in archive.getmembers():
                normalised = _normalise_member(member)
                if normalised in seen:
                    raise ArchiveRejected(
                        f"archive member collision: {member.name} and {seen[normalised]}"
                    )
                seen[normalised] = member.name
                regular = member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
                if not (regular or member.isdir()):
                    raise ArchiveRejected(
                        f"archive contains {_member_kind(member)} member: {member.name}"
                    )
                members.append((member, normalised))
                padded_size = ((member.size + 511) // 512) * 512
                tar_payload_end = max(tar_payload_end, member.offset_data + padded_size)
                if normalised == ".":
                    continue
                if member.isdir():
                    directories.add(normalised)
                else:
                    files[normalised] = member

            if "manifest.jsonl" not in files:
                raise ManifestMissing("missing required manifest.jsonl")
            for name in seen:
                if name == ".":
                    continue
                parts = PurePosixPath(name).parts
                for index in range(1, len(parts)):
                    ancestor = PurePosixPath(*parts[:index]).as_posix()
                    if ancestor in files:
                        raise ArchiveRejected(
                            f"archive member hierarchy collides with file: {ancestor}"
                        )

            declared_payload = 0
            manifest_buffer = bytearray()
            for member, normalised in members:
                if member.isdir():
                    continue
                declared_payload += member.size
                if declared_payload > max_archive_bytes:
                    raise ArchiveRejected("archive file payload exceeds payload cap")
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveRejected(f"cannot read archive member: {normalised}")
                if normalised == "manifest.jsonl" and member.size > MANIFEST_MAX_BYTES:
                    raise ArchiveRejected("manifest.jsonl exceeds its safety cap")
                actual_size = 0
                with source:
                    while chunk := source.read(1024 * 1024):
                        actual_size += len(chunk)
                        if normalised == "manifest.jsonl":
                            manifest_buffer.extend(chunk)
                if actual_size != member.size:
                    raise ArchiveRejected(f"truncated archive member: {normalised}")
            manifest_payload = bytes(manifest_buffer)

        _validate_node_manifest(manifest_payload, set(files), expected_host)

        snapshot.seek(0)
        tail_size = 0
        with gzip.GzipFile(fileobj=snapshot, mode="rb") as compressed:
            compressed.seek(tar_payload_end)
            while chunk := compressed.read(1024 * 1024):
                tail_size += len(chunk)
                if any(chunk):
                    raise ArchiveRejected("tar stream has data after its final member")
        if tail_size < 1024 or tail_size % 512 != 0:
            raise ArchiveRejected("tar stream is missing its end-of-archive blocks")

        destination.mkdir(mode=0o700)
        destination_created = True
        snapshot.seek(0)
        with tarfile.open(fileobj=snapshot, mode="r:gz") as archive:
            by_name = {member.name: member for member in archive.getmembers()}
            for directory in sorted(
                directories, key=lambda item: len(PurePosixPath(item).parts)
            ):
                (destination / directory).mkdir(
                    mode=0o700, parents=True, exist_ok=True
                )
            for validated_member, normalised in members:
                if validated_member.isdir():
                    continue
                target = destination / normalised
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not _contained(target, destination):
                    raise ArchiveRejected(
                        f"extraction target escaped workspace: {normalised}"
                    )
                source = archive.extractfile(by_name[validated_member.name])
                if source is None:
                    raise ArchiveRejected(f"cannot extract archive member: {normalised}")
                output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    output_flags |= os.O_NOFOLLOW
                output_descriptor = os.open(target, output_flags, 0o600)
                with source, os.fdopen(output_descriptor, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        if destination_created:
            shutil.rmtree(destination)
        raise ArchiveRejected("invalid or unreadable archive") from error
    except ArchiveRejected:
        if destination_created:
            shutil.rmtree(destination)
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        snapshot.close()


def _ssh_base_options(
    ssh_key: Path, connection_timeout: int, known_hosts_file: Path | None
) -> list[str]:
    """The single SSH option vector every remote command shares.

    LogLevel=ERROR keeps ssh's own chatter out of captured artifacts; the
    runtime known_hosts file accepts new keys without touching the operator's.
    """

    options = [
        "ssh",
        "-i",
        str(ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={connection_timeout}",
        "-o",
        f"ServerAliveInterval={connection_timeout}",
        "-o",
        "ServerAliveCountMax=1",
    ]
    if known_hosts_file is not None:
        options.extend(
            [
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={known_hosts_file} {Path.home() / '.ssh/known_hosts'}",
            ]
        )
    return options


def _ssh_command(
    ssh_key: Path,
    target: str,
    connection_timeout: int,
    known_hosts_file: Path | None,
    encoded_config: str,
) -> list[str]:
    command = _ssh_base_options(ssh_key, connection_timeout, known_hosts_file)
    remote_command = (
        "python3 -c "
        + shlex.quote(REMOTE_BOOTSTRAP)
        + " "
        + shlex.quote(encoded_config)
    )
    command.extend([target, remote_command])
    return command


CAPABILITY_PROBE = (
    'caps=""; command -v cephadm >/dev/null 2>&1 && caps="$caps cephadm"; '
    'command -v ceph >/dev/null 2>&1 && caps="$caps ceph"; '
    'command -v kubectl >/dev/null 2>&1 && caps="$caps kubectl"; '
    'printf "%s\\n" "$caps"'
)


def _run_ssh_probe_command(
    command: Sequence[str], *, timeout: int, capture_output: bool = False
) -> tuple[int, str]:
    """Run one bounded SSH probe and normalize transport failures."""

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return _exit_code_of(completed.returncode), completed.stdout or ""


def probe_node_capabilities(
    *,
    workdir: Path,
    target: str,
    ssh_key: Path,
    connection_timeout: int,
    known_hosts_file: Path | None,
) -> frozenset[str]:
    """Return the reviewed command capabilities advertised by one node.

    A transport failure is evidence, not an empty successful probe: preserve a
    bounded diagnostic and make the node ineligible as a cluster source.
    """

    command = [
        *_ssh_base_options(ssh_key, connection_timeout, known_hosts_file),
        target,
        CAPABILITY_PROBE,
    ]
    exit_code, output = _run_ssh_probe_command(
        command, timeout=connection_timeout, capture_output=True
    )
    if exit_code != 0:
        with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
            errors.write(
                f"{_utc_now()} capability probe failed for {target} "
                f"(ssh exit {exit_code}) — node not considered as a cluster source\n"
            )
        if exit_code in (255, 124, 137):
            write_ssh_debug_log(
                workdir=workdir,
                label="capability-probe",
                target=target,
                ssh_key=ssh_key,
                connection_timeout=connection_timeout,
                known_hosts_file=known_hosts_file,
            )
        return frozenset()
    advertised = frozenset(output.split())
    return advertised.intersection({"ceph", "cephadm", "kubectl"})


def select_ceph_runner(
    *,
    workdir: Path,
    target: str,
    ssh_key: Path,
    connection_timeout: int,
    known_hosts_file: Path | None,
) -> str | None:
    """Select the first safe Ceph runner whose read-only probe succeeds."""

    for runner, prefix in (("direct", ["ceph"]), ("sudo", ["sudo", "-n", "ceph"])):
        command = [
            *_ssh_base_options(ssh_key, connection_timeout, known_hosts_file),
            target,
            *prefix,
            "--connect-timeout",
            "5",
            "-s",
        ]
        exit_code, _ = _run_ssh_probe_command(
            command, timeout=connection_timeout
        )
        if exit_code == 0:
            return runner
        if exit_code in (255, 124, 137):
            write_ssh_debug_log(
                workdir=workdir,
                label=f"cluster-ceph-{runner}",
                target=target,
                ssh_key=ssh_key,
                connection_timeout=connection_timeout,
                known_hosts_file=known_hosts_file,
            )
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exit_code_of(returncode: int) -> int:
    # A signalled child reports a negative returncode; the observable contract
    # uses the shell's 128+signal convention (137 for SIGKILL).
    return 128 - returncode if returncode < 0 else returncode


def _append_manifest_entry(
    manifest: Path,
    *,
    host: str,
    collector: str,
    artifact: Path,
    command: str,
    exit_code: int,
    started: str,
    ended: str,
) -> None:
    entry = {
        "host": host,
        "collector": collector,
        "artifact": str(artifact),
        "command": command,
        "exit_code": exit_code,
        "started": started,
        "ended": ended,
    }
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, separators=(",", ":")) + "\n")


def run_capture(
    *,
    manifest: Path,
    errors_log: Path | None,
    host: str,
    collector: str,
    artifact: Path,
    command: Sequence[str],
    timeout: int,
) -> int:
    """Capture one external command into an artifact, manifest, and errors.log.

    stdout and stderr are merged below a fixed comment header, the artifact is
    renamed into place only once the capture finished, and the command's exit
    code is returned so callers can apply their own required/optional policy.
    """

    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = _utc_now()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=artifact.parent, prefix=f".{artifact.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    command_string = shlex.join(command)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                f"# host: {host}\n"
                f"# collector: {collector}\n"
                f"# started: {started}\n"
                f"# timeout: {timeout}s\n".encode()
            )
            stream.flush()
            try:
                completed = subprocess.run(
                    list(command),
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
                exit_code = _exit_code_of(completed.returncode)
            except FileNotFoundError:
                stream.write(f"# MISSING: {command[0]}: command not found\n".encode())
                exit_code = 127
            except subprocess.TimeoutExpired:
                exit_code = 124
            if exit_code in (124, 137):
                stream.write(
                    f"# TRUNCATED: command timed out after {timeout}s "
                    f"(exit {exit_code})\n".encode()
                )
        ended = _utc_now()
        os.replace(temporary, artifact)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    _append_manifest_entry(
        manifest,
        host=host,
        collector=collector,
        artifact=artifact,
        command=command_string,
        exit_code=exit_code,
        started=started,
        ended=ended,
    )
    if exit_code != 0 and errors_log is not None:
        with errors_log.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{ended} host={host} collector={collector} artifact={artifact} "
                f"exit={exit_code} command={command_string}\n"
            )
    return exit_code


def _artifact_component(value: str, fallback: str) -> str:
    """Reduce a dynamic value to one safe artifact path component."""

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    while ".." in safe:
        safe = safe.replace("..", "__")
    return safe or fallback


def write_ssh_debug_log(
    *,
    workdir: Path,
    label: str,
    target: str,
    ssh_key: Path,
    connection_timeout: int,
    known_hosts_file: Path | None,
) -> None:
    """Re-probe a failed SSH target verbosely so the bundle explains the failure."""

    debug_dir = workdir / "ssh-debug"
    debug_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifact = debug_dir / (
        f"{_artifact_component(label, 'ssh')}-{_artifact_component(target, 'ssh')}.log"
    )
    command = [
        *_ssh_base_options(ssh_key, connection_timeout, known_hosts_file),
        "-vvv",
        "-o",
        "LogLevel=DEBUG3",
        target,
        "true",
    ]
    started = _utc_now()
    with artifact.open("wb") as stream:
        stream.write(
            "# ssh debug log\n"
            f"# target: {target}\n"
            f"# label: {label}\n"
            f"# started: {started}\n"
            f"# command: {shlex.join(command)}\n".encode()
        )
        stream.flush()
        try:
            completed = subprocess.run(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=connection_timeout,
                check=False,
            )
            exit_code = _exit_code_of(completed.returncode)
        except FileNotFoundError:
            exit_code = 127
        except subprocess.TimeoutExpired:
            exit_code = 124
        stream.write(
            f"# ended: {_utc_now()}\n# exit_code: {exit_code}\n".encode()
        )


def _unique_crash_artifact(crash_dir: Path, stem: str) -> Path:
    artifact = crash_dir / f"{stem}.json"
    suffix = 2
    while os.path.lexists(artifact):
        artifact = crash_dir / f"{stem}-{suffix}.json"
        suffix += 1
    return artifact


def _extract_crash_ids(crash_ls_artifact: Path) -> list[str] | None:
    """Return up to ten crash ids, or None when the crash list is unparseable."""

    try:
        payload = crash_ls_artifact.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = "\n".join(
        line for line in payload.splitlines() if not line.lstrip().startswith("#")
    )
    crash_ids = CRASH_ID_PATTERN.findall(body)[:CEPH_CRASH_INFO_LIMIT]
    if crash_ids:
        return crash_ids
    if "".join(body.split()) in EMPTY_CRASH_LISTS:
        return []
    return None


def collect_direct_ceph_cluster(
    *,
    workdir: Path,
    seed: str,
    ssh_key: Path,
    connection_timeout: int,
    command_timeout: int,
    known_hosts_file: Path | None,
    runner: str = DEFAULT_CEPH_RUNNER,
) -> int:
    """Collect cluster evidence through the direct Ceph CLI over SSH.

    ``runner`` selects the remote Ceph CLI prefix — ``direct`` runs ``ceph`` and
    ``sudo`` runs ``sudo -n ceph``; both are read-only and neither may reach
    ``cephadm shell``.  Choosing between them (and probing for capability) is
    the caller's job, not this collector's.

    Every command runs; a failed capture keeps its output as evidence and makes
    the layer partial (2).  An unparseable crash list is recorded as a SKIPPED
    artifact and is not itself a failure.
    """

    try:
        runner_argv = CEPH_RUNNER_ARGV[runner]
    except KeyError as error:
        raise ValueError(f"unsupported Ceph CLI runner: {runner}") from error

    manifest = workdir / "manifest.jsonl"
    errors_log = workdir / "errors.log"
    json_dir = workdir / "cluster" / "ceph" / "json"
    text_dir = workdir / "cluster" / "ceph" / "text"
    json_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    text_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_options = _ssh_base_options(ssh_key, connection_timeout, known_hosts_file)

    def capture(artifact: Path, words: Sequence[str]) -> int:
        exit_code = run_capture(
            manifest=manifest,
            errors_log=errors_log,
            host=seed,
            collector=CEPH_COLLECTOR,
            artifact=artifact,
            command=[*base_options, seed, *runner_argv, *words],
            timeout=command_timeout,
        )
        if exit_code in (255, 124, 137):
            write_ssh_debug_log(
                workdir=workdir,
                label="cluster-ceph",
                target=seed,
                ssh_key=ssh_key,
                connection_timeout=connection_timeout,
                known_hosts_file=known_hosts_file,
            )
        return exit_code

    failed = False
    for artifact_name, words in CEPH_JSON_COMMANDS:
        if capture(json_dir / artifact_name, words) != 0:
            failed = True
    for artifact_name, words in CEPH_TEXT_COMMANDS:
        if capture(text_dir / artifact_name, words) != 0:
            failed = True

    crash_ids = _extract_crash_ids(json_dir / "crash-ls.json")
    if crash_ids is None:
        (text_dir / "crash-info-skip.txt").write_text(
            "SKIPPED: unable to parse crash list JSON for recent crash inspection\n",
            encoding="utf-8",
        )
    else:
        crash_dir = json_dir / "crash-info"
        for crash_id in crash_ids:
            artifact = _unique_crash_artifact(
                crash_dir, _artifact_component(crash_id, "crash")
            )
            if capture(artifact, ("crash", "info", crash_id)) != 0:
                failed = True

    return 2 if failed else 0


def _compact_error(detail: str) -> str:
    """Flatten a multi-line command error into one SKIPPED-artifact line."""

    return detail.replace("\r", "").strip().replace("\n", " | ")


def _rook_probe_reason(
    namespace: str, kube_context: str, location: str, detail: str
) -> str:
    """Classify why the namespace probe failed, keeping the raw error attached.

    Order matters: the connection and namespace phrasings overlap, so the more
    specific cause is matched first, exactly as the shell reference does.
    """

    context_name = kube_context or "<current-context>"
    classifications: tuple[tuple[str, str], ...] = (
        (
            r"kubectl:?\s+command\s+not\s+found|command\s+not\s+found:?\s+kubectl",
            f"kubectl command not found on {location}",
        ),
        (
            r"no\s+context\s+exists|context.*(?:not\s+found|does\s+not\s+exist)",
            f"kubectl context not found: {context_name} on {location}",
        ),
        (
            r"connection\s+to\s+the\s+server|unable\s+to\s+connect\s+to\s+the\s+server"
            r"|connection\s+refused|i/o\s+timeout|context\s+deadline\s+exceeded"
            r"|no\s+route\s+to\s+host|tls\s+handshake\s+timeout",
            f"kubectl cannot connect to cluster API from {location}",
        ),
        (
            r"namespaces?.*not\s+found|notfound",
            f"rook namespace not found: {namespace}",
        ),
        (
            r"forbidden|unauthorized|permission\s+denied",
            "kubectl cannot read rook namespace due to authorization failure: "
            f"{namespace} on {location}",
        ),
    )
    reason = (
        f"kubectl namespace probe failed for rook namespace {namespace} on {location}"
    )
    for pattern, classified in classifications:
        if re.search(pattern, detail, re.IGNORECASE):
            reason = classified
            break
    compact = _compact_error(detail)
    return f"{reason}: {compact}" if compact else reason


def _run_probe(command: Sequence[str], timeout: int) -> tuple[int, str]:
    """Run one read-only probe, merging its output; never raises on failure."""

    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{command[0]}: command not found"
    except subprocess.TimeoutExpired as expired:
        captured = expired.output or b""
        return 124, (
            f"{captured.decode('utf-8', errors='replace')}"
            f"\nprobe timed out after {timeout}s"
        )
    return (
        _exit_code_of(completed.returncode),
        completed.stdout.decode("utf-8", errors="replace"),
    )


def _write_skip_artifact(artifact: Path, reason: str) -> None:
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifact.write_text(f"SKIPPED: {reason}\n", encoding="utf-8")


def _first_pod_name(command: Sequence[str], timeout: int) -> str:
    """Return the first Pod name a label selector matched, or "" on any failure.

    A lookup failure must yield no Pod — and therefore a SKIPPED artifact — not
    abort the collector, so this deliberately swallows the probe's exit code.
    A name that could be read as an option is treated as no match at all.
    """

    exit_code, output = _run_probe(command, timeout)
    if exit_code != 0:
        return ""
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate.startswith("pod/"):
            continue
        name = candidate.removeprefix("pod/")
        return name if SAFE_POD_NAME.fullmatch(name) else ""
    return ""


def collect_rook_cluster(
    *,
    workdir: Path,
    namespace: str,
    operator_namespace: str,
    since: str,
    command_timeout: int,
    kube_context: str = "",
    ssh_target: str | None = None,
    ssh_key: Path | None = None,
    connection_timeout: int = 20,
    known_hosts_file: Path | None = None,
    allow_skip: bool = False,
) -> int:
    """Collect Rook cluster evidence through read-only kubectl operations.

    ``ssh_target`` selects the runner: with it, kubectl runs on that node over
    the shared SSH option vector; without it, kubectl runs on the workstation
    against its inherited kubeconfig.  ``kube_context`` selects a kubeconfig
    context for either runner.  Every kubectl invocation is an explicit argv
    array of read-only verbs; nothing here can create a process inside a Pod.

    Returns 0 when every required artifact was captured and 2 when the layer is
    partial.  When ``allow_skip`` is true, an unavailable Rook preflight is a
    successful optional-layer skip; required captures still fail as partial.
    """

    rook_dir = workdir / "cluster" / "rook"
    rook_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = workdir / "manifest.jsonl"
    errors_log = workdir / "errors.log"

    if ssh_target is not None:
        if ssh_key is None:
            raise ValueError("the remote kubectl runner requires an SSH key")
        kubectl: list[str] = [
            *_ssh_base_options(ssh_key, connection_timeout, known_hosts_file),
            ssh_target,
            "kubectl",
        ]
        location = ssh_target
    else:
        kubectl = ["kubectl"]
        location = "local"
    if kube_context:
        kubectl.extend(["--context", kube_context])

    # Collecting nothing is a partial layer, not a silent success.  Only the
    # local runner is checked here; a missing remote kubectl surfaces through
    # the namespace probe, which classifies it by the same rules.
    if ssh_target is None and shutil.which("kubectl") is None:
        _write_skip_artifact(rook_dir / "SKIPPED.txt", "kubectl command not found")
        return 0 if allow_skip else 2

    probe_exit, probe_detail = _run_probe(
        [*kubectl, "get", "namespace", namespace], command_timeout
    )
    if probe_exit != 0:
        _write_skip_artifact(
            rook_dir / "SKIPPED.txt",
            _rook_probe_reason(namespace, kube_context, location, probe_detail),
        )
        return 0 if allow_skip else 2

    def capture(artifact_name: str, words: Sequence[str]) -> int:
        return run_capture(
            manifest=manifest,
            errors_log=errors_log,
            host=ROOK_HOST,
            collector=ROOK_COLLECTOR,
            artifact=rook_dir / artifact_name,
            command=[*kubectl, *words],
            timeout=command_timeout,
        )

    failed = False
    required: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("pods-wide.txt", ("get", "pods", "-n", namespace, "-o", "wide")),
        (
            "events.txt",
            ("get", "events", "-n", namespace, "--sort-by=.lastTimestamp"),
        ),
        (
            "rook-resources.yaml",
            ("get", ROOK_RESOURCE_KINDS, "-n", namespace, "-o", "yaml"),
        ),
    )
    for artifact_name, words in required:
        if capture(artifact_name, words) != 0:
            failed = True

    operator_pod = _first_pod_name(
        [
            *kubectl,
            "get",
            "pods",
            "-n",
            operator_namespace,
            "-l",
            ROOK_OPERATOR_LABEL,
            "-o",
            "name",
        ],
        command_timeout,
    )
    if operator_pod:
        if capture(
            "operator.log",
            ("logs", "-n", operator_namespace, operator_pod, f"--since={since}"),
        ) != 0:
            failed = True
    else:
        _write_skip_artifact(
            rook_dir / "operator-SKIPPED.txt",
            f"rook operator Pod not found in namespace: {operator_namespace}",
        )

    # Preserve the shell collector's read-only command ledger even though this
    # candidate deliberately has no kubectl-exec opt-in or execution path.
    _first_pod_name(
        [
            *kubectl,
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            ROOK_TOOLBOX_LABEL,
            "-o",
            "name",
        ],
        command_timeout,
    )
    _write_skip_artifact(rook_dir / "toolbox-SKIPPED.txt", ROOK_TOOLBOX_SKIP_REASON)
    return 2 if failed else 0


@dataclass(frozen=True)
class PrometheusCollectionResult:
    exit_code: int
    masked_url: str
    jobs_matched: tuple[str, ...]
    # False when the layer was skipped before any metric could be dumped, so
    # the caller records the dump in environment.txt only once one happened.
    dump_completed: bool = False


def prometheus_duration_seconds(value: str) -> int | None:
    """Parse the --since grammar into seconds, or None when it is unusable."""

    match = PROMETHEUS_DURATION.fullmatch(value)
    if match is None:
        return None
    # Base 10 explicitly: a pasted 008 is eight seconds, not an octal error.
    amount = int(match.group(1), 10)
    if amount <= 0:
        return None
    return amount * PROMETHEUS_DURATION_UNITS[match.group(2)]


def mask_prometheus_url(url: str) -> str:
    """Replace an embedded basic-auth password with *** for artifact use."""

    match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*://)", url)
    if match is None:
        return url
    authority_start = match.end()
    authority_end = len(url)
    for delimiter in ("/", "?", "#"):
        position = url.find(delimiter, authority_start)
        if position >= 0:
            authority_end = min(authority_end, position)
    credentials_end = url.rfind("@", authority_start, authority_end)
    if credentials_end < 0:
        return url
    credentials = url[authority_start:credentials_end]
    username = credentials.split(":", 1)[0]
    return f"{url[:authority_start]}{username}:***@{url[credentials_end + 1:]}"


def _prometheus_auto_step(window: int) -> int:
    step = -(-window // PROMETHEUS_MAX_POINTS)
    return max(step, PROMETHEUS_MIN_STEP_SECONDS)


def _prometheus_job_matches(pattern: str, job: str) -> bool:
    """Apply the public job filter with the shell contract's POSIX ERE dialect."""

    try:
        completed = subprocess.run(
            ["grep", "-qiE", "--", pattern],
            input=job.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROMETHEUS_FILTER_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return False
    return completed.returncode == 0


def _reserve_prometheus_component(
    *, identity: str, candidate: str, fallback: str, used: set[str]
) -> str:
    """Reserve a safe, collision-free component for one untrusted label value."""

    base = _artifact_component(candidate, fallback)[:PROMETHEUS_COMPONENT_MAX_LENGTH]
    if base in (".", ".."):
        base = fallback
    collision_key = base.casefold()
    if collision_key not in used:
        used.add(collision_key)
        return base

    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    suffix = f"--{digest}"
    shortened = base[: PROMETHEUS_COMPONENT_MAX_LENGTH - len(suffix)]
    reserved = f"{shortened}{suffix}"
    ordinal = 2
    while reserved.casefold() in used:
        numbered_suffix = f"{suffix}-{ordinal}"
        shortened = base[: PROMETHEUS_COMPONENT_MAX_LENGTH - len(numbered_suffix)]
        reserved = f"{shortened}{numbered_suffix}"
        ordinal += 1
    used.add(reserved.casefold())
    return reserved


def _prometheus_display_label(value: str) -> str:
    """Escape control characters before an untrusted label enters text evidence."""

    return value.encode("unicode_escape").decode("ascii")


def _prometheus_job_is_safe(job: str) -> bool:
    return not any(character in job for character in UNSAFE_JOB_CHARACTERS) and not any(
        ord(character) < 32 or ord(character) == 127 for character in job
    )


def _prometheus_get(
    *,
    url: str,
    path: str,
    artifact: Path,
    timeout: int,
    parameters: Sequence[str] = (),
) -> tuple[int, str]:
    """GET one Prometheus endpoint straight into an artifact file.

    curl writes the response body itself so no capture header can corrupt the
    JSON, and every query parameter goes through --data-urlencode so a PromQL
    matcher never needs manual encoding.  Returns curl's exit code and its
    merged diagnostics.
    """

    command = [
        "curl",
        "-q",
        "-fsS",
        "-G",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout),
        "-o",
        str(artifact),
        f"{url}{path}",
    ]
    for parameter in parameters:
        command.extend(["--data-urlencode", parameter])
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout + PROMETHEUS_TRANSPORT_GRACE_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{command[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"curl exceeded its local time budget of {timeout}s"
    detail = _compact_error(completed.stdout.decode("utf-8", errors="replace"))
    # curl normally omits credentials from diagnostics, but the collector must
    # not trust an external command to preserve the bundle's secret boundary.
    # Replace the exact request base before any caller can persist stderr.
    detail = detail.replace(url, mask_prometheus_url(url))
    return (
        _exit_code_of(completed.returncode),
        detail,
    )


def _prometheus_data_values(artifact: Path) -> list[str] | None:
    """Read a label-values response's data[] strings, or None if unusable."""

    try:
        document = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or document.get("status") != "success":
        return None
    data = document.get("data", [])
    if not isinstance(data, list):
        return None
    if not all(isinstance(value, str) for value in data):
        return None
    return data


def _prometheus_response_succeeded(artifact: Path) -> bool:
    """Confirm a metric dump really is a Prometheus success response."""

    try:
        with artifact.open("rb") as stream:
            return PROMETHEUS_SUCCESS_MARKER in stream.read(
                PROMETHEUS_SUCCESS_PROBE_BYTES
            )
    except OSError:
        return False


def collect_prometheus_cluster(
    *,
    workdir: Path,
    url: str,
    since: str,
    job_regex: str = PROMETHEUS_DEFAULT_JOB_REGEX,
    step: str = "",
    command_timeout: int = 20,
    budget: int = PROMETHEUS_DEFAULT_BUDGET_SECONDS,
) -> PrometheusCollectionResult:
    """Collect metrics evidence over the Prometheus HTTP API with curl.

    Every request is an explicit read-only argv array; the layer dumps each
    metric of the scrape jobs matching ``job_regex`` over the ``since`` window
    and returns 0 or the partial status 2, never raising on a server failure.
    """

    window = prometheus_duration_seconds(since)
    if window is None:
        raise ValueError(f"unusable Prometheus window: {since}")

    # Operator-pasted trailing slashes would become //api in every request.
    url = url.rstrip("/")
    masked_url = mask_prometheus_url(url)
    prometheus_dir = workdir / "cluster" / "prometheus"
    manifest = workdir / "manifest.jsonl"
    errors_log = workdir / "errors.log"

    def record_error(message: str) -> None:
        with errors_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{_utc_now()} {message}\n")

    def skip(reason: str) -> PrometheusCollectionResult:
        _write_skip_artifact(prometheus_dir / "SKIPPED.txt", reason)
        record_error(f"prometheus dump skipped: {reason}")
        return PrometheusCollectionResult(2, masked_url, ())

    # Collecting nothing is a partial layer naming its cause, not a crash on
    # the first request: the workstation must be able to speak HTTP at all.
    if shutil.which("curl") is None:
        return skip("curl not found on this workstation")

    prometheus_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    end_epoch = int(datetime.now(timezone.utc).timestamp())
    start_epoch = end_epoch - window
    step_seconds = int(step) if step else _prometheus_auto_step(window)
    deadline = time.monotonic() + budget

    def add_manifest_entry(
        artifact: Path, command: str, exit_code: int, started: str, ended: str
    ) -> None:
        _append_manifest_entry(
            manifest,
            host=PROMETHEUS_HOST,
            collector=PROMETHEUS_COLLECTOR,
            artifact=artifact,
            command=command,
            exit_code=exit_code,
            started=started,
            ended=ended,
        )

    failed = False

    # buildinfo doubles as the connectivity probe for the whole layer.
    build_info = prometheus_dir / "buildinfo.json"
    started = _utc_now()
    exit_code, detail = _prometheus_get(
        url=url,
        path="/api/v1/status/buildinfo",
        artifact=build_info,
        timeout=command_timeout,
    )
    if exit_code != 0:
        # curl truncates its output file before failing, so the partial write
        # must not survive as evidence of a server that never answered.
        build_info.unlink(missing_ok=True)
        return skip(
            f"prometheus not reachable: {masked_url} (curl exit {exit_code}: {detail})"
        )
    add_manifest_entry(
        build_info,
        f"GET {masked_url}/api/v1/status/buildinfo",
        exit_code,
        started,
        _utc_now(),
    )

    # Target health is useful context but not a reason to abandon the dump.
    targets = prometheus_dir / "targets.json"
    started = _utc_now()
    exit_code, detail = _prometheus_get(
        url=url, path="/api/v1/targets", artifact=targets, timeout=command_timeout
    )
    if exit_code != 0:
        targets.unlink(missing_ok=True)
        record_error(f"prometheus targets fetch failed (curl exit {exit_code}): {detail}")
        failed = True
    add_manifest_entry(
        targets, f"GET {masked_url}/api/v1/targets", exit_code, started, _utc_now()
    )

    # The user-facing contract is "find metrics by exporter (job) name", so the
    # filter applies to scrape-job labels rather than to metric names.
    jobs_artifact = prometheus_dir / ".jobs.json"
    exit_code, detail = _prometheus_get(
        url=url,
        path="/api/v1/label/job/values",
        artifact=jobs_artifact,
        timeout=command_timeout,
    )
    jobs_seen = _prometheus_data_values(jobs_artifact) if exit_code == 0 else None
    jobs_artifact.unlink(missing_ok=True)
    if jobs_seen is None:
        return skip(
            f"prometheus job listing failed (curl exit {exit_code}): "
            f"{detail or 'unparseable JSON'}"
        )
    jobs_matched = []
    for job in jobs_seen:
        # grep receives the pattern after `--`, preserving the shell's
        # case-insensitive POSIX ERE semantics without an option injection seam.
        if not _prometheus_job_matches(job_regex, job):
            continue
        if not _prometheus_job_is_safe(job):
            record_error(
                "prometheus job skipped (unsafe name): "
                f"{_prometheus_display_label(job)}"
            )
            failed = True
            continue
        jobs_matched.append(job)
    if not jobs_matched:
        return skip(
            f"no scrape job matched regex '{job_regex}' "
            f"(jobs seen: "
            f"{' '.join(_prometheus_display_label(job) for job in jobs_seen) or '<none>'})"
        )

    metrics_ok = 0
    metrics_failed = 0
    truncated = False
    used_job_components = set(PROMETHEUS_RESERVED_TOP_LEVEL_COMPONENTS)
    for job in jobs_matched:
        if truncated:
            break
        job_component = _reserve_prometheus_component(
            identity=job,
            candidate=job,
            fallback="job",
            used=used_job_components,
        )
        job_dir = prometheus_dir / job_component
        job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        index = job_dir / "index.txt"
        index.write_text("", encoding="utf-8")
        job_exit_code = 0
        started = _utc_now()

        names_artifact = job_dir / ".names.json"
        exit_code, detail = _prometheus_get(
            url=url,
            path="/api/v1/label/__name__/values",
            artifact=names_artifact,
            timeout=command_timeout,
            parameters=(
                f'match[]={{job="{job}"}}',
                f"start={start_epoch}",
                f"end={end_epoch}",
            ),
        )
        metric_names = (
            _prometheus_data_values(names_artifact) if exit_code == 0 else None
        )
        names_artifact.unlink(missing_ok=True)
        if metric_names is None:
            with index.open("a", encoding="utf-8") as index_stream:
                index_stream.write(f"FAILED: metric listing for job {job}\n")
            record_error(
                f"prometheus metric listing failed for job {job} "
                f"(curl exit {exit_code}): {detail or 'unparseable JSON'}"
            )
            failed = True
            add_manifest_entry(
                index,
                f"GET {masked_url}/api/v1/label/__name__/values "
                f'match[]={{job="{job}"}}',
                2,
                started,
                _utc_now(),
            )
            continue

        used_metric_components: set[str] = set()
        with index.open("a", encoding="utf-8") as index_stream:
            for metric in metric_names:
                if time.monotonic() >= deadline:
                    truncated = True
                    index_stream.write(f"TRUNCATED: budget {budget}s exceeded\n")
                    record_error(
                        f"prometheus dump truncated: budget {budget}s exceeded "
                        f"at job {job}"
                    )
                    job_exit_code = 2
                    failed = True
                    break
                if SAFE_METRIC_NAME.fullmatch(metric) is None:
                    # Never a query and never a filename: it is only recorded.
                    display_metric = _prometheus_display_label(metric)
                    index_stream.write(f"skipped {display_metric} unsafe-name\n")
                    record_error(
                        "prometheus metric skipped (unsafe name) "
                        f"job={job} metric={display_metric}"
                    )
                    job_exit_code = 2
                    failed = True
                    continue
                metric_component = _reserve_prometheus_component(
                    identity=metric,
                    candidate=metric.replace(":", "__"),
                    fallback="metric",
                    used=used_metric_components,
                )
                artifact = job_dir / f"{metric_component}.json"
                exit_code, detail = _prometheus_get(
                    url=url,
                    path="/api/v1/query_range",
                    artifact=artifact,
                    timeout=command_timeout,
                    parameters=(
                        f'query={{__name__="{metric}",job="{job}"}}',
                        f"start={start_epoch}",
                        f"end={end_epoch}",
                        f"step={step_seconds}",
                    ),
                )
                if exit_code != 0 or not _prometheus_response_succeeded(artifact):
                    artifact.unlink(missing_ok=True)
                    index_stream.write(f"failed {metric} -\n")
                    record_error(
                        f"prometheus query_range failed job={job} metric={metric} "
                        f"(curl exit {exit_code}): {detail}"
                    )
                    metrics_failed += 1
                    job_exit_code = 2
                    failed = True
                    continue
                compressed = artifact.with_name(f"{artifact.name}.gz")
                with artifact.open("rb") as source, gzip.open(
                    compressed, "wb"
                ) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                artifact.unlink()
                index_stream.write(f"ok {metric} {compressed.name}\n")
                metrics_ok += 1

        add_manifest_entry(
            index,
            f"GET {masked_url}/api/v1/query_range "
            f'query={{__name__="<metric>",job="{job}"}} '
            f"start={start_epoch} end={end_epoch} step={step_seconds} "
            f"({len(metric_names)} metrics)",
            job_exit_code,
            started,
            _utc_now(),
        )

    (prometheus_dir / "dump-info.txt").write_text(
        "".join(
            f"{key}={value}\n"
            for key, value in (
                ("url", masked_url),
                ("since", since),
                ("window_start_epoch", start_epoch),
                ("window_start_utc", _epoch_utc(start_epoch)),
                ("window_end_epoch", end_epoch),
                ("window_end_utc", _epoch_utc(end_epoch)),
                ("step_seconds", step_seconds),
                ("job_regex", job_regex),
                (
                    "jobs_seen",
                    " ".join(_prometheus_display_label(job) for job in jobs_seen)
                    or "<none>",
                ),
                (
                    "jobs_matched",
                    " ".join(_prometheus_display_label(job) for job in jobs_matched)
                    or "<none>",
                ),
                ("metrics_ok", metrics_ok),
                ("metrics_failed", metrics_failed),
                ("truncated", 1 if truncated else 0),
            )
        ),
        encoding="utf-8",
    )
    return PrometheusCollectionResult(
        2 if failed else 0, masked_url, tuple(jobs_matched), dump_completed=True
    )


def _stop_process(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    process.terminate()
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def _write_skipped(destination: Path, reason: str) -> None:
    destination.mkdir(mode=0o700)
    (destination / "SKIPPED.txt").write_text(
        f"SKIPPED: {reason}\n", encoding="utf-8"
    )


def _write_node_ssh_debug_log(
    *,
    workspace: Path,
    host_alias: str,
    target: str,
    ssh_key: Path,
    connection_timeout: int,
    known_hosts_file: Path | None,
) -> None:
    """Keep the reference collector's per-node transport diagnostic."""

    write_ssh_debug_log(
        workdir=workspace,
        label=f"node-{host_alias}",
        target=target,
        ssh_key=ssh_key,
        connection_timeout=connection_timeout,
        known_hosts_file=known_hosts_file,
    )


def collect_single_node(
    *,
    workspace: Path,
    destination: Path,
    host_alias: str,
    target: str,
    ssh_key: Path,
    node_source: bytes,
    connection_timeout: int,
    node_timeout: int,
    command_timeout: int,
    known_hosts_file: Path | None,
    skip_logs: bool,
    keep_original_logs: bool,
    var_log_max_bytes: int | str,
    since: str,
) -> NodeCollectionResult:
    """Collect one node through the fixed stdin/archive SSH process contract."""

    invocation_id = uuid.uuid4().hex
    config = json.dumps(
        {
            "host_alias": host_alias,
            "invocation_id": invocation_id,
            "command_timeout": command_timeout,
            "skip_logs": skip_logs,
            "keep_original_logs": keep_original_logs,
            "var_log_max_bytes": var_log_max_bytes,
            "since": since,
        },
        separators=(",", ":"),
    ).encode()
    encoded_config = base64.urlsafe_b64encode(config).decode().rstrip("=")
    candidate = workspace / f".node-{host_alias}-{invocation_id}.tar.gz"
    command = _ssh_command(
        ssh_key, target, connection_timeout, known_hosts_file, encoded_config
    )
    with candidate.open("xb") as candidate_stream:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=candidate_stream,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            # The local transport itself is missing: this node is a Skipped
            # Node like any other unreachable one, not a fatal collect.
            candidate.unlink(missing_ok=True)
            reason = f"node transport is unavailable: {command[0]}: command not found"
            _write_skipped(destination, reason)
            return NodeCollectionResult(2, 127, 127, False, reason, invocation_id)
        try:
            _, stderr = process.communicate(input=node_source, timeout=node_timeout)
        except subprocess.TimeoutExpired:
            _, stderr = _stop_process(process)
            if stderr:
                sys.stderr.buffer.write(stderr)
            candidate.unlink(missing_ok=True)
            reason = f"node collection timed out after {node_timeout}s from {target}"
            _write_node_ssh_debug_log(
                workspace=workspace,
                host_alias=host_alias,
                target=target,
                ssh_key=ssh_key,
                connection_timeout=connection_timeout,
                known_hosts_file=known_hosts_file,
            )
            _write_skipped(destination, reason)
            # The reference bounds the whole collection with `timeout` and turns
            # its 124/137 into a plain 2 before reporting it.
            return NodeCollectionResult(2, 124, 2, False, reason, invocation_id)
        except KeyboardInterrupt as error:
            _stop_process(process)
            candidate.unlink(missing_ok=True)
            raise CollectionInterrupted from error

    if stderr:
        sys.stderr.buffer.write(stderr)
    remote_exit_code = process.returncode
    # An archive the workstation cannot use is a failure of the collection even
    # when the remote said it succeeded; the reference reports it as 2 rather
    # than as the remote's 0.
    rejected_exit_code = remote_exit_code if remote_exit_code != 0 else 2
    if _exit_code_of(remote_exit_code) in (255, 137):
        # A transport-level failure explains itself only with a verbose probe.
        _write_node_ssh_debug_log(
            workspace=workspace,
            host_alias=host_alias,
            target=target,
            ssh_key=ssh_key,
            connection_timeout=connection_timeout,
            known_hosts_file=known_hosts_file,
        )
    try:
        if candidate.stat().st_size == 0:
            if remote_exit_code in (75, 127):
                reason = "Python 3.11 or newer is unavailable on node"
            else:
                reason = (
                    f"no usable node archive returned from {target} "
                    f"(exit {remote_exit_code})"
                )
            _write_skipped(destination, reason)
            return NodeCollectionResult(
                2, remote_exit_code, rejected_exit_code, False, reason, invocation_id
            )
        try:
            if isinstance(var_log_max_bytes, int):
                archive_cap = min(
                    var_log_max_bytes + NODE_ARCHIVE_OVERHEAD_BYTES,
                    NODE_ARCHIVE_SAFETY_CEILING_BYTES,
                )
            else:
                archive_cap = NODE_ARCHIVE_SAFETY_CEILING_BYTES
            accept_node_archive(
                candidate,
                destination,
                workspace,
                host_alias,
                max_archive_bytes=archive_cap,
            )
        except ManifestMissing:
            reason = f"incomplete node archive returned from {target}: missing manifest"
            _write_skipped(destination, reason)
            return NodeCollectionResult(
                2, remote_exit_code, rejected_exit_code, False, reason, invocation_id
            )
        except ArchiveRejected as error:
            reason = f"no usable node archive returned from {target}: {error}"
            _write_skipped(destination, reason)
            return NodeCollectionResult(
                2, remote_exit_code, rejected_exit_code, False, reason, invocation_id
            )
    finally:
        candidate.unlink(missing_ok=True)

    exit_code = 0 if remote_exit_code == 0 else 2
    # The reported code is the detail; the reason says what it means.
    reason = (
        None
        if exit_code == 0
        else "remote collector reported partial node evidence"
    )
    return NodeCollectionResult(
        exit_code, remote_exit_code, remote_exit_code, True, reason, invocation_id
    )
