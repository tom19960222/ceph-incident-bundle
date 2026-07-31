"""Reading a bundle safely, deciding coverage, and reducing it to a contract."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from validation.lab_bundle import (
    BundleUnreadable,
    contract_of,
    coverage_of,
    read_bundle,
)
from validation.lab_contract import describe_differences


HOSTS = ("monitor01", "osd01")


def capture(host: str, collector: str, started: str, body: str) -> bytes:
    return (
        f"# host: {host}\n# collector: {collector}\n# started: {started}\n"
        f"# timeout: 20s\n{body}"
    ).encode("utf-8")


def bundle_members(
    *,
    started: str = "2026-07-31T01:00:00Z",
    counter: int = 1,
    layers: tuple[str, ...] = ("ceph", "rook", "prometheus"),
    hosts: tuple[str, ...] = HOSTS,
    var_log: bool = True,
    extra: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    members: dict[str, bytes] = {
        "README-FIRST.txt": b"Ceph incident bundle\n",
        "errors.log": b"",
        "environment.txt": (
            f"created_utc={started}\nmode=auto\nseed=operator@10.0.0.11\n"
            "since=24h\ntimeout=20\ngit_commit=abc\nceph_source=monitor01\n"
            "ceph_runner=ceph\nrook_source=local\n"
        ).encode("utf-8"),
        "summary.txt": (
            f"Ceph incident bundle summary\ncreated_utc: {started}\nmode: auto\n"
            "seed: operator@10.0.0.11\ncluster_status: 0\n"
            f"node_ok: {len(hosts)}\nnode_failed: 0\nfinal_status: 0\n"
        ).encode("utf-8"),
    }
    manifest: list[dict[str, object]] = []
    if "ceph" in layers:
        members["cluster/ceph/json/status.json"] = capture(
            "monitor01", "cluster-ceph", started, json.dumps({"num_pgs": counter}) + "\n"
        )
        manifest.append(
            {
                "host": "monitor01",
                "collector": "cluster-ceph",
                "artifact": "cluster/ceph/json/status.json",
                "command": "ssh -i /key monitor01 ceph -s -f json",
                "exit_code": 0,
                "started": started,
                "ended": started,
            }
        )
    if "rook" in layers:
        members["cluster/rook/pods-wide.txt"] = capture(
            "local", "cluster-rook", started, f"rook-ceph-mon-a 1/1 {counter}s\n"
        )
    if "prometheus" in layers:
        members["cluster/prometheus/ceph/up.json.gz"] = gzip.compress(
            json.dumps({"samples": counter}).encode("utf-8")
        )
    for host in hosts:
        members[f"nodes/{host}/system/hostname.txt"] = capture(
            host, "node", started, f"{host}\n"
        )
        members[f"nodes/{host}/manifest.jsonl"] = (
            json.dumps(
                {
                    "host": host,
                    "collector": "node",
                    "artifact": f"nodes/{host}/system/hostname.txt",
                    "command": "hostname",
                    "exit_code": 0,
                    "started": started,
                    "ended": started,
                }
            )
            + "\n"
        ).encode("utf-8")
        if var_log:
            members[f"nodes/{host}/logs/var-log/merged/syslog.merged"] = (
                f"{started} {host} kernel: line {counter}\n".encode("utf-8")
            )
    members["manifest.jsonl"] = "".join(
        json.dumps(record) + "\n" for record in manifest
    ).encode("utf-8")
    members.update(extra or {})
    return members


class BundleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, members: dict[str, bytes], name: str = "bundle.tar.gz") -> Path:
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            for member, payload in sorted(members.items()):
                info = tarfile.TarInfo(f"./{member}")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
        return path

    def write_raw(self, build) -> Path:
        path = self.root / "hostile.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            build(archive)
        return path


class SafeReadingTests(BundleTestCase):
    def test_reads_a_bundle_without_extracting_it(self) -> None:
        contents = read_bundle(self.write(bundle_members()))
        self.assertIn("manifest.jsonl", contents.names)
        self.assertEqual(sorted(self.root.iterdir()), [self.root / "bundle.tar.gz"])

    def test_refuses_a_symlink_member(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            info = tarfile.TarInfo("./escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)

        with self.assertRaises(BundleUnreadable) as raised:
            read_bundle(self.write_raw(build))
        self.assertIn("link member", str(raised.exception))

    def test_refuses_a_traversal_member(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            info = tarfile.TarInfo("./../outside.txt")
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))

        with self.assertRaises(BundleUnreadable) as raised:
            read_bundle(self.write_raw(build))
        self.assertIn("traversal", str(raised.exception))

    def test_refuses_an_absolute_member(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            info = tarfile.TarInfo("/etc/shadow")
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))

        with self.assertRaises(BundleUnreadable) as raised:
            read_bundle(self.write_raw(build))
        self.assertIn("absolute", str(raised.exception))

    def test_refuses_a_device_member(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            info = tarfile.TarInfo("./dev/null")
            info.type = tarfile.CHRTYPE
            archive.addfile(info)

        with self.assertRaises(BundleUnreadable) as raised:
            read_bundle(self.write_raw(build))
        self.assertIn("special member", str(raised.exception))

    def test_refuses_a_missing_or_unreadable_bundle(self) -> None:
        with self.assertRaises(BundleUnreadable):
            read_bundle(self.root / "absent.tar.gz")
        broken = self.root / "broken.tar.gz"
        broken.write_bytes(b"not an archive")
        with self.assertRaises(BundleUnreadable):
            read_bundle(broken)


class CoverageTests(BundleTestCase):
    def coverage(self, **kwargs):
        return coverage_of(read_bundle(self.write(bundle_members(**kwargs))), HOSTS)

    def test_a_full_collect_covers_all_four_paths(self) -> None:
        coverage = self.coverage()
        self.assertTrue(coverage.complete, coverage.document())

    def test_a_missing_cluster_layer_is_a_gap(self) -> None:
        coverage = self.coverage(layers=("ceph", "rook"))
        self.assertEqual(coverage.prometheus, "missing")
        self.assertFalse(coverage.complete)

    def test_a_skipped_layer_is_still_a_gap(self) -> None:
        coverage = coverage_of(
            read_bundle(
                self.write(
                    bundle_members(
                        layers=("ceph", "rook"),
                        extra={"cluster/prometheus/SKIPPED.txt": b"SKIPPED: not reachable\n"},
                    )
                )
            ),
            HOSTS,
        )
        self.assertEqual(coverage.prometheus, "skipped")
        self.assertFalse(coverage.complete)

    def test_a_missing_node_names_the_node(self) -> None:
        coverage = self.coverage(hosts=("monitor01",))
        self.assertEqual(coverage.nodes, "missing: osd01")
        self.assertEqual(coverage.var_log, "missing: osd01")

    def test_nodes_without_var_log_are_a_gap(self) -> None:
        coverage = self.coverage(var_log=False)
        self.assertEqual(coverage.nodes, "collected")
        self.assertEqual(coverage.var_log, "missing: monitor01, osd01")


class ContractTests(BundleTestCase):
    def contract(self, **kwargs):
        self.written = getattr(self, "written", 0) + 1
        return contract_of(
            read_bundle(self.write(bundle_members(**kwargs), f"run-{self.written}.tar.gz"))
        )

    def test_two_collects_of_a_live_cluster_are_equivalent(self) -> None:
        reference = self.contract(started="2026-07-31T01:00:00Z", counter=11)
        candidate = self.contract(started="2026-07-31T01:07:42Z", counter=93)
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_a_missing_artifact_is_a_difference(self) -> None:
        reference = self.contract()
        candidate = self.contract(layers=("ceph", "rook"))
        differences = describe_differences(reference, candidate)
        self.assertTrue(any("members" in line for line in differences), differences)

    def test_a_different_exit_code_is_a_difference(self) -> None:
        reference = self.contract()
        failed = bundle_members()
        record = json.loads(failed["manifest.jsonl"].decode("utf-8").splitlines()[0])
        record["exit_code"] = 1
        failed["manifest.jsonl"] = (json.dumps(record) + "\n").encode("utf-8")
        candidate = contract_of(read_bundle(self.write(failed, "failed.tar.gz")))
        differences = describe_differences(reference, candidate)
        self.assertTrue(any("exit_code" in line for line in differences), differences)

    def test_a_different_command_is_a_difference(self) -> None:
        reference = self.contract()
        other = bundle_members()
        record = json.loads(other["manifest.jsonl"].decode("utf-8").splitlines()[0])
        record["command"] = "ssh -i /key monitor01 cephadm shell -- ceph -s -f json"
        other["manifest.jsonl"] = (json.dumps(record) + "\n").encode("utf-8")
        candidate = contract_of(read_bundle(self.write(other, "other.tar.gz")))
        self.assertTrue(
            any("command" in line for line in describe_differences(reference, candidate))
        )

    def test_shell_quoting_style_is_not_a_difference(self) -> None:
        reference = self.contract()
        quoted = bundle_members()
        record = json.loads(quoted["manifest.jsonl"].decode("utf-8").splitlines()[0])
        record["command"] = "ssh -i '/key' 'monitor01' ceph -s -f json"
        quoted["manifest.jsonl"] = (json.dumps(record) + "\n").encode("utf-8")
        candidate = contract_of(read_bundle(self.write(quoted, "quoted.tar.gz")))
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_live_evidence_values_are_not_compared(self) -> None:
        # Both implementations record a command's output verbatim, so the numbers
        # in it are the cluster's answer at that moment — including a health check
        # that only exists in one of the two runs.
        reference = self.contract()
        moved = bundle_members()
        moved["cluster/ceph/json/status.json"] = capture(
            "monitor01",
            "cluster-ceph",
            "2026-07-31T01:07:42Z",
            json.dumps({"num_pgs": 9999, "health": {"checks": {"OSD_SLOW": {}}}}) + "\n",
        )
        candidate = contract_of(read_bundle(self.write(moved, "moved.tar.gz")))
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_evidence_that_stopped_being_json_is_a_difference(self) -> None:
        reference = self.contract()
        wrapped = bundle_members()
        wrapped["cluster/ceph/json/status.json"] = capture(
            "monitor01",
            "cluster-ceph",
            "2026-07-31T01:00:00Z",
            "cluster status: num_pgs=4\n",
        )
        candidate = contract_of(read_bundle(self.write(wrapped, "wrapped.tar.gz")))
        self.assertTrue(
            any("status.json" in line for line in describe_differences(reference, candidate))
        )

    def test_a_different_runner_selection_is_a_difference(self) -> None:
        reference = self.contract()
        other = bundle_members()
        other["environment.txt"] = other["environment.txt"].replace(
            b"ceph_runner=ceph", b"ceph_runner=sudo-ceph"
        )
        candidate = contract_of(read_bundle(self.write(other, "runner.tar.gz")))
        self.assertTrue(
            any("ceph_runner" in line for line in describe_differences(reference, candidate))
        )

    def test_a_different_partial_status_is_a_difference(self) -> None:
        reference = self.contract()
        other = bundle_members()
        other["summary.txt"] = other["summary.txt"].replace(
            b"final_status: 0", b"final_status: 2"
        )
        candidate = contract_of(read_bundle(self.write(other, "partial.tar.gz")))
        self.assertTrue(
            any("final_status" in line for line in describe_differences(reference, candidate))
        )

    def test_a_different_skip_reason_is_a_difference(self) -> None:
        reference = contract_of(
            read_bundle(
                self.write(
                    bundle_members(
                        layers=("ceph", "rook"),
                        extra={"cluster/prometheus/SKIPPED.txt": b"SKIPPED: not reachable\n"},
                    ),
                    "skip-a.tar.gz",
                )
            )
        )
        candidate = contract_of(
            read_bundle(
                self.write(
                    bundle_members(
                        layers=("ceph", "rook"),
                        extra={"cluster/prometheus/SKIPPED.txt": b"SKIPPED: no scrape job matched\n"},
                    ),
                    "skip-b.tar.gz",
                )
            )
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_candidate_only_environment_keys_are_not_a_difference(self) -> None:
        reference = self.contract()
        other = bundle_members()
        other["environment.txt"] += (
            b"node_target_monitor01=10.0.0.11\n"
            b"node_invocation_id_monitor01=" + b"a" * 32 + b"\n"
        )
        candidate = contract_of(read_bundle(self.write(other, "extra-env.tar.gz")))
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_var_log_payload_bytes_are_not_compared(self) -> None:
        reference = self.contract()
        other = bundle_members()
        other["nodes/monitor01/logs/var-log/merged/syslog.merged"] = (
            b"a completely different set of log lines\n"
        )
        candidate = contract_of(read_bundle(self.write(other, "logs.tar.gz")))
        self.assertEqual(describe_differences(reference, candidate), ())


if __name__ == "__main__":
    unittest.main()
