from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fixture_product import node_bundle_member, node_manifest_artifact


ROOT = Path(__file__).resolve().parents[1]
FAKE_SSH_ENTRYPOINTS = (
    ROOT / "tests/fixtures/bin/ssh",
    ROOT / "tests/fixtures/python-node/bin/ssh",
    ROOT / "tests/fixtures/python-ceph/bin/ssh",
    ROOT / "tests/fixtures/python-prometheus/bin/ssh",
    ROOT / "tests/fixtures/python-rook/bin/ssh",
    ROOT / "tests/fixtures/lab/bin/ssh",
)
REJECT_EXIT_CODES = {
    ROOT / "tests/fixtures/bin/ssh": 99,
    ROOT / "tests/fixtures/python-node/bin/ssh": 127,
    ROOT / "tests/fixtures/python-ceph/bin/ssh": 99,
    ROOT / "tests/fixtures/python-prometheus/bin/ssh": 99,
    ROOT / "tests/fixtures/python-rook/bin/ssh": 99,
    ROOT / "tests/fixtures/lab/bin/ssh": 99,
}


class FakeSshStdinContractTests(unittest.TestCase):
    """The fake transports model OpenSSH's unconditional stdin forwarding."""

    def assert_consumes_stdin(
        self, fake_ssh: Path, *arguments: str, environment: dict[str, str] | None = None
    ) -> None:
        payload = b"fixture-fidelity-probe\n"
        read_fd, write_fd = os.pipe()
        observer_fd = os.dup(read_fd)
        try:
            os.write(write_fd, payload)
            os.close(write_fd)
            write_fd = -1
            subprocess.run(
                [str(fake_ssh), *arguments],
                cwd=ROOT,
                env={**os.environ, **(environment or {})},
                stdin=read_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
            self.assertEqual(
                os.read(observer_fd, len(payload) + 1),
                b"",
                f"{fake_ssh.relative_to(ROOT)} left stdin unread",
            )
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)
            os.close(observer_fd)

    def test_every_fake_consumes_stdin_before_it_dispatches_an_invocation(self) -> None:
        for fake_ssh in FAKE_SSH_ENTRYPOINTS:
            with self.subTest(fake_ssh=fake_ssh.relative_to(ROOT)):
                self.assert_consumes_stdin(
                    fake_ssh, "fixture-fidelity-rejected-invocation"
                )

    def test_python_node_timeout_route_consumes_stdin_even_if_remote_does_not(self) -> None:
        self.assert_consumes_stdin(
            ROOT / "tests/fixtures/python-node/bin/ssh",
            "exit 23",
            environment={"FAKE_SSH_MODE": "timeout"},
        )


class FakeSshProcessContractTests(unittest.TestCase):
    def test_rejected_invocations_preserve_exit_and_stream_separation(self) -> None:
        for fake_ssh, expected_exit in REJECT_EXIT_CODES.items():
            with self.subTest(fake_ssh=fake_ssh.relative_to(ROOT)):
                result = subprocess.run(
                    [str(fake_ssh), "fixture-fidelity-rejected-invocation"],
                    cwd=ROOT,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, expected_exit)
                self.assertEqual(result.stdout, b"")
                self.assertNotEqual(result.stderr, b"")

    def test_argv_ledgers_preserve_argument_boundaries(self) -> None:
        arguments = ("fixture token with spaces", "tail")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for fake_ssh in FAKE_SSH_ENTRYPOINTS:
                with self.subTest(fake_ssh=fake_ssh.relative_to(ROOT)):
                    ledger = root / f"{fake_ssh.parent.parent.name}-{fake_ssh.parent.name}"
                    environment = os.environ.copy()
                    if fake_ssh == ROOT / "tests/fixtures/bin/ssh":
                        environment["FAKE_SSH_ARGV_NUL_LOG"] = str(ledger)
                    elif fake_ssh == ROOT / "tests/fixtures/lab/bin/ssh":
                        environment["FAKE_LAB_SSH_LOG"] = str(ledger)
                    else:
                        environment["FAKE_SSH_LOG"] = str(ledger)

                    subprocess.run(
                        [str(fake_ssh), *arguments],
                        cwd=ROOT,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=5,
                    )
                    if fake_ssh == ROOT / "tests/fixtures/bin/ssh":
                        recorded = tuple(ledger.read_bytes().split(b"\0")[:-1])
                        self.assertEqual(recorded, tuple(arg.encode() for arg in arguments))
                    else:
                        records = ledger.read_text(encoding="utf-8").splitlines()
                        self.assertEqual(tuple(json.loads(records[-1])), arguments)


class NodeProductShapeContractTests(unittest.TestCase):
    def test_manifest_and_bundle_paths_share_one_relative_artifact(self) -> None:
        self.assertEqual(
            node_manifest_artifact("system/hostname.txt"),
            "/tmp/ceph-incident-node.fixture/out/system/hostname.txt",
        )
        self.assertEqual(
            node_bundle_member("monitor01", "system/hostname.txt"),
            "nodes/monitor01/system/hostname.txt",
        )


if __name__ == "__main__":
    unittest.main()
