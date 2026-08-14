import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


COMMAND = os.environ.get("CEPH_INCIDENT_BUNDLE_COMMAND")


@unittest.skipUnless(COMMAND, "installed CLI path not provided")
class InstalledCliTests(unittest.TestCase):
    def run_cli(
        self, *arguments: str, cwd: Path
    ) -> subprocess.CompletedProcess[bytes]:
        command = COMMAND
        assert command is not None
        return subprocess.run(
            [command, *arguments],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_installed_cli_entrypoint_uses_tested_cpython_3_10(self) -> None:
        command = COMMAND
        assert command is not None
        console_script_shebang = Path(command).read_bytes().splitlines()[0]

        self.assertEqual(sys.implementation.name, "cpython")
        self.assertEqual(sys.version_info[:2], (3, 10))
        self.assertEqual(console_script_shebang, f"#!{sys.executable}".encode("utf-8"))

    def test_default_output_contains_exact_generated_defaults(self) -> None:
        expected = b"""\
[common]
ssh_user = root
probe_timeout = 30m
ssh_connect_timeout = 15s

[nodes]
mon01 = mon01.example.test
worker = worker.example.test

[ceph]
source = mon01

[kubernetes]
# context =
consumer_namespace = rook-ceph-external
operator_namespace = rook-ceph

[prometheus]
# url =
metrics_filter_regex =
query_step = 15s
request_timeout = 5m
"""
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "synthetic-hosts"
            hosts.write_text(
                "127.0.0.1 localhost\n"
                "192.0.2.10 mon01.example.test alias\n"
                "192.0.2.20 worker.example.test alias\n",
                encoding="utf-8",
            )

            completed = self.run_cli(
                "generate-inventory", "--hosts-file", str(hosts), cwd=cwd
            )

            inventory = (cwd / "inventory.ini").read_bytes()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(inventory, expected)

    def test_collect_without_implementation_has_controlled_nondelivery(self) -> None:
        with TemporaryDirectory() as directory:
            completed = self.run_cli("collect", cwd=Path(directory))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"collect is not available in this preparation-only build\n"
            b"FAIL: no Incident Bundle delivered\n",
        )

    def test_overrides_convert_hosts_and_force_only_controls_requested_output(self) -> None:
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "source-hosts"
            output = cwd / "review.ini"
            hosts.write_text("192.0.2.10 mon01.example.test\n", encoding="utf-8")
            output.write_bytes(b"reviewed inventory\n")

            refused = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                str(cwd / "missing-hosts"),
                "--output",
                str(output),
                cwd=cwd,
            )
            unchanged = output.read_bytes()
            forced = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                str(hosts),
                "--output",
                str(output),
                "--force",
                cwd=cwd,
            )

            replaced = output.read_bytes()

        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, b"")
        self.assertEqual(unchanged, b"reviewed inventory\n")
        self.assertEqual(forced.returncode, 0)
        self.assertEqual(forced.stdout, b"")
        self.assertIn(b"mon01 = mon01.example.test\n", replaced)

    def test_collisions_are_written_and_reported_with_nonzero_status(self) -> None:
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "hosts"
            output = cwd / "inventory.ini"
            hosts.write_text(
                "192.0.2.10 Mon01.first.example\n"
                "192.0.2.11 mon01.second.example\n",
                encoding="utf-8",
            )

            completed = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                str(hosts),
                "--output",
                str(output),
                cwd=cwd,
            )

            generated = output.read_bytes()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"ACTION REQUIRED", generated)
        self.assertIn(b"Mon01 = Mon01.first.example", generated)
        self.assertIn(b"mon01 = mon01.second.example", generated)
        self.assertIn(b"Inventory Name collision", completed.stderr)


if __name__ == "__main__":
    unittest.main()
