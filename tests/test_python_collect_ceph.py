from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ceph_incident_collectors import REMOTE_BOOTSTRAP, _ssh_command  # noqa: E402

ENTRYPOINT = ROOT / "ceph_incident_bundle.py"
SHELL_VERIFIER = ROOT / "lib" / "verify-bundle.sh"
FIXTURE_BIN = ROOT / "tests" / "fixtures" / "python-ceph" / "bin"
SEED = "ceph@10.0.0.1"

# Drives the workstation collector seam directly so runner semantics can be
# observed through the fake adapter without a public CLI flag.
RUNNER_SEAM_DRIVER = """\
import sys
from pathlib import Path

from ceph_incident_collectors import collect_direct_ceph_cluster

workdir, seed, ssh_key, runner = sys.argv[1:]
status = collect_direct_ceph_cluster(
    workdir=Path(workdir),
    seed=seed,
    ssh_key=Path(ssh_key),
    connection_timeout=5,
    command_timeout=5,
    known_hosts_file=None,
    runner=runner,
)
print(f"status={status}")
"""

EXPECTED_JSON_ARTIFACTS = {
    "status.json": ("status", "--format", "json-pretty"),
    "health-detail.json": ("health", "detail", "--format", "json-pretty"),
    "versions.json": ("versions", "--format", "json-pretty"),
    "df-detail.json": ("df", "detail", "--format", "json-pretty"),
    "osd-tree.json": ("osd", "tree", "--format", "json-pretty"),
    "osd-df.json": ("osd", "df", "--format", "json-pretty"),
    "osd-dump.json": ("osd", "dump", "--format", "json-pretty"),
    "osd-perf.json": ("osd", "perf", "--format", "json-pretty"),
    "osd-blocked-by.json": ("osd", "blocked-by", "--format", "json-pretty"),
    "pg-stat.json": ("pg", "stat", "--format", "json-pretty"),
    "pg-dump.json": ("pg", "dump", "--format", "json-pretty"),
    "pg-dump-stuck.json": ("pg", "dump_stuck", "--format", "json-pretty"),
    "mon-dump.json": ("mon", "dump", "--format", "json-pretty"),
    "quorum-status.json": ("quorum_status", "--format", "json-pretty"),
    "mgr-dump.json": ("mgr", "dump", "--format", "json-pretty"),
    "orch-host-ls.json": ("orch", "host", "ls", "--format", "json-pretty"),
    "orch-ps.json": ("orch", "ps", "--format", "json-pretty"),
    "orch-device-ls-wide.json": (
        "orch",
        "device",
        "ls",
        "--wide",
        "--format",
        "json-pretty",
    ),
    "config-dump.json": ("config", "dump", "--format", "json-pretty"),
    "crash-ls.json": ("crash", "ls", "--format", "json-pretty"),
}

EXPECTED_TEXT_ARTIFACTS = {
    "status.txt": ("status",),
    "health-detail.txt": ("health", "detail"),
    "osd-tree.txt": ("osd", "tree"),
    "orch-ps.txt": ("orch", "ps"),
}


