#!/usr/bin/env python3
"""Public command-line entrypoint for Ceph incident evidence collection."""

from __future__ import annotations

import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath


USAGE = "Usage: ceph_incident_bundle.py verify <bundle-dir|bundle.tar.gz>"
REQUIRED_FILES = ("manifest.jsonl", "summary.txt", "README-FIRST.txt")


class VerificationError(Exception):
    """An incident bundle failed structural verification."""


def _normalise_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise VerificationError(f"unsafe archive member: {name}")
    return path.as_posix()


def _verify_required_files(files: set[str]) -> None:
    for required in REQUIRED_FILES:
        if required not in files:
            raise VerificationError(f"missing required file: {required}")


def _verify_cluster_artifact(files: set[str]) -> None:
    if not any(name.startswith("cluster/") for name in files):
        raise VerificationError("missing cluster/ artifact")


def _verify_nodes_artifact(files: set[str]) -> None:
    if not any(name.startswith("nodes/") for name in files):
        raise VerificationError("missing nodes/ artifact")


def _read_archive(target: Path) -> set[str]:
    files: set[str] = set()
    try:
        with tarfile.open(target, mode="r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                _normalise_member_name(member.name)
                if not (member.isfile() or member.isdir()):
                    member_kind = (
                        "symlink or hardlink"
                        if member.issym() or member.islnk()
                        else "non-file/non-directory"
                    )
                    raise VerificationError(
                        f"archive contains {member_kind} member: {member.name}"
                    )

            for member in members:
                if member.isdir():
                    continue
                files.add(_normalise_member_name(member.name))
                stream = archive.extractfile(member)
                if stream is None:
                    raise VerificationError(f"cannot read archive member: {member.name}")
                with stream:
                    while stream.read(1024 * 1024):
                        pass
    except (OSError, tarfile.TarError, EOFError) as error:
        raise VerificationError(f"invalid archive: {target}") from error
    return files


def _verify_directory(target: Path) -> None:
    files: set[str] = set()
    for path in target.rglob("*"):
        relative_name = path.relative_to(target).as_posix()
        if path.is_symlink():
            raise VerificationError(f"symlink is not allowed in bundle: {relative_name}")
        if path.is_file():
            files.add(relative_name)
    _verify_required_files(files)
    _verify_cluster_artifact(files)
    _verify_nodes_artifact(files)


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 2 or args[0] != "verify":
        print(USAGE, file=sys.stderr)
        return 1

    target = Path(args[1])
    if target.is_dir():
        try:
            _verify_directory(target)
        except VerificationError as error:
            print(f"VERIFY FAIL: {error}", file=sys.stderr)
            return 1
        print(f"VERIFY PASS: {args[1]}")
        return 0

    if target.is_file() and args[1].endswith(".tar.gz"):
        try:
            files = _read_archive(target)
            _verify_required_files(files)
            _verify_cluster_artifact(files)
            _verify_nodes_artifact(files)
        except VerificationError as error:
            print(f"VERIFY FAIL: {error}", file=sys.stderr)
            return 1
        print(f"VERIFY PASS: {args[1]}")
        return 0

    print(
        f"VERIFY FAIL: expected a directory or .tar.gz bundle: {args[1]}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
