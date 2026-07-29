#!/usr/bin/env python3
"""Emit fake Node Evidence Archives for public shell Collect tests."""

from __future__ import annotations

import gzip
import io
import os
import sys
import tarfile


def add_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    archive.addfile(member, io.BytesIO(content))


def main() -> None:
    case = sys.argv[1]
    alias = sys.argv[2]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        add_file(archive, "system/hostname.txt", f"node {alias}\n".encode())

        if case == "absolute":
            add_file(
                archive,
                os.environ["FAKE_SSH_ARCHIVE_OUTSIDE_PATH"],
                b"overwritten\n",
            )
        elif case == "empty":
            add_file(archive, "", b"empty-name\n")
        elif case == "traversal":
            add_file(archive, "../../../escape.txt", b"escaped\n")
        elif case in {"symlink", "hardlink", "device", "fifo", "socket"}:
            member = tarfile.TarInfo(f"system/unsafe-{case}")
            member.type = {
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "device": tarfile.CHRTYPE,
                "fifo": tarfile.FIFOTYPE,
                "socket": b"s",
            }[case]
            if case in {"symlink", "hardlink"}:
                member.linkname = "system/hostname.txt"
            if case == "device":
                member.devmajor = 1
                member.devminor = 3
            archive.addfile(member)
        elif case == "collision":
            add_file(archive, "system//hostname.txt", b"collision\n")
        elif case == "oversize":
            add_file(archive, "system/large.txt", b"x" * 131072)
        elif case not in {
            "malformed-manifest",
            "missing-manifest",
            "tar-truncated",
            "truncated",
            "unsafe-manifest-artifact",
            "valid",
        }:
            raise SystemExit(f"unknown fake node archive case: {case}")

        if case != "missing-manifest":
            manifest = (
                '{"host":"%s","collector":"collect-node",'
                '"artifact":"/rmt/out/system/hostname.txt",'
                '"command":"hostname","exit_code":0,'
                '"started":"t0","ended":"t1"}\n' % alias
            ).encode()
            if case == "malformed-manifest":
                manifest = b"{not-json\n"
            elif case == "unsafe-manifest-artifact":
                manifest = manifest.replace(
                    b'"/rmt/out/system/hostname.txt"', b'"/etc/passwd"'
                )
            add_file(
                archive,
                "manifest.jsonl",
                manifest,
            )

    tar_payload = output.getvalue()
    if case == "tar-truncated":
        nonzero_blocks = [
            index
            for index in range(0, len(tar_payload), 512)
            if any(tar_payload[index : index + 512])
        ]
        tar_payload = tar_payload[: nonzero_blocks[-1] + 512]
    payload = gzip.compress(tar_payload)
    if case == "truncated":
        payload = payload[:-8]
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()
