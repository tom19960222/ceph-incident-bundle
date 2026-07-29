#!/usr/bin/env bash
set -euo pipefail

# Accept a Node Evidence Archive into a fresh directory under the current
# collector-owned workspace.  The Python standard library is used here because
# parsing `tar -tv` output is neither portable nor a safe member-type boundary.
# Validation consumes the complete gzip stream and every regular-file payload
# before the first extraction write.  Extraction is manual so archive modes,
# ownership, links, and special members are never trusted.
NODE_ARCHIVE_REJECTION_CLASS=

accept_node_archive() {
  local archive=$1 destination=$2 workspace=$3 var_log_max_bytes=$4
  local python_rc=0
  NODE_ARCHIVE_REJECTION_CLASS=

  python3 - "$archive" "$destination" "$workspace" "$var_log_max_bytes" <<'PY' || python_rc=$?
from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath


archive_path = Path(sys.argv[1])
destination_argument = Path(sys.argv[2])
workspace = Path(sys.argv[3]).resolve(strict=True)
var_log_max_bytes = sys.argv[4]
test_cap = os.environ.get("CEPH_INCIDENT_TEST_NODE_ARCHIVE_MAX_BYTES")


class ArchiveRejected(Exception):
    pass


class ManifestMissing(ArchiveRejected):
    pass


MISSING_MANIFEST_EXIT = 3
MANIFEST_MAX_BYTES = 16 * 1024 * 1024


def contained(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def payload_cap() -> int:
    if test_cap is not None:
        cap = int(test_cap)
        if cap <= 0:
            raise ArchiveRejected("test payload cap must be positive")
        return cap
    if var_log_max_bytes == "unlimited":
        # `unlimited` remains effectively unlimited for the documented log
        # interface, while the archive parser still has a finite safety bound.
        return 1024**4
    # Non-log node evidence receives a fixed 1 GiB allowance in addition to the
    # existing per-node /var/log cap.
    return int(var_log_max_bytes) + 1024**3


def normalise_member_name(member: tarfile.TarInfo) -> str:
    name = member.name
    if not name:
        raise ArchiveRejected("empty archive member name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ArchiveRejected(f"unsafe archive member: {name}")
    normalised = path.as_posix()
    if normalised == ".":
        if member.isdir() and name in (".", "./"):
            return normalised
        raise ArchiveRejected(f"unsafe archive member: {name}")
    return normalised


def member_kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk():
        return "device"
    if member.isfifo():
        return "FIFO"
    return "special"


def validate_manifest(payload: bytes, files: set[str], expected_host: str) -> None:
    if len(payload) > MANIFEST_MAX_BYTES:
        raise ArchiveRejected("manifest.jsonl exceeds its safety cap")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ArchiveRejected("manifest.jsonl is not valid UTF-8") from error
    if not lines:
        raise ArchiveRejected("manifest.jsonl is empty")

    string_fields = ("host", "collector", "artifact", "command", "started", "ended")
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
        artifact_relative = PurePosixPath(
            *artifact.parts[out_indexes[-1] + 1 :]
        ).as_posix()
        if artifact_relative not in files:
            raise ArchiveRejected(
                f"manifest.jsonl line {line_number} references a missing artifact"
            )


cap = payload_cap()
destination_parent = destination_argument.parent.resolve(strict=True)
destination = destination_parent / destination_argument.name
archive_lstat = archive_path.lstat()
archive_resolved = archive_path.resolve(strict=True)

if not contained(archive_resolved, workspace) or archive_resolved.parent != workspace:
    raise SystemExit("node archive rejected: candidate is outside its owned workspace")
if stat.S_ISLNK(archive_lstat.st_mode) or not stat.S_ISREG(archive_lstat.st_mode):
    raise SystemExit("node archive rejected: candidate is not a regular file")
if not contained(destination, workspace) or os.path.lexists(destination):
    raise SystemExit("node archive rejected: extraction directory is not fresh and owned")

open_flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    open_flags |= os.O_NOFOLLOW
candidate_fd = os.open(archive_path, open_flags)
snapshot = tempfile.TemporaryFile(mode="w+b", dir=workspace)
members: list[tuple[tarfile.TarInfo, str]] = []
files: dict[str, tarfile.TarInfo] = {}
directories: set[str] = set()
seen: dict[str, str] = {}
destination_created = False
manifest_payload: bytes | None = None
tar_payload_end = 0

try:
    candidate_stat = os.fstat(candidate_fd)
    if not stat.S_ISREG(candidate_stat.st_mode) or candidate_stat.st_size > cap:
        raise ArchiveRejected("compressed archive exceeds payload cap")

    # Copy SSH stdout to an anonymous workspace-owned snapshot. Validation and
    # extraction both use this one descriptor, so pathname replacement or
    # in-place candidate mutation cannot change the bytes after acceptance.
    compressed_bytes = 0
    with os.fdopen(candidate_fd, "rb", closefd=True) as candidate:
        while chunk := candidate.read(1024 * 1024):
            compressed_bytes += len(chunk)
            if compressed_bytes > cap:
                raise ArchiveRejected("compressed archive exceeds payload cap")
            snapshot.write(chunk)
    snapshot.flush()

    # tarfile can stop at the tar end marker without consuming the gzip trailer.
    # This separate pass proves the complete compressed stream and bounds it.
    snapshot.seek(0)
    expanded_bytes = 0
    with gzip.GzipFile(fileobj=snapshot, mode="rb") as compressed_stream:
        while chunk := compressed_stream.read(1024 * 1024):
            expanded_bytes += len(chunk)
            if expanded_bytes > cap:
                raise ArchiveRejected("expanded archive exceeds payload cap")

    snapshot.seek(0)
    with tarfile.open(fileobj=snapshot, mode="r:gz") as node_archive:
        for member in node_archive.getmembers():
            normalised = normalise_member_name(member)
            if normalised in seen:
                raise ArchiveRejected(
                    f"archive member collision: {member.name} and {seen[normalised]}"
                )
            seen[normalised] = member.name
            regular_file = member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
            if not (regular_file or member.isdir()):
                raise ArchiveRejected(
                    f"archive contains {member_kind(member)} member: {member.name}"
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

        # Reject file/directory hierarchy collisions before creating paths.
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

        # Read each declared payload to EOF now.  A truncated tar must fail here,
        # before destination.mkdir below can create the extraction root.
        declared_payload = 0
        manifest_buffer = bytearray()
        for member, normalised in members:
            if member.isdir():
                continue
            declared_payload += member.size
            if declared_payload > cap:
                raise ArchiveRejected("archive file payload exceeds payload cap")
            source = node_archive.extractfile(member)
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
                    if actual_size > member.size:
                        raise ArchiveRejected(
                            f"archive member exceeds declared size: {normalised}"
                        )
            if actual_size != member.size:
                raise ArchiveRejected(f"truncated archive member: {normalised}")
        manifest_payload = bytes(manifest_buffer)

    validate_manifest(manifest_payload, set(files), destination.name)

    # tarfile accepts a stream that ends after the final member padding. Require
    # the two POSIX end-of-archive blocks and allow only zero padding after them.
    snapshot.seek(0)
    tail_size = 0
    with gzip.GzipFile(fileobj=snapshot, mode="rb") as compressed_stream:
        compressed_stream.seek(tar_payload_end)
        while chunk := compressed_stream.read(1024 * 1024):
            tail_size += len(chunk)
            if any(chunk):
                raise ArchiveRejected("tar stream has data after its final member")
    if tail_size < 1024 or tail_size % 512 != 0:
        raise ArchiveRejected("tar stream is missing its end-of-archive blocks")

    destination.mkdir(mode=0o700)
    destination_created = True
    snapshot.seek(0)
    with tarfile.open(fileobj=snapshot, mode="r:gz") as node_archive:
        by_original_name = {member.name: member for member in node_archive.getmembers()}
        for directory in sorted(directories, key=lambda item: len(PurePosixPath(item).parts)):
            (destination / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        for validated_member, normalised in members:
            if validated_member.isdir():
                continue
            target = destination / normalised
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not contained(target, destination):
                raise ArchiveRejected(f"extraction target escaped workspace: {normalised}")
            source = node_archive.extractfile(by_original_name[validated_member.name])
            if source is None:
                raise ArchiveRejected(f"cannot extract archive member: {normalised}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
except ManifestMissing as error:
    if destination_created:
        shutil.rmtree(destination)
    print(f"node archive rejected: {error}", file=sys.stderr)
    raise SystemExit(MISSING_MANIFEST_EXIT)
except (ArchiveRejected, OSError, tarfile.TarError, EOFError, zlib.error) as error:
    if destination_created:
        shutil.rmtree(destination)
    print(f"node archive rejected: {error}", file=sys.stderr)
    raise SystemExit(2)
finally:
    snapshot.close()
PY

  if [[ $python_rc -eq 0 ]]; then
    return 0
  fi
  if [[ $python_rc -eq 3 ]]; then
    # Consumed by run/collect.sh after this sourced function returns.
    # shellcheck disable=SC2034
    NODE_ARCHIVE_REJECTION_CLASS=missing-manifest
  fi
  return 2
}
