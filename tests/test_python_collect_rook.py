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
FIXTURE_BIN = ROOT / "tests" / "fixtures" / "python-rook" / "bin"
NODE_TARGET = "ceph@10.0.0.1"

REQUIRED_ROOK_ARTIFACTS = (
    "cluster/rook/pods-wide.txt",
    "cluster/rook/events.txt",
    "cluster/rook/rook-resources.yaml",
)


class RookFixture:
    """Black-box helpers: fake kubectl/ssh on PATH, public CLI, bundle reading."""

    def make_fake_environment(
        self, root: Path, **knobs: str
    ) -> tuple[dict[str, str], Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        for name in ("kubectl", "ssh"):
            (fake_bin / name).symlink_to(FIXTURE_BIN / name)
        kubectl_ledger = root / "kubectl-argv.jsonl"
        ssh_ledger = root / "ssh-argv.jsonl"
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_KUBECTL_LOG": str(kubectl_ledger),
            "FAKE_SSH_LOG": str(ssh_ledger),
            **knobs,
        }
        return environment, kubectl_ledger, ssh_ledger

    def run_collect(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        inventory_text: str | None = None,
        extra_arguments: tuple[str, ...] = ("--kube-mode", "local"),
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
        return subprocess.run(
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
                "--timeout",
                "5",
            "--node-timeout",
            "20",
            "--mode",
            "rook",
            "--no-trust-ssh-host-key",
                *extra_arguments,
            ],
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
                stream = archive.extractfile(member)
                self.assertIsNotNone(stream)
                contents[member.name.removeprefix("./")] = stream.read().decode(
                    "utf-8", errors="replace"
                )
        return contents

    def ledger(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def kubectl_commands(self, kubectl_ledger: Path) -> list[list[str]]:
        return self.ledger(kubectl_ledger)

    def manifest_entries(self, contents: dict[str, str]) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in contents["manifest.jsonl"].splitlines()
            if line.strip()
        ]

    def rook_entries(self, contents: dict[str, str]) -> dict[str, dict[str, object]]:
        return {
            Path(str(entry["artifact"])).name: entry
            for entry in self.manifest_entries(contents)
            if entry["collector"] == "collect-cluster-rook"
        }

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

class LocalKubectlRunnerTests(RookFixture, unittest.TestCase):
    def test_local_kube_mode_collects_rook_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)

            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertIn(artifact, contents)
            pods = contents["cluster/rook/pods-wide.txt"]
            self.assertTrue(pods.startswith("# host: rook\n"), pods)
            self.assertIn("# collector: collect-cluster-rook\n", pods)
            self.assertIn("# timeout: 5s\n", pods)
            self.assertIn("rook-ceph-mon-a", pods)
            self.assertIn("ns=rook-ceph", pods)
            self.assertIn("BackOff", contents["cluster/rook/events.txt"])
            self.assertIn(
                "kind: CephCluster", contents["cluster/rook/rook-resources.yaml"]
            )
            self.assertIn(
                "operator log for rook-ceph-operator-rook-ceph-abc123",
                contents["cluster/rook/operator.log"],
            )

            # The namespace is detected before anything is collected from it.
            commands = self.kubectl_commands(kubectl_ledger)
            self.assertEqual(commands[0], ["get", "namespace", "rook-ceph"])
            self.assertIn(
                [
                    "logs",
                    "-n",
                    "rook-ceph",
                    "rook-ceph-operator-rook-ceph-abc123",
                    "--since=24h",
                ],
                commands,
            )

            # The local runner must not reach for ssh to talk to Kubernetes.
            for arguments in self.ledger(ssh_ledger):
                self.assertNotIn("kubectl", arguments)

            self.assert_bundle_verifies(bundle)


