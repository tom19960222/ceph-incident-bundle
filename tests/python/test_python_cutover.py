from __future__ import annotations

import configparser
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
NON_ACTIVE_REFERENCE_ALLOWLIST = {
    Path("PROM-VALIDATION-2026-07.md"),
    Path("docs/python-cutover-coverage.md"),
}
EXPECTED_PRODUCT_MEMBERS = {
    "ceph_incident_bundle/__init__.py",
    "ceph_incident_bundle/cli.py",
    "ceph_incident_bundle/generate_inventory.py",
    "ceph_incident_bundle/inventory.py",
    "ceph_incident_bundle/remote_collector.py",
    "ceph_incident_bundle/collect/__init__.py",
    "ceph_incident_bundle/collect/bundle.py",
    "ceph_incident_bundle/collect/kubernetes.py",
    "ceph_incident_bundle/collect/node.py",
    "ceph_incident_bundle/collect/node_archive.py",
    "ceph_incident_bundle/collect/prometheus.py",
}
EXPECTED_METADATA_MEMBERS = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
}


class PythonOnlyRepositorySurfaceTests(unittest.TestCase):
    def test_historical_product_and_qualification_paths_are_absent(self) -> None:
        removed_paths = (
            "run",
            "lib",
            "tests/run-tests.sh",
            "tests/test-cephadm-collector.sh",
            "tests/test-collect.sh",
            "tests/test-common.sh",
            "tests/test-node-collector.sh",
            "tests/test-prom-collector.sh",
            "tests/test-rook-collector.sh",
            "tests/test-var-log-collector.sh",
            "tests/test-verify-bundle.sh",
            "tests/fixtures",
            "inventory/ceph-lab.example.env",
        )

        present = [path for path in removed_paths if (ROOT / path).exists()]

        self.assertEqual(present, [])

    def test_redundant_development_and_superseded_requirement_paths_are_absent(
        self,
    ) -> None:
        removed_paths = (
            ".devcontainer",
            ".dockerignore",
            "Dockerfile.dev",
            "compose.yaml",
            "docs/development.md",
            "docs/rewrite-requirements.md",
            "tools/bootstrap-debian-docker.sh",
            "tools/dev-entrypoint.sh",
            "tools/dev-profile.sh",
        )

        present = [path for path in removed_paths if (ROOT / path).exists()]

        self.assertEqual(present, [])

    def test_final_repository_surface_has_no_embedded_agent_skills(self) -> None:
        required_guidance = (
            "AGENTS.md",
            "CONTEXT.md",
            "docs/adr",
            "docs/agents/domain.md",
            "docs/agents/issue-tracker.md",
            "docs/agents/triage-labels.md",
            "docs/read-only-safety.md",
            "docs/lab-validation-runbook.md",
            "docs/lab-bundle-contract.md",
        )
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        example_inventory = (ROOT / "inventory" / "example.ini").read_text(
            encoding="utf-8"
        )

        embedded_skill_files = (
            [
                str(path.relative_to(ROOT))
                for path in (ROOT / ".agents" / "skills").rglob("*")
                if path.is_file()
            ]
            if (ROOT / ".agents" / "skills").exists()
            else []
        )
        self.assertEqual(embedded_skill_files, [])
        self.assertFalse((ROOT / "skills-lock.json").exists())
        self.assertEqual(
            [path for path in required_guidance if not (ROOT / path).exists()], []
        )
        self.assertIn("dependencies = []", project)
        self.assertNotIn("ssh_user", example_inventory)

    def test_active_product_files_describe_only_the_python_interface(self) -> None:
        active_files = (
            ROOT / "README.md",
            ROOT / "Makefile",
            ROOT / "pyproject.toml",
            ROOT / "inventory" / "example.ini",
        )
        forbidden_text = (
            "run/collect.sh",
            "verify-bundle.sh",
            "tests/run-tests.sh",
            "--allow-cephadm-shell",
            "--allow-kubectl-exec",
            "--redact",
            "--no-redact",
            "manifest.jsonl",
            "summary.txt",
            "environment.txt",
        )

        missing = [str(path.relative_to(ROOT)) for path in active_files if not path.is_file()]
        stale: list[str] = []
        for path in active_files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            stale.extend(
                f"{path.relative_to(ROOT)}: {needle}"
                for needle in forbidden_text
                if needle in text
            )

        self.assertEqual(missing, [])
        self.assertEqual(stale, [])

    def test_repository_has_no_legacy_executable_or_stale_active_invocation(self) -> None:
        forbidden_text = (
            "run/collect.sh",
            "verify-bundle.sh",
            "tests/run-tests.sh",
            "inventory/ceph-lab.example.env",
            "--allow-cephadm-shell",
            "--allow-kubectl-exec",
            "--no-redact",
        )
        stale: list[str] = []
        legacy_executables: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            if os.access(path, os.X_OK) and (
                relative.parts[0] in {"run", "lib"}
                or relative.name in {"collect.sh", "verify-bundle.sh"}
            ):
                legacy_executables.append(str(relative))
            if relative in NON_ACTIVE_REFERENCE_ALLOWLIST or relative == Path(
                "tests/python/test_python_cutover.py"
            ):
                continue
            if path.name != "Makefile" and path.suffix not in {
                ".md",
                ".ini",
                ".toml",
                ".txt",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            stale.extend(
                f"{relative}: {needle}" for needle in forbidden_text if needle in text
            )

        self.assertEqual(legacy_executables, [])
        self.assertEqual(stale, [])

    def test_distribution_metadata_exposes_exactly_one_python_console(self) -> None:
        with TemporaryDirectory() as directory:
            configuration = configparser.ConfigParser()
            entry_points = _wheel_entry_points(_build_wheel(Path(directory)))
            configuration.read_string(entry_points)

        self.assertEqual(configuration.sections(), ["console_scripts"])
        self.assertEqual(
            dict(configuration["console_scripts"]),
            {"ceph-incident-bundle": "ceph_incident_bundle.cli:main"},
        )


class InstalledWheelSurfaceTests(unittest.TestCase):
    def test_clean_wheel_contains_only_python_product_and_two_subcommands(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheel = _build_wheel(temporary / "wheelhouse")
            with zipfile.ZipFile(wheel) as archive:
                members = archive.namelist()
                product_members = {
                    name for name in members if not name.startswith("ceph_incident_bundle-")
                }
                metadata_members = {
                    Path(name).name
                    for name in members
                    if name.startswith("ceph_incident_bundle-")
                }
                metadata = archive.read(
                    next(name for name in members if name.endswith(".dist-info/METADATA"))
                ).decode("utf-8")

            self.assertEqual(product_members, EXPECTED_PRODUCT_MEMBERS)
            self.assertEqual(metadata_members, EXPECTED_METADATA_MEMBERS)
            self.assertNotIn("\nRequires-Dist:", metadata)

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
            top_help = _run(command, "--help")
            generate_help = _run(command, "generate-inventory", "--help")
            collect_help = _run(command, "collect", "--help")

            self.assertTrue(command.is_file())
            self.assertEqual(
                _help_subcommands(top_help.stdout),
                {"generate-inventory", "collect"},
            )
            self.assertIn("--hosts-file", generate_help.stdout)
            self.assertIn("--inventory", collect_help.stdout)
            self.assertEqual(
                sorted(path.name for path in (environment / "bin").glob("ceph*")),
                ["ceph-incident-bundle"],
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


def _wheel_entry_points(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")]
        if len(names) != 1:
            raise AssertionError(f"expected one entry_points.txt, found {names}")
        return archive.read(names[0]).decode("utf-8")


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
