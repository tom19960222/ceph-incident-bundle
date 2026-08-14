from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect import run


INVENTORY = b"""\
[common]
ssh_user = root
[nodes]
node-a = node-a.example.test
"""


class TopLevelCollectionTests(unittest.TestCase):
    def test_unexpected_node_exception_still_publishes_truthful_partial_bundle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "ceph_incident_bundle.collect.collect_node",
                    side_effect=RuntimeError("receive failed\nsecond line"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundles = list(root.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root_name = bundles[0].name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
                node_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/nodes/")
                ]

        self.assertEqual(status, 0)
        self.assertEqual(outcome, "partial")
        self.assertEqual(node_members, [])
        self.assertEqual(stdout.getvalue(), f"{bundles[0].resolve()} (partial)\n")
        self.assertIn("receive failed\\x0asecond line", stderr.getvalue())
        self.assertNotIn("FAIL: no Incident Bundle delivered", stderr.getvalue())

    def test_invalid_startup_never_reaches_the_ssh_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            missing_output = root / "missing"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node") as node_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "yesterday", missing_output)

        self.assertNotEqual(status, 0)
        node_operation.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid evidence window", stderr.getvalue())
        self.assertIn("cannot use output directory", stderr.getvalue())
        self.assertTrue(
            stderr.getvalue().endswith("FAIL: no Incident Bundle delivered\n")
        )


if __name__ == "__main__":
    unittest.main()