class RemoteKubectlRunnerTests(RookFixture, unittest.TestCase):
    def test_remote_kube_mode_runs_kubectl_on_the_inventory_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root, environment, extra_arguments=("--kube-mode", "remote")
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertIn(artifact, contents)

            remote = [
                arguments
                for arguments in self.ledger(ssh_ledger)
                if "kubectl" in arguments
            ]
            self.assertTrue(remote, self.ledger(ssh_ledger))
            for arguments in remote:
                index = arguments.index("kubectl")
                # kubectl runs on the node, behind the shared option vector.
                self.assertEqual(arguments[index - 1], NODE_TARGET)
                self.assertIn("BatchMode=yes", arguments)
                self.assertIn("ConnectTimeout=5", arguments)
                self.assertIn("IdentitiesOnly=yes", arguments)

            self.assertIn(
                ["get", "pods", "-n", "rook-ceph", "-o", "wide"],
                self.kubectl_commands(kubectl_ledger),
            )
            self.assertIn("rook_source=ceph@10.0.0.1\n", contents["environment.txt"])
            self.assert_bundle_verifies(bundle)

    def test_kube_context_is_forwarded_to_every_kubectl_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--kube-mode",
                    "remote",
                    "--kube-context",
                    "arn:aws:eks:eu-west-1:123456789012:cluster/lab",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.kubectl_commands(kubectl_ledger)
            self.assertTrue(commands)
            for arguments in commands:
                self.assertEqual(
                    arguments[:2],
                    ["--context", "arn:aws:eks:eu-west-1:123456789012:cluster/lab"],
                )
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "kube_context=arn:aws:eks:eu-west-1:123456789012:cluster/lab\n",
                contents["environment.txt"],
            )

    def test_local_runner_also_forwards_the_kube_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--kube-context", "lab"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            for arguments in self.kubectl_commands(kubectl_ledger):
                self.assertEqual(arguments[:2], ["--context", "lab"])


class RookNamespaceTests(RookFixture, unittest.TestCase):
    def test_external_cluster_splits_resource_and_operator_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                inventory_text=(
                    'SSH_USER="ceph"\n'
                    'ROOK_NAMESPACE="rook-ceph-external"\n'
                    'ROOK_OPERATOR_NAMESPACE="rook-ceph"\n'
                    'HOSTS=(\n  "monitor01=10.0.0.1"\n)\n'
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("ns=rook-ceph-external", contents["cluster/rook/pods-wide.txt"])
            self.assertIn(
                "namespace: rook-ceph-external",
                contents["cluster/rook/rook-resources.yaml"],
            )
            # The operator Pod and its log come from the operator namespace.
            self.assertIn(
                "rook-ceph-operator-rook-ceph-abc123 in rook-ceph",
                contents["cluster/rook/operator.log"],
            )

            commands = self.kubectl_commands(kubectl_ledger)
            self.assertIn(
                ["get", "namespace", "rook-ceph-external"], commands
            )
            self.assertIn(
                [
                    "get",
                    "pods",
                    "-n",
                    "rook-ceph",
                    "-l",
                    "app=rook-ceph-operator",
                    "-o",
                    "name",
                ],
                commands,
            )
            self.assertIn(
                "rook_namespace=rook-ceph-external\n", contents["environment.txt"]
            )
            self.assertIn(
                "rook_operator_namespace=rook-ceph\n", contents["environment.txt"]
            )

    def test_operator_namespace_has_its_own_rook_ceph_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                inventory_text=(
                    'SSH_USER="ceph"\n'
                    'ROOK_NAMESPACE="rook-ceph-external"\n'
                    'HOSTS=(\n  "monitor01=10.0.0.1"\n)\n'
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "rook_operator_namespace=rook-ceph\n",
                contents["environment.txt"],
            )

    def test_empty_kube_context_preserves_current_context_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--kube-context", ""),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.kubectl_commands(kubectl_ledger))
            for arguments in self.kubectl_commands(kubectl_ledger):
                self.assertNotIn("--context", arguments)

    def assert_rejected_before_any_command(
        self, root: Path, environment: dict[str, str], ledgers: tuple[Path, Path], **kwargs
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_collect(root, environment, **kwargs)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        for ledger in ledgers:
            self.assertFalse(ledger.exists(), ledger)
        return result

    def test_unsupported_kube_mode_is_rejected_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.assert_rejected_before_any_command(
                root,
                environment,
                (kubectl_ledger, ssh_ledger),
                extra_arguments=("--kube-mode", "bogus"),
            )

            self.assertIn("kube-mode", result.stderr)

    def test_kube_context_metacharacters_are_rejected_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.assert_rejected_before_any_command(
                root,
                environment,
                (kubectl_ledger, ssh_ledger),
                extra_arguments=("--kube-mode", "local", "--kube-context", "bad;ctx"),
            )

            # The usage text names every option, so only the rejection wording
            # distinguishes "refused this context" from "printed the usage".
            self.assertIn("--kube-context may only contain", result.stderr)

    def test_a_real_world_kube_context_passes_validation(self) -> None:
        """O24: the names operators actually have must survive the filter.

        `kubernetes-admin@kubernetes` and an EKS ARN carry `@`, `:` and `/`.
        Rejecting a legal context would be as wrong as accepting a metacharacter,
        so this asserts the run gets past validation and stops on the missing
        inventory instead.
        """

        for context in (
            "kubernetes-admin@kubernetes",
            "arn:aws:eks:us-east-1:1/x@k8s",
        ):
            with self.subTest(kube_context=context):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, kubectl_ledger, ssh_ledger = (
                        self.make_fake_environment(root)
                    )
                    ssh_key = root / "id_ed25519"
                    ssh_key.write_text("fixture key path only\n", encoding="utf-8")

                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ENTRYPOINT),
                            "collect",
                            "--inventory",
                            str(root / "absent.env"),
                            "--ssh-key",
                            str(ssh_key),
                            "--out",
                            str(root / "results"),
                            "--kube-mode",
                            "local",
                            "--kube-context",
                            context,
                        ],
                        cwd=ROOT,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertNotIn("--kube-context may only contain", result.stderr)
                    self.assertIn("missing inventory", result.stderr)

    def test_remote_since_metacharacters_are_rejected_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.assert_rejected_before_any_command(
                root,
                environment,
                (kubectl_ledger, ssh_ledger),
                extra_arguments=(
                    "--kube-mode",
                    "remote",
                    "--since",
                    "24h; touch /tmp/rook-mutated",
                ),
            )

            self.assertIn("--since", result.stderr)

    def test_unsafe_inventory_namespace_is_rejected_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.assert_rejected_before_any_command(
                root,
                environment,
                (kubectl_ledger, ssh_ledger),
                inventory_text=(
                    'ROOK_NAMESPACE="../../etc"\n'
                    'HOSTS=(\n  "monitor01=10.0.0.1"\n)\n'
                ),
            )

            self.assertIn("ROOK_NAMESPACE", result.stderr)


