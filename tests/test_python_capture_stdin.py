"""No capture, on either side, may read the stdin its caller was holding.

The shell reference lost seven of nine `ceph crash info` ids because a captured
`ssh` drained the herestring its caller's loop was still iterating (#52).  The
shell fix is pinned by `tests/test-common.sh`; these are the Python candidate's
mirrors, one per capture seam — `run_capture` on the workstation and
`_write_artifact` on the node.  Each feeds the test process's own fd 0 from a
pipe, captures a command that would happily eat it (`cat`), and then requires
both that the artifact stayed clean and that the pipe still holds every byte.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ceph_incident_collectors import run_capture  # noqa: E402
from ceph_incident_node import _write_artifact  # noqa: E402

SENTINEL = b"one\ntwo\nthree\n"


class CaptureStdinIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def feed_own_stdin(self) -> int:
        """Point this process's fd 0 at a pipe holding the sentinel.

        Returns the pipe's read end.  Because `os.dup2` shares the open file
        description, a captured child that inherits fd 0 and reads it moves the
        shared offset — so whatever the child ate is exactly what a later
        `os.read` of the returned end no longer sees.
        """

        read_end, write_end = os.pipe()
        os.write(write_end, SENTINEL)
        os.close(write_end)
        saved = os.dup(0)
        os.dup2(read_end, 0)

        def restore() -> None:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(read_end)

        self.addCleanup(restore)
        return read_end

    def test_the_workstation_capture_cannot_read_the_callers_stdin(self) -> None:
        read_end = self.feed_own_stdin()
        artifact = self.root / "capture.txt"
        code = run_capture(
            manifest=self.root / "manifest.jsonl",
            errors_log=None,
            host="host-a",
            collector="collector-a",
            artifact=artifact,
            command=["cat"],
            timeout=5,
        )
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, artifact.read_bytes())
        self.assertEqual(os.read(read_end, len(SENTINEL) + 1), SENTINEL)

    def test_the_node_capture_cannot_read_the_callers_stdin(self) -> None:
        read_end = self.feed_own_stdin()
        output = self.root / "out"
        output.mkdir()
        manifest = self.root / "manifest.jsonl"
        # The node collector creates its manifest before any capture; the append
        # helper stats it to enforce the receiver safety cap.
        manifest.touch()
        ok = _write_artifact(
            output,
            manifest,
            "node-a",
            5,
            "system/capture.txt",
            ["cat"],
        )
        self.assertTrue(ok)
        self.assertNotIn(SENTINEL, (output / "system/capture.txt").read_bytes())
        self.assertEqual(os.read(read_end, len(SENTINEL) + 1), SENTINEL)


if __name__ == "__main__":
    unittest.main()
