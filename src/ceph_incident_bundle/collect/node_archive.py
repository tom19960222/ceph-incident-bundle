"""Fail-closed admission of one untrusted Node Evidence Archive."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import tarfile
import unicodedata
import zlib


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
            _validate_members(archive, members, ceph_allowed=ceph_allowed)
            first_eof_offset = _first_tar_eof_offset(members)
            _validate_trailing_tar_padding(archive_path, first_eof_offset)
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
    """Require exactly one complete gzip member and terminal tar zero blocks."""
    total = 0
    tail = b""
    for chunk in _one_gzip_member_chunks(archive_path):
        total += len(chunk)
        tail = (tail + chunk)[-1024:]
    if total < 1024 or total % 512 != 0 or tail != b"\0" * 1024:
        raise ArchiveRejected("Node Evidence Archive is truncated")


def _one_gzip_member_chunks(archive_path: Path) -> Iterator[bytes]:
    """Yield one gzip member while rejecting truncation and all trailing bytes.

    ``zlib.decompressobj`` documents ``eof`` as the end-of-stream marker and
    ``unused_data`` as bytes following that marker.  Unlike ``gzip.open``, this
    lets admission reject a second gzip member even when it expands to no bytes
    or only zero padding.
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        with archive_path.open("rb") as compressed:
            while True:
                raw_chunk = compressed.read(1024 * 1024)
                if not raw_chunk:
                    break
                if decompressor.eof:
                    raise ArchiveRejected(
                        "Node Evidence Archive has bytes after its gzip member"
                    )
                uncompressed = decompressor.decompress(raw_chunk)
                if decompressor.unused_data or decompressor.unconsumed_tail:
                    raise ArchiveRejected(
                        "Node Evidence Archive has bytes after its gzip member"
                    )
                if uncompressed:
                    yield uncompressed
            if not decompressor.eof:
                raise ArchiveRejected(
                    "Node Evidence Archive has an incomplete gzip member"
                )
            final_bytes = decompressor.flush()
            if final_bytes:
                yield final_bytes
    except ArchiveRejected:
        raise
    except (OSError, zlib.error) as error:
        raise ArchiveRejected(f"invalid compressed archive: {error}") from error


def _first_tar_eof_offset(members: list[tarfile.TarInfo]) -> int:
    """Derive the first tar EOF position from documented member attributes.

    ``TarInfo.offset_data`` is the start of a member's data and ``size`` is its
    byte count.  Admission has already limited members to ordinary directories
    and nonsparse regular files, so rounding each payload to a 512-byte tar block
    gives the next possible header position.  The greatest such position is where
    the first required pair of tar EOF blocks must start.
    """
    member_end = 0
    for member in members:
        if member.offset_data < 512 or member.offset_data % 512 != 0:
            raise ArchiveRejected(
                f"invalid archive member data offset: {member.name}"
            )
        payload_size = member.size if member.isreg() else 0
        payload_blocks, remainder = divmod(payload_size, 512)
        if remainder:
            payload_blocks += 1
        candidate_end = member.offset_data + payload_blocks * 512
        member_end = max(member_end, candidate_end)
    return member_end


def _validate_trailing_tar_padding(archive_path: Path, member_end: int) -> None:
    """Reject a second tar stream or any other data after the first tar EOF."""
    padding_bytes = 0
    uncompressed_offset = 0
    for chunk in _one_gzip_member_chunks(archive_path):
        next_offset = uncompressed_offset + len(chunk)
        if next_offset > member_end:
            padding = chunk[max(0, member_end - uncompressed_offset) :]
            padding_bytes += len(padding)
            if padding.strip(b"\0"):
                raise ArchiveRejected(
                    "Node Evidence Archive has data after its first tar stream"
                )
        uncompressed_offset = next_offset
    if uncompressed_offset < member_end:
        raise ArchiveRejected("Node Evidence Archive member data is incomplete")
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
        if (not member.isdir() and not member.isreg()) or member.issparse():
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