class RookUnavailableTests(RookFixture, unittest.TestCase):
    """Collecting no Rook evidence is a partial bundle naming its cause."""

    def make_environment_without_kubectl(self, root: Path) -> dict[str, str]:
        """A PATH with the node transport and packaging tool but no kubectl."""

        toolbox = root / "toolbox"
        toolbox.mkdir()
        (toolbox / "ssh").symlink_to(FIXTURE_BIN / "ssh")
        real_tar = shutil.which("tar")
        self.assertIsNotNone(real_tar)
        (toolbox / "tar").symlink_to(str(real_tar))
        environment = {
            **os.environ,
            "PATH": str(toolbox),
            "FAKE_SSH_LOG": str(root / "ssh-argv.jsonl"),
        }
        self.assertIsNone(shutil.which("kubectl", path=environment["PATH"]))
        return environment

    def test_missing_local_kubectl_is_a_partial_bundle_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment_without_kubectl(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = contents["cluster/rook/SKIPPED.txt"]
            self.assertIn("SKIPPED", skipped)
            self.assertIn("kubectl command not found", skipped)
            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertNotIn(artifact, contents)
            self.assertIn("cluster collection exited 2", contents["errors.log"])
            self.assertIn("cluster_status: 2", contents["summary.txt"])
            self.assertIn("final_status: 2", contents["summary.txt"])
            self.assert_bundle_verifies(bundle)

    def test_missing_remote_kubectl_is_classified_from_the_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_SSH_MISSING_KUBECTL="1"
            )

            result = self.run_collect(
                root, environment, extra_arguments=("--kube-mode", "remote")
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            skipped = contents["cluster/rook/SKIPPED.txt"]
            self.assertIn("no kubectl-capable node found", skipped)

    def assert_probe_failure(
        self, mode: str, expected: str, kube_context: str = ""
    ) -> str:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_KUBECTL_MODE=mode
            )
            context_prefix = ["--context", kube_context] if kube_context else []

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--kube-mode",
                    "local",
                    *(("--kube-context", kube_context) if kube_context else ()),
                ),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = contents["cluster/rook/SKIPPED.txt"]
            self.assertIn(expected, skipped)
            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertNotIn(artifact, contents)
            # A failed probe must not be followed by further kubectl commands.
            self.assertEqual(
                self.kubectl_commands(kubectl_ledger),
                [[*context_prefix, "get", "namespace", "rook-ceph"]],
            )
            self.assert_bundle_verifies(bundle)
            return skipped

    def test_missing_namespace_is_reported_with_the_raw_kubectl_error(self) -> None:
        skipped = self.assert_probe_failure(
            "missing-namespace", "rook namespace not found: rook-ceph"
        )
        self.assertIn('namespaces "rook-ceph" not found', skipped)

    def test_missing_context_is_reported_with_the_raw_kubectl_error(self) -> None:
        skipped = self.assert_probe_failure(
            "context-missing",
            "kubectl context not found: lab on local",
            kube_context="lab",
        )
        self.assertIn("no context exists", skipped)

    def test_unreachable_api_server_is_reported_with_the_raw_kubectl_error(
        self,
    ) -> None:
        skipped = self.assert_probe_failure(
            "connection-refused", "kubectl cannot connect to cluster API from local"
        )
        self.assertIn("was refused", skipped)

    def test_authorization_failure_is_reported_with_the_raw_kubectl_error(self) -> None:
        skipped = self.assert_probe_failure(
            "forbidden",
            "kubectl cannot read rook namespace due to authorization failure",
        )
        self.assertIn("Forbidden", skipped)


