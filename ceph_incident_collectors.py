"""Workstation command, transport, capture, and manifest policies."""

from __future__ import annotations

import base64
import gzip
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


NODE_ARCHIVE_MAX_BYTES = 1024**3
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
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
            or candidate_stat.st_size > NODE_ARCHIVE_MAX_BYTES
        ):
            raise ArchiveRejected("compressed archive exceeds payload cap")
        compressed_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            while chunk := source.read(1024 * 1024):
                compressed_bytes += len(chunk)
                if compressed_bytes > NODE_ARCHIVE_MAX_BYTES:
                    raise ArchiveRejected("compressed archive exceeds payload cap")
                snapshot.write(chunk)
        snapshot.flush()

        snapshot.seek(0)
        expanded_bytes = 0
        with gzip.GzipFile(fileobj=snapshot, mode="rb") as compressed:
            while chunk := compressed.read(1024 * 1024):
                expanded_bytes += len(chunk)
                if expanded_bytes > NODE_ARCHIVE_MAX_BYTES:
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
                if declared_payload > NODE_ARCHIVE_MAX_BYTES:
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


def _ssh_command(
    ssh_key: Path,
    target: str,
    connection_timeout: int,
    known_hosts_file: Path | None,
    encoded_config: str,
) -> list[str]:
    command = [
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
        command.extend(
            [
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={known_hosts_file} {Path.home() / '.ssh/known_hosts'}",
            ]
        )
    remote_command = (
        "python3 -c "
        + shlex.quote(REMOTE_BOOTSTRAP)
        + " "
        + shlex.quote(encoded_config)
    )
    command.extend([target, remote_command])
    return command


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
) -> NodeCollectionResult:
    """Collect one node through the fixed stdin/archive SSH process contract."""

    invocation_id = uuid.uuid4().hex
    config = json.dumps(
        {
            "host_alias": host_alias,
            "invocation_id": invocation_id,
            "command_timeout": command_timeout,
        },
        separators=(",", ":"),
    ).encode()
    encoded_config = base64.urlsafe_b64encode(config).decode().rstrip("=")
    candidate = workspace / f".node-{host_alias}-{invocation_id}.tar.gz"
    command = _ssh_command(
        ssh_key, target, connection_timeout, known_hosts_file, encoded_config
    )
    with candidate.open("xb") as candidate_stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=candidate_stream,
            stderr=subprocess.PIPE,
        )
        try:
            _, stderr = process.communicate(input=node_source, timeout=node_timeout)
        except subprocess.TimeoutExpired:
            _, stderr = _stop_process(process)
            if stderr:
                sys.stderr.buffer.write(stderr)
            candidate.unlink(missing_ok=True)
            reason = f"node collection timed out after {node_timeout}s from {target}"
            _write_skipped(destination, reason)
            return NodeCollectionResult(2, 124, False, reason, invocation_id)
        except KeyboardInterrupt as error:
            _stop_process(process)
            candidate.unlink(missing_ok=True)
            raise CollectionInterrupted from error

    if stderr:
        sys.stderr.buffer.write(stderr)
    remote_exit_code = process.returncode
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
                2, remote_exit_code, False, reason, invocation_id
            )
        try:
            accept_node_archive(candidate, destination, workspace, host_alias)
        except ManifestMissing:
            reason = f"incomplete node archive returned from {target}: missing manifest"
            _write_skipped(destination, reason)
            return NodeCollectionResult(
                2, remote_exit_code, False, reason, invocation_id
            )
        except ArchiveRejected as error:
            reason = f"no usable node archive returned from {target}: {error}"
            _write_skipped(destination, reason)
            return NodeCollectionResult(
                2, remote_exit_code, False, reason, invocation_id
            )
    finally:
        candidate.unlink(missing_ok=True)

    exit_code = 0 if remote_exit_code == 0 else 2
    reason = None if exit_code == 0 else f"node collector exited {remote_exit_code}"
    return NodeCollectionResult(
        exit_code, remote_exit_code, True, reason, invocation_id
    )
