"""Self-contained Remote Node Collector streamed unchanged over SSH."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from typing import NamedTuple


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Probe(NamedTuple):
    """One built-in Evidence Probe: a stable name, area, and fixed argv.

    The Remote Node Collector is a self-contained payload streamed unchanged
    over SSH, so this catalog is declared here rather than imported from the
    workstation package.
    """

    name: str
    area: str
    argv: tuple[str, ...]


# Fixed Target Node Probe catalog. See docs/python-rewrite-spec.md's "Fixed
# Target Node Probe catalog" table for the built-in name/argv contract.
NODE_PROBE_CATALOG: tuple[Probe, ...] = (
    Probe(name="hostname", area="node", argv=("hostname",)),
    Probe(
        name="current-utc",
        area="node",
        argv=("date", "-u", "+%Y-%m-%dT%H:%M:%SZ"),
    ),
    Probe(name="uname", area="node", argv=("uname", "-a")),
    Probe(name="uptime", area="node", argv=("uptime",)),
    Probe(name="lscpu", area="node", argv=("lscpu",)),
    Probe(name="free", area="node", argv=("free", "-h")),
    Probe(name="processes", area="node", argv=("ps", "auxfww")),
    Probe(name="df", area="node", argv=("df", "-hT")),
    Probe(
        name="lsblk",
        area="node",
        argv=(
            "lsblk",
            "-a",
            "-o",
            "NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
        ),
    ),
    Probe(name="iostat", area="node", argv=("iostat", "-xz", "1", "3")),
    Probe(name="pvs", area="node", argv=("pvs", "--noheadings", "--separator", " ")),
    Probe(name="vgs", area="node", argv=("vgs", "--noheadings", "--separator", " ")),
    Probe(name="lvs", area="node", argv=("lvs", "--noheadings", "--separator", " ")),
    Probe(name="ip-address", area="node", argv=("ip", "addr", "show")),
    Probe(name="dmesg", area="node", argv=("dmesg", "-T")),
    Probe(
        name="failed-units",
        area="node",
        argv=("systemctl", "--failed", "--no-pager", "--plain"),
    ),
    Probe(name="podman-ps", area="node", argv=("podman", "ps", "-a")),
    Probe(name="docker-ps", area="node", argv=("docker", "ps", "-a")),
    Probe(name="chronyc-tracking", area="node", argv=("chronyc", "tracking")),
    Probe(name="chronyc-sources", area="node", argv=("chronyc", "sources", "-v")),
    Probe(name="ntpq-peers", area="node", argv=("ntpq", "-pn")),
    Probe(name="timedatectl-status", area="node", argv=("timedatectl", "status")),
    Probe(
        name="timedatectl-show-timesync",
        area="node",
        argv=("timedatectl", "show-timesync", "--all"),
    ),
    Probe(
        name="timedatectl-timesync-status",
        area="node",
        argv=("timedatectl", "timesync-status"),
    ),
    Probe(
        name="systemd-timesyncd-status",
        area="node",
        argv=(
            "systemctl",
            "status",
            "systemd-timesyncd",
            "--no-pager",
            "--plain",
        ),
    ),
)


def _run_probe(probe: Probe, capture: Path, timeout_seconds: int) -> bool:
    capture.mkdir(parents=True)
    started_at = _utc_now()
    outcome = "exited"
    exit_code = None
    error = None

    with (capture / "stdout").open("xb") as stdout_file, (
        capture / "stderr"
    ).open("xb") as stderr_file:
        try:
            process = subprocess.Popen(
                list(probe.argv),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError as process_error:
            outcome = "failed_to_start"
            error = {
                "kind": type(process_error).__name__,
                "message": str(process_error),
            }
        else:
            try:
                timeout = timeout_seconds or None
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                outcome = "timed_out"
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    # The child can leave between wait timing out and the
                    # normal V1 process-group termination request.  It still
                    # timed out from the Probe contract's point of view, so
                    # retain that result instead of losing its capture.
                    pass
                process.wait()
                error = {
                    "kind": "timeout",
                    "message": f"{probe.name} exceeded {timeout_seconds} seconds",
                }
            except (OverflowError, ValueError) as wait_error:
                exit_code = process.poll()
                if exit_code is None:
                    outcome = "timed_out"
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    error = {
                        "kind": "timeout",
                        "message": (
                            f"cannot apply {probe.name} timeout: {wait_error}"
                        ),
                    }

    finished_at = _utc_now()
    result = {
        "argv": list(probe.argv),
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
        error_detail = ""
        if error is not None:
            error_detail = f" error={error['kind']}: {error['message']}"
        print(
            f"{probe.name} Probe failed: outcome={outcome} exit_code={exit_code}"
            f"{error_detail}",
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
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
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
    if not _fits_process_wait_timeout(arguments.probe_timeout_seconds):
        parser.error("probe timeout exceeds the supported range")
    return arguments


def _canonical_seconds(value: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value, re.ASCII) is None:
        raise argparse.ArgumentTypeError(
            "control values must use canonical ASCII decimal seconds"
        )
    return int(value)


def _fits_process_wait_timeout(seconds: int) -> bool:
    try:
        return math.isfinite(float(seconds))
    except OverflowError:
        return False


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

        for probe in NODE_PROBE_CATALOG:
            try:
                probe_succeeded = _run_probe(
                    probe,
                    node / "probes" / probe.name,
                    arguments.probe_timeout_seconds,
                )
            except OSError as capture_error:
                print(
                    f"cannot preserve {probe.name} Probe Capture: {capture_error}",
                    file=sys.stderr,
                )
                probe_succeeded = False
            succeeded = succeeded and probe_succeeded
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
