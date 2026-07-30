"""One offline fake world, handed unchanged to both Collect implementations.

The gate's whole claim rests on this module: the shell reference and the Python
candidate receive the same fake executables, the same inventory, the same SSH
key path, the same scenario knobs, the same payload limits and the same clock
inputs.  Anything a run is allowed to differ in has to come from the collector
itself, so this builder never branches on which implementation it is preparing —
only on which entrypoint to execute.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FAKES = ROOT / "tests" / "differential" / "fakes"
SHARED_CURL = ROOT / "tests" / "fixtures" / "bin" / "curl"

SHELL_ENTRYPOINT = ("bash", str(ROOT / "run" / "collect.sh"))
PYTHON_ENTRYPOINT = (sys.executable, str(ROOT / "ceph_incident_bundle.py"), "collect")

IMPLEMENTATIONS = ("shell", "python")

DEFAULT_INVENTORY = (
    'SSH_USER="ceph"\n'
    'ROOK_NAMESPACE="rook-ceph"\n'
    "HOSTS=(\n"
    '  "monitor01=10.0.0.1"\n'
    '  "kubenode=10.0.0.9"\n'
    ")\n"
)


@dataclass(frozen=True)
class CollectRun:
    """Everything one Collect invocation observably produced."""

    implementation: str
    exit_code: int
    stdout: str
    stderr: str
    output_root: Path
    archive: Path | None
    workdir: Path | None
    ledgers: dict[str, list[str]] = field(default_factory=dict)


def build_world(root: Path, *, inventory_text: str, knobs: dict[str, str]) -> dict[str, str]:
    """Create the fake world under ``root`` and return the run environment."""

    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    for name in ("ssh", "kubectl", "timeout"):
        shutil.copy2(FAKES / name, fake_bin / name)
    shutil.copy2(FAKES / "node_archive.py", fake_bin / "node_archive.py")
    shutil.copy2(SHARED_CURL, fake_bin / "curl")
    for name in ("ssh", "kubectl", "timeout", "curl"):
        (fake_bin / name).chmod(0o755)

    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    ssh_key = root / "id_ed25519"
    ssh_key.write_text("fixture key path only\n", encoding="utf-8")
    ssh_key.chmod(0o600)
    inventory = root / "inventory.env"
    inventory.write_text(inventory_text, encoding="utf-8")
    ledgers = root / "ledgers"
    ledgers.mkdir()

    environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "LC_ALL": "C",
        "TZ": "UTC",
        "FAKE_KUBECTL_BIN": str(fake_bin / "kubectl"),
        "FAKE_SSH_LOG": str(ledgers / "ssh-argv.jsonl"),
        "FAKE_KUBECTL_LOG": str(ledgers / "kubectl-argv.jsonl"),
        "FAKE_CURL_LOG": str(ledgers / "curl.log"),
        "FAKE_CURL_ARGV_LOG": str(ledgers / "curl-argv.nul"),
        "FAKE_TIMEOUT_LOG": str(ledgers / "timeout.log"),
        "FAKE_NODE_LEDGER": str(ledgers / "node-requests.jsonl"),
        "FAKE_BOOTSTRAP_LEDGER": str(ledgers / "bootstrap.jsonl"),
        **knobs,
    }
    if "SYSTEMROOT" in os.environ:  # keep subprocess spawnable on odd hosts
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def entrypoint_command(
    implementation: str, *, inventory: Path, ssh_key: Path, out: Path, arguments: tuple[str, ...]
) -> list[str]:
    base = SHELL_ENTRYPOINT if implementation == "shell" else PYTHON_ENTRYPOINT
    return [
        *base,
        "--inventory",
        str(inventory),
        "--ssh-key",
        str(ssh_key),
        "--out",
        str(out),
        *arguments,
    ]


def run_collect(
    root: Path,
    implementation: str,
    *,
    inventory_text: str,
    knobs: dict[str, str],
    arguments: tuple[str, ...],
    interrupt_after: str | None = None,
    timeout: int = 300,
) -> CollectRun:
    """Run one Collect implementation in a freshly built fake world."""

    environment = build_world(root, inventory_text=inventory_text, knobs=knobs)
    output_root = root / "results"
    command = entrypoint_command(
        implementation,
        inventory=root / "inventory.env",
        ssh_key=root / "id_ed25519",
        out=output_root,
        arguments=arguments,
    )

    if interrupt_after is None:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code, stdout, stderr = (
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
    else:
        exit_code, stdout, stderr = _run_and_interrupt(
            command, environment, interrupt_after, timeout
        )

    return CollectRun(
        implementation=implementation,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output_root=output_root,
        archive=_archive_in(output_root),
        workdir=_workdir_in(output_root),
        ledgers=_read_ledgers(root / "ledgers"),
    )


def _run_and_interrupt(
    command: list[str], environment: dict[str, str], marker: str, timeout: int
) -> tuple[int, str, str]:
    """Deliver SIGINT once the run reaches a progress marker, then collect it.

    Both entrypoints announce their progress on stderr, so the interrupt lands
    at the same lifecycle point for both instead of at an arbitrary wall clock
    offset.  The signal goes to the whole process group because the shell
    reference is a pipeline of children.
    """

    pattern = re.compile(marker)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stderr is not None
    observed: list[str] = []
    deadline = time.monotonic() + timeout
    signalled = False
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if not line:
            break
        observed.append(line)
        if pattern.search(line):
            os.killpg(process.pid, signal.SIGINT)
            signalled = True
            break
    if not signalled:
        process.kill()
        raise AssertionError(
            f"progress marker {marker!r} never appeared; saw: {''.join(observed)}"
        )
    stdout, stderr = process.communicate(timeout=timeout)
    return process.returncode, stdout, "".join(observed) + stderr


def _archive_in(output_root: Path) -> Path | None:
    if not output_root.is_dir():
        return None
    archives = sorted(output_root.glob("ceph-incident-*.tar.gz"))
    return archives[0] if archives else None


def _workdir_in(output_root: Path) -> Path | None:
    if not output_root.is_dir():
        return None
    workdirs = sorted(item for item in output_root.glob("tmp.*") if item.is_dir())
    return workdirs[0] if workdirs else None


def _read_ledgers(directory: Path) -> dict[str, list[str]]:
    ledgers: dict[str, list[str]] = {}
    for path in sorted(directory.iterdir()) if directory.is_dir() else []:
        if path.name.endswith(".nul"):
            continue
        ledgers[path.name] = path.read_text(encoding="utf-8").splitlines()
    return ledgers
