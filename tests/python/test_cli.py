import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from ceph_incident_bundle.inventory import draft_inventory
from prometheus_test_support import loopback_http_server


COMMAND = os.environ.get("CEPH_INCIDENT_BUNDLE_COMMAND")
KUBERNETES_PODS = {
    "apiVersion": "v1",
    "items": [
        {
            "metadata": {"name": "rook-pod", "namespace": "rook-shared"},
            "spec": {
                "containers": [{"name": "main"}],
                "initContainers": [{"name": "init"}],
                "ephemeralContainers": [{"name": "debugger"}],
            },
            "status": {
                "containerStatuses": [{"name": "main", "restartCount": 1}],
                "initContainerStatuses": [{"name": "init", "restartCount": 0}],
                "ephemeralContainerStatuses": [
                    {"name": "debugger", "restartCount": 1}
                ],
            },
        }
    ],
}


def _installed_prometheus_response(
    request_path: str,
) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
    bodies = {
        "/api/v1/status/buildinfo": b'{"status":"success","data":{"version":"3.0"}}',
        "/api/v1/targets": b'{"status":"success","data":{"activeTargets":[]}}',
        "/api/v1/label/job/values": b'{"status":"success","data":["other","node-exporter"]}',
        "/api/v1/label/__name__/values": b'{"status":"success","data":["node_cpu_seconds_total"]}',
        "/api/v1/query_range": b'{"status":"success","data":{"resultType":"matrix","result":[]}}',
    }
    body = bodies[urlsplit(request_path).path]
    return 200, [(0, body)], len(body), False


