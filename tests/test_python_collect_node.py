from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"
NODE_COLLECTOR = ROOT / "ceph_incident_node.py"


class CollectSingleNodeCliTests(unittest.TestCase):
    def make_fake_environment(self, root: Path) -> tuple[dict[str, str], Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        ssh_log = root / "ssh-argv.json"
        payload_log = root / "ssh-stdin.py"
        remote_tmp = root / "remote-tmp"
        remote_tmp.mkdir()
        remote_bin = root / "remote-bin"
        remote_bin.mkdir()

        fixture_bin = ROOT / "tests" / "fixtures" / "python-node" / "bin"
        (fake_bin / "ssh").symlink_to(fixture_bin / "ssh")
        for command in ("hostname", "uname", "uptime", "free", "df", "ip", "systemctl"):
            (fake_bin / command).symlink_to(fixture_bin / "node-command")
        (remote_bin / "tar").symlink_to(fixture_bin / "node-command")

        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(remote_tmp),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_SSH_PAYLOAD": str(payload_log),
            "FAKE_REMOTE_BIN": str(remote_bin),
        }
        return environment, ssh_log, payload_log

    def run_collect(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        node_timeout: int = 10,
    ) -> subprocess.CompletedProcess[str]:
        command = self.prepare_collect(root, node_timeout=node_timeout)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def prepare_collect(self, root: Path, *, node_timeout: int = 10) -> list[str]:
        inventory = root / "inventory.env"
        inventory.write_text(
            'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n',
            encoding="utf-8",
        )
        ssh_key = root / "id_ed25519"
        ssh_key.write_text("fixture key path only\n", encoding="utf-8")
        output = root / "results"
        return [
            sys.executable,
            str(ENTRYPOINT),
            "collect",
            "--inventory",
            str(inventory),
            "--ssh-key",
            str(ssh_key),
            "--out",
            str(output),
            "--timeout",
            "3",
            "--node-timeout",
            str(node_timeout),
            "--no-trust-ssh-host-key",
        ]

    def test_public_collect_streams_one_node_and_saves_basic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ssh_log, payload_log = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^bundle: .+\.tar\.gz\n$")
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            self.assertTrue(bundle.is_file())
            self.assertEqual(payload_log.read_bytes(), NODE_COLLECTOR.read_bytes())
            ssh_arguments = json.loads(ssh_log.read_text(encoding="utf-8"))
            self.assertEqual(sum("10.0.0.1" in item for item in ssh_arguments), 1)
            self.assertIn("python3 -c", ssh_arguments[-1])

            with tarfile.open(bundle, "r:gz") as archive:
                names = {member.name.removeprefix("./") for member in archive}
                self.assertIn("cluster/SKIPPED.txt", names)
                self.assertIn("nodes/monitor01/manifest.jsonl", names)
                self.assertIn("nodes/monitor01/system/hostname.txt", names)
                hostname = archive.extractfile("./nodes/monitor01/system/hostname.txt")
                self.assertIsNotNone(hostname)
                self.assertIn(b"monitor01", hostname.read())

            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_unsupported_node_is_skipped_in_a_partial_bundle(self) -> None:
        for mode, diagnostic in (
            ("unsupported", "Python 3.11 or newer is required"),
            ("missing-python", "python3: command not found"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                environment["FAKE_SSH_MODE"] = mode

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                    self.assertIsNotNone(skipped)
                    self.assertIn(
                        b"Python 3.11 or newer is unavailable", skipped.read()
                    )
                    summary = archive.extractfile("./summary.txt")
                    self.assertIsNotNone(summary)
                    self.assertIn(b"final_status=2", summary.read())
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_valid_archive_is_preserved_when_node_collector_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_NODE_FAIL_COMMAND"] = "ip"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertNotIn("./nodes/monitor01/SKIPPED.txt", names)
                network = archive.extractfile("./nodes/monitor01/network/ip-addr.txt")
                self.assertIsNotNone(network)
                network_payload = network.read()
                self.assertIn(b"# host: monitor01\n", network_payload)
                self.assertIn(b"# collector: collect-node\n", network_payload)
                self.assertIn(b"# started: ", network_payload)
                self.assertIn(b"# timeout: ", network_payload)
                self.assertNotIn(b"# ended:", network_payload)
                self.assertNotIn(b"# exit_code:", network_payload)
                self.assertNotIn(b"# command:", network_payload)
                self.assertIn(b"simulated failure for ip", network_payload)
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                entries = [json.loads(line) for line in manifest.read().splitlines()]
                ip_entry = next(
                    entry for entry in entries if entry["command"] == "ip addr show"
                )
                self.assertEqual(ip_entry["exit_code"], 17)
                errors = archive.extractfile("./nodes/monitor01/errors.log")
                self.assertIsNotNone(errors)
                self.assertIn(b"command=ip addr show", errors.read())
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_untrusted_node_archives_are_rejected_before_extraction(self) -> None:
        expected_reasons = {
            "corrupt": "invalid or unreadable archive",
            "truncated": "invalid or unreadable archive",
            "missing-manifest": "missing manifest",
            "unsafe": "unsafe archive member",
            "unmanifested": "archive contains evidence without a manifest mapping",
            "duplicate-manifest": "duplicates an artifact mapping",
        }
        for mode, expected_reason in expected_reasons.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                environment["FAKE_SSH_MODE"] = mode

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    names = set(archive.getnames())
                    self.assertIn("./nodes/monitor01/SKIPPED.txt", names)
                    self.assertNotIn(
                        "./nodes/monitor01/system/hostname.txt", names
                    )
                    skipped = archive.extractfile(
                        "./nodes/monitor01/SKIPPED.txt"
                    )
                    self.assertIsNotNone(skipped)
                    self.assertIn(expected_reason.encode(), skipped.read())
                self.assertFalse((root / "escape.txt").exists())
                self.assertFalse((root / "results" / "escape.txt").exists())
                self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_remote_fatal_failure_cleans_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "remote-failure"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"exit 74", skipped.read())
            self.assertIn("simulated archive failure", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_disconnect_signal_cleans_remote_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_NODE_SIGNAL_PARENT"] = "HUP"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"no usable node archive", skipped.read())
            self.assertIn("node collector interrupted", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_timeout_cleans_remote_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "timeout"
            environment["FAKE_NODE_SLEEP_COMMAND"] = "hostname"

            result = self.run_collect(root, environment, node_timeout=3)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"timed out after 3s", skipped.read())
            self.assertIn("node collector interrupted", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_interruption_cleans_remote_and_workstation_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "timeout"
            environment["FAKE_NODE_SLEEP_COMMAND"] = "hostname"
            process = subprocess.Popen(
                self.prepare_collect(root, node_timeout=30),
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while not list((root / "remote-tmp").iterdir()):
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("node collector did not create its owned workspace")
                time.sleep(0.05)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("interrupted", stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])
            results = root / "results"
            self.assertEqual(list(results.glob("tmp.*")), [])
            self.assertEqual(list(results.glob("*.tar.gz")), [])

    def test_packaging_interruption_removes_reserved_archive_and_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            fixture_bin = ROOT / "tests" / "fixtures" / "python-node" / "bin"
            (root / "bin" / "tar").symlink_to(fixture_bin / "tar-wrapper")
            marker = root / "packaging-started"
            environment["FAKE_LOCAL_TAR_MARKER"] = str(marker)
            process = subprocess.Popen(
                self.prepare_collect(root, node_timeout=30),
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 15
            while not marker.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("collector did not reach final bundle packaging")
                time.sleep(0.05)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("interrupted", stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])
            results = root / "results"
            self.assertEqual(list(results.glob("tmp.*")), [])
            self.assertEqual(list(results.glob("*.tar.gz")), [])

    def test_unsafe_inventory_is_rejected_before_ssh_or_output_writes(self) -> None:
        cases = (
            'SSH_USER="$(touch should-not-exist)"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n',
            'SSH_USER="ceph"\nHOSTS=(\n  "../escape=10.0.0.1"\n)\n',
            'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=--ProxyCommand=bad"\n)\n',
        )
        for inventory_payload in cases:
            with self.subTest(inventory=inventory_payload), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, ssh_log, _ = self.make_fake_environment(root)
                command = self.prepare_collect(root)
                (root / "inventory.env").write_text(
                    inventory_payload, encoding="utf-8"
                )

                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertFalse(ssh_log.exists())
                self.assertFalse((root / "should-not-exist").exists())
                self.assertFalse((root / "results").exists())


if __name__ == "__main__":
    unittest.main()
