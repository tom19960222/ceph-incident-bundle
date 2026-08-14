"""Fail-closed admission of one untrusted Node Evidence Archive."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import tarfile
import unicodedata


class ArchiveRejected(Exception):
    """The complete Node Evidence Archive is not structurally admissible."""


def admit_archive(
    archive_path: Path,
    extraction_directory: Path,
    contribution_directory: Path,
    *,
    ceph_allowed: bool,
) -> None:
    """Validate every member, extract privately, then promote one contribution."""
    archive_path = Path(archive_path)
    extraction_directory = Path(extraction_directory)
    contribution_directory = Path(contribution_directory)
    staging_directory = archive_path.parent

    _validate_destinations(
        archive_path,
        staging_directory,
        extraction_directory,
        contribution_directory,
    )
    _validate_complete_tar_stream(archive_path)

    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            _validate_trailing_tar_padding(archive_path, archive.offset)
            _validate_members(archive, members, ceph_allowed=ceph_allowed)
            _extract_members(archive, members, extraction_directory)
    except ArchiveRejected:
        raise
    except (gzip.BadGzipFile, EOFError, tarfile.TarError) as error:
        raise ArchiveRejected(f"invalid Node Evidence Archive: {error}") from error

    os.rename(extraction_directory, contribution_directory)


def _validate_destinations(
    archive_path: Path,
    staging_directory: Path,
    extraction_directory: Path,
    contribution_directory: Path,
) -> None:
    try:
        archive_mode = archive_path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise ArchiveRejected(
            f"cannot inspect Node Evidence Archive: {error}"
        ) from error
    if not stat.S_ISREG(archive_mode):
        raise ArchiveRejected("Node Evidence Archive must be an owned regular file")

    try:
        staging_mode = staging_directory.stat(follow_symlinks=False).st_mode
        contribution_parent_mode = contribution_directory.parent.stat(
            follow_symlinks=False
        ).st_mode
    except OSError as error:
        raise ArchiveRejected(
            f"cannot inspect admission destinations: {error}"
        ) from error
    if not stat.S_ISDIR(staging_mode):
        raise ArchiveRejected("node staging must be an ordinary directory")
    if not stat.S_ISDIR(contribution_parent_mode):
        raise ArchiveRejected(
            "admitted contribution parent must be an ordinary directory"
        )

    staging = staging_directory.resolve()
    if archive_path.parent.resolve() != staging:
        raise ArchiveRejected(
            "Node Evidence Archive must be directly inside node staging"
        )
    if extraction_directory.parent.resolve() != staging:
        raise ArchiveRejected("private extraction must be a child of node staging")
    if _path_exists(extraction_directory):
        raise ArchiveRejected("private extraction destination already exists")
    if _path_exists(contribution_directory):
        raise ArchiveRejected("admitted contribution destination already exists")
    if (
        staging_directory.stat(follow_symlinks=False).st_dev
        != contribution_directory.parent.stat(follow_symlinks=False).st_dev
    ):
        raise ArchiveRejected(
            "private staging and admitted contribution must share a filesystem"
        )


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _validate_complete_tar_stream(archive_path: Path) -> None:
    """Require a complete gzip stream and the two terminal tar zero blocks."""
    total = 0
    tail = b""
    try:
        with gzip.open(archive_path, "rb") as uncompressed:
            while True:
                chunk = uncompressed.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                tail = (tail + chunk)[-1024:]
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ArchiveRejected(f"invalid compressed archive: {error}") from error
    if total < 1024 or total % 512 != 0 or tail != b"\0" * 1024:
        raise ArchiveRejected("Node Evidence Archive is truncated")


def _validate_trailing_tar_padding(archive_path: Path, member_end: int) -> None:
    """Reject a second tar stream or any other data after the first tar EOF."""
    padding_bytes = 0
    try:
        with gzip.open(archive_path, "rb") as uncompressed:
            uncompressed.seek(member_end)
            while True:
                chunk = uncompressed.read(1024 * 1024)
                if not chunk:
                    break
                padding_bytes += len(chunk)
                if chunk.strip(b"\0"):
                    raise ArchiveRejected(
                        "Node Evidence Archive has data after its first tar stream"
                    )
    except ArchiveRejected:
        raise
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ArchiveRejected(f"invalid compressed archive: {error}") from error
    if padding_bytes < 1024:
        raise ArchiveRejected("Node Evidence Archive is missing its tar EOF blocks")


def _validate_members(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    *,
    ceph_allowed: bool,
) -> None:
    by_path: dict[tuple[str, ...], tarfile.TarInfo] = {}
    by_portable_path: dict[tuple[str, ...], tuple[str, ...]] = {}

    for member in members:
        components = _safe_components(member.name)
        if components[0] not in {"node", "ceph"}:
            raise ArchiveRejected(f"unknown archive root: {components[0]}")
        if components[0] == "ceph" and not ceph_allowed:
            raise ArchiveRejected("ceph evidence is not allowed for this Target Node")
        if not member.isdir() and not member.isreg():
            raise ArchiveRejected(f"unsupported archive member type: {member.name}")
        if components in by_path:
            raise ArchiveRejected(f"duplicate archive member: {member.name}")

        portable = tuple(_portable_component(component) for component in components)
        previous = by_portable_path.get(portable)
        if previous is not None:
            raise ArchiveRejected(
                "portable path collision: "
                f"{'/'.join(previous)} conflicts with {member.name}"
            )
        by_path[components] = member
        by_portable_path[portable] = components

    required_directories = {
        ("node",),
        ("node", "probes"),
        ("node", "files"),
    }
    if ceph_allowed:
        required_directories.update({("ceph",), ("ceph", "probes")})
    for required in required_directories:
        member = by_path.get(required)
        if member is None or not member.isdir():
            raise ArchiveRejected(
                f"missing required archive directory: {'/'.join(required)}"
            )

    for components, member in by_path.items():
        for length in range(1, len(components)):
            parent_components = components[:length]
            parent = by_path.get(parent_components)
            if parent is None:
                raise ArchiveRejected(
                    f"archive member has an absent ancestor: {member.name}"
                )
            if not parent.isdir():
                raise ArchiveRejected(
                    f"regular file is an ancestor of another member: {parent.name}"
                )

        if member.isreg():
            payload = archive.extractfile(member)
            if payload is None:
                raise ArchiveRejected(f"cannot read archive member: {member.name}")
            while payload.read(1024 * 1024):
                pass


def _safe_components(name: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        raise ArchiveRejected(f"unsafe archive path: {name!r}")
    raw_components = name.split("/")
    if any(component in {"", ".", ".."} for component in raw_components):
        raise ArchiveRejected(f"ambiguous archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or tuple(path.parts) != tuple(raw_components):
        raise ArchiveRejected(f"unsafe archive path: {name!r}")
    return tuple(raw_components)


def _portable_component(component: str) -> str:
    """Return the portable identity used only for archive-admission collisions.

    Final bundle validation repeats this small rule independently so the two safety
    seams stay complete and neither peer capability depends on the other.
    """
    normalized = unicodedata.normalize("NFC", component)
    return unicodedata.normalize("NFC", normalized.casefold())


def _extract_members(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    extraction_directory: Path,
) -> None:
    extraction_directory.mkdir(mode=0o700)
    directories = sorted(
        (member for member in members if member.isdir()),
        key=lambda member: len(member.name.split("/")),
    )
    for member in directories:
        destination = extraction_directory.joinpath(*member.name.split("/"))
        destination.mkdir(mode=0o700)

    for member in members:
        if not member.isreg():
            continue
        source = archive.extractfile(member)
        if source is None:
            raise ArchiveRejected(f"cannot read archive member: {member.name}")
        destination = extraction_directory.joinpath(*member.name.split("/"))
        with destination.open("xb") as output:
            shutil.copyfileobj(source, output)
