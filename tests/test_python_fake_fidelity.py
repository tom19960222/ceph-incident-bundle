from __future__ import annotations

import os
import subprocess
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
    ROOT / "tests/differential/fakes/ssh",
    ROOT / "tests/fixtures/lab/bin/ssh",
)


class FakeSshStdinContractTests(unittest.TestCase):
    """The fake transports model OpenSSH's unconditional stdin forwarding."""

    def test_every_fake_consumes_stdin_before_it_dispatches_an_invocation(self) -> None:
        payload = b"fixture-fidelity-probe\n"

        for fake_ssh in FAKE_SSH_ENTRYPOINTS:
            with self.subTest(fake_ssh=fake_ssh.relative_to(ROOT)):
                read_fd, write_fd = os.pipe()
                observer_fd = os.dup(read_fd)
                try:
                    os.write(write_fd, payload)
                    os.close(write_fd)
                    write_fd = -1
                    subprocess.run(
                        [str(fake_ssh), "fixture-fidelity-rejected-invocation"],
                        cwd=ROOT,
                        env=os.environ.copy(),
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
