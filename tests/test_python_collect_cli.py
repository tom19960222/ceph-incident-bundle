"""The public CLI contract of `collect`: usage, inventory and option failures.

These are the entry-level scenarios of the behaviour inventory (R3, R6, O1, O2,
O23): a usage failure must be fatal (1), must explain itself, and must not create
a workspace or reach the network.  They live apart from the collector suites
because none of them ever gets as far as an external command.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"

# Every option the shell reference documents in its usage text, minus the two
# default-off opt-ins (`--allow-cephadm-shell`, `--allow-kubectl-exec`) the
# Python candidate deliberately has no implementation path for.
DOCUMENTED_OPTIONS = (
    "--inventory",
    "--ssh-key",
    "--seed",
    "--out",
    "--mode",
    "--kube-context",
    "--kube-mode",
    "--since",
    "--prom-url",
    "--prom-job-regex",
    "--prom-step",
    "--prom-timeout",
    "--timeout",
    "--node-timeout",
    "--skip-logs",
    "--keep-original-logs",
    "--var-log-max-bytes",
    "--trust-ssh-host-key",
    "--no-trust-ssh-host-key",
    "--redact",
    "--no-redact",
    "--quiet",
    "--keep-workdir",
)
WITHDRAWN_OPTIONS = ("--allow-cephadm-shell", "--allow-kubectl-exec")


class CollectCliContractTests(unittest.TestCase):
    def run_collect(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), "collect", *arguments],
            cwd=ROOT,
            env={**os.environ, "PATH": "/nonexistent-so-no-command-can-run"},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_documents_every_supported_collect_option(self) -> None:
        result = self.run_collect("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for option in DOCUMENTED_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, result.stdout)
        for option in WITHDRAWN_OPTIONS:
            with self.subTest(option=option):
                self.assertNotIn(option, result.stdout)

    def test_collect_without_required_options_is_a_fatal_usage_failure(self) -> None:
        result = self.run_collect()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Usage:", result.stderr)
        self.assertIn("--inventory", result.stderr)

    def test_an_unknown_option_is_a_fatal_usage_failure(self) -> None:
        result = self.run_collect("--inventory", "x", "--ssh-key", "y", "--bogus")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("--bogus", result.stderr)
        self.assertIn("Usage:", result.stderr)

    def test_a_missing_inventory_names_the_file_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ssh_key = root / "id_ed25519"
            ssh_key.write_text("fixture key path only\n", encoding="utf-8")

            result = self.run_collect(
                "--inventory",
                str(root / "absent.env"),
                "--ssh-key",
                str(ssh_key),
                "--out",
                str(root / "results"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("missing inventory", result.stderr)
            self.assertIn("absent.env", result.stderr)
            self.assertFalse((root / "results").exists())

    def test_a_missing_ssh_key_names_the_file_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inventory = root / "inventory.env"
            inventory.write_text(
                'HOSTS=(\n  "monitor01=10.0.0.1"\n)\n', encoding="utf-8"
            )

            result = self.run_collect(
                "--inventory",
                str(inventory),
                "--ssh-key",
                str(root / "absent-key"),
                "--out",
                str(root / "results"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("missing ssh key", result.stderr)
            self.assertFalse((root / "results").exists())

    def test_an_empty_host_list_is_a_fatal_usage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inventory = root / "inventory.env"
            inventory.write_text('SSH_USER="ceph"\nHOSTS=()\n', encoding="utf-8")
            ssh_key = root / "id_ed25519"
            ssh_key.write_text("fixture key path only\n", encoding="utf-8")

            result = self.run_collect(
                "--inventory",
                str(inventory),
                "--ssh-key",
                str(ssh_key),
                "--out",
                str(root / "results"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("HOSTS is empty", result.stderr)
            self.assertFalse((root / "results").exists())


if __name__ == "__main__":
    unittest.main()