class RookPartialCollectionTests(RookFixture, unittest.TestCase):
    def test_failed_required_artifact_is_partial_and_keeps_other_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_KUBECTL_FAIL_ON="get events"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)

            # The failing command's output is kept as evidence…
            self.assertIn(
                "simulated kubectl failure for get events",
                contents["cluster/rook/events.txt"],
            )
            # …and every other artifact is still collected.
            self.assertIn("rook-ceph-mon-a", contents["cluster/rook/pods-wide.txt"])
            self.assertIn(
                "kind: CephCluster", contents["cluster/rook/rook-resources.yaml"]
            )
            self.assertIn("operator log", contents["cluster/rook/operator.log"])

            entries = self.rook_entries(contents)
            self.assertEqual(entries["events.txt"]["exit_code"], 17)
            self.assertEqual(entries["pods-wide.txt"]["exit_code"], 0)
            self.assertEqual(entries["rook-resources.yaml"]["exit_code"], 0)
            self.assertEqual(entries["operator.log"]["exit_code"], 0)
            for entry in entries.values():
                self.assertEqual(entry["host"], "rook")

            errors = contents["errors.log"]
            self.assertIn("exit=17", errors)
            self.assertIn("events.txt", errors)
            self.assertIn("cluster collection exited 2", errors)
            self.assertIn("cluster_status: 2", contents["summary.txt"])
            self.assertIn("final_status: 2", contents["summary.txt"])
            self.assert_bundle_verifies(bundle)

    def test_rook_partial_does_not_hide_a_successful_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_KUBECTL_FAIL_ON="get pods -n rook-ceph -o wide"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("nodes/monitor01/system/hostname.txt", contents)
            self.assertIn("node_ok: 1", contents["summary.txt"])
            self.assertIn("node_failed: 0", contents["summary.txt"])


