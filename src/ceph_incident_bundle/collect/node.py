"""One complete Target Node operation over one system OpenSSH process."""

from __future__ import annotations

import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from .. import remote_collector
from ..inventory import TargetNode
from .node_archive import ArchiveRejected, admit_archive


def collect_node(
    node: TargetNode,
    *,
    ssh_user: str,
    since_seconds: int,
    probe_timeout_seconds: int,
    ssh_connect_timeout_seconds: int,
    ceph_allowed: bool,
    staging_directory: Path,
    contribution_directory: Path,
) -> list[str]:
    """Receive and admit one Target Node contribution, returning its problems."""
    staging_directory = Path(staging_directory)
    contribution_directory = Path(contribution_directory)
    try:
        staging_directory.mkdir(mode=0o700)
    except OSError as error:
        return [
            f"Target Node {node.inventory_name}: "
            f"cannot create private staging: {error}"
        ]

    archive_path = staging_directory / "node-evidence.tar.gz"
    extraction_directory = staging_directory / "extracted"
    source_path = Path(remote_collector.__file__)
    source = source_path.read_bytes()
    argv = _ssh_argv(
        node,
        ssh_user=ssh_user,
        since_seconds=since_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        ssh_connect_timeout_seconds=ssh_connect_timeout_seconds,
        ceph_allowed=ceph_allowed,
    )

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as error:
        return [f"Target Node {node.inventory_name}: cannot start SSH: {error}"]

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    received_stderr = io.BytesIO()
    stream_errors: list[str] = []

    def send_source() -> None:
        try:
            process.stdin.write(source)
            process.stdin.close()
        except (BrokenPipeError, OSError) as error:
            stream_errors.append(f"cannot send Remote Node Collector: {error}")

    def receive_archive() -> None:
        archive_file = None
        try:
            archive_file = archive_path.open("xb")
        except OSError as error:
            stream_errors.append(f"cannot receive Node Evidence Archive: {error}")
        while True:
            try:
                chunk = process.stdout.read(1024 * 1024)
            except OSError as error:
                stream_errors.append(f"cannot receive Node Evidence Archive: {error}")
                break
            if not chunk:
                break
            if archive_file is not None:
                try:
                    archive_file.write(chunk)
                except OSError as error:
                    stream_errors.append(
                        f"cannot receive Node Evidence Archive: {error}"
                    )
                    try:
                        archive_file.close()
                    except OSError:
                        pass
                    archive_file = None
        if archive_file is not None:
            try:
                archive_file.close()
            except OSError as error:
                stream_errors.append(f"cannot finish Node Evidence Archive: {error}")

    def receive_diagnostics() -> None:
        try:
            while True:
                chunk = process.stderr.read(64 * 1024)
                if not chunk:
                    break
                received_stderr.write(chunk)
        except OSError as error:
            stream_errors.append(f"cannot receive SSH diagnostics: {error}")

    threads = [
        threading.Thread(target=send_source),
        threading.Thread(target=receive_archive),
        threading.Thread(target=receive_diagnostics),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
        process.stdout.close()
        process.stderr.close()

    _print_diagnostics(node.inventory_name, received_stderr.getvalue())
    problems = [
        f"Target Node {node.inventory_name}: {problem}" for problem in stream_errors
    ]

    try:
        admit_archive(
            archive_path,
            extraction_directory,
            contribution_directory,
            ceph_allowed=ceph_allowed,
        )
    except (ArchiveRejected, OSError) as error:
        problems.append(
            f"Target Node {node.inventory_name}: "
            f"Node Evidence Archive rejected: {error}"
        )
        return problems

    if return_code != 0:
        problems.append(
            f"Target Node {node.inventory_name}: Remote Node Collector exited "
            f"with status {return_code}; admitted evidence is partial"
        )
    return problems


def _ssh_argv(
    node: TargetNode,
    *,
    ssh_user: str,
    since_seconds: int,
    probe_timeout_seconds: int,
    ssh_connect_timeout_seconds: int,
    ceph_allowed: bool,
) -> list[str]:
    argv = ["ssh", "-T", "-o", "BatchMode=yes"]
    if ssh_connect_timeout_seconds:
        argv.extend(["-o", f"ConnectTimeout={ssh_connect_timeout_seconds}"])
    argv.extend(
        [
            f"{ssh_user}@{node.ssh_address}",
            "python3",
            "-",
            "--since-seconds",
            str(since_seconds),
            "--probe-timeout-seconds",
            str(probe_timeout_seconds),
        ]
    )
    if ceph_allowed:
        argv.append("--collect-ceph")
    return argv


def _print_diagnostics(inventory_name: str, received: bytes) -> None:
    if not received:
        return
    lines = received.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    for line in lines:
        print(f"[{inventory_name}] {_terminal_safe(line)}", file=sys.stderr)


def _terminal_safe(value: bytes) -> str:
    """Decode and render untrusted SSH stderr without terminal controls.

    This byte-oriented rule stays with SSH diagnostic rendering.  The top-level flow
    separately renders local exception strings at its own output boundary.
    """
    decoded = value.decode("utf-8", errors="backslashreplace")
    escaped: list[str] = []
    for character in decoded:
        if character.isprintable():
            escaped.append(character)
        elif ord(character) <= 0xFF:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character.encode("unicode_escape").decode("ascii"))
    return "".join(escaped)
