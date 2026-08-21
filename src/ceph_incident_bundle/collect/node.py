"""One complete Target Node operation over one system OpenSSH process."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import signal
import subprocess
import sys

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
    diagnostics_path = staging_directory / "ssh-stderr"
    extraction_directory = staging_directory / "extracted"
    source_path = Path(remote_collector.__file__)
    argv = _ssh_argv(
        node,
        ssh_user=ssh_user,
        since_seconds=since_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        ssh_connect_timeout_seconds=ssh_connect_timeout_seconds,
        ceph_allowed=ceph_allowed,
    )

    start_error: OSError | None = None
    interrupted = False
    interruption_problems: list[str] = []
    try:
        # Regular files let OpenSSH read and write without pipe backpressure.  The
        # lexical scope also proves stdin, the complete stdout archive, and stderr
        # diagnostics all closed successfully before archive admission begins.
        with source_path.open("rb") as source_file, archive_path.open(
            "xb"
        ) as archive_file, diagnostics_path.open("xb") as diagnostics_file:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=source_file,
                    stdout=archive_file,
                    stderr=diagnostics_file,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as error:
                start_error = error
            else:
                try:
                    return_code = process.wait()
                except KeyboardInterrupt:
                    # The SSH process owns a separate group, so the operator's
                    # Ctrl-C reaches only the workstation flow.  Forward SIGINT
                    # to request the remote Python collector's normal ``finally``
                    # cleanup while the connection is still reachable.
                    interrupted = True
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                    except ProcessLookupError:
                        # The group already completed its cleanup and exited.
                        pass
                    except OSError as error:
                        interruption_problems.append(
                            f"Target Node {node.inventory_name}: cannot request "
                            f"cleanup from process group {process.pid}: {error}"
                        )
                    try:
                        process.wait()
                    except OSError as error:
                        interruption_problems.append(
                            f"Target Node {node.inventory_name}: cannot wait for "
                            f"interrupted process group {process.pid}: {error}"
                        )
    except OSError as error:
        if interrupted:
            interruption_problems.append(
                f"Target Node {node.inventory_name}: cannot close interrupted "
                f"SSH protocol streams: {error}"
            )
        else:
            return [
                f"Target Node {node.inventory_name}: "
                f"cannot complete one-SSH stream files: {error}"
            ]

    if start_error is not None:
        return [
            f"Target Node {node.inventory_name}: cannot start SSH: {start_error}"
        ]

    if interrupted:
        try:
            _print_diagnostics(node.inventory_name, diagnostics_path)
        except Exception:
            pass
        if interruption_problems:
            # The top-level interrupt owner reports this standard chained cause
            # after all three protocol streams have left their context managers.
            raise KeyboardInterrupt from OSError("; ".join(interruption_problems))
        raise KeyboardInterrupt

    try:
        _print_diagnostics(node.inventory_name, diagnostics_path)
    except Exception:
        # SSH diagnostics are best-effort terminal output.  A broken diagnostic
        # stream must not prevent admission of an otherwise complete archive.
        pass
    problems: list[str] = []

    try:
        admit_archive(
            archive_path,
            extraction_directory,
            contribution_directory,
            ceph_allowed=ceph_allowed,
        )
    except ArchiveRejected as error:
        problems.append(
            f"Target Node {node.inventory_name}: "
            f"Node Evidence Archive rejected: {error}"
        )
        return problems
    except OSError as error:
        problems.append(
            f"Target Node {node.inventory_name}: local Node Evidence Archive "
            f"admission failed in private staging {staging_directory}: {error}"
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


def _print_diagnostics(inventory_name: str, diagnostics_path: Path) -> None:
    try:
        diagnostics = diagnostics_path.open("rb")
    except FileNotFoundError:
        return
    prefix = f"[{inventory_name}] "
    decoder = codecs.getincrementaldecoder("utf-8")(errors="backslashreplace")
    at_line_start = True

    def render(text: str) -> None:
        nonlocal at_line_start
        position = 0
        while position < len(text):
            if at_line_start:
                sys.stderr.write(prefix)
                at_line_start = False
            newline = text.find("\n", position)
            end = len(text) if newline < 0 else newline
            _write_terminal_safe(text[position:end])
            if newline < 0:
                return
            sys.stderr.write("\n")
            at_line_start = True
            position = newline + 1

    with diagnostics:
        while True:
            chunk = diagnostics.read(64 * 1024)
            if not chunk:
                break
            render(decoder.decode(chunk))
        render(decoder.decode(b"", final=True))
    if not at_line_start:
        sys.stderr.write("\n")


def _write_terminal_safe(value: str) -> None:
    """Write one decoded diagnostic segment without terminal controls."""
    run_start = 0
    for index, character in enumerate(value):
        if character.isprintable():
            continue
        sys.stderr.write(value[run_start:index])
        if ord(character) <= 0xFF:
            sys.stderr.write(f"\\x{ord(character):02x}")
        else:
            sys.stderr.write(character.encode("unicode_escape").decode("ascii"))
        run_start = index + 1
    sys.stderr.write(value[run_start:])
