"""Fail-closed admission of one untrusted Node Evidence Archive."""

from __future__ import annotations

from collections.abc import Iterator
import gzip
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from typing import BinaryIO
import unicodedata
import zlib


_STREAM_CHUNK_BYTES = 1024 * 1024


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
    _validate_archive_file(archive_path)

    try:
        with tempfile.TemporaryFile() as tar_stream:
            for chunk in _one_gzip_member_chunks(archive_path):
                tar_stream.write(chunk)
            tar_stream.seek(0)
            with tarfile.open(fileobj=tar_stream, mode="r:") as archive:
                members = archive.getmembers()
                first_eof_offset = _first_tar_eof_offset(archive)
                _validate_members(archive, members, ceph_allowed=ceph_allowed)
                _validate_tar_end(tar_stream, first_eof_offset)
                _extract_members(archive, members, extraction_directory)
    except ArchiveRejected:
        raise
    except (EOFError, tarfile.TarError) as error:
        # CPython's gzip tar opener translates an initial local OSError into a
        # ReadError.  Restore that ordinary file-system meaning, while keeping
        # BadGzipFile classified as corrupt archive input.
        cause = error.__cause__
        if isinstance(cause, OSError) and not isinstance(cause, gzip.BadGzipFile):
            raise cause
        raise ArchiveRejected(f"invalid Node Evidence Archive: {error}") from error

    os.rename(extraction_directory, contribution_directory)


def _validate_archive_file(archive_path: Path) -> None:
    archive_mode = archive_path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(archive_mode):
        raise ArchiveRejected("Node Evidence Archive must be an owned regular file")


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
                raw_chunk = compressed.read(_STREAM_CHUNK_BYTES)
                if not raw_chunk:
                    break
                if decompressor.eof:
                    raise ArchiveRejected(
                        "Node Evidence Archive has bytes after its gzip member"
                    )
                pending = raw_chunk
                while pending:
                    uncompressed = decompressor.decompress(
                        pending, max_length=_STREAM_CHUNK_BYTES
                    )
                    pending = decompressor.unconsumed_tail
                    if decompressor.unused_data:
                        raise ArchiveRejected(
                            "Node Evidence Archive has bytes after its gzip member"
                        )
                    if uncompressed:
                        yield uncompressed
            if not decompressor.eof:
                raise ArchiveRejected(
                    "Node Evidence Archive has an incomplete gzip member"
                )
            # Reaching EOF after exhausting unconsumed_tail means every output
            # byte was returned by the bounded decompress calls above.  Calling
            # flush here would reintroduce an output allocation without a cap.
    except ArchiveRejected:
        raise
    except zlib.error as error:
        raise ArchiveRejected(f"invalid compressed archive: {error}") from error


def _first_tar_eof_offset(archive: tarfile.TarFile) -> int:
    """Read CPython's next-header offset, failing closed on runtime drift."""
    member_end = getattr(archive, "offset", None)
    if type(member_end) is not int or member_end < 0 or member_end % 512 != 0:
        raise ArchiveRejected("invalid archive end offset")
    return member_end


def _validate_tar_end(tar_stream: BinaryIO, member_end: int) -> None:
    """Reject a second tar stream or any other data after the first tar EOF."""
    stream_end = tar_stream.seek(0, os.SEEK_END)
    if stream_end < member_end:
        raise ArchiveRejected("Node Evidence Archive member data is incomplete")
    if stream_end % 512 != 0:
        raise ArchiveRejected("Node Evidence Archive tar stream is not aligned")
    if stream_end - member_end < 1024:
        raise ArchiveRejected("Node Evidence Archive is missing its tar EOF blocks")
    tar_stream.seek(member_end)
    while padding := tar_stream.read(_STREAM_CHUNK_BYTES):
        if padding.strip(b"\0"):
            raise ArchiveRejected(
                "Node Evidence Archive has data after its first tar stream"
            )


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
        # ``issparse`` is another CPython tarfile detail rather than a documented
        # cross-implementation promise.  Guard it so runtime drift rejects input.
        sparse_check = getattr(member, "issparse", None)
        if not callable(sparse_check):
            raise ArchiveRejected(
                f"archive member lacks CPython sparse-layout check: {member.name}"
            )
        try:
            is_sparse = sparse_check()
        except Exception as error:
            raise ArchiveRejected(
                f"cannot check archive member sparse layout: {member.name}: {error}"
            ) from error
        if type(is_sparse) is not bool:
            raise ArchiveRejected(
                f"invalid archive member sparse-layout result: {member.name}"
            )
        if (not member.isdir() and not member.isreg()) or is_sparse:
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
    for required in required_directories:
        member = by_path.get(required)
        if member is None or not member.isdir():
            raise ArchiveRejected(
                f"missing required archive directory: {'/'.join(required)}"
            )

    # ``ceph/`` is permitted when the workstation allows it, never required: a
    # Target Node may still contribute only ordinary ``node/`` evidence.  When
    # ``ceph/`` is present, its own structure is validated exactly like any
    # other required directory so a partial or malformed ``ceph/`` is rejected.
    if ("ceph",) in by_path:
        for required in (("ceph",), ("ceph", "probes")):
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
