import os
from pathlib import Path
import pwd
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ceph_incident_bundle.inventory import draft_inventory


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

    def test_generate_inventory_does_not_resolve_collect_default_from_deleted_cwd(
        self,
    ) -> None:
        command = COMMAND
        assert command is not None
        helper = """\
import os
import sys

deleted_cwd, console_script, hosts, output = sys.argv[1:]
os.chdir(deleted_cwd)
os.rmdir(deleted_cwd)
os.execv(
    console_script,
    [
        console_script,
        "generate-inventory",
        "--hosts-file",
        hosts,
        "--output",
        output,
    ],
)
"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            deleted_cwd = root / "deleted-cwd"
            deleted_cwd.mkdir()
            hosts = root / "hosts"
            hosts.write_text("192.0.2.10 node.example.test\n", encoding="utf-8")
            output = root / "inventory.ini"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    helper,
                    str(deleted_cwd),
                    command,
                    str(hosts),
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            generated = output.read_bytes() if output.exists() else None
            cwd_was_removed = not deleted_cwd.exists()

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertTrue(cwd_was_removed)
        self.assertIsNotNone(generated)
        self.assertIn(b"node = node.example.test\n", generated)

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

    def test_zero_options_use_etc_hosts_and_default_inventory_path(self) -> None:
        system_hosts = Path("/etc/hosts")
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts_snapshot = system_hosts.read_bytes()
            fixture = cwd / "etc-hosts-snapshot"
            fixture.write_bytes(hosts_snapshot)
            expected_inventory, expected_problems = draft_inventory(fixture)

            completed = self.run_cli("generate-inventory", cwd=cwd)

            generated = (cwd / "inventory.ini").read_bytes()
            hosts_after = system_hosts.read_bytes()

        expected_stderr = "".join(
            f"{problem}\n" for problem in expected_problems
        ).encode("utf-8")
        self.assertEqual(hosts_after, hosts_snapshot)
        self.assertEqual(generated, expected_inventory)
        self.assertEqual(completed.returncode, 1 if expected_problems else 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, expected_stderr)

    def test_unresolvable_user_paths_fail_without_traceback_or_residue(self) -> None:
        missing_username = None
        for username in (
            "cib_no_user_6f9e2d7c",
            "cib_no_user_14a8c3b5",
            "cib_no_user_f27d91e4",
        ):
            try:
                pwd.getpwnam(username)
            except KeyError:
                candidate = Path(f"~{username}/probe")
                try:
                    candidate.expanduser()
                except RuntimeError:
                    missing_username = username
                    break
        if missing_username is None:
            self.fail("deterministic missing-user candidates unexpectedly exist")

        unresolved_hosts = f"~{missing_username}/hosts"
        unresolved_output = f"~{missing_username}/inventory.ini"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "source-hosts"
            hosts.write_text("192.0.2.10 node.example.test\n", encoding="utf-8")

            hosts_failure = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                unresolved_hosts,
                cwd=cwd,
            )
            residue_after_hosts_failure = tuple(item.name for item in cwd.iterdir())

            output_failure = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                str(hosts),
                "--output",
                unresolved_output,
                cwd=cwd,
            )
            residue_after_output_failure = tuple(item.name for item in cwd.iterdir())

        for completed, path, boundary in (
            (hosts_failure, unresolved_hosts, b"cannot read hosts file"),
            (output_failure, unresolved_output, b"cannot resolve Inventory output"),
        ):
            with self.subTest(path=path):
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")
                self.assertIn(boundary, completed.stderr)
                self.assertIn(path.encode("utf-8"), completed.stderr)
                self.assertNotIn(b"Traceback", completed.stderr)
                self.assertEqual(completed.stderr.count(b"\n"), 1)
        self.assertEqual(residue_after_hosts_failure, ("source-hosts",))
        self.assertEqual(residue_after_output_failure, ("source-hosts",))

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

    def test_unrenderable_inventory_duration_has_no_transitional_collect_activity(
        self,
    ) -> None:
        command = COMMAND
        assert command is not None
        maximum_parseable_decimal = "9" * 4300
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            inventory = cwd / "duration-boundary.ini"
            inventory.write_text(
                "[common]\n"
                "ssh_user = root\n"
                f"ssh_connect_timeout = {maximum_parseable_decimal}m\n"
                "[nodes]\n"
                "node = node.example\n",
                encoding="ascii",
            )
            fake_bin = cwd / "fake-bin"
            fake_bin.mkdir()
            process_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/bin/sh\n: > \"$CIB_PROCESS_MARKER\"\nexit 99\n",
                encoding="ascii",
            )
            fake_ssh.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["CIB_PROCESS_MARKER"] = str(process_marker)

            completed = subprocess.run(
                [command, "collect", "--inventory", str(inventory)],
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            residue = tuple(sorted(path.name for path in cwd.iterdir()))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"collect is not available in this preparation-only build\n"
            b"FAIL: no Incident Bundle delivered\n",
        )
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertEqual(residue, ("duration-boundary.ini", "fake-bin"))

    def test_inaccessible_output_parent_fails_without_traceback_or_residue(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "source-hosts"
            hosts.write_text("192.0.2.10 node.example.test\n", encoding="utf-8")
            inaccessible_parent = cwd / "inaccessible"
            inaccessible_parent.mkdir()
            output = inaccessible_parent / "inventory.ini"
            inaccessible_parent.chmod(0)
            try:
                resolved_output = output.resolve()
                with self.assertRaises(PermissionError):
                    output.exists()

                completed = self.run_cli(
                    "generate-inventory",
                    "--hosts-file",
                    str(hosts),
                    "--output",
                    str(output),
                    cwd=cwd,
                )
            finally:
                inaccessible_parent.chmod(0o700)
            residue = tuple(inaccessible_parent.iterdir())

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"cannot inspect Inventory output", completed.stderr)
        self.assertIn(str(resolved_output).encode("utf-8"), completed.stderr)
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertEqual(completed.stderr.count(b"\n"), 1)
        self.assertEqual(residue, ())

    def test_overrides_convert_hosts_and_force_only_controls_requested_output(self) -> None:
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            hosts = cwd / "source-hosts"
            output = cwd / "review.ini"
            hosts.write_text("192.0.2.10 mon01.example.test\n", encoding="utf-8")
            output.write_bytes(b"reviewed inventory\n")
            resolved_output = output.resolve()

            refused = self.run_cli(
                "generate-inventory",
                "--hosts-file",
                str(hosts),
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
        self.assertEqual(
            refused.stderr,
            f"Inventory output already exists: {resolved_output}\n".encode("utf-8"),
        )
        self.assertEqual(unchanged, b"reviewed inventory\n")
        self.assertEqual(forced.returncode, 0)
        self.assertEqual(forced.stdout, b"")
        self.assertEqual(forced.stderr, b"")
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