class RookTimeoutTests(RookFixture, unittest.TestCase):
    def test_timed_out_artifact_is_truncated_and_makes_the_layer_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root,
                FAKE_KUBECTL_SLEEP_ON="get events",
                FAKE_KUBECTL_SLEEP_SECONDS="30",
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--timeout", "2"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            events = contents["cluster/rook/events.txt"]
            self.assertIn("# timeout: 2s", events)
            self.assertIn("# TRUNCATED", events)
            self.assertIn("exit 124", events)
            self.assertEqual(
                self.rook_entries(contents)["events.txt"]["exit_code"], 124
            )
            self.assertIn("cluster collection exited 2", contents["errors.log"])
            self.assert_bundle_verifies(bundle)

    def test_timed_out_namespace_probe_is_a_skip_not_a_hang(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root,
                FAKE_KUBECTL_SLEEP_ON="get namespace",
                FAKE_KUBECTL_SLEEP_SECONDS="30",
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--timeout", "2"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = contents["cluster/rook/SKIPPED.txt"]
            self.assertIn("SKIPPED", skipped)
            self.assertIn("probe timed out after 2s", skipped)
            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertNotIn(artifact, contents)
            self.assert_bundle_verifies(bundle)


class RookOptionalArtifactTests(RookFixture, unittest.TestCase):
    def test_operator_pod_lookup_failure_skips_only_the_operator_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_KUBECTL_MODE="op-lookup-fail"
            )

            result = self.run_collect(root, environment)

            # An optional lookup failure must not make the collect partial.
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = contents["cluster/rook/operator-SKIPPED.txt"]
            self.assertIn("SKIPPED", skipped)
            self.assertIn("operator Pod not found", skipped)
            self.assertIn("rook-ceph", skipped)
            self.assertNotIn("cluster/rook/operator.log", contents)
            for artifact in REQUIRED_ROOK_ARTIFACTS:
                self.assertIn(artifact, contents)
            # No `logs` command may be issued without a Pod to read.
            for arguments in self.kubectl_commands(kubectl_ledger):
                self.assertNotEqual(arguments[:1], ["logs"])
            self.assert_bundle_verifies(bundle)

    def test_an_option_shaped_operator_pod_name_is_not_used_as_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, FAKE_KUBECTL_MODE="op-unsafe-name"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "operator Pod not found",
                contents["cluster/rook/operator-SKIPPED.txt"],
            )
            self.assertNotIn("cluster/rook/operator.log", contents)
            for arguments in self.kubectl_commands(kubectl_ledger):
                self.assertNotIn("--as=system:admin", arguments)

    def test_operator_log_uses_the_since_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--since", "6h"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("since 6h", contents["cluster/rook/operator.log"])
            self.assertIn(
                [
                    "logs",
                    "-n",
                    "rook-ceph",
                    "rook-ceph-operator-rook-ceph-abc123",
                    "--since=6h",
                ],
                self.kubectl_commands(kubectl_ledger),
            )


class KubectlExecIsNeverEnabledTests(RookFixture, unittest.TestCase):
    """`kubectl exec` creates a process in a Pod, so no path may reach it."""

    def test_toolbox_is_skipped_and_no_exec_is_ever_issued(self) -> None:
        for mode in ("local", "remote"):
            with self.subTest(kube_mode=mode):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, kubectl_ledger, ssh_ledger = (
                        self.make_fake_environment(root)
                    )

                    result = self.run_collect(
                        root, environment, extra_arguments=("--kube-mode", mode)
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    skipped = contents["cluster/rook/toolbox-SKIPPED.txt"]
                    self.assertIn("SKIPPED", skipped)
                    self.assertIn("kubectl exec disabled", skipped)
                    self.assertNotIn("cluster/rook/toolbox-status.txt", contents)
                    for arguments in self.kubectl_commands(kubectl_ledger):
                        self.assertNotIn("exec", arguments)
                    for arguments in self.ledger(ssh_ledger):
                        self.assertNotIn("exec", arguments)

    def test_an_opt_in_flag_for_kubectl_exec_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--kube-mode", "local", "--allow-kubectl-exec"),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("--allow-kubectl-exec", result.stderr)
            self.assertFalse(kubectl_ledger.exists())

    def test_toolbox_lookup_preserves_the_shell_read_only_command_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                [
                    "get",
                    "pods",
                    "-n",
                    "rook-ceph",
                    "-l",
                    "app=rook-ceph-tools",
                    "-o",
                    "name",
                ],
                self.kubectl_commands(kubectl_ledger),
            )