def missing_username_without_a_home() -> str:
    """Return a deterministic username whose tilde cannot be expanded."""
    for username in (
        "cib_no_user_6f9e2d7c",
        "cib_no_user_14a8c3b5",
        "cib_no_user_f27d91e4",
    ):
        try:
            pwd.getpwnam(username)
        except KeyError:
            try:
                Path(f"~{username}/probe").expanduser()
            except RuntimeError:
                return username
    raise AssertionError("deterministic missing-user candidates unexpectedly exist")


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

    def test_ctrl_c_requests_remote_cleanup_and_delivers_no_bundle(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (root / "inventory.ini").write_bytes(inventory)
            output = root / "output"
            output.mkdir()
            temporary_root = root / "temporary"
            temporary_root.mkdir()
            remote_workspace = root / "remote-owned-workspace"
            ready = root / "ssh-ready"
            events = root / "boundary-events"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(root / "ssh-record")
            environment["FAKE_SSH_INTERRUPT_READY"] = str(ready)
            environment["FAKE_SSH_REMOTE_WORKSPACE"] = str(remote_workspace)
            environment["FAKE_EVENT_LOG"] = str(events)
            command = COMMAND
            assert command is not None

            process = subprocess.Popen(
                [command, "collect", "--output-dir", str(output)],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _attempt in range(200):
                    if ready.exists():
                        break
                    threading.Event().wait(0.01)
                else:
                    self.fail("fake SSH did not reach its interrupt boundary")
                child_pid = int(ready.read_text(encoding="ascii"))
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            candidates = list(output.glob(".*.candidate.*"))
            local_workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            boundary_events = events.read_text(encoding="utf-8").splitlines()
            remote_workspace_exists = remote_workspace.exists()
            child_is_running = True
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_is_running = False

        self.assertEqual(process.returncode, 130)
        self.assertEqual(stdout, b"")
        self.assertTrue(stderr.endswith(b"FAIL: no Incident Bundle delivered\n"))
        self.assertIn(b"normal remote cleanup requested", stderr)
        self.assertFalse(child_is_running)
        self.assertFalse(remote_workspace_exists)
        self.assertEqual(local_workspaces, [])
        self.assertEqual(candidates, [])
        self.assertEqual(bundles, [])
        self.assertEqual(boundary_events, ["ssh-start", "remote-cleanup-request"])

    def test_ctrl_c_during_publication_reports_owned_cleanup_residue(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (root / "inventory.ini").write_bytes(inventory)
            output = root / "output"
            output.mkdir()
            temporary_root = root / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(root / "ssh-record")
            environment["FAKE_SSH_LARGE_EVIDENCE_BYTES"] = str(32 * 1024 * 1024)
            environment["FAKE_SSH_LOCK_WORKSPACE_PARENT_AFTER_REMOTE"] = "1"
            command = COMMAND
            assert command is not None

            process = subprocess.Popen(
                [command, "collect", "--output-dir", str(output)],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _attempt in range(3000):
                    if list(output.glob(".*.candidate.*")):
                        break
                    if process.poll() is not None:
                        self.fail("collection exited before publication could be interrupted")
                    threading.Event().wait(0.01)
                else:
                    self.fail("publication candidate was not observed")
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                temporary_root.chmod(0o755)
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            candidates = list(output.glob(".*.candidate.*"))
            local_workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(process.returncode, 130)
        self.assertEqual(stdout, b"")
        self.assertEqual(bundles, [])
        self.assertEqual(candidates, [])
        self.assertEqual(len(local_workspaces), 1)
        self.assertIn(str(local_workspaces[0]).encode("utf-8"), stderr)
        self.assertIn(b"cannot remove workstation workspace", stderr)
        self.assertTrue(stderr.endswith(b"FAIL: no Incident Bundle delivered\n"))

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
        missing_username = missing_username_without_a_home()
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

    def test_collect_rejected_at_startup_has_controlled_nondelivery(self) -> None:
        with TemporaryDirectory() as directory:
            completed = self.run_cli("collect", cwd=Path(directory))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"cannot read Inventory", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )

    def test_startup_nondelivery_escapes_inventory_controls_before_activity(
        self,
    ) -> None:
        hostile_timeout = "evil\x1b]2;spoofed\x07"
        inventory = (
            "[common]\n"
            f"probe_timeout = {hostile_timeout}\n"
            "[nodes]\n"
            "node-a = node-a.example\n"
        )
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            (cwd / "inventory.ini").write_text(inventory, encoding="utf-8")
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            process_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
from pathlib import Path
Path({str(process_marker)!r}).write_text("started", encoding="ascii")
raise SystemExit(99)
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            output = cwd / "output"
            output.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)

            completed = self.run_cli(
                "collect",
                "--output-dir",
                str(output),
                cwd=cwd,
                env=environment,
            )

            process_started = process_marker.exists()
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            output_entries = list(output.iterdir())

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"invalid probe_timeout 'evil\\x1b]2;spoofed\\x07'\n"
            b"FAIL: no Incident Bundle delivered\n",
        )
        self.assertNotIn(b"\x1b", completed.stderr)
        self.assertNotIn(b"\x07", completed.stderr)
        self.assertFalse(process_started)
        self.assertEqual(workspaces, [])
        self.assertEqual(output_entries, [])

    def test_legacy_ssh_user_is_rejected_before_workspace_or_ssh(self) -> None:
        inventory = (
            b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        )
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            (cwd / "inventory.ini").write_bytes(inventory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            process_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
from pathlib import Path
Path({str(process_marker)!r}).write_text("started", encoding="ascii")
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

            completed = self.run_cli("collect", cwd=cwd, env=environment)

            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"unknown key 'ssh_user' in [common]", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )
        self.assertFalse(process_marker.exists())
        self.assertEqual(workspaces, [])

    def test_collect_unresolvable_inventory_user_rejects_before_activity(
        self,
    ) -> None:
        missing_username = missing_username_without_a_home()
        unresolved_inventory = f"~{missing_username}/inventory.ini"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            process_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
from pathlib import Path
Path({str(process_marker)!r}).write_text("started", encoding="ascii")
raise SystemExit(99)
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            output = cwd / "output"
            output.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)

            completed = self.run_cli(
                "collect",
                "--inventory",
                unresolved_inventory,
                "--output-dir",
                str(output),
                cwd=cwd,
                env=environment,
            )

            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            output_entries = list(output.iterdir())
            process_started = process_marker.exists()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        diagnostic, final_line = completed.stderr.splitlines()
        self.assertTrue(
            diagnostic.startswith(
                f"cannot read Inventory {unresolved_inventory}: ".encode("utf-8")
            )
        )
        self.assertEqual(final_line, b"FAIL: no Incident Bundle delivered")
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertFalse(process_started)
        self.assertEqual(workspaces, [])
        self.assertEqual(output_entries, [])

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

    def test_collect_parser_escapes_hostile_unknown_argument_before_activity(
        self,
    ) -> None:
        hostile_argument = (
            "--unknown\x1b]8;;https://example.invalid\x07click\x1b]8;;\x07"
        )
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            inventory = cwd / "inventory.ini"
            inventory.write_text(
                "[common]\n"
                "probe_timeout = 30m\n"
                "ssh_connect_timeout = 15s\n"
                "[nodes]\n"
                "node-a = node-a.example\n",
                encoding="ascii",
            )
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            process_marker = cwd / "ssh-started"
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
from pathlib import Path
Path({str(process_marker)!r}).write_text("started", encoding="ascii")
raise SystemExit(99)
""",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            temporary_root = cwd / "temporary"
            temporary_root.mkdir()
            output = cwd / "output"
            output.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)

            completed = self.run_cli(
                "collect",
                "--inventory",
                str(inventory),
                "--output-dir",
                str(output),
                hostile_argument,
                cwd=cwd,
                env=environment,
            )

            process_started = process_marker.exists()
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            output_entries = list(output.iterdir())

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"usage: ceph-incident-bundle", completed.stderr)
        self.assertIn(b"error: unrecognized arguments:", completed.stderr)
        self.assertIn(
            b"--unknown\\x1b]8;;https://example.invalid\\x07click"
            b"\\x1b]8;;\\x07",
            completed.stderr,
        )
        self.assertNotIn(b"\x1b", completed.stderr)
        self.assertNotIn(b"\x07", completed.stderr)
        self.assertTrue(
            completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
        )
        self.assertFalse(process_started)
        self.assertEqual(workspaces, [])
        self.assertEqual(output_entries, [])

    def test_unusable_ssh_timeouts_have_no_collect_activity(self) -> None:
        command = COMMAND
        assert command is not None
        maximum_parseable_decimal = "9" * 4300
        invalid_timeouts = (
            ("unrenderable normalized seconds", f"{maximum_parseable_decimal}m"),
            ("OpenSSH signed-int overflow", "2147483648s"),
        )
        for case_name, timeout in invalid_timeouts:
            with self.subTest(case=case_name), TemporaryDirectory() as directory:
                cwd = Path(directory)
                inventory = cwd / "duration-boundary.ini"
                inventory.write_text(
                    "[common]\n"
                    f"ssh_connect_timeout = {timeout}\n"
                    "[nodes]\n"
                    "node = node.example\n",
                    encoding="ascii",
                )
                fake_bin = cwd / "fake-bin"
                fake_bin.mkdir()
                collector_tmp = cwd / "collector-tmp"
                collector_tmp.mkdir()
                process_marker = cwd / "ssh-started"
                fake_ssh = fake_bin / "ssh"
                fake_ssh.write_text(
                    "#!/bin/sh\n: > \"$CIB_PROCESS_MARKER\"\nexit 99\n",
                    encoding="ascii",
                )
                fake_ssh.chmod(0o755)
                environment = os.environ.copy()
                environment["PATH"] = (
                    f"{fake_bin}{os.pathsep}{environment['PATH']}"
                )
                environment["CIB_PROCESS_MARKER"] = str(process_marker)
                environment["TMPDIR"] = str(collector_tmp)

                completed = subprocess.run(
                    [command, "collect", "--inventory", str(inventory)],
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                residue = tuple(sorted(path.name for path in cwd.iterdir()))
                workspace_residue = tuple(collector_tmp.iterdir())

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, b"")
            self.assertTrue(
                completed.stderr.startswith(b"invalid ssh_connect_timeout '")
            )
            self.assertTrue(
                completed.stderr.endswith(b"FAIL: no Incident Bundle delivered\n")
            )
            self.assertNotIn(b"Traceback", completed.stderr)
            self.assertEqual(
                residue,
                ("collector-tmp", "duration-boundary.ini", "fake-bin"),
            )
            self.assertEqual(workspace_residue, ())

    def test_enormous_since_is_rejected_before_workspace_or_ssh(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    def test_unrenderable_normalized_since_is_rejected_before_activity(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        maximum_parseable_decimal = "9" * 4300
        evidence_window = f"{maximum_parseable_decimal}w"
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
            output = cwd / "output"
            output.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)

            completed = self.run_cli(
                "collect",
                "--inventory",
                str(inventory_path),
                "--since",
                evidence_window,
                "--output-dir",
                str(output),
                cwd=cwd,
                env=environment,
            )

            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            output_entries = list(output.iterdir())
            ssh_started = ssh_marker.exists()

        expected_diagnostic = (
            f"invalid evidence window '{evidence_window}'; expected a positive "
            "integer plus m, h, d, or w\n"
            "FAIL: no Incident Bundle delivered\n"
        ).encode("ascii")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, expected_diagnostic)
        self.assertFalse(ssh_started)
        self.assertEqual(workspaces, [])
        self.assertEqual(output_entries, [])

    def test_collect_uses_one_ssh_and_delivers_one_complete_bundle(self) -> None:
        inventory = b"""\
