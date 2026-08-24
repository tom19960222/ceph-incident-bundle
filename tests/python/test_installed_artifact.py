from __future__ import annotations

from email.parser import Parser
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


class InstalledArtifactTests(unittest.TestCase):
    def test_wheel_preserves_the_public_runtime_contract(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheel = _build_wheel(temporary / "wheelhouse")
            with zipfile.ZipFile(wheel) as archive:
                metadata = Parser().parsestr(
                    archive.read(
                        next(
                            name
                            for name in archive.namelist()
                            if name.endswith(".dist-info/METADATA")
                        )
                    ).decode("utf-8")
                )

            self.assertEqual(metadata["Requires-Python"], ">=3.10")
            self.assertEqual(metadata.get_all("Requires-Dist", []), [])

            environment = temporary / "installed"
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
            installed_python = environment / "bin" / "python"
            subprocess.run(
                [
                    str(installed_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--force-reinstall",
                    str(wheel),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            command = environment / "bin" / "ceph-incident-bundle"

            self.assertTrue(command.is_file())
            self.assertEqual(
                _help_subcommands(_run(command, "--help").stdout),
                {"generate-inventory", "collect"},
            )


def _build_wheel(wheelhouse: Path) -> Path:
    existing_wheel = os.environ.get("CEPH_INCIDENT_BUNDLE_WHEEL")
    if existing_wheel:
        wheel = Path(existing_wheel)
        if not wheel.is_file():
            raise AssertionError(f"expected built wheel, found {wheel}")
        return wheel

    wheelhouse.mkdir(parents=True, exist_ok=True)
    source = wheelhouse / "_source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "*.egg-info",
            "build",
            "dist",
            ".venv",
            "results",
            ".ssh",
            "*.tar.gz",
            "inventory.ini",
        ),
    )
    subprocess.run(
        [
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
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wheels = sorted(wheelhouse.glob("ceph_incident_bundle-*.whl"))
    if len(wheels) != 1:
        raise AssertionError(f"expected one wheel, found {wheels}")
    return wheels[0]


def _help_subcommands(help_text: str) -> set[str]:
    match = re.search(r"\{([^{}]+)\}", help_text)
    if match is None:
        raise AssertionError(f"no subcommand set in help output: {help_text!r}")
    return set(match.group(1).split(","))


def _run(command: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(command), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


if __name__ == "__main__":
    unittest.main()