class InheritedKubeconfigTests(RookFixture, unittest.TestCase):
    """Kubeconfig selection stays inherited; only the context is chosen by flag."""

    def test_local_kubectl_inherits_the_workstation_kubeconfig(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kubeconfig = root / "lab.kubeconfig"
            kubeconfig.write_text("fixture kubeconfig path only\n", encoding="utf-8")
            environment, kubectl_ledger, ssh_ledger = self.make_fake_environment(
                root, KUBECONFIG=str(kubeconfig)
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                f"# kubeconfig={kubeconfig}", contents["cluster/rook/pods-wide.txt"]
            )
            # No invented flag: the path is never passed on the argv.
            for arguments in self.kubectl_commands(kubectl_ledger):
                self.assertNotIn("--kubeconfig", arguments)
                self.assertNotIn(str(kubeconfig), arguments)

    def test_no_runner_ever_injects_a_kubeconfig_path(self) -> None:
        """Over ssh the node's own kubeconfig applies, so nothing is forwarded."""

        for mode in ("local", "remote"):
            with self.subTest(kube_mode=mode):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, kubectl_ledger, ssh_ledger = (
                        self.make_fake_environment(root)
                    )

                    result = self.run_collect(
                        root, environment, extra_arguments=("--kube-mode", mode)
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    for arguments in self.kubectl_commands(kubectl_ledger):
                        self.assertNotIn("--kubeconfig", arguments)
                    for arguments in self.ledger(ssh_ledger):
                        self.assertNotIn("--kubeconfig", arguments)
                        self.assertNotIn("KUBECONFIG", " ".join(arguments))


class FakeKubectlArgvContractTests(unittest.TestCase):
    """The offline proof rests on the fake adapter answering only exact argv."""

    def run_fake_kubectl(
        self, arguments: list[str]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(FIXTURE_BIN / "kubectl"), *arguments],
            capture_output=True,
            check=False,
        )

    def assert_rejected(self, arguments: list[str]) -> None:
        completed = self.run_fake_kubectl(arguments)
        self.assertEqual(completed.returncode, 99, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"unexpected kubectl command", completed.stderr)

    def test_whitelisted_read_commands_are_answered(self) -> None:
        for words in (
            ["get", "namespace", "rook-ceph"],
            ["get", "pods", "-n", "rook-ceph", "-o", "wide"],
            ["get", "events", "-n", "rook-ceph", "--sort-by=.lastTimestamp"],
            ["get", "pods", "-n", "rook-ceph", "-l", "app=rook-ceph-operator", "-o", "name"],
        ):
            with self.subTest(words=words):
                completed = self.run_fake_kubectl(words)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(completed.stdout)
                completed = self.run_fake_kubectl(["--context", "lab", *words])
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_kubectl_exec_is_never_answered(self) -> None:
        self.assert_rejected(
            ["exec", "-n", "rook-ceph", "rook-ceph-tools-abc", "--", "ceph", "status"]
        )
        self.assert_rejected(
            ["--context", "lab", "exec", "-n", "rook-ceph", "pod", "--", "ceph", "-s"]
        )

    def test_mutating_verbs_are_never_answered(self) -> None:
        for words in (
            ["delete", "pod", "-n", "rook-ceph", "rook-ceph-mon-a"],
            ["apply", "-f", "cluster.yaml"],
            ["patch", "cephcluster", "rook-ceph", "-p", "{}"],
            ["scale", "deployment", "rook-ceph-operator", "--replicas=0"],
            ["rollout", "restart", "deployment/rook-ceph-operator"],
            ["cordon", "node-1"],
            ["drain", "node-1"],
            ["port-forward", "svc/rook-ceph-mgr", "8080:8080"],
            ["debug", "node/node-1"],
            ["edit", "cephcluster", "rook-ceph"],
        ):
            with self.subTest(words=words):
                self.assert_rejected(words)

    def test_tokens_appended_to_a_whitelisted_command_fail_closed(self) -> None:
        accepted = ["get", "pods", "-n", "rook-ceph", "-o", "wide"]
        for appended in (
            ["--as", "system:admin"],
            ["--kubeconfig", "/tmp/other.kubeconfig"],
            [";", "kubectl", "delete", "pod", "x"],
            ["-o", "yaml"],
        ):
            with self.subTest(appended=appended):
                self.assert_rejected([*accepted, *appended])

    def test_reading_non_rook_resources_fails_closed(self) -> None:
        for words in (
            ["get", "secrets", "-n", "rook-ceph", "-o", "yaml"],
            ["get", "configmaps", "-n", "rook-ceph", "-o", "yaml"],
            ["get", "nodes", "-o", "wide"],
        ):
            with self.subTest(words=words):
                self.assert_rejected(words)

    def test_a_simulated_failure_knob_cannot_widen_the_whitelist(self) -> None:
        completed = subprocess.run(
            [str(FIXTURE_BIN / "kubectl"), "exec", "-n", "rook-ceph", "pod"],
            capture_output=True,
            check=False,
            env={**os.environ, "FAKE_KUBECTL_FAIL_ON": "exec"},
        )
        self.assertEqual(completed.returncode, 99, completed.stderr)
        self.assertIn(b"unexpected kubectl command", completed.stderr)


class FakeSshArgvContractTests(unittest.TestCase):
    """The remote runner's transport must answer only two exact remote shapes."""

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

    def test_remote_read_only_kubectl_is_answered(self) -> None:
        completed = self.run_fake_ssh(
            [
                *self.base_options(),
                NODE_TARGET,
                "kubectl",
                "get",
                "pods",
                "-n",
                "rook-ceph",
                "-o",
                "wide",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(b"rook-ceph-mon-a", completed.stdout)

    def test_remote_kubectl_inherits_the_kubectl_whitelist(self) -> None:
        completed = self.run_fake_ssh(
            [
                *self.base_options(),
                NODE_TARGET,
                "kubectl",
                "exec",
                "-n",
                "rook-ceph",
                "pod",
                "--",
                "ceph",
                "status",
            ]
        )
        self.assertEqual(completed.returncode, 99, completed.stderr)
        self.assertIn(b"unexpected kubectl command", completed.stderr)

    def test_remote_commands_other_than_kubectl_fail_closed(self) -> None:
        for remote in (
            ["ceph", "status"],
            ["sudo", "-n", "kubectl", "get", "pods"],
            ["sh", "-c", "kubectl get pods"],
            ["kubectl get pods -n rook-ceph -o wide"],
        ):
            with self.subTest(remote=remote):
                self.assert_rejected([*self.base_options(), NODE_TARGET, *remote])

    def test_argv_outside_the_shared_option_vector_fails_closed(self) -> None:
        self.assert_rejected([NODE_TARGET, "kubectl", "get", "namespace", "rook-ceph"])
        self.assert_rejected(
            [
                *self.base_options(),
                "-o",
                "ProxyCommand=touch /tmp/pwned",
                NODE_TARGET,
                "kubectl",
                "get",
                "namespace",
                "rook-ceph",
            ]
        )
        tampered = self.base_options()
        tampered[tampered.index("BatchMode=yes")] = "BatchMode=no"
        self.assert_rejected(
            [*tampered, NODE_TARGET, "kubectl", "get", "namespace", "rook-ceph"]
        )

    def test_bootstrap_source_other_than_the_pinned_one_fails_closed(self) -> None:
        accepted = _ssh_command(
            Path("/tmp/fixture-key"), NODE_TARGET, 5, None, "Zm9vYmFy"
        )[-1]
        completed = self.run_fake_ssh([*self.base_options(), NODE_TARGET, accepted])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith(b"\x1f\x8b"), completed.stdout[:8])

        for label, source in (
            ("mutating", "import shutil\nshutil.rmtree('/var/log')\n"),
            ("smuggled-suffix", REMOTE_BOOTSTRAP + "import os\nos.remove('/etc/fstab')\n"),
            ("empty", ""),
        ):
            with self.subTest(source=label):
                tampered = shlex.join(["python3", "-c", source, "Zm9vYmFy"])
                self.assert_rejected([*self.base_options(), NODE_TARGET, tampered])


if __name__ == "__main__":
    unittest.main()