class DirectCephFixture:
    """Black-box helpers: fake ssh on PATH, public CLI, bundle inspection."""

    def make_fake_environment(self, root: Path, **knobs: str) -> tuple[dict[str, str], Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "ssh").symlink_to(FIXTURE_BIN / "ssh")
        ledger = root / "ssh-argv.jsonl"
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(ledger),
            **knobs,
        }
        return environment, ledger

    def run_collect(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        seed: str | None = SEED,
        inventory_text: str | None = None,
        extra_arguments: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        inventory = root / "inventory.env"
        inventory.write_text(
            inventory_text
            if inventory_text is not None
            else 'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n',
            encoding="utf-8",
        )
        ssh_key = root / "id_ed25519"
        ssh_key.write_text("fixture key path only\n", encoding="utf-8")
        command = [
            sys.executable,
            str(ENTRYPOINT),
            "collect",
            "--inventory",
            str(inventory),
            "--ssh-key",
            str(ssh_key),
            "--out",
            str(root / "results"),
            "--timeout",
            "5",
            "--node-timeout",
            "20",
            "--mode",
            "cephadm",
            "--no-trust-ssh-host-key",
            *extra_arguments,
        ]
        if seed is not None:
            command.extend(["--seed", seed])
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def bundle_of(self, result: subprocess.CompletedProcess[str]) -> Path:
        self.assertRegex(result.stdout, r"^bundle: .+\.tar\.gz\n$")
        bundle = Path(result.stdout.removeprefix("bundle: ").strip())
        self.assertTrue(bundle.is_file())
        return bundle

    def extract(self, bundle: Path) -> dict[str, str]:
        contents: dict[str, str] = {}
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                name = member.name.removeprefix("./")
                stream = archive.extractfile(member)
                self.assertIsNotNone(stream)
                contents[name] = stream.read().decode("utf-8", errors="replace")
        return contents

    def remote_commands(self, ledger: Path) -> list[list[str]]:
        commands: list[list[str]] = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            arguments = json.loads(line)
            if "ceph" in arguments:
                commands.append(arguments[arguments.index("ceph") :])
        return commands

    def manifest_entries(self, contents: dict[str, str]) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in contents["manifest.jsonl"].splitlines()
            if line.strip()
        ]

    def assert_bundle_verifies(self, bundle: Path) -> None:
        python_verify = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "verify", str(bundle)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(python_verify.returncode, 0, python_verify.stderr)
        self.assertIn("VERIFY PASS", python_verify.stdout)

        shell_verify = subprocess.run(
            ["bash", str(SHELL_VERIFIER), str(bundle)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(shell_verify.returncode, 0, shell_verify.stderr)
        self.assertIn("VERIFY PASS", shell_verify.stdout)


class CollectDirectCephCliTests(DirectCephFixture, unittest.TestCase):
    def test_direct_ceph_seed_collects_json_and_text_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)

            for artifact in EXPECTED_JSON_ARTIFACTS:
                self.assertIn(f"cluster/ceph/json/{artifact}", contents)
            for artifact in EXPECTED_TEXT_ARTIFACTS:
                self.assertIn(f"cluster/ceph/text/{artifact}", contents)

            status = contents["cluster/ceph/json/status.json"]
            self.assertTrue(status.startswith(f"# host: {SEED}\n"), status)
            self.assertIn("# collector: collect-cluster-cephadm\n", status)
            self.assertIn("# timeout: 5s\n", status)
            self.assertIn('"health":"HEALTH_OK"', status)
            self.assertIn("cluster is healthy", contents["cluster/ceph/text/status.txt"])

    def test_direct_ceph_runs_plain_ceph_argv_over_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.remote_commands(ledger)
            issued = {tuple(command[1:]) for command in commands}
            for words in EXPECTED_JSON_ARTIFACTS.values():
                self.assertIn(words, issued)
            for words in EXPECTED_TEXT_ARTIFACTS.values():
                self.assertIn(words, issued)

            ledger_text = ledger.read_text(encoding="utf-8")
            self.assertNotIn("cephadm", ledger_text)
            self.assertNotIn("sudo", ledger_text)
            for line in ledger_text.splitlines():
                arguments = json.loads(line)
                if "ceph" not in arguments:
                    continue
                self.assertEqual(arguments[arguments.index("ceph") - 1], SEED)
                self.assertIn("ConnectTimeout=5", arguments)
                self.assertIn("BatchMode=yes", arguments)

    def test_direct_ceph_manifest_records_every_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            entries = self.manifest_entries(contents)
            self.assertEqual(len(entries), 34)
            for entry in entries:
                self.assertEqual(entry["host"], SEED)
                self.assertEqual(entry["collector"], "collect-cluster-cephadm")
                self.assertEqual(entry["exit_code"], 0)
                self.assertTrue(entry["artifact"].startswith("/"))
                self.assertIn("ssh", entry["command"])
                self.assertRegex(entry["started"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
                self.assertRegex(entry["ended"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
            self.assertEqual(contents["errors.log"], "")

    def test_direct_ceph_bundle_passes_both_verifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)
            bundle = self.bundle_of(result)

            self.assert_bundle_verifies(bundle)

    def test_recent_crash_inspection_caps_at_ten_and_avoids_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            crash_artifacts = {
                name.removeprefix("cluster/ceph/json/crash-info/")
                for name in contents
                if name.startswith("cluster/ceph/json/crash-info/")
            }
            self.assertEqual(
                crash_artifacts,
                {
                    "crash-01.json",
                    "crash_02.json",
                    "crash_02-2.json",
                    "crash-04.json",
                    "crash-05.json",
                    "crash-06.json",
                    "crash-07.json",
                    "crash-08.json",
                    "crash-09.json",
                    "crash-10.json",
                },
            )
            self.assertIn(
                "detail for crash/02",
                contents["cluster/ceph/json/crash-info/crash_02.json"],
            )
            self.assertIn(
                "detail for crash:02",
                contents["cluster/ceph/json/crash-info/crash_02-2.json"],
            )
            crash_requests = [
                command
                for command in self.remote_commands(ledger)
                if command[1:3] == ["crash", "info"]
            ]
            self.assertEqual(len(crash_requests), 10)
            self.assertNotIn("crash-11", ledger.read_text(encoding="utf-8"))

    def test_unparseable_crash_list_is_skipped_without_failing_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_SSH_CRASH_LS_BROKEN="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "SKIPPED", contents["cluster/ceph/text/crash-info-skip.txt"]
            )
            self.assertIn("crash", contents["cluster/ceph/text/crash-info-skip.txt"])
            self.assertFalse(
                any(
                    name.startswith("cluster/ceph/json/crash-info/")
                    for name in contents
                )
            )
            self.assertNotIn("crash info", ledger.read_text(encoding="utf-8"))

    def test_empty_crash_list_collects_no_crash_info(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_SSH_CRASH_LS_EMPTY="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertNotIn("cluster/ceph/text/crash-info-skip.txt", contents)
            self.assertFalse(
                any(
                    name.startswith("cluster/ceph/json/crash-info/")
                    for name in contents
                )
            )

    def test_environment_records_direct_ceph_source_and_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(f"ceph_source={SEED}\n", contents["environment.txt"])
            self.assertIn("ceph_runner=direct\n", contents["environment.txt"])


class DirectCephFailureSemanticsTests(DirectCephFixture, unittest.TestCase):
    def test_failed_required_command_is_partial_and_keeps_other_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_SSH_FAIL_ON="osd perf"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "simulated failure for osd perf",
                contents["cluster/ceph/json/osd-perf.json"],
            )
            for artifact in EXPECTED_JSON_ARTIFACTS:
                self.assertIn(f"cluster/ceph/json/{artifact}", contents)
            entries = {
                Path(str(entry["artifact"])).name: entry
                for entry in self.manifest_entries(contents)
            }
            self.assertEqual(entries["osd-perf.json"]["exit_code"], 17)
            self.assertEqual(entries["status.json"]["exit_code"], 0)
            self.assertIn("exit=17", contents["errors.log"])
            self.assertIn("osd-perf.json", contents["errors.log"])
            self.assertIn("cluster collection exited 2", contents["errors.log"])
            self.assertIn("cluster_status: 2", contents["summary.txt"])
            self.assertIn("final_status: 2", contents["summary.txt"])
            self.assert_bundle_verifies(bundle)

    def test_timed_out_command_is_truncated_and_writes_ssh_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root,
                FAKE_SSH_SLEEP_ON="pg dump --format",
                FAKE_SSH_SLEEP_SECONDS="30",
            )

            result = self.run_collect(
                root, environment, extra_arguments=("--timeout", "2")
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn("# TRUNCATED", contents["cluster/ceph/json/pg-dump.json"])
            self.assertIn("exit 124", contents["cluster/ceph/json/pg-dump.json"])
            entries = {
                Path(str(entry["artifact"])).name: entry
                for entry in self.manifest_entries(contents)
            }
            self.assertEqual(entries["pg-dump.json"]["exit_code"], 124)
            debug_logs = [
                name for name in contents if name.startswith("ssh-debug/cluster-ceph-")
            ]
            self.assertEqual(len(debug_logs), 1, sorted(contents))
            self.assertIn("# label: cluster-ceph", contents[debug_logs[0]])
            self.assertIn("# exit_code:", contents[debug_logs[0]])
            self.assert_bundle_verifies(bundle)

    def test_missing_remote_ceph_command_keeps_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_SSH_MISSING_CEPH="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "ceph: command not found", contents["cluster/ceph/json/status.json"]
            )
            entries = {
                Path(str(entry["artifact"])).name: entry
                for entry in self.manifest_entries(contents)
            }
            self.assertEqual(entries["status.json"]["exit_code"], 127)
            self.assertIn(
                "SKIPPED", contents["cluster/ceph/text/crash-info-skip.txt"]
            )
            self.assertIn("nodes/monitor01/system/hostname.txt", contents)
            self.assert_bundle_verifies(bundle)

    def test_ssh_transport_failure_records_debug_log_and_partial_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(
                root, FAKE_SSH_CONNECT_FAIL="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "ssh-debug/cluster-ceph-ceph_10.0.0.1.log", contents
            )
            debug_log = contents["ssh-debug/cluster-ceph-ceph_10.0.0.1.log"]
            self.assertIn("# target: ceph@10.0.0.1", debug_log)
            self.assertIn("debug1:", debug_log)
            self.assertIn("# exit_code: 255", debug_log)
            self.assertIn("exit=255", contents["errors.log"])
            self.assert_bundle_verifies(bundle)


class DirectCephSeedSelectionTests(DirectCephFixture, unittest.TestCase):
    def test_unsafe_seed_argument_fails_before_any_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root, environment, seed="--ProxyCommand=touch /tmp/pwned"
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("seed", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(ledger.exists())

    def test_inventory_seed_host_selects_the_direct_ceph_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                seed=None,
                inventory_text=(
                    'SSH_USER="ceph"\n'
                    'SEED_HOST="10.0.0.1"\n'
                    'HOSTS=(\n  "monitor01=10.0.0.1"\n)\n'
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("ceph_source=ceph@10.0.0.1\n", contents["environment.txt"])
            self.assertIn("cluster/ceph/json/status.json", contents)

    def test_collect_without_a_seed_auto_selects_the_first_capable_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment, seed=None)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("cluster/ceph/json/status.json", contents)
            self.assertIn(
                "ceph_source=ceph@10.0.0.1\n", contents["environment.txt"]
            )
            self.assertIn("ceph_runner=direct\n", contents["environment.txt"])
            self.assertTrue(self.remote_commands(ledger))


class DirectCephRunnerSeamTests(DirectCephFixture, unittest.TestCase):
    """CA4/CA5: the collector seam runs `ceph` or `sudo -n ceph`, never cephadm."""

    def collect_with_runner(
        self, root: Path, environment: dict[str, str], runner: str
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        workdir = root / "workdir"
        workdir.mkdir()
        ssh_key = root / "id_ed25519"
        ssh_key.write_text("fixture key path only\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                RUNNER_SEAM_DRIVER,
                str(workdir),
                SEED,
                str(ssh_key),
                runner,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed, workdir

    def ceph_invocations(self, ledger: Path) -> list[list[str]]:
        return [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if "ceph" in json.loads(line)
        ]

    def test_sudo_runner_runs_sudo_n_ceph_and_never_cephadm_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            completed, workdir = self.collect_with_runner(root, environment, "sudo")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("status=0", completed.stdout)
            self.assertTrue((workdir / "cluster/ceph/json/status.json").is_file())
            invocations = self.ceph_invocations(ledger)
            self.assertEqual(len(invocations), 34)
            for arguments in invocations:
                index = arguments.index("sudo")
                self.assertEqual(arguments[index - 1], SEED)
                self.assertEqual(arguments[index : index + 3], ["sudo", "-n", "ceph"])
            ledger_text = ledger.read_text(encoding="utf-8")
            self.assertNotIn("cephadm", ledger_text)
            self.assertNotIn("shell", ledger_text)

    def test_direct_runner_runs_plain_ceph_without_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            completed, workdir = self.collect_with_runner(root, environment, "direct")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("status=0", completed.stdout)
            for arguments in self.ceph_invocations(ledger):
                self.assertEqual(arguments[arguments.index("ceph") - 1], SEED)
            ledger_text = ledger.read_text(encoding="utf-8")
            self.assertNotIn("sudo", ledger_text)
            self.assertNotIn("cephadm", ledger_text)

    def test_unsupported_runner_is_rejected_before_any_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ledger = self.make_fake_environment(root)

            completed, _ = self.collect_with_runner(root, environment, "cephadm")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported Ceph CLI runner: cephadm", completed.stderr)
            self.assertFalse(ledger.exists())


class MissingLocalSshTransportTests(DirectCephFixture, unittest.TestCase):
    def make_environment_without_ssh(self, root: Path) -> dict[str, str]:
        """A PATH holding only the packaging tool — no ssh anywhere on it."""

        toolbox = root / "toolbox"
        toolbox.mkdir()
        for command in ("tar",):
            real_command = shutil.which(command)
            self.assertIsNotNone(real_command, command)
            (toolbox / command).symlink_to(str(real_command))
        environment = {**os.environ, "PATH": str(toolbox)}
        self.assertIsNone(shutil.which("ssh", path=environment["PATH"]))
        return environment

    def test_missing_local_ssh_keeps_a_verified_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment_without_ssh(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)

            skipped = contents["nodes/monitor01/SKIPPED.txt"]
            self.assertIn("SKIPPED", skipped)
            self.assertIn("ssh: command not found", skipped)

            self.assertIn("SKIPPED", contents["cluster/ceph/SKIPPED.txt"])
            self.assertNotIn("cluster/ceph/json/status.json", contents)

            errors = contents["errors.log"]
            self.assertIn("no cephadm-capable node", contents["cluster/ceph/SKIPPED.txt"])
            self.assertIn("node monitor01 (ceph@10.0.0.1):", errors)
            self.assertIn("ssh: command not found", errors)
            self.assertIn("final_status: 2", contents["summary.txt"])
            self.assertIn("node_failed: 1", contents["summary.txt"])

            self.assert_bundle_verifies(bundle)


class FakeSshArgvContractTests(unittest.TestCase):
    """The offline proof rests on the fake adapter answering only exact argv."""

    def base_options(self) -> list[str]:
        return [
            "-i",
            "/tmp/fixture-key",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
        ]

    def bootstrap_command(self, *extra: str) -> str:
        """The production remote command, built by the production seam.

        The fixture pins the bootstrap source by an independently recorded
        SHA-256, so generating the accepted invocation from ``_ssh_command``
        here cannot make the fixture agree with itself: a bootstrap change that
        nobody re-pins turns this into a rejection.
        """

        remote = _ssh_command(
            Path("/tmp/fixture-key"),
            SEED,
            5,
            None,
            "Zm9vYmFy",
        )[-1]
        return " ".join([remote, *(shlex.quote(word) for word in extra)])

    def run_fake_ssh(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(FIXTURE_BIN / "ssh"), *arguments],
            input=b"",
            capture_output=True,
            check=False,
        )

    def assert_rejected(self, arguments: list[str]) -> None:
        completed = self.run_fake_ssh(arguments)
        self.assertEqual(completed.returncode, 99, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"unexpected ssh command", completed.stderr)

    def test_whitelisted_ceph_read_commands_are_answered(self) -> None:
        for prefix in (["ceph"], ["sudo", "-n", "ceph"]):
            with self.subTest(prefix=prefix):
                completed = self.run_fake_ssh(
                    [
                        *self.base_options(),
                        SEED,
                        *prefix,
                        "status",
                        "--format",
                        "json-pretty",
                    ]
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(b"HEALTH_OK", completed.stdout)

    def test_tokens_appended_to_a_whitelisted_ceph_command_fail_closed(self) -> None:
        accepted = [*self.base_options(), SEED, "ceph", "status", "--format", "json-pretty"]
        for appended in (
            ["&&", "ceph", "osd", "set", "noout"],
            [";", "rm", "-rf", "/var/log"],
            ["--connect-timeout", "1"],
        ):
            with self.subTest(appended=appended):
                self.assert_rejected([*accepted, *appended])

    def test_mutating_ceph_verbs_are_never_answered(self) -> None:
        for words in (
            ["osd", "set", "noout"],
            ["config", "set", "mon", "mon_allow_pool_delete", "true"],
            ["orch", "daemon", "restart", "mon.a"],
        ):
            with self.subTest(words=words):
                self.assert_rejected([*self.base_options(), SEED, "ceph", *words])
                self.assert_rejected(
                    [*self.base_options(), SEED, "sudo", "-n", "ceph", *words]
                )

    def test_cephadm_shell_is_never_answered(self) -> None:
        self.assert_rejected(
            [
                *self.base_options(),
                SEED,
                "sudo",
                "-n",
                "cephadm",
                "shell",
                "--",
                "ceph",
                "status",
                "--format",
                "json-pretty",
            ]
        )

    def test_debug_probe_is_answered_only_in_its_complete_shape(self) -> None:
        probe = [*self.base_options(), "-vvv", "-o", "LogLevel=DEBUG3", SEED, "true"]

        completed = self.run_fake_ssh(probe)
        self.assertEqual(completed.returncode, 255)
        self.assertIn(b"debug1:", completed.stdout)

        self.assert_rejected([*probe, ";", "touch", "/tmp/pwned"])
        self.assert_rejected([*probe, "ceph", "osd", "set", "noout"])
        self.assert_rejected([*self.base_options(), "-vvv", SEED, "true"])
        self.assert_rejected(
            [*self.base_options(), "-vvv", "-o", "LogLevel=DEBUG3", SEED, "ceph", "status"]
        )

    def test_node_bootstrap_is_answered_only_as_one_exact_remote_command(self) -> None:
        accepted = self.bootstrap_command()
        self.assertEqual(len(shlex.split(accepted)), 4, accepted)

        completed = self.run_fake_ssh([*self.base_options(), SEED, accepted])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith(b"\x1f\x8b"), completed.stdout[:8])

        self.assert_rejected(
            [*self.base_options(), SEED, self.bootstrap_command("extra-argument")]
        )
        self.assert_rejected(
            [
                *self.base_options(),
                SEED,
                self.bootstrap_command() + " && touch /tmp/pwned",
            ]
        )
        self.assert_rejected(
            [
                *self.base_options(),
                SEED,
                self.bootstrap_command() + "; ceph osd set noout",
            ]
        )
        self.assert_rejected(
            [*self.base_options(), SEED, self.bootstrap_command(), "true"]
        )

    def test_bootstrap_source_other_than_the_pinned_one_fails_closed(self) -> None:
        """Four remote tokens and a valid config are not enough on their own.

        Each source below keeps the accepted shape — ``python3 -c <source>
        <encoded-config>`` — and varies only the Python the collector would
        have run.  The fixture answers none of them, so "the fake ran the
        reviewed bootstrap" is pinned rather than assumed.
        """

        for label, source in (
            ("mutating", "import shutil\nshutil.rmtree('/var/log')\n"),
            ("smuggled-suffix", REMOTE_BOOTSTRAP + "import os\nos.remove('/etc/fstab')\n"),
            ("unrelated", "print('a completely different program')\n"),
            ("whitespace-drift", REMOTE_BOOTSTRAP.replace("\n", "\n ")),
            ("empty", ""),
        ):
            with self.subTest(source=label):
                tampered = shlex.join(["python3", "-c", source, "Zm9vYmFy"])
                self.assertEqual(len(shlex.split(tampered)), 4, tampered)
                self.assert_rejected([*self.base_options(), SEED, tampered])

    def test_argv_outside_the_shared_option_vector_fails_closed(self) -> None:
        self.assert_rejected([SEED, "ceph", "status", "--format", "json-pretty"])
        self.assert_rejected(
            [
                *self.base_options(),
                "-o",
                "ProxyCommand=touch /tmp/pwned",
                SEED,
                "ceph",
                "status",
                "--format",
                "json-pretty",
            ]
        )
        tampered = self.base_options()
        tampered[tampered.index("BatchMode=yes")] = "BatchMode=no"
        self.assert_rejected([*tampered, SEED, "ceph", "status", "--format", "json-pretty"])


if __name__ == "__main__":
    unittest.main()
