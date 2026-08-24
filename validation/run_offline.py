#!/usr/bin/env python3
"""Build, install, and test the Python product without network access."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 10):
        print(
            "validate requires an explicitly selected CPython 3.10.x interpreter; "
            f"received {sys.implementation.name} {sys.version.split()[0]}",
            file=sys.stderr,
        )
        return 2

    print(f"production runtime: {sys.executable} ({sys.version.split()[0]})")
    with TemporaryDirectory(prefix="ceph-incident-validation-") as directory:
        validation_root = Path(directory)
        source = validation_root / "source"
        wheelhouse = validation_root / "wheelhouse"
        environment = validation_root / "installed"
        _copy_clean_source(source)
        wheelhouse.mkdir()
        _run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(source),
        )
        wheels = sorted(wheelhouse.glob("ceph_incident_bundle-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one built wheel, found {wheels}")

        _run(sys.executable, "-m", "venv", str(environment))
        installed_python = environment / "bin" / "python"
        _run(
            str(installed_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            str(wheels[0]),
        )
        command = environment / "bin" / "ceph-incident-bundle"
        if not command.is_file():
            raise RuntimeError("installed wheel did not create ceph-incident-bundle")

        test_environment = os.environ.copy()
        test_environment.pop("PYTHONPATH", None)
        test_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        test_environment["CEPH_INCIDENT_BUNDLE_COMMAND"] = str(command)
        test_environment["CEPH_INCIDENT_BUNDLE_WHEEL"] = str(wheels[0])
        _run(
            str(installed_python),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/python",
            "-v",
            cwd=source,
            env=test_environment,
        )
    return 0


def _copy_clean_source(destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        ".git",
        "__pycache__",
        "*.pyc",
        "*.pyo",
        "*.egg-info",
        ".pytest_cache",
        "build",
        "dist",
        ".venv",
        "results",
        ".ssh",
        "*.tar.gz",
        "inventory.ini",
    )
    shutil.copytree(ROOT, destination, ignore=ignored)


def _run(
    *argv: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    completed = subprocess.run(argv, cwd=cwd, env=env, check=False)
    if completed.returncode != 0:
        rendered = " ".join(argv)
        raise RuntimeError(f"command failed with exit {completed.returncode}: {rendered}")


if __name__ == "__main__":
    raise SystemExit(main())
