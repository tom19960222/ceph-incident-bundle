import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect.kubernetes import collect_kubernetes


PODS = {
    "apiVersion": "v1",
    "items": [
        {
            "metadata": {"name": "rook-pod", "namespace": "rook-consumer"},
            "spec": {
                "containers": [{"name": "main"}],
                "initContainers": [{"name": "init"}],
                "ephemeralContainers": [{"name": "debugger"}],
            },
            "status": {
                "containerStatuses": [{"name": "main", "restartCount": 2}],
                "initContainerStatuses": [{"name": "init", "restartCount": 0}],
                "ephemeralContainerStatuses": [
                    {"name": "debugger", "restartCount": 1}
                ],
            },
        }
    ],
}


class KubernetesCollectionTests(unittest.TestCase):
    def test_fixed_get_catalog_is_captured_in_order_with_explicit_scope(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
            )

            argv = self._read_record(record)
            staging_exists = staging.exists()
            admitted_is_directory = admitted.is_dir()
            consumer_json_bytes = (
                admitted / "probes" / "consumer-pods-json" / "stdout"
            ).read_bytes()
            result = json.loads(
                (
                    admitted / "probes" / "consumer-pods-json" / "result.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(problems, [])
        self.assertEqual(
            argv,
            [
                [
                    "--context=lab-context",
                    "--namespace=rook-consumer",
                    "get",
                    "pods",
                    "--output=wide",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-consumer",
                    "get",
                    "events",
                    "--sort-by=.lastTimestamp",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-consumer",
                    "get",
                    "cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,"
                    "cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io",
                    "--output=yaml",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-consumer",
                    "get",
                    "pods",
                    "--output=json",
                ],
                [
                    "--context=lab-context",
                    "--namespace=rook-operator",
                    "get",
                    "pods",
                    "--output=json",
                ],
            ],
        )
        self.assertFalse(staging_exists)
        self.assertTrue(admitted_is_directory)
        self.assertEqual(consumer_json_bytes, json.dumps(PODS).encode("utf-8"))
        self.assertEqual(
            set(result),
            {"argv", "started_at", "finished_at", "outcome", "exit_code", "error"},
        )
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])
        self.assertEqual(
            result["argv"],
            [
                "kubectl",
                "--context=lab-context",
                "--namespace=rook-consumer",
                "get",
                "pods",
                "--output=json",
            ],
        )

    def test_equal_namespaces_use_one_pods_json_control_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-shared",
                operator_namespace="rook-shared",
            )

            argv = self._read_record(record)
            probe_names = [path.name for path in (admitted / "probes").iterdir()]

        self.assertEqual(problems, [])
        self.assertEqual(len(argv), 4)
        self.assertEqual(
            set(probe_names),
            {
                "consumer-pods-wide",
                "consumer-events",
                "consumer-rook-resources-yaml",
                "consumer-pods-json",
            },
        )
        self.assertNotIn("operator-pods-json", probe_names)

    def test_failed_and_unparseable_controls_do_not_stop_independent_probes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
                extra_environment={
                    "FAKE_KUBECTL_FAIL_EVENTS": "1",
                    "FAKE_KUBECTL_INVALID_CONSUMER_JSON": "1",
                },
            )

            argv = self._read_record(record)
            operator_result_exists = (
                admitted / "probes" / "operator-pods-json" / "result.json"
            ).is_file()
            invalid_json_bytes = (
                admitted / "probes" / "consumer-pods-json" / "stdout"
            ).read_bytes()

        self.assertEqual(len(argv), 5)
        self.assertEqual(len(problems), 2)
        self.assertIn("consumer-events Probe failed", problems[0])
        self.assertIn("exit_code=7", problems[0])
        self.assertIn("consumer-pods-json control response is not valid JSON", problems[1])
        self.assertTrue(operator_result_exists)
        self.assertEqual(invalid_json_bytes, b"{not-json")

    def test_timeout_kills_stubborn_group_preserves_partial_bytes_and_continues(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"
            timed_out_pid = root / "timed-out.pid"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-consumer",
                timeout=1,
                extra_environment={
                    "FAKE_KUBECTL_TIMEOUT_WIDE": "1",
                    "FAKE_KUBECTL_STUBBORN_PID": str(timed_out_pid),
                },
            )

            argv = self._read_record(record)
            capture = admitted / "probes" / "consumer-pods-wide"
            result = json.loads((capture / "result.json").read_text(encoding="utf-8"))
            stdout_bytes = (capture / "stdout").read_bytes()
            stderr_bytes = (capture / "stderr").read_bytes()
            stubborn_pid = int(timed_out_pid.read_text(encoding="ascii"))
            stubborn_child_pid = int(
                timed_out_pid.with_suffix(".child").read_text(encoding="ascii")
            )

        self.assertEqual(len(argv), 4)
        self.assertEqual(argv[1][-2:], ["events", "--sort-by=.lastTimestamp"])
        self.assertEqual(stdout_bytes, b"partial stdout")
        self.assertEqual(stderr_bytes, b"partial stderr")
        self.assertEqual(result["outcome"], "timed_out")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["error"]["kind"], "timeout")
        self.assertEqual(len(problems), 1)
        self.assertIn("consumer-pods-wide Probe failed: outcome=timed_out", problems[0])
        with self.assertRaises(ProcessLookupError):
            os.kill(stubborn_pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(stubborn_child_pid, 0)

    def test_existing_admitted_destination_is_never_replaced(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.mkdir(parents=True)
            sentinel = admitted / "sentinel"
            sentinel.write_bytes(b"unchanged")
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
            )
            sentinel_bytes = sentinel.read_bytes()
            recorded_argv = self._read_record(record)
            staging_exists = staging.exists()

        self.assertEqual(sentinel_bytes, b"unchanged")
        self.assertEqual(recorded_argv, [])
        self.assertFalse(staging_exists)
        self.assertEqual(len(problems), 1)
        self.assertIn("admitted Kubernetes contribution already exists", problems[0])

    def test_failed_atomic_promotion_keeps_private_state_out_of_admitted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            with patch(
                "ceph_incident_bundle.collect.kubernetes.os.rename",
                side_effect=OSError("injected promotion failure"),
            ):
                problems = self._collect(
                    staging,
                    admitted,
                    fake_bin=fake_bin,
                    record=record,
                    consumer_namespace="rook-consumer",
                    operator_namespace="rook-operator",
                )

            staging_exists = staging.exists()
            admitted_exists = admitted.exists()

        self.assertTrue(staging_exists)
        self.assertFalse(admitted_exists)
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot atomically promote complete contribution", problems[0])
        self.assertIn(f"private residue: {staging}", problems[0])

    def test_capture_write_failure_keeps_private_state_out_of_admitted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"
            capture = staging / "contribution" / "probes" / "consumer-pods-wide"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
                extra_environment={"FAKE_KUBECTL_LOCK_CAPTURE": str(capture)},
            )
            admitted_exists = admitted.exists()
            staging_exists = staging.exists()
            capture.chmod(0o700)

        self.assertFalse(admitted_exists)
        self.assertTrue(staging_exists)
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot preserve consumer-pods-wide Probe", problems[0])
        self.assertIn(f"private residue: {staging}", problems[0])

    def test_missing_private_parent_fails_before_kubectl_or_admission(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kubectl(fake_bin / "kubectl")
            staging = root / "missing-private" / "kubernetes"
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=fake_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
            )
            admitted_exists = admitted.exists()
            recorded_argv = self._read_record(record)

        self.assertFalse(admitted_exists)
        self.assertEqual(recorded_argv, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot validate private contribution boundaries", problems[0])

    def test_missing_kubectl_records_each_failed_start_and_promotes_captures(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            empty_bin = root / "bin"
            empty_bin.mkdir()
            staging = root / "private" / "kubernetes"
            staging.parent.mkdir()
            admitted = root / "admitted" / "kubernetes"
            admitted.parent.mkdir()
            record = root / "kubectl.jsonl"

            problems = self._collect(
                staging,
                admitted,
                fake_bin=empty_bin,
                record=record,
                consumer_namespace="rook-consumer",
                operator_namespace="rook-operator",
                extra_environment={"PATH": str(empty_bin)},
            )
            results = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((admitted / "probes").glob("*/result.json"))
            ]

        self.assertEqual(len(problems), 5)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result["outcome"] == "failed_to_start" for result in results))
        self.assertTrue(all(result["exit_code"] is None for result in results))
        self.assertTrue(all(result["error"]["kind"] == "FileNotFoundError" for result in results))

    def _collect(
        self,
        staging: Path,
        admitted: Path,
        *,
        fake_bin: Path,
        record: Path,
        consumer_namespace: str,
        operator_namespace: str,
        timeout: int = 5,
        extra_environment: dict[str, str] | None = None,
    ) -> list[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["FAKE_KUBECTL_RECORD"] = str(record)
        if extra_environment:
            environment.update(extra_environment)
        with patch.dict(os.environ, environment, clear=True):
            return collect_kubernetes(
                context="lab-context",
                consumer_namespace=consumer_namespace,
                operator_namespace=operator_namespace,
                probe_timeout_seconds=timeout,
                staging_directory=staging,
                contribution_directory=admitted,
            )

    @staticmethod
    def _read_record(path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _write_fake_kubectl(path: Path) -> None:
        path.write_text(
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys
import time

record = Path(os.environ["FAKE_KUBECTL_RECORD"])
record_descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try:
    os.write(record_descriptor, (json.dumps(sys.argv[1:]) + "\\n").encode("utf-8"))
    os.fsync(record_descriptor)
finally:
    os.close(record_descriptor)
args = sys.argv[1:]
locked_capture = os.environ.get("FAKE_KUBECTL_LOCK_CAPTURE")
if locked_capture:
    Path(locked_capture).chmod(0o500)
if os.environ.get("FAKE_KUBECTL_TIMEOUT_WIDE") and args[-2:] == ["pods", "--output=wide"]:
    stubborn_pid = os.environ.get("FAKE_KUBECTL_STUBBORN_PID")
    if stubborn_pid:
        child_pid_path = str(Path(stubborn_pid).with_suffix(".child"))
        child_code = (
            "import os, signal, sys, time; "
            "from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); "
            "time.sleep(10)"
        )
        subprocess.Popen([sys.executable, "-c", child_code, child_pid_path])
        deadline = time.monotonic() + 5
        while not Path(child_pid_path).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("stubborn child did not become ready")
            time.sleep(0.01)
        Path(stubborn_pid).write_text(str(os.getpid()), encoding="ascii")
    sys.stdout.write("partial stdout")
    sys.stdout.flush()
    sys.stderr.write("partial stderr")
    sys.stderr.flush()
    time.sleep(10)
if os.environ.get("FAKE_KUBECTL_FAIL_EVENTS") and "events" in args:
    sys.stderr.write("events unavailable")
    raise SystemExit(7)
if args[-2:] == ["pods", "--output=json"]:
    if os.environ.get("FAKE_KUBECTL_INVALID_CONSUMER_JSON") and "--namespace=rook-consumer" in args:
        sys.stdout.write("{{not-json")
    else:
        sys.stdout.write({json.dumps(json.dumps(PODS))})
elif args[-2:] == ["pods", "--output=wide"]:
    sys.stdout.buffer.write(b"wide\\x00bytes")
elif "events" in args:
    sys.stdout.write("events")
else:
    sys.stdout.write("rook yaml")
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
