"""Self-contained collector streamed to a supported node over SSH stdin."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path


NODE_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("system/hostname.txt", ("hostname",)),
    ("system/uname.txt", ("uname", "-a")),
    ("system/uptime.txt", ("uptime",)),
    ("resources/free.txt", ("free", "-h")),
    ("storage/df.txt", ("df", "-hT")),
    ("network/ip-addr.txt", ("ip", "addr", "show")),
    (
        "systemd/failed-units.txt",
        ("systemctl", "--failed", "--no-pager", "--plain"),
    ),
)
SAFE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SAFE_INVOCATION_ID = re.compile(r"[a-f0-9]{32}\Z")


class NodeInterrupted(Exception):
    """The node payload received a recoverable termination signal."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_config(encoded: str) -> tuple[str, str, int]:
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
        config = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid node collector configuration") from error
    if not isinstance(config, dict):
        raise ValueError("invalid node collector configuration")
    alias = config.get("host_alias")
    invocation_id = config.get("invocation_id")
    timeout = config.get("command_timeout")
    if not isinstance(alias, str) or not SAFE_ALIAS.fullmatch(alias):
        raise ValueError("invalid host alias")
    if not isinstance(invocation_id, str) or not SAFE_INVOCATION_ID.fullmatch(
        invocation_id
    ):
        raise ValueError("invalid invocation identifier")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("invalid command timeout")
    return alias, invocation_id, timeout


def _write_artifact(
    output: Path,
    manifest: Path,
    host_alias: str,
    timeout: int,
    relative_artifact: str,
    command: Sequence[str],
) -> bool:
    artifact = output / relative_artifact
    artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = _utc_now()
    exit_code = 0
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except FileNotFoundError:
        exit_code = 127
        stdout = b""
        stderr = f"SKIPPED: command not found: {command[0]}\n".encode()
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        stdout = error.stdout or b""
        stderr = (error.stderr or b"") + b"command timed out\n"
    ended = _utc_now()
    header = (
        f"# host: {host_alias}\n"
        "# collector: collect-node\n"
        f"# started: {started}\n"
        f"# timeout: {timeout}s\n"
    ).encode()
    timeout_marker = b""
    if exit_code in (124, 137):
        timeout_marker = (
            f"# TRUNCATED: command timed out after {timeout}s (exit {exit_code})\n"
        ).encode()
    artifact.write_bytes(header + stdout + stderr + timeout_marker)
    entry = {
        "host": host_alias,
        "collector": "collect-node",
        "artifact": str(artifact),
        "command": shlex.join(command),
        "exit_code": exit_code,
        "started": started,
        "ended": ended,
    }
    with manifest.open("a", encoding="utf-8") as output_stream:
        output_stream.write(json.dumps(entry, sort_keys=True) + "\n")
    if exit_code != 0:
        with (manifest.parent / "errors.log").open("a", encoding="utf-8") as errors:
            errors.write(
                f"{ended} host={host_alias} collector=collect-node "
                f"artifact={artifact} exit={exit_code} "
                f"command={shlex.join(command)}\n"
            )
    return exit_code == 0


def _write_archive(output: Path, workspace: Path) -> bool:
    uncompressed = workspace / "node-evidence.tar"
    tar_result = subprocess.run(
        ["tar", "-cf", str(uncompressed), "-C", str(output), "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
        check=False,
    )
    if tar_result.returncode != 0:
        sys.stderr.buffer.write(tar_result.stderr)
        return False
    gzip_result = subprocess.run(
        ["gzip", "-c", str(uncompressed)],
        stdout=sys.stdout.buffer,
        stderr=subprocess.PIPE,
        check=False,
    )
    if gzip_result.returncode != 0:
        sys.stderr.buffer.write(gzip_result.stderr)
        return False
    sys.stdout.buffer.flush()
    return True


def _raise_interrupted(signum: int, _frame: object) -> None:
    raise NodeInterrupted(f"signal {signum}")


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 1:
        print("node collector configuration is required", file=sys.stderr)
        return 1
    try:
        host_alias, invocation_id, timeout = _decode_config(args[0])
    except ValueError as error:
        print(f"node collector configuration rejected: {error}", file=sys.stderr)
        return 1

    workspace: Path | None = None
    previous_handlers: dict[int, object] = {}
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    for signal_number in handled_signals:
        previous_handlers[signal_number] = signal.signal(
            signal_number, _raise_interrupted
        )
    try:
        workspace = Path(
            tempfile.mkdtemp(prefix=f"ceph-incident-node-{invocation_id}-")
        ).resolve(strict=True)
        workspace.chmod(0o700)
        output = workspace / "out"
        output.mkdir(mode=0o700)
        manifest = output / "manifest.jsonl"
        manifest.touch(mode=0o600)
        failed = False
        for relative_artifact, command in NODE_COMMANDS:
            if not _write_artifact(
                output, manifest, host_alias, timeout, relative_artifact, command
            ):
                failed = True
        if not _write_archive(output, workspace):
            return 74
        return 2 if failed else 0
    except NodeInterrupted as error:
        print(f"node collector interrupted ({error})", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"node collector failed: {error}", file=sys.stderr)
        return 1
    finally:
        if workspace is not None:
            shutil.rmtree(workspace)
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
