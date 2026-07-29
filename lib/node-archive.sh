#!/usr/bin/env bash
set -euo pipefail

# Accept a Node Evidence Archive into a fresh directory under the current
# collector-owned workspace.  The Python standard library is used here because
# parsing `tar -tv` output is neither portable nor a safe member-type boundary.
# Validation consumes the complete gzip stream and every regular-file payload
# before the first extraction write.  Extraction is manual so archive modes,
# ownership, links, and special members are never trusted.
accept_node_archive() {
  local archive=$1 destination=$2 workspace=$3 var_log_max_bytes=$4

  python3 - "$archive" "$destination" "$workspace" "$var_log_max_bytes" <<'PY'
from __future__ import annotations

import gzip
import os
import shutil
import stat
import sys
import tarfile
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


cap = payload_cap()
destination_parent = destination_argument.parent.resolve(strict=True)
destination = destination_parent / destination_argument.name
archive_lstat = archive_path.lstat()
archive = archive_path.resolve(strict=True)

if not contained(archive, workspace) or archive.parent != workspace:
    raise SystemExit("node archive rejected: candidate is outside its owned workspace")
if stat.S_ISLNK(archive_lstat.st_mode) or not stat.S_ISREG(archive_lstat.st_mode):
    raise SystemExit("node archive rejected: candidate is not a regular file")
if not contained(destination, workspace) or os.path.lexists(destination):
    raise SystemExit("node archive rejected: extraction directory is not fresh and owned")

members: list[tuple[tarfile.TarInfo, str]] = []
files: dict[str, tarfile.TarInfo] = {}
directories: set[str] = set()
seen: dict[str, str] = {}
destination_created = False

try:
    if archive.stat().st_size > cap:
        raise ArchiveRejected("compressed archive exceeds payload cap")

    # tarfile can stop at the tar end marker without consuming the gzip trailer.
    # This separate pass proves the complete compressed stream and bounds it.
    expanded_bytes = 0
    with gzip.open(archive, mode="rb") as compressed_stream:
        while chunk := compressed_stream.read(1024 * 1024):
            expanded_bytes += len(chunk)
            if expanded_bytes > cap:
                raise ArchiveRejected("expanded archive exceeds payload cap")

    with tarfile.open(archive, mode="r:gz") as node_archive:
        for member in node_archive.getmembers():
            normalised = normalise_member_name(member)
            if normalised in seen:
                raise ArchiveRejected(
                    f"archive member collision: {member.name} and {seen[normalised]}"
                )
            seen[normalised] = member.name
            if not (member.isfile() or member.isdir()):
                raise ArchiveRejected(
                    f"archive contains {member_kind(member)} member: {member.name}"
                )
            members.append((member, normalised))
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
        for member, normalised in members:
            if member.isdir():
                continue
            declared_payload += member.size
            if declared_payload > cap:
                raise ArchiveRejected("archive file payload exceeds payload cap")
            source = node_archive.extractfile(member)
            if source is None:
                raise ArchiveRejected(f"cannot read archive member: {normalised}")
            actual_size = 0
            with source:
                while chunk := source.read(1024 * 1024):
                    actual_size += len(chunk)
                    if actual_size > member.size:
                        raise ArchiveRejected(
                            f"archive member exceeds declared size: {normalised}"
                        )
            if actual_size != member.size:
                raise ArchiveRejected(f"truncated archive member: {normalised}")

    destination.mkdir(mode=0o700)
    destination_created = True
    with tarfile.open(archive, mode="r:gz") as node_archive:
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
    raise SystemExit(3)
except (ArchiveRejected, OSError, tarfile.TarError, EOFError, zlib.error) as error:
    if destination_created:
        shutil.rmtree(destination)
    print(f"node archive rejected: {error}", file=sys.stderr)
    raise SystemExit(2)
PY
}
