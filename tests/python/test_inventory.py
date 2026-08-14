import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.inventory import (
    InventoryRejected,
    TargetNode,
    draft_inventory,
    load_inventory,
)


class DraftInventoryTests(unittest.TestCase):
    def test_hosts_path_expands_literal_tilde_with_controlled_home(self) -> None:
        with TemporaryDirectory() as directory:
            controlled_home = Path(directory)
            (controlled_home / "hosts").write_text(
                "192.0.2.10 mon01.example.test\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"HOME": directory}):
                generated, problems = draft_inventory(Path("~/hosts"))

        self.assertIn(b"mon01 = mon01.example.test\n", generated)
        self.assertEqual(problems, ())

    def test_hosts_are_converted_in_order_with_all_generated_defaults(self) -> None:
        hosts = b"""\
# ignored
127.0.0.1 localhost
::1 loopback
192.0.2.10 mon01.example.com mon01
not-an-ip ignored.example.com
192.0.2.20 worker.example.com worker
192.0.2.21 mon01.example.com repeated-alias
192.0.2.22
"""
        expected = b"""\
[common]
ssh_user = root
probe_timeout = 30m
ssh_connect_timeout = 15s

[nodes]
mon01 = mon01.example.com
worker = worker.example.com

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
            hosts_path = Path(directory) / "hosts"
            hosts_path.write_bytes(hosts)

            generated, problems = draft_inventory(hosts_path)

        self.assertEqual(generated, expected)
        self.assertEqual(problems, ())

    def test_colliding_names_remain_active_and_are_all_marked(self) -> None:
        hosts = b"""\
192.0.2.10 Mon01.first.example
192.0.2.11 mon01.second.example
"""
        expected_nodes = b"""\
[nodes]
# ACTION REQUIRED: rename colliding Inventory Name "Mon01"
Mon01 = Mon01.first.example
# ACTION REQUIRED: rename colliding Inventory Name "mon01"
mon01 = mon01.second.example
"""

        with TemporaryDirectory() as directory:
            hosts_path = Path(directory) / "hosts"
            hosts_path.write_bytes(hosts)

            generated, problems = draft_inventory(hosts_path)

        self.assertIn(expected_nodes, generated)
        self.assertEqual(
            problems,
            (
                "Inventory Name collision: 'Mon01' (Mon01.first.example) "
                "conflicts with 'mon01' (mon01.second.example)",
            ),
        )

    def test_conventional_local_names_and_malformed_hostnames_are_ignored(self) -> None:
        hosts = b"""\
192.0.2.1 localhost4.localdomain4
192.0.2.2 ip6-loopback
192.0.2.3 invalid/name
2001:db8::4 node-v6.example.test alias
"""
        with TemporaryDirectory() as directory:
            hosts_path = Path(directory) / "hosts"
            hosts_path.write_bytes(hosts)

            generated, problems = draft_inventory(hosts_path)

        self.assertNotIn(b"localhost", generated)
        self.assertNotIn(b"loopback", generated)
        self.assertNotIn(b"invalid/name", generated)
        self.assertIn(b"node-v6 = node-v6.example.test", generated)
        self.assertEqual(problems, ())

    def test_ceph_source_naming_substrings_are_case_insensitive(self) -> None:
        examples = (
            ("BACKUP-CP01.example.test", "BACKUP-CP01"),
            ("archive-CM02.example.test", "archive-CM02"),
            ("MON03.example.test", "MON03"),
        )
        for hostname, expected_source in examples:
            with self.subTest(hostname=hostname), TemporaryDirectory() as directory:
                hosts_path = Path(directory) / "hosts"
                hosts_path.write_text(
                    f"192.0.2.10 {hostname}\n",
                    encoding="utf-8",
                )

                generated, problems = draft_inventory(hosts_path)

            self.assertIn(
                f"[ceph]\nsource = {expected_source}\n".encode("utf-8"), generated
            )
            self.assertEqual(problems, ())

    def test_ceph_source_is_first_matching_hostname_in_hosts_order(self) -> None:
        hosts = b"""\
192.0.2.10 worker.example.test
192.0.2.11 backup-cp01.example.test
192.0.2.12 mon01.example.test
"""
        with TemporaryDirectory() as directory:
            hosts_path = Path(directory) / "hosts"
            hosts_path.write_bytes(hosts)

            generated, problems = draft_inventory(hosts_path)

        self.assertIn(b"[ceph]\nsource = backup-cp01\n", generated)
        self.assertEqual(problems, ())

    def test_ceph_source_is_inactive_when_no_hostname_matches(self) -> None:
        with TemporaryDirectory() as directory:
            hosts_path = Path(directory) / "hosts"
            hosts_path.write_text(
                "192.0.2.10 worker.example.test\n",
                encoding="utf-8",
            )

            generated, problems = draft_inventory(hosts_path)

        self.assertIn(b"[ceph]\n# source =\n", generated)
        self.assertEqual(problems, ())


class LoadInventoryTests(unittest.TestCase):
    def test_valid_inventory_is_immutable_ordered_and_keeps_exact_bytes(self) -> None:
        inventory_bytes = b"""\
[common]
ssh_user = root

[nodes]
mon-a = mon-a.example.com
node-v4 = 192.0.2.20
node-v6 = 2001:db8::20

[ceph]
source = mon-a

[kubernetes]
context = cluster%blue

[prometheus]
url = https://operator:secret@prom.example.test/base/
metrics_filter_regex = ^node_.+%$
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.ini"
            path.write_bytes(inventory_bytes)

            inventory = load_inventory(path)

        self.assertEqual(inventory.snapshot, inventory_bytes)
        self.assertEqual(
            inventory.nodes,
            (
                TargetNode("mon-a", "mon-a.example.com"),
                TargetNode("node-v4", "192.0.2.20"),
                TargetNode("node-v6", "2001:db8::20"),
            ),
        )
        self.assertEqual(inventory.ssh_user, "root")
        self.assertEqual(inventory.probe_timeout_seconds, 1800)
        self.assertEqual(inventory.ssh_connect_timeout_seconds, 15)
        self.assertEqual(inventory.ceph_source, "mon-a")
        self.assertEqual(inventory.kubernetes_context, "cluster%blue")
        self.assertEqual(inventory.consumer_namespace, "rook-ceph-external")
        self.assertEqual(inventory.operator_namespace, "rook-ceph")
        self.assertEqual(
            inventory.prometheus_url,
            "https://operator:secret@prom.example.test/base/",
        )
        self.assertEqual(inventory.metrics_filter_regex, "^node_.+%$")
        self.assertEqual(inventory.query_step, "15s")
        self.assertEqual(inventory.request_timeout_seconds, 300)
        with self.assertRaises(AttributeError):
            inventory.ssh_user = "other"  # type: ignore[misc]

    def test_invalid_inventory_reports_all_practical_problems_together(self) -> None:
        inventory_bytes = b"""\
[common]
ssh_user = admin
probe_timeout = 10s
ssh_connect_timeout = later
surprise = yes
ssh_user = root

[nodes]
bad/name = user@host
Node = host.example.com
node = other.example.com

[ceph]
source = missing

[prometheus]
url = ftp://prom.example.test/api?query=x#fragment
metrics_filter_regex = [
query_step = 0
request_timeout = -1s

[unknown]
key = value

[common]
ssh_user = root
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.ini"
            path.write_bytes(inventory_bytes)

            with self.assertRaises(InventoryRejected) as caught:
                load_inventory(path)

        message = str(caught.exception)
        for expected in (
            "duplicate section [common]",
            "duplicate key 'ssh_user' in [common]",
            "unknown key 'surprise' in [common]",
            "ssh_user must be exactly 'root'",
            "invalid probe_timeout '10s'",
            "invalid ssh_connect_timeout 'later'",
            "invalid Inventory Name 'bad/name'",
            "invalid SSH Address 'user@host'",
            "Inventory Name collision: 'Node' conflicts with 'node'",
            "Ceph source 'missing' does not reference a Target Node",
            "invalid Prometheus URL",
            "invalid metrics_filter_regex",
            "invalid query_step '0'",
            "invalid request_timeout '-1s'",
            "unknown section [unknown]",
        ):
            self.assertIn(expected, message)

    def test_duplicate_and_unicode_portable_name_collisions_are_all_rejected(self) -> None:
        inventory_bytes = """\
[common]
ssh_user = root
[nodes]
same = host-a.example
same = host-b.example
é = host-c.example
é = host-d.example
""".encode("utf-8")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.ini"
            path.write_bytes(inventory_bytes)

            with self.assertRaises(InventoryRejected) as caught:
                load_inventory(path)

        self.assertIn("duplicate key 'same' in [nodes]", str(caught.exception))
        self.assertIn(
            "Inventory Name collision: 'same' conflicts with 'same'",
            str(caught.exception),
        )
        self.assertIn(
            "Inventory Name collision: 'é' conflicts with 'é'",
            str(caught.exception),
        )

    def test_present_optional_empty_values_are_rejected(self) -> None:
        inventory_bytes = b"""\
[common]
ssh_user = root
[nodes]
node = node.example
[ceph]
source =
[kubernetes]
context =
consumer_namespace =
[prometheus]
url =
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.ini"
            path.write_bytes(inventory_bytes)

            with self.assertRaises(InventoryRejected) as caught:
                load_inventory(path)

        message = str(caught.exception)
        self.assertIn("[ceph] source must be nonempty when present", message)
        self.assertIn("[kubernetes] context must be nonempty when present", message)
        self.assertIn("invalid consumer_namespace ''", message)
        self.assertIn("invalid Prometheus URL ''", message)

    def test_unreadable_inventory_uses_the_single_inventory_rejection(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.ini"
            with self.assertRaises(InventoryRejected) as caught:
                load_inventory(missing)

        self.assertIn("cannot read Inventory", str(caught.exception))

    def test_valid_duration_overrides_are_converted_to_seconds(self) -> None:
        inventory_bytes = b"""\
[common]
ssh_user = root
probe_timeout = 0
ssh_connect_timeout = 2m
[nodes]
node = node.example
[prometheus]
query_step = 1w
request_timeout = 0
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.ini"
            path.write_bytes(inventory_bytes)

            inventory = load_inventory(path)

        self.assertEqual(inventory.probe_timeout_seconds, 0)
        self.assertEqual(inventory.ssh_connect_timeout_seconds, 120)
        self.assertEqual(inventory.query_step, "1w")
        self.assertEqual(inventory.request_timeout_seconds, 0)

    def test_rejection_does_not_start_process_network_or_create_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "inventory.ini"
            path.write_text("[common]\nssh_user = admin\n", encoding="utf-8")
            with patch("subprocess.Popen") as popen, patch(
                "urllib.request.urlopen"
            ) as urlopen:
                with self.assertRaises(InventoryRejected):
                    load_inventory(path)

            self.assertEqual([item.name for item in root.iterdir()], ["inventory.ini"])
            popen.assert_not_called()
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
