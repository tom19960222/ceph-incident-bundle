"""The shared inventory both qualification collects receive."""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.lab_fixture import FakeLab
from validation.lab_inventory import (
    render_inventory,
    write_inventory,
    write_known_hosts_home,
)
from validation.lab_profile import load_profile


ROOT = Path(__file__).resolve().parents[1]


class InventoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.profile = load_profile(self.lab.write_profile())


class RenderTests(InventoryTestCase):
    def test_describes_exactly_the_profiles_lab(self) -> None:
        text = render_inventory(self.profile)
        self.assertIn('SSH_USER="operator"', text)
        self.assertIn('SEED_HOST="10.0.0.11"', text)
        self.assertIn('ROOK_NAMESPACE="rook-ceph"', text)
        self.assertIn('ROOK_OPERATOR_NAMESPACE="rook-ceph"', text)
        self.assertIn('  "monitor01=10.0.0.11"', text)
        self.assertIn('  "osd01=10.0.0.21"', text)

    def test_names_the_profile_it_was_derived_from(self) -> None:
        text = render_inventory(self.profile)
        self.assertIn(self.profile.profile_hash, text)
        self.assertIn("do not edit", text)

    def test_the_seed_is_the_profiles_seed_host_address(self) -> None:
        profile = load_profile(self.lab.write_profile(seed="osd01"))
        self.assertIn('SEED_HOST="10.0.0.21"', render_inventory(profile))


class ParserAgreementTests(InventoryTestCase):
    """The whole point of one inventory is that both implementations read it."""

    def test_the_python_candidates_parser_accepts_it(self) -> None:
        path = write_inventory(self.profile, self.root)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, str(sys.argv[1])); "
                "from ceph_incident_bundle import _read_inventory; "
                "from pathlib import Path; "
                "inventory = _read_inventory(Path(sys.argv[2])); "
                "print(inventory.seed); "
                "print(' '.join(node.host_alias for node in inventory.nodes)); "
                "print(inventory.rook_namespace)",
                str(ROOT),
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seed, aliases, namespace = completed.stdout.splitlines()
        self.assertEqual(seed, "operator@10.0.0.11")
        self.assertEqual(aliases, "monitor01 mon02 osd01")
        self.assertEqual(namespace, "rook-ceph")

    def test_the_shell_reference_can_source_it(self) -> None:
        path = write_inventory(self.profile, self.root)
        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'set -eu; . "{path}"; printf "%s|%s|%s|%s\\n" '
                '"$SSH_USER" "$SEED_HOST" "$ROOK_NAMESPACE" "${HOSTS[*]}"',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "operator|10.0.0.11|rook-ceph|"
            "monitor01=10.0.0.11 mon02=10.0.0.12 osd01=10.0.0.21",
        )


class WriteTests(InventoryTestCase):
    def test_the_inventory_is_local_only_and_owner_only(self) -> None:
        path = write_inventory(self.profile, self.root)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_pinned_home_carries_only_the_trusted_host_keys(self) -> None:
        home = self.root / "home"
        home.mkdir()
        known_hosts = write_known_hosts_home(home, ("10.0.0.11 ssh-ed25519 AAAAtrusted",))
        self.assertEqual(known_hosts, home / ".ssh" / "known_hosts")
        self.assertEqual(
            known_hosts.read_text(encoding="utf-8"), "10.0.0.11 ssh-ed25519 AAAAtrusted\n"
        )
        self.assertEqual(stat.S_IMODE(known_hosts.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((home / ".ssh").stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
