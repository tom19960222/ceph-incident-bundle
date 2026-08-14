"""Self-contained Remote Node Collector streamed unchanged over SSH."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_hostname(capture: Path, timeout_seconds: int) -> bool:
    capture.mkdir(parents=True)
    started_at = _utc_now()
    stdout = b""
    stderr = b""
    outcome = "exited"
    exit_code = None
    error = None

    try:
        process = subprocess.Popen(
            ["hostname"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            timeout = timeout_seconds or None
            stdout, stderr = process.communicate(timeout=timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            outcome = "timed_out"
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate()
            error = {
                "kind": "timeout",
                "message": f"hostname exceeded {timeout_seconds} seconds",
            }
    except OSError as process_error:
        outcome = "failed_to_start"
        error = {
            "kind": type(process_error).__name__,
            "message": str(process_error),
        }

    finished_at = _utc_now()
    (capture / "stdout").write_bytes(stdout)
    (capture / "stderr").write_bytes(stderr)
    result = {
        "argv": ["hostname"],
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": outcome,
        "exit_code": exit_code,
        "error": error,
    }
    (capture / "result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )

    succeeded = outcome == "exited" and exit_code == 0
    if not succeeded:
        print(
            f"hostname Probe failed: outcome={outcome} exit_code={exit_code}",
            file=sys.stderr,
        )
    return succeeded


def _stream_archive(evidence_root: Path) -> None:
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        archive.add(evidence_root / "node", arcname="node", recursive=True)
        ceph = evidence_root / "ceph"
        if ceph.exists():
            archive.add(ceph, arcname="ceph", recursive=True)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--since-seconds", type=_canonical_seconds, required=True)
    parser.add_argument(
        "--probe-timeout-seconds", type=_canonical_seconds, required=True
    )
    parser.add_argument("--collect-ceph", action="store_true")
    raw_arguments = sys.argv[1:]
    if (
        raw_arguments.count("--since-seconds") != 1
        or raw_arguments.count("--probe-timeout-seconds") != 1
        or raw_arguments.count("--collect-ceph") > 1
    ):
        parser.error("each fixed Remote Node Collector switch may appear only once")
    arguments = parser.parse_args()
    if arguments.since_seconds <= 0:
        parser.error("since seconds must be positive")
    return arguments


def _canonical_seconds(value: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value, re.ASCII) is None:
        raise argparse.ArgumentTypeError(
            "control values must use canonical ASCII decimal seconds"
        )
    return int(value)


def main() -> int:
    if sys.implementation.name != "cpython" or sys.version_info < (3, 10):
        print("Remote Node Collector requires CPython 3.10 or newer", file=sys.stderr)
        return 1

    arguments = _parse_arguments()
    # The #87 hostname tracer does not yet consume the evidence window.  It is
    # still a required validated control in the fixed one-SSH protocol so later
    # time-bounded built-in Probes can reuse this exact remote invocation.
    workspace = Path(tempfile.mkdtemp(prefix="ceph-incident-node."))
    succeeded = True
    try:
        node = workspace / "node"
        (node / "probes").mkdir(parents=True)
        (node / "files").mkdir()
        if arguments.collect_ceph:
            (workspace / "ceph" / "probes").mkdir(parents=True)

        try:
            succeeded = _run_hostname(
                node / "probes" / "hostname", arguments.probe_timeout_seconds
            )
        except OSError as capture_error:
            print(
                f"cannot preserve hostname Probe Capture: {capture_error}",
                file=sys.stderr,
            )
            succeeded = False
        try:
            _stream_archive(workspace)
        except (OSError, tarfile.TarError) as archive_error:
            print(
                f"cannot produce Node Evidence Archive: {archive_error}",
                file=sys.stderr,
            )
            succeeded = False
    finally:
        try:
            shutil.rmtree(workspace)
        except OSError as cleanup_error:
            print(
                f"cannot remove Remote Node Collector workspace {workspace}: "
                f"{cleanup_error}",
                file=sys.stderr,
            )
            succeeded = False
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
