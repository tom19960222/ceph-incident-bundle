from __future__ import annotations

import os
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.test_python_collect_ceph import DirectCephFixture


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"
CEPH_SSH = ROOT / "tests" / "fixtures" / "python-ceph" / "bin" / "ssh"
ROOK_BIN = ROOT / "tests" / "fixtures" / "python-rook" / "bin"
PROM_BIN = ROOT / "tests" / "fixtures" / "python-prometheus" / "bin"
FAKE_CURL = ROOT / "tests" / "fixtures" / "bin" / "curl"


class MultiSourceOrchestrationTests(unittest.TestCase):
    def test_one_collect_combines_all_evidence_paths_and_every_inventory_node(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "ssh").symlink_to(CEPH_SSH)
            (fake_bin / "kubectl").symlink_to(ROOK_BIN / "kubectl")
            (fake_bin / "curl").symlink_to(FAKE_CURL)
            (fake_bin / "grep").symlink_to(PROM_BIN / "grep")
            inventory = root / "inventory.env"
            inventory.write_text(
                'SSH_USER="ceph"\n'
                'HOSTS=(\n'
                '  "monitor01=10.0.0.1"\n'
                '  "storage01=10.0.0.2"\n'
                ')\n',
                encoding="utf-8",
            )
            ssh_key = root / "id_ed25519"
            ssh_key.write_text("fixture key path only\n", encoding="utf-8")
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_SSH_LOG": str(root / "ssh-argv.jsonl"),
                "FAKE_KUBECTL_LOG": str(root / "kubectl-argv.jsonl"),
                "FAKE_CURL_LOG": str(root / "curl.log"),
                "FAKE_CURL_ARGV_LOG": str(root / "curl-argv.nul"),
                "FAKE_GREP_LOG": str(root / "grep-argv.jsonl"),
                "FAKE_NODE_CONFIG_LOG": str(root / "node-config.jsonl"),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENTRYPOINT),
                    "collect",
                    "--inventory",
                    str(inventory),
                    "--ssh-key",
                    str(ssh_key),
                    "--out",
                    str(root / "results"),
                    "--kube-mode",
                    "local",
                    "--prom-url",
                    "http://prom.example:9090",
                    "--timeout",
                    "5",
                    "--node-timeout",
                    "20",
                    "--no-trust-ssh-host-key",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertRegex(result.stdout, r"^bundle: .+\.tar\.gz\n$")
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = {member.name.removeprefix("./") for member in archive}
                summary_file = archive.extractfile("./summary.txt")
                self.assertIsNotNone(summary_file)
                summary = summary_file.read().decode()
                errors_file = archive.extractfile("./errors.log")
                self.assertIsNotNone(errors_file)
                errors = errors_file.read().decode()

            self.assertEqual(result.returncode, 0, result.stderr + errors)

            for expected in (
                "cluster/ceph/json/status.json",
                "cluster/rook/pods-wide.txt",
                "cluster/prometheus/buildinfo.json",
                "nodes/monitor01/logs/var-log/INDEX.tsv",
                "nodes/storage01/logs/var-log/INDEX.tsv",
            ):
                self.assertIn(expected, names)
            self.assertIn("node_ok: 2\n", summary)
            self.assertIn("node_failed: 0\n", summary)
            self.assertIn("final_status: 0\n", summary)
            configs = [
                json.loads(line)
                for line in (root / "node-config.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                {config["host_alias"] for config in configs},
                {"monitor01", "storage01"},
            )
            self.assertTrue(all(config["skip_logs"] is False for config in configs))


class CephRunnerSelectionTests(DirectCephFixture, unittest.TestCase):
    def test_quiet_suppresses_default_progress_messages(self) -> None:
        for extra_arguments, expect_progress in (((), True), (("--quiet",), False)):
            with self.subTest(extra_arguments=extra_arguments):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, _ = self.make_fake_environment(root)

                    result = self.run_collect(
                        root, environment, extra_arguments=extra_arguments
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("starting:" in result.stderr, expect_progress)
                    self.assertEqual(
                        "packaging incident bundle" in result.stderr,
                        expect_progress,
                    )

    def test_ceph_source_falls_back_from_direct_to_noninteractive_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_DIRECT_PROBE_FAIL="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("ceph_runner=sudo\n", contents["environment.txt"])
            invocations = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    arguments[-5:] == ["sudo", "-n", "ceph", "status", "--format"]
                    or arguments[-4:] == ["sudo", "-n", "ceph", "status"]
                    for arguments in invocations
                )
            )

    def test_one_failed_node_keeps_the_other_nodes_and_bundle_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_EXIT_ALIAS="storage01"
            )
            inventory = (
                'SSH_USER="ceph"\n'
                'HOSTS=(\n'
                '  "monitor01=10.0.0.1"\n'
                '  "storage01=10.0.0.2"\n'
                ')\n'
            )

            result = self.run_collect(
                root, environment, inventory_text=inventory
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("nodes/monitor01/system/hostname.txt", contents)
            self.assertIn("nodes/storage01/system/hostname.txt", contents)
            self.assertIn("node_ok: 1\n", contents["summary.txt"])
            self.assertIn("node_failed: 1\n", contents["summary.txt"])
            self.assertIn("final_status: 2\n", contents["summary.txt"])

    def test_an_unreachable_explicit_seed_never_falls_back_to_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            explicit_seed = "ceph@10.0.0.9"
            environment, _ = self.make_fake_environment(
                root, FAKE_CEPH_PROBE_FAIL_TARGET=explicit_seed
            )

            result = self.run_collect(root, environment, seed=explicit_seed)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("cluster/ceph/SKIPPED.txt", contents)
            self.assertNotIn("cluster/ceph/json/status.json", contents)
            self.assertIn(f"ceph_source={explicit_seed}\n", contents["environment.txt"])
            self.assertIn("ceph_runner=<none>\n", contents["environment.txt"])

    def test_auto_mode_tolerates_an_unavailable_rook_layer_when_ceph_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(root)
            fake_bin = root / "bin"
            (fake_bin / "kubectl").symlink_to(
                ROOT / "tests" / "fixtures" / "python-rook" / "bin" / "kubectl"
            )
            environment["FAKE_KUBECTL_MODE"] = "missing-namespace"
            environment["FAKE_KUBECTL_LOG"] = str(root / "kubectl-argv.jsonl")

            result = self.run_collect(
                root,
                environment,
                seed=None,
                extra_arguments=("--mode", "auto", "--kube-mode", "local"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("cluster/ceph/json/status.json", contents)
            self.assertIn("cluster/rook/SKIPPED.txt", contents)
            self.assertIn("cluster_status: 0\n", contents["summary.txt"])

    def test_auto_mode_without_any_capable_node_is_partial_but_still_collects_nodes(
        self,
    ) -> None:
        """No cluster source at all: both layers skip, the nodes still land."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(root, FAKE_NODE_CAPS="")

            result = self.run_collect(
                root,
                environment,
                seed=None,
                extra_arguments=("--mode", "auto", "--kube-mode", "remote"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("cluster/ceph/SKIPPED.txt", contents)
            self.assertIn("cluster/rook/SKIPPED.txt", contents)
            self.assertNotIn("cluster/ceph/json/status.json", contents)
            self.assertIn("nodes/monitor01/system/hostname.txt", contents)
            self.assertIn("cluster_status: 2\n", contents["summary.txt"])
            self.assertIn("ceph_source=<none>\n", contents["environment.txt"])
            self.assertIn("rook_source=<none>\n", contents["environment.txt"])

    def test_ceph_only_mode_does_not_validate_remote_rook_since(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(root)

            result = self.run_collect(
                root, environment, extra_arguments=("--since", "yesterday")
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "cluster/ceph/json/status.json",
                self.extract(self.bundle_of(result)),
            )


if __name__ == "__main__":
    unittest.main()
