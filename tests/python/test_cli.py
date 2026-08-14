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
        self,
        *arguments: str,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = COMMAND
        assert command is not None
        return subprocess.run(
            [command, *arguments],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
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

    def test_collect_does_not_accept_abbreviated_controls(self) -> None:
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            completed = self.run_cli("collect", "--out", str(cwd), cwd=cwd)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"unrecognized arguments: --out", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )

    def test_enormous_since_is_rejected_before_workspace_or_ssh(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            inventory_path = cwd / "inventory.ini"
            inventory_path.write_bytes(inventory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            ssh_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
from pathlib import Path
Path({str(ssh_marker)!r}).write_text("started", encoding="ascii")
raise SystemExit(99)
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            command = COMMAND
            assert command is not None

            completed = subprocess.run(
                [command, "collect", "--since", f"{'9' * 5000}h"],
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            ssh_started = ssh_marker.exists()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"invalid evidence window", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )
        self.assertFalse(ssh_started)
        self.assertEqual(workspaces, [])
        self.assertEqual(bundles, [])

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
            process_count = (record / "count").read_text(encoding="ascii")
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
        self.assertEqual(process_count, "1\n")
        self.assertEqual(bundled_inventory, inventory)
        self.assertTrue(hostname)
        self.assertEqual(metadata["outcome"], "complete")
        self.assertTrue(all(member.isdir() or member.isreg() for member in members))
        for top_level in ("nodes", "ceph", "kubernetes", "prometheus"):
            self.assertIn(f"{root}/{top_level}", names)

    def test_ssh_diagnostics_are_incrementally_escaped_without_losing_delivery(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_DIAGNOSTIC"] = "1"

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

        self.assertEqual(completed.returncode, 0)
        self.assertIn(b"[node-a] remote\\x00\\xff\n", completed.stderr)
        self.assertTrue(completed.stdout.endswith(b" (complete)\n"))

    def test_large_ssh_diagnostics_and_nonzero_exit_keep_complete_evidence(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_EXIT"] = "9"
            environment["FAKE_SSH_LARGE_DIAGNOSTIC"] = "1"

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root = bundles[0].name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.endswith(b" (partial)\n"))
        self.assertIn(b"Remote Node Collector exited with status 9", completed.stderr)
        self.assertGreater(completed.stderr.count(b"x"), 100_000)
        self.assertIn(f"{root}/nodes/node-a/probes/hostname/stdout", names)
        self.assertEqual(outcome, "partial")

    def test_corrupt_ssh_stdout_publishes_partial_without_a_node_contribution(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_CORRUPT"] = "1"

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root = bundles[0].name.removesuffix(".tar.gz")
                node_names = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root}/nodes/node-a")
                ]

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.endswith(b" (partial)\n"))
        self.assertIn(b"Node Evidence Archive rejected", completed.stderr)
        self.assertEqual(node_names, [])

    def test_local_admission_failure_is_not_reported_as_archive_rejection(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_PRECREATE_EXTRACTION"] = "node-a"

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertIn(
            b"local Node Evidence Archive admission failed", completed.stderr
        )
        self.assertNotIn(b"Node Evidence Archive rejected", completed.stderr)
        self.assertEqual(outcome, "partial")
        self.assertFalse(
            any(name.startswith(f"{root}/nodes/node-a") for name in names)
        )
        self.assertFalse(any("private" in name for name in names))
        self.assertEqual(workspaces, [])

    def test_truncated_genuine_ssh_archive_stays_private_and_delivers_partial(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_TRUNCATE"] = "1"

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                inventory_file = archive.extractfile(f"{root}/inventory.ini")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert inventory_file is not None
                assert metadata_file is not None
                bundled_inventory = inventory_file.read()
                outcome = json.load(metadata_file)["outcome"]
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(
            completed.stderr,
            b"Target Node node-a: Node Evidence Archive rejected: "
            b"Node Evidence Archive has an incomplete gzip member\n",
        )
        self.assertEqual(bundled_inventory, inventory)
        self.assertEqual(outcome, "partial")
        self.assertEqual(
            names,
            {
                root,
                f"{root}/inventory.ini",
                f"{root}/nodes",
                f"{root}/ceph",
                f"{root}/kubernetes",
                f"{root}/prometheus",
                f"{root}/collection.json",
            },
        )
        self.assertEqual(workspaces, [])

    def test_structurally_hostile_ssh_archive_cannot_escape_private_staging(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            outside_sentinel = cwd / "outside-sentinel"
            outside_sentinel.write_bytes(b"unchanged\n")
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_ABSOLUTE_MEMBER"] = str(
                outside_sentinel.resolve()
            )

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=20
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                members = archive.getmembers()
                names = {member.name for member in members}
                regular_payloads = []
                for member in members:
                    if not member.isreg():
                        continue
                    payload = archive.extractfile(member)
                    assert payload is not None
                    regular_payloads.append(payload.read())
                inventory_file = archive.extractfile(f"{root}/inventory.ini")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert inventory_file is not None
                assert metadata_file is not None
                bundled_inventory = inventory_file.read()
                metadata = json.load(metadata_file)
            sentinel_bytes = outside_sentinel.read_bytes()
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(
            completed.stderr,
            (
                "Target Node node-a: Node Evidence Archive rejected: "
                f"unsafe archive path: {str(outside_sentinel.resolve())!r}\n"
            ).encode("utf-8"),
        )
        self.assertEqual(sentinel_bytes, b"unchanged\n")
        self.assertEqual(bundled_inventory, inventory)
        self.assertEqual(
            set(metadata),
            {"collector_version", "started_at", "finished_at", "since", "outcome"},
        )
        self.assertEqual(metadata["outcome"], "partial")
        self.assertEqual(
            names,
            {
                root,
                f"{root}/inventory.ini",
                f"{root}/nodes",
                f"{root}/ceph",
                f"{root}/kubernetes",
                f"{root}/prometheus",
                f"{root}/collection.json",
            },
        )
        self.assertNotIn(
            b"fake-ssh private archive payload\n", b"".join(regular_payloads)
        )
        self.assertEqual(workspaces, [])

    def test_publication_failure_has_controlled_installed_cli_nondelivery(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            output = cwd / "output"
            output.mkdir()
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_REMOVE_OUTPUT_DIR"] = str(output)

            completed = self.run_cli(
                "collect",
                "--output-dir",
                str(output),
                cwd=cwd,
                env=environment,
                timeout=20,
            )

            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"cannot publish Incident Bundle", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )
        self.assertEqual(workspaces, [])

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
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

record = Path(os.environ["FAKE_SSH_RECORD"])
record.mkdir()
(record / "count").write_text("1\\n", encoding="ascii")
(record / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
source = sys.stdin.buffer.read()
(record / "stdin.py").write_bytes(source)
precreate_extraction = os.environ.get("FAKE_SSH_PRECREATE_EXTRACTION")
if precreate_extraction:
    temporary_root = Path(os.environ["TMPDIR"])
    workspaces = list(temporary_root.glob("ceph-incident-work.*"))
    if len(workspaces) != 1:
        raise RuntimeError("expected one workstation workspace")
    (
        workspaces[0]
        / "private"
        / "nodes"
        / precreate_extraction
        / "extracted"
    ).mkdir()
remove_output = os.environ.get("FAKE_REMOVE_OUTPUT_DIR")
if remove_output:
    Path(remove_output).rmdir()
if os.environ.get("FAKE_SSH_CORRUPT"):
    sys.stdout.buffer.write(b"not-an-archive")
    raise SystemExit(0)
absolute_member = os.environ.get("FAKE_SSH_ABSOLUTE_MEMBER")
if absolute_member:
    private_payload = b"fake-ssh private archive payload\\n"
    hostile_archive = io.BytesIO()
    with tarfile.open(fileobj=hostile_archive, mode="w:gz") as archive:
        for name in ("node", "node/probes", "node/files"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
        hostile = tarfile.TarInfo(absolute_member)
        hostile.mode = 0o600
        hostile.size = len(private_payload)
        archive.addfile(hostile, io.BytesIO(private_payload))
    sys.stdout.buffer.write(hostile_archive.getvalue())
    raise SystemExit(0)
remote_start = sys.argv.index("python3") + 1
completed = subprocess.run(
    [sys.executable, *sys.argv[remote_start:]],
    input=source,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
archive_bytes = completed.stdout
if os.environ.get("FAKE_SSH_TRUNCATE"):
    archive_bytes = archive_bytes[:-8]
sys.stdout.buffer.write(archive_bytes)
if os.environ.get("FAKE_SSH_LARGE_DIAGNOSTIC"):
    os.write(2, b"x" * 200000 + b"\\n")
elif os.environ.get("FAKE_SSH_DIAGNOSTIC"):
    os.write(2, b"remote\\x00\\xff\\n")
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(int(os.environ.get("FAKE_SSH_EXIT", completed.returncode)))
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
