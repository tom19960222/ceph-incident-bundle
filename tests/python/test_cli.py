import os
import json
from pathlib import Path
import subprocess
import sys
import tarfile
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

    def test_collect_rejected_at_startup_has_controlled_nondelivery(self) -> None:
        with TemporaryDirectory() as directory:
            completed = self.run_cli("collect", cwd=Path(directory))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"cannot read Inventory", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )

    def test_collect_uses_one_ssh_and_delivers_one_complete_bundle(self) -> None:
        inventory = b"""\
[common]
ssh_user = root
probe_timeout = 30m
ssh_connect_timeout = 15s
[nodes]
node-a = node-a.example.test
"""
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            inventory_path = cwd / "inventory.ini"
            inventory_path.write_bytes(inventory)
            record = cwd / "ssh-record"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(record)
            command = COMMAND
            assert command is not None

            completed = subprocess.run(
                [command, "collect"],
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                root = bundle.name.removesuffix(".tar.gz")
                inventory_file = archive.extractfile(f"{root}/inventory.ini")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                hostname_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/stdout"
                )
                assert inventory_file is not None
                assert metadata_file is not None
                assert hostname_file is not None
                bundled_inventory = inventory_file.read()
                metadata = json.load(metadata_file)
                hostname = hostname_file.read()
            argv = json.loads(
                (record / "argv.json").read_text(encoding="utf-8")
            )
            transferred_source = (record / "stdin.py").read_bytes()
            from ceph_incident_bundle import remote_collector

            installed_source = Path(remote_collector.__file__).read_bytes()

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (complete)\n".encode("utf-8"),
        )
        self.assertEqual(
            argv,
            [
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "root@node-a.example.test",
                "python3",
                "-",
                "--since-seconds",
                "86400",
                "--probe-timeout-seconds",
                "1800",
            ],
        )
        self.assertEqual(transferred_source, installed_source)
        self.assertEqual(bundled_inventory, inventory)
        self.assertTrue(hostname)
        self.assertEqual(metadata["outcome"], "complete")
        self.assertTrue(all(member.isdir() or member.isreg() for member in members))
        for top_level in ("nodes", "ceph", "kubernetes", "prometheus"):
            self.assertIn(f"{root}/{top_level}", names)

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

    @staticmethod
    def _write_fake_ssh(path: Path) -> None:
        path.write_text(
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys

record = Path(os.environ["FAKE_SSH_RECORD"])
record.mkdir()
(record / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
source = sys.stdin.buffer.read()
(record / "stdin.py").write_bytes(source)
remote_start = sys.argv.index("python3") + 1
completed = subprocess.run(
    [sys.executable, *sys.argv[remote_start:]],
    input=source,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