[common]
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
            ceph_record = cwd / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(record)
            environment["FAKE_SSH_CEPH_RECORD"] = str(ceph_record)
            command = COMMAND
            assert command is not None

            completed = subprocess.run(
                [command, "collect"],
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                umask=0o022,
                check=False,
            )

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            bundle_mode = stat.S_IMODE(bundle.stat().st_mode)
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
            ceph_was_invoked = ceph_record.exists()

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(bundle_mode, 0o644)
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
        self.assertFalse(ceph_was_invoked)
        self.assertEqual(bundled_inventory, inventory)
        self.assertTrue(hostname)
        self.assertEqual(metadata["outcome"], "complete")
        for probe_name in (
            "hostname",
            "current-utc",
            "uname",
            "uptime",
            "lscpu",
            "free",
            "processes",
            "df",
            "lsblk",
            "iostat",
            "pvs",
            "vgs",
            "lvs",
            "ip-address",
            "dmesg",
            "failed-units",
            "podman-ps",
            "docker-ps",
            "chronyc-tracking",
            "chronyc-sources",
            "ntpq-peers",
            "timedatectl-status",
            "timedatectl-show-timesync",
            "timedatectl-timesync-status",
            "systemd-timesyncd-status",
            "journal-system",
        ):
            self.assertIn(
                f"{root}/nodes/node-a/probes/{probe_name}/result.json", names
            )
        self.assertIn(f"{root}/nodes/node-a/files/var/log/fake.log", names)
        self.assertTrue(all(member.isdir() or member.isreg() for member in members))

    def test_installed_collect_preserves_prometheus_controls_from_loopback(self) -> None:
        with loopback_http_server(_installed_prometheus_response) as (
            server,
            prometheus_url,
        ), TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            inventory = (
                "[common]\n"
                "[nodes]\n"
                "node-a = node-a.example.test\n"
                "[prometheus]\n"
                f"url = {prometheus_url}\n"
                "metrics_filter_regex = ^node_cpu_seconds_total$\n"
                "query_step = 45s\n"
                "request_timeout = 1s\n"
            ).encode("ascii")
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=30
            )

            requests = list(server.requests)
            bundle = next(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            root_name = bundle.name.removesuffix(".tar.gz")
            with tarfile.open(bundle, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
                job_values_file = archive.extractfile(
                    f"{root_name}/prometheus/job-values/response"
                )
                metric_result_file = archive.extractfile(
                    f"{root_name}/prometheus/metric-names/000001/result.json"
                )
                range_result_file = archive.extractfile(
                    f"{root_name}/prometheus/query-range/000001/result.json"
                )
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert job_values_file is not None
                assert metric_result_file is not None
                assert range_result_file is not None
                assert metadata_file is not None
                job_values = job_values_file.read()
                metric_result = json.load(metric_result_file)
                range_result = json.load(range_result_file)
                outcome = json.load(metadata_file)["outcome"]

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (complete)\n".encode("utf-8"),
        )
        self.assertEqual([method for method, _ in requests], ["GET"] * 5)
        self.assertEqual(
            [urlsplit(request).path for _, request in requests],
            [
                "/api/v1/status/buildinfo",
                "/api/v1/targets",
                "/api/v1/label/job/values",
                "/api/v1/label/__name__/values",
                "/api/v1/query_range",
            ],
        )
        range_query = parse_qs(urlsplit(requests[-1][1]).query)
        self.assertEqual(set(range_query), {"query", "start", "end", "step"})
        self.assertEqual(
            range_query["query"],
            ['{job="node-exporter",__name__="node_cpu_seconds_total"}'],
        )
        self.assertEqual(range_query["step"], ["45s"])
        self.assertEqual(
            int(range_query["end"][0]) - int(range_query["start"][0]),
            86400,
        )
        self.assertEqual(
            job_values,
            b'{"status":"success","data":["other","node-exporter"]}',
        )
        self.assertEqual(metric_result["job_name"], "node-exporter")
        self.assertEqual(metric_result["outcome"], "received")
        self.assertEqual(range_result["job_name"], "node-exporter")
        self.assertEqual(range_result["metric_name"], "node_cpu_seconds_total")
        self.assertEqual(range_result["outcome"], "received")
        self.assertEqual(outcome, "complete")
        self.assertIn(f"{root_name}/prometheus/buildinfo/response", names)

    def test_installed_collect_captures_configured_kubernetes_get_snapshot(self) -> None:
        inventory = b"""\
[common]
probe_timeout = 30m
ssh_connect_timeout = 15s
[nodes]
node-a = node-a.example.test
[kubernetes]
context = lab-context
consumer_namespace = rook-shared
operator_namespace = rook-shared
"""
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            self._write_fake_kubectl(fake_bin / "kubectl")
            (cwd / "inventory.ini").write_bytes(inventory)
            record = cwd / "ssh-record"
            kubectl_record = cwd / "kubectl.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(record)
            environment["FAKE_SSH_CORRUPT"] = "1"
            environment["FAKE_KUBECTL_RECORD"] = str(kubectl_record)

            completed = self.run_cli(
                "collect", cwd=cwd, env=environment, timeout=30
            )

            bundle = next(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            root_name = bundle.name.removesuffix(".tar.gz")
            with tarfile.open(bundle, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
                events_file = archive.extractfile(
                    f"{root_name}/kubernetes/probes/consumer-events/stdout"
                )
                assert events_file is not None
                events_bytes = events_file.read()
                current_log_file = archive.extractfile(
                    f"{root_name}/kubernetes/probes/pod-log-000001/stdout"
                )
                assert current_log_file is not None
                current_log_bytes = current_log_file.read()
            kubectl_argv = [
                json.loads(line)
                for line in kubectl_record.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(completed.returncode, 0)
        self.assertIn(b"Node Evidence Archive rejected", completed.stderr)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(len(kubectl_argv), 9)
        self.assertEqual(
            kubectl_argv[0],
            [
                "--context=lab-context",
                "--namespace=rook-shared",
                "get",
                "pods",
                "--output=wide",
            ],
        )
        self.assertEqual(
            kubectl_argv[4:],
            [
                [
                    "--context=lab-context",
                    "--namespace=rook-shared",
                    "logs",
                    "rook-pod",
                    "--container=main",
                    "--since=24h",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-shared",
                    "logs",
                    "rook-pod",
                    "--container=main",
                    "--since=24h",
                    "--previous",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-shared",
                    "logs",
                    "rook-pod",
                    "--container=init",
                    "--since=24h",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-shared",
                    "logs",
                    "rook-pod",
                    "--container=debugger",
                    "--since=24h",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-shared",
                    "logs",
                    "rook-pod",
                    "--container=debugger",
                    "--since=24h",
                    "--previous",
                ],
            ],
        )
        self.assertEqual(events_bytes, b"events raw bytes\x00")
        self.assertEqual(current_log_bytes, b"pod log raw bytes\x00\xff")
        self.assertIn(
            f"{root_name}/kubernetes/probes/consumer-pods-json/result.json", names
        )

    def test_rejected_node_keeps_other_admitted_evidence(self) -> None:
        inventory = b"""\
[common]
[nodes]
node-a = node-a.example.test
node-b = node-b.example.test
"""
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            record = cwd / "ssh-record"
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(record)
            environment["FAKE_SSH_CORRUPT_HOST"] = "node-a.example.test"

            completed = self.run_cli("collect", cwd=cwd, env=environment, timeout=20)

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
            count = (record / "count").read_text(encoding="ascii")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.endswith(b" (partial)\n"))
        self.assertIn(b"Target Node node-a: Node Evidence Archive rejected", completed.stderr)
        self.assertEqual(count, "2\n")
        self.assertFalse(
            any(name.startswith(f"{root}/nodes/node-a") for name in names)
        )
        self.assertIn(f"{root}/nodes/node-b/probes/hostname/stdout", names)
        self.assertEqual(outcome, "partial")

    def test_delivered_bundle_remains_success_when_stdout_result_cannot_be_written(
        self,
    ) -> None:
        inventory = b"""\
[common]
[nodes]
node-a = node-a.example.test
"""
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["PYTHONUNBUFFERED"] = "1"
            command = COMMAND
            assert command is not None

            read_descriptor, write_descriptor = os.pipe()
            os.close(read_descriptor)
            try:
                completed = subprocess.run(
                    [command, "collect"],
                    cwd=cwd,
                    env=environment,
                    stdout=write_descriptor,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            finally:
                os.close(write_descriptor)

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root = bundles[0].name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert metadata_file is not None
                metadata = json.load(metadata_file)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(metadata["outcome"], "complete")
        self.assertIn(b"Incident Bundle delivered at", completed.stderr)
        self.assertIn(b"cannot write the final standard-output result", completed.stderr)
        self.assertNotIn(b"FAIL: no Incident Bundle delivered", completed.stderr)

    def test_partial_bundle_is_delivered_when_stderr_cannot_be_written(
        self,
    ) -> None:
        inventory = b"""\
[common]
[nodes]
node-a = node-a.example.test
"""
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
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_EXIT"] = "9"
            environment["TMPDIR"] = str(temporary_root)
            environment["PYTHONUNBUFFERED"] = "1"
            command = COMMAND
            assert command is not None

            read_descriptor, write_descriptor = os.pipe()
            os.close(read_descriptor)
            try:
                completed = subprocess.run(
                    [command, "collect", "--output-dir", str(output)],
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=write_descriptor,
                    check=False,
                )
            finally:
                os.close(write_descriptor)

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                hostname_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/stdout"
                )
                assert metadata_file is not None
                assert hostname_file is not None
                outcome = json.load(metadata_file)["outcome"]
                hostname = hostname_file.read()
            output_entries = list(output.iterdir())
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "partial")
        self.assertTrue(hostname)
        self.assertEqual(output_entries, [bundle])
        self.assertEqual(workspaces, [])

    def test_delivered_bundle_stays_delivered_when_cleanup_problem_stderr_write_fails(
        self,
    ) -> None:
        inventory = b"""\
[common]
[nodes]
node-a = node-a.example.test
"""
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
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_LOCK_WORKSPACE_PARENT"] = "1"
            environment["TMPDIR"] = str(temporary_root)
            environment["PYTHONUNBUFFERED"] = "1"
            command = COMMAND
            assert command is not None

            read_descriptor, write_descriptor = os.pipe()
            os.close(read_descriptor)
            try:
                completed = subprocess.run(
                    [command, "collect", "--output-dir", str(output)],
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=write_descriptor,
                    check=False,
                )
            finally:
                os.close(write_descriptor)
                # Publication left the workstation workspace's parent
                # non-writable on purpose; restore it or the surrounding
                # ``TemporaryDirectory`` cleanup (and later tests) would break.
                temporary_root.chmod(0o755)

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
            output_entries = list(output.iterdir())
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "partial")
        self.assertEqual(output_entries, [bundle])
        # The locked parent really did leave a residual, unremovable
        # workspace behind; this is the genuine cleanup problem the bundle
        # must survive, not merely a no-op hook.
        self.assertEqual(len(workspaces), 1)

    def test_workstation_cleanup_residue_is_a_truthful_partial_delivery(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (root / "inventory.ini").write_bytes(inventory)
            output = root / "output"
            output.mkdir()
            temporary_root = root / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(root / "ssh-record")
            environment["FAKE_SSH_LOCK_WORKSPACE_PARENT_AFTER_REMOTE"] = "1"

            try:
                completed = self.run_cli(
                    "collect",
                    "--output-dir",
                    str(output),
                    cwd=root,
                    env=environment,
                    timeout=20,
                )
            finally:
                temporary_root.chmod(0o755)

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))
            output_entries = list(output.iterdir())

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "partial")
        self.assertEqual(output_entries, [bundle])
        self.assertEqual(len(workspaces), 1)
        self.assertIn(str(workspaces[0]).encode("utf-8"), completed.stderr)
        self.assertIn(b"cannot remove workstation workspace", completed.stderr)
        self.assertNotIn(b"FAIL: no Incident Bundle delivered", completed.stderr)

    def test_complete_archive_is_admitted_when_ssh_diagnostics_cannot_be_written(
        self,
    ) -> None:
        inventory = b"""\
[common]
[nodes]
node-a = node-a.example.test
"""
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
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")
            environment["FAKE_SSH_DIAGNOSTIC"] = "1"
            environment["TMPDIR"] = str(temporary_root)
            environment["PYTHONUNBUFFERED"] = "1"
            command = COMMAND
            assert command is not None

            read_descriptor, write_descriptor = os.pipe()
            os.close(read_descriptor)
            try:
                completed = subprocess.run(
                    [command, "collect", "--output-dir", str(output)],
                    cwd=cwd,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=write_descriptor,
                    check=False,
                )
            finally:
                os.close(write_descriptor)

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root}/collection.json")
                hostname_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/stdout"
                )
                assert metadata_file is not None
                assert hostname_file is not None
                outcome = json.load(metadata_file)["outcome"]
                hostname = hostname_file.read()
            output_entries = list(output.iterdir())
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (complete)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "complete")
        self.assertTrue(hostname)
        self.assertEqual(output_entries, [bundle])
        self.assertEqual(workspaces, [])

    def test_ssh_diagnostics_are_incrementally_escaped_without_losing_delivery(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    def test_remote_probe_failure_delivers_an_admitted_partial_bundle_without_diagnostics(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            fake_bin = cwd / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"raw hostname output\\x00")
os.write(2, b"raw hostname error\\xff")
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            (cwd / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(cwd / "ssh-record")

            completed = self.run_cli("collect", cwd=cwd, env=environment, timeout=20)

            bundles = list(cwd.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root = bundle.name.removesuffix(".tar.gz")
                stdout_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/stdout"
                )
                stderr_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/stderr"
                )
                result_file = archive.extractfile(
                    f"{root}/nodes/node-a/probes/hostname/result.json"
                )
                metadata_file = archive.extractfile(f"{root}/collection.json")
                assert stdout_file is not None
                assert stderr_file is not None
                assert result_file is not None
                assert metadata_file is not None
                probe_stdout = stdout_file.read()
                probe_stderr = stderr_file.read()
                result = json.load(result_file)
                outcome = json.load(metadata_file)["outcome"]
                member_names = {member.name for member in archive.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.endswith(b" (partial)\n"))
        self.assertIn(b"[node-a] hostname Probe failed", completed.stderr)
        self.assertIn(b"Remote Node Collector exited with status 1", completed.stderr)
        self.assertEqual(probe_stdout, b"raw hostname output\x00")
        self.assertEqual(probe_stderr, b"raw hostname error\xff")
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 7)
        self.assertIsNone(result["error"])
        self.assertEqual(outcome, "partial")
        self.assertFalse(any("diagnostic" in member for member in member_names))

    def test_remote_cleanup_failure_preserves_evidence_and_reports_residue(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (root / "inventory.ini").write_bytes(inventory)
            output = root / "output"
            output.mkdir()
            temporary_root = root / "temporary"
            temporary_root.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(temporary_root)
            environment["FAKE_SSH_RECORD"] = str(root / "ssh-record")
            environment["FAKE_REMOTE_CLEANUP_FAILURE"] = "1"

            completed = self.run_cli(
                "collect",
                "--output-dir",
                str(output),
                cwd=root,
                env=environment,
                timeout=20,
            )

            bundles = list(output.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
            remote_residue = list(temporary_root.glob("ceph-incident-node.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "partial")
        self.assertEqual(len(remote_residue), 1, completed.stderr)
        self.assertIn(str(remote_residue[0]).encode("utf-8"), completed.stderr)
        self.assertIn(
            b"cannot remove Remote Node Collector workspace", completed.stderr
        )
        self.assertIn(f"{root_name}/nodes/node-a/probes/hostname/stdout", names)
        self.assertFalse(any("/private/" in name for name in names))

    def test_connection_failure_delivers_metadata_only_partial_bundle(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            (root / "inventory.ini").write_bytes(inventory)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SSH_RECORD"] = str(root / "ssh-record")
            environment["FAKE_SSH_CONNECTION_FAILURE"] = "1"

            completed = self.run_cli("collect", cwd=root, env=environment, timeout=20)
            bundles = list(root.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                names = {member.name for member in archive.getmembers()}
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(outcome, "partial")
        self.assertIn(b"[node-a] ssh: connection refused", completed.stderr)
        self.assertIn(b"Node Evidence Archive rejected", completed.stderr)
        self.assertFalse(
            any(name.startswith(f"{root_name}/nodes/node-a") for name in names)
        )

    def test_local_admission_failure_is_not_reported_as_archive_rejection(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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
        self.assertFalse(any(name.startswith(f"{root}/nodes/node-a") for name in names))
        self.assertFalse(any("private" in name for name in names))
        self.assertEqual(workspaces, [])

    def test_structurally_hostile_ssh_archive_cannot_escape_private_staging(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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
            environment["FAKE_SSH_HOSTILE_MEMBER"] = str(
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
        self.assertNotIn(
            b"fake-ssh private archive payload\n", b"".join(regular_payloads)
        )
        self.assertEqual(workspaces, [])

    def test_hostile_archive_member_controls_are_escaped_on_standard_error(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        hostile_root = "evil\x1b]2;spoofed\x07"
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
            environment["FAKE_SSH_HOSTILE_MEMBER"] = f"{hostile_root}/member"

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
            sentinel_bytes = outside_sentinel.read_bytes()
            workspaces = list(temporary_root.glob("ceph-incident-work.*"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            f"{bundle.resolve()} (partial)\n".encode("utf-8"),
        )
        self.assertEqual(
            completed.stderr,
            b"Target Node node-a: Node Evidence Archive rejected: "
            b"unknown archive root: evil\\x1b]2;spoofed\\x07\n",
        )
        self.assertNotIn(b"\x1b", completed.stderr)
        self.assertNotIn(b"\x07", completed.stderr)
        self.assertEqual(outcome, "partial")
        self.assertFalse(
            any(name.startswith(f"{root}/nodes/node-a") for name in names)
        )
        self.assertFalse(any("private" in name for name in names))
        self.assertEqual(sentinel_bytes, b"unchanged\n")
        self.assertEqual(workspaces, [])

    def test_publication_failure_has_controlled_installed_cli_nondelivery(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    @staticmethod
    @staticmethod
    def _write_fake_ssh(path: Path) -> None:
        path.write_text(
            f"""#!{sys.executable}
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile

record = Path(os.environ["FAKE_SSH_RECORD"])
record.mkdir(exist_ok=True)
(record / "count").touch()
count_file = record / "count"
count = int(count_file.read_text(encoding="ascii") or "0") + 1
count_file.write_text(f"{{count}}\\n", encoding="ascii")
argv = json.dumps(sys.argv[1:])
(record / "argv.json").write_text(argv, encoding="utf-8")
(record / f"argv.{{count}}.json").write_text(argv, encoding="utf-8")
source = sys.stdin.buffer.read()
(record / "stdin.py").write_bytes(source)
(record / f"stdin.{{count}}.py").write_bytes(source)
interrupt_ready = os.environ.get("FAKE_SSH_INTERRUPT_READY")
if interrupt_ready:
    remote_workspace = Path(os.environ["FAKE_SSH_REMOTE_WORKSPACE"])
    remote_workspace.mkdir()
    event_log = Path(os.environ["FAKE_EVENT_LOG"])
    with event_log.open("a", encoding="utf-8") as events:
        events.write("ssh-start\\n")
    Path(interrupt_ready).write_text(str(os.getpid()), encoding="ascii")
    def handle_interrupt(_signal, _frame):
        shutil.rmtree(remote_workspace)
        with event_log.open("a", encoding="utf-8") as events:
            events.write("remote-cleanup-request\\n")
        sys.stderr.write("normal remote cleanup requested\\n")
        raise SystemExit(130)
    def handle_termination(_signal, _frame):
        sys.stderr.write(
            f"remote cleanup not requested; known residue {{remote_workspace}}\\n"
        )
        raise SystemExit(143)
    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_termination)
    while True:
        signal.pause()
if os.environ.get("FAKE_SSH_CONNECTION_FAILURE"):
    sys.stderr.write("ssh: connection refused\\n")
    raise SystemExit(255)
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
lock_workspace_parent = os.environ.get("FAKE_SSH_LOCK_WORKSPACE_PARENT")
if lock_workspace_parent:
    temporary_root = Path(os.environ["TMPDIR"])
    workspaces = list(temporary_root.glob("ceph-incident-work.*"))
    if len(workspaces) != 1:
        raise RuntimeError("expected one workstation workspace")
    # Removing write permission on the workspace's parent leaves the workspace's
    # own contents removable but makes the final ``rmdir`` of the now-empty
    # workspace fail, reproducing a genuine post-publication cleanup problem.
    temporary_root.chmod(0o555)
if os.environ.get("FAKE_SSH_SELECTED_FILE_FAILURE"):
    selected_file_archive = io.BytesIO()
    with tarfile.open(fileobj=selected_file_archive, mode="w:gz") as archive:
        for name in ("node", "node/probes", "node/files"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
    sys.stdout.buffer.write(selected_file_archive.getvalue())
    sys.stderr.write("cannot copy selected file /etc/hosts: fixture failure\\n")
    raise SystemExit(1)
if os.environ.get("FAKE_SSH_CORRUPT"):
    sys.stdout.buffer.write(b"not-an-archive")
    raise SystemExit(0)
corrupt_host = os.environ.get("FAKE_SSH_CORRUPT_HOST")
if corrupt_host and f"root@{{corrupt_host}}" in sys.argv:
    sys.stdout.buffer.write(b"not-an-archive")
    raise SystemExit(0)
hostile_member = os.environ.get("FAKE_SSH_HOSTILE_MEMBER")
if hostile_member:
    private_payload = b"fake-ssh private archive payload\\n"
    hostile_archive = io.BytesIO()
    with tarfile.open(fileobj=hostile_archive, mode="w:gz") as archive:
        for name in ("node", "node/probes", "node/files"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
        hostile = tarfile.TarInfo(hostile_member)
        hostile.mode = 0o600
        hostile.size = len(private_payload)
        archive.addfile(hostile, io.BytesIO(private_payload))
    sys.stdout.buffer.write(hostile_archive.getvalue())
    raise SystemExit(0)
remote_python_index = sys.argv.index("python3")
remote_destination = sys.argv[remote_python_index - 1]
remote_start = remote_python_index + 1
probe_bin = Path(tempfile.mkdtemp(prefix="fake-ssh-probes."))
remote_support = Path(tempfile.mkdtemp(prefix="fake-ssh-remote-support."))
remote_source_root = remote_support / "source"
remote_log = remote_source_root / "var/log/fake.log"
remote_log.parent.mkdir(parents=True)
large_evidence_bytes = int(os.environ.get("FAKE_SSH_LARGE_EVIDENCE_BYTES", "0"))
if large_evidence_bytes:
    remote_log.write_bytes(os.urandom(large_evidence_bytes))
else:
    remote_log.write_bytes(b"fake remote var log bytes\\x00\\xff")
(remote_support / "sitecustomize.py").write_text(
    "import os\\n"
    "import shutil\\n"
    "_source_root = os.environ['FAKE_REMOTE_SOURCE_ROOT']\\n"
    "_original_open = os.open\\n"
    "def open(path, flags, *args, **kwargs):\\n"
    "    if os.fspath(path) == '/' and flags & os.O_DIRECTORY:\\n"
    "        return _original_open(_source_root, flags, *args, **kwargs)\\n"
    "    return _original_open(path, flags, *args, **kwargs)\\n"
    "os.open = open\\n"
    "_original_rmtree = shutil.rmtree\\n"
    "def rmtree(path, *args, **kwargs):\\n"
    "    if (os.environ.get('FAKE_REMOTE_CLEANUP_FAILURE') and "
    "os.path.basename(os.fspath(path)).startswith('ceph-incident-node.')):\\n"
    "        raise OSError('injected remote cleanup failure')\\n"
    "    return _original_rmtree(path, *args, **kwargs)\\n"
    "shutil.rmtree = rmtree\\n",
    encoding="utf-8",
)
probe_script = f"#!{sys.executable}\\nimport os\\nprint(os.path.basename(__file__))\\n"
for command in (
    "hostname", "date", "uname", "uptime", "lscpu", "free", "ps", "df",
    "lsblk", "iostat", "pvs", "vgs", "lvs", "ip", "dmesg", "systemctl",
    "podman", "docker", "chronyc", "ntpq", "timedatectl", "journalctl",
):
    executable = probe_bin / command
    executable.write_text(probe_script, encoding="utf-8")
    executable.chmod(0o755)
ceph = probe_bin / "ceph"
ceph.write_text(
    f"#!{sys.executable}\\n"
    "import json\\n"
    "import os\\n"
    "from pathlib import Path\\n"
    "import sys\\n"
    "argv = ['ceph', *sys.argv[1:]]\\n"
    "record = os.environ.get('FAKE_SSH_CEPH_RECORD')\\n"
    "if record:\\n"
    "    with Path(record).open('a', encoding='utf-8') as events:\\n"
    "        event = [os.environ['FAKE_SSH_DESTINATION'], *argv]\\n"
    "        events.write(json.dumps(event) + '\\\\n')\\n"
    "failed = json.loads(os.environ.get('FAKE_SSH_CEPH_FAIL_ARGV', 'null'))\\n"
    "if argv == failed:\\n"
    "    os.write(1, b'failed ceph stdout\\\\x00\\\\xff')\\n"
    "    os.write(2, b'failed ceph stderr\\\\x00\\\\xfe')\\n"
    "    raise SystemExit(9)\\n"
    "if sys.argv[1:] == ['crash', 'ls', '--format', 'json-pretty']:\\n"
    "    os.write(1, b'[]')\\n"
    "else:\\n"
    "    os.write(1, b'ordinary ceph stdout\\\\x00\\\\xff')\\n",
    encoding="utf-8",
)
ceph.chmod(0o755)
if os.environ.get("FAKE_SSH_JOURNAL_FAILURE"):
    journalctl = probe_bin / "journalctl"
    journalctl.write_text(
        "#!" + sys.executable + "\\n"
        "import os\\n"
        "os.write(1, b'partial journal stdout\\\\x00\\\\xff')\\n"
        "os.write(2, b'partial journal stderr\\\\x00\\\\xfe')\\n"
        "raise SystemExit(9)\\n",
        encoding="utf-8",
    )
    journalctl.chmod(0o755)
remote_environment = os.environ.copy()
remote_environment["FAKE_REMOTE_SOURCE_ROOT"] = str(remote_source_root)
remote_environment["FAKE_SSH_DESTINATION"] = remote_destination
remote_environment["PYTHONPATH"] = str(remote_support)
path_entries = remote_environment["PATH"].split(os.pathsep)
remote_environment["PATH"] = os.pathsep.join(
    (path_entries[0], str(probe_bin), remote_environment["PATH"])
)
try:
    completed = subprocess.run(
        [sys.executable, *sys.argv[remote_start:]],
        input=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=remote_environment,
        check=False,
    )
finally:
    shutil.rmtree(probe_bin)
    shutil.rmtree(remote_support)
if os.environ.get("FAKE_SSH_LOCK_WORKSPACE_PARENT_AFTER_REMOTE"):
    Path(os.environ["TMPDIR"]).chmod(0o555)
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

    @staticmethod
    def _write_fake_kubectl(path: Path) -> None:
        path.write_text(
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

record = Path(os.environ["FAKE_KUBECTL_RECORD"])
with record.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
event_log = os.environ.get("FAKE_EVENT_LOG")
if event_log:
    with Path(event_log).open("a", encoding="utf-8") as events:
        events.write("kubernetes " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[3:4] == ["logs"]:
    sys.stdout.buffer.write(b"pod log raw bytes\\x00\\xff")
elif sys.argv[-2:] == ["pods", "--output=json"]:
    sys.stdout.write(json.dumps({json.dumps(KUBERNETES_PODS)}))
elif "events" in sys.argv:
    sys.stdout.buffer.write(b"events raw bytes\\x00")
else:
    sys.stdout.write("snapshot")
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
