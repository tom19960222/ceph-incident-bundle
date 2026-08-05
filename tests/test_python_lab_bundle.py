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
    BLANKED_LINE,
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
        self.assertEqual(coverage.nodes, "osd01=missing")
        self.assertEqual(coverage.var_log, "osd01=missing")

    def test_a_skip_written_into_the_evidences_own_artifact_is_a_gap(self) -> None:
        # The `/var/log` over-limit path deletes the payload and rewrites
        # `journal-all-since.txt` to `SKIPPED: ...`, so a filename-only check
        # would count a node that collected no logs at all as covered.
        coverage = coverage_of(
            read_bundle(
                self.write(
                    bundle_members(
                        var_log=False,
                        extra={
                            f"nodes/{host}/logs/var-log/journal-all-since.txt": (
                                b"SKIPPED: not collected because /var/log payload "
                                b"exceeded the per-node cap\n"
                            )
                            for host in HOSTS
                        },
                    )
                )
            ),
            HOSTS,
        )
        self.assertEqual(coverage.var_log, "monitor01=skipped, osd01=skipped")
        self.assertFalse(coverage.complete)

    def test_nodes_without_var_log_are_a_gap(self) -> None:
        coverage = self.coverage(var_log=False)
        self.assertEqual(coverage.nodes, "collected")
        self.assertEqual(coverage.var_log, "monitor01=missing, osd01=missing")


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

    def with_command(self, command: str, name: str):
        members = bundle_members()
        record = json.loads(members["manifest.jsonl"].decode("utf-8").splitlines()[0])
        record["command"] = command
        members["manifest.jsonl"] = (json.dumps(record) + "\n").encode("utf-8")
        return contract_of(read_bundle(self.write(members, name)))

    def test_each_implementations_own_workdir_name_is_not_a_difference(self) -> None:
        # The reference names its scratch directory `tmp.<stamp>.$$` and the
        # candidate takes one from `mkdtemp`, whose alphabet includes `_`.  Both
        # are workstation scratch, and a rule that erases only part of either
        # leaves the rest — `<workdir>.61493` against `<workdir>` — looking like
        # a contract difference in all 41 cluster entries (#52).
        reference = self.with_command(
            "ssh -i /key monitor01 cat /out/tmp.20260805T140056Z.61493/x", "wd-ref.tar.gz"
        )
        candidate = self.with_command(
            "ssh -i /key monitor01 cat /out/tmp.d9_n496h/x", "wd-cand.tar.gz"
        )
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_the_same_prometheus_window_at_another_moment_is_not_a_difference(self) -> None:
        # `start=`/`end=` are epochs taken from the collect's own start, so two
        # runs twenty minutes apart can never write the same pair.
        reference = self.with_command(
            "curl -s /api/v1/query_range start=1785852083 end=1785938483 step=15",
            "window-ref.tar.gz",
        )
        candidate = self.with_command(
            "curl -s /api/v1/query_range start=1785853334 end=1785939734 step=15",
            "window-cand.tar.gz",
        )
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_a_different_prometheus_window_is_still_a_difference(self) -> None:
        # What the window normalization must never hide: `--since` is a decision,
        # and a candidate that queried twelve hours where the reference queried
        # twenty-four has to say so.
        reference = self.with_command(
            "curl -s /api/v1/query_range start=1785852083 end=1785938483 step=15",
            "since-ref.tar.gz",
        )
        candidate = self.with_command(
            "curl -s /api/v1/query_range start=1785896534 end=1785939734 step=15",
            "since-cand.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

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


# The remote workspace as both implementations really name it, `out/` included:
# each invokes its node collector with `--out <workspace>/out`, so that segment
# is in every artifact path their manifests record and is gone from the packed
# `nodes/<alias>/` tree.  A fixture that left it out made every bundle lookup in
# the ADR 0010 reduction ask about a member that could not exist, which is how
# these tests stayed green while the real gate let 13 entries through (#52).
WORKSPACE = "/tmp/ceph-incident-node.Ab3xY9/out"


def node_entry(
    host: str, artifact: str, command: str, exit_code: int = 0
) -> dict[str, object]:
    """One node manifest entry, with the workspace-absolute artifact both write."""

    return {
        "host": host,
        "collector": "collect-node",
        "artifact": f"{WORKSPACE}/{artifact}",
        "command": command,
        "exit_code": exit_code,
        "started": "2026-07-31T01:00:00Z",
        "ended": "2026-07-31T01:00:01Z",
    }


class NodeManifestTests(BundleTestCase):
    """ADR 0010 makes the node manifest's *coverage* deliberately divergent.

    The Python node manifest indexes every evidence in the archive; the shell
    reference only records the commands it ran.  A gate that compared the two
    entry-for-entry would overturn an adjudication the project already made, so
    it compares the surface both implementations claim — and nothing wider.
    """

    HOST = "monitor01"

    def bundle(self, manifest_lines: list[object], name: str):
        """One bundle whose node manifest holds `manifest_lines`.

        A line is either an entry dict, serialised as JSON, or an already-final
        string — the way `BLANKED_LINE` reaches a real manifest: content safety
        rewrites the serialised line itself, leaving nothing to parse.
        """

        members = bundle_members(hosts=(self.HOST,))
        members[f"nodes/{self.HOST}/cephadm/var-lib-ceph-listing.txt"] = capture(
            self.HOST, "collect-node", "2026-07-31T01:00:00Z", "d /var/lib/ceph\n"
        )
        members[f"nodes/{self.HOST}/time/systemd-timesyncd-config/timesyncd.conf"] = (
            b"[Time]\nNTP=10.0.0.1\n"
        )
        members[f"nodes/{self.HOST}/resources/iostat.txt"] = (
            b"SKIPPED: command not found: iostat\n"
        )
        # Both implementations run and record this one, so it is evidence with a
        # capture header — not part of the generated tree around it.
        members[f"nodes/{self.HOST}/logs/var-log/journal-all-since.txt"] = capture(
            self.HOST, "collect-node", "2026-07-31T01:00:00Z", "-- Journal begins --\n"
        )
        members[f"nodes/{self.HOST}/logs/var-log/INDEX.tsv"] = b"path\tbytes\n"
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n"
            for line in manifest_lines
        ).encode("utf-8")
        return contract_of(read_bundle(self.write(members, name)))

    def shared(self) -> list[object]:
        """The entries the reference records, which the candidate must match."""

        return [
            node_entry(self.HOST, "system/hostname.txt", "hostname"),
            node_entry(self.HOST, "kernel/dmesg.txt", "sudo -n dmesg -T"),
            node_entry(
                self.HOST,
                "logs/var-log/journal-all-since.txt",
                "sudo -n journalctl --since -24h --no-pager",
            ),
        ]

    def reference(self, extra: list[object] | None = None):
        # The reference's own listing entry does not survive redaction: its real
        # `find` expression names `*keyring*`, so content safety blanks the line.
        return self.bundle(
            self.shared() + [BLANKED_LINE] + (extra or []), "reference.tar.gz"
        )

    def candidate(self, extra: list[object] | None = None):
        return self.bundle(
            self.shared()
            + [
                node_entry(
                    self.HOST,
                    "cephadm/var-lib-ceph-listing.txt",
                    "collect-node list /var/lib/ceph",
                )
            ]
            + (extra or []),
            "candidate.tar.gz",
        )

    def test_the_index_only_entries_adr_0010_adds_are_not_a_difference(self) -> None:
        reference = self.reference()
        candidate = self.candidate(
            [
                node_entry(
                    self.HOST,
                    "time/systemd-timesyncd-config/timesyncd.conf",
                    "collect-node copy /etc/systemd/timesyncd.conf",
                ),
                node_entry(
                    self.HOST,
                    "logs/var-log/merged/syslog.merged",
                    "collect-var-log /var/log",
                ),
                node_entry(
                    self.HOST, "logs/var-log/INDEX.tsv", "collect-var-log /var/log"
                ),
                node_entry(self.HOST, "resources/iostat.txt", "iostat -xz 1 3", 127),
            ]
        )
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_the_journal_capture_under_var_log_is_still_compared(self) -> None:
        # `logs/var-log/` is mostly the generated tree, but the journal capture
        # in it is a command both implementations ran — losing `sudo -n` or the
        # `--since` window there has to fail the gate.
        reference = self.reference()
        candidate = self.bundle(
            [
                node_entry(self.HOST, "system/hostname.txt", "hostname"),
                node_entry(self.HOST, "kernel/dmesg.txt", "sudo -n dmesg -T"),
                node_entry(
                    self.HOST,
                    "logs/var-log/journal-all-since.txt",
                    "journalctl --since -24h --no-pager",
                ),
                node_entry(
                    self.HOST,
                    "cephadm/var-lib-ceph-listing.txt",
                    "collect-node list /var/lib/ceph",
                ),
            ],
            "unprivileged-journal.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_a_marker_the_reference_also_indexes_is_still_compared(self) -> None:
        # The over-limit journal is a SKIPPED marker the reference records too,
        # with the exit code 75 that ADR 0010 never enumerated.  Recognising
        # markers by artifact alone would drop it and lose the degradation.
        over_limit = [
            node_entry(self.HOST, "system/hostname.txt", "hostname"),
            node_entry(self.HOST, "kernel/dmesg.txt", "sudo -n dmesg -T"),
            node_entry(
                self.HOST,
                "logs/var-log/journal-all-since.txt",
                "sudo -n journalctl --since -24h --no-pager",
                75,
            ),
        ]
        members = bundle_members(hosts=(self.HOST,))
        members[f"nodes/{self.HOST}/logs/var-log/journal-all-since.txt"] = (
            b"SKIPPED: not collected because combined /var/log and journal text "
            b"exceeded the per-node cap\n"
        )
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            json.dumps(record) + "\n" for record in over_limit
        ).encode("utf-8")
        reference = contract_of(read_bundle(self.write(members, "over-limit-a.tar.gz")))
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            json.dumps(record) + "\n" for record in over_limit[:2]
        ).encode("utf-8")
        candidate = contract_of(read_bundle(self.write(members, "over-limit-b.tar.gz")))
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_an_index_verb_over_a_real_capture_is_still_compared(self) -> None:
        # The index verbs say "nobody ran a command for this".  The artifact's
        # capture header says otherwise, and the bundle is believed over the
        # manifest — otherwise any entry could vanish by wearing the verb.
        reference = self.reference()
        candidate = self.bundle(
            [
                node_entry(self.HOST, "system/hostname.txt", "hostname"),
                node_entry(self.HOST, "kernel/dmesg.txt", "sudo -n dmesg -T"),
                node_entry(
                    self.HOST,
                    "logs/var-log/journal-all-since.txt",
                    "collect-var-log /var/log",
                ),
                node_entry(
                    self.HOST,
                    "cephadm/var-lib-ceph-listing.txt",
                    "collect-node list /var/lib/ceph",
                ),
            ],
            "verb-over-capture.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_the_var_lib_ceph_listing_verb_is_not_a_difference(self) -> None:
        # ADR 0010 moved this entry's command policy to the N9 argv ledger, so
        # the gate compares that the listing was recorded, not how.
        self.assertEqual(describe_differences(self.reference(), self.candidate()), ())

    def test_a_node_without_a_readable_var_lib_ceph_agrees_it_has_no_listing(self) -> None:
        # The lab's Kubernetes node has no `/var/lib/ceph`, so both sides write
        # the same SKIPPED marker — and only the candidate indexes the marker.
        # The recorded fact is about the *evidence*, so an index entry over a
        # marker must not make one side claim a listing the archive does not
        # hold: reading it from the entry instead of from the bundle turned this
        # node into a disagreement on the real lab (#52).
        members = bundle_members(hosts=(self.HOST,))
        members[f"nodes/{self.HOST}/cephadm/var-lib-ceph-listing.txt"] = (
            b"SKIPPED: /var/lib/ceph is not a readable directory on this node\n"
        )
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            json.dumps(line) + "\n" for line in self.shared()
        ).encode("utf-8")
        reference = contract_of(read_bundle(self.write(members, "no-listing-ref.tar.gz")))
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            json.dumps(line) + "\n"
            for line in self.shared()
            + [
                node_entry(
                    self.HOST,
                    "cephadm/var-lib-ceph-listing.txt",
                    "collect-node list /var/lib/ceph",
                    2,
                )
            ]
        ).encode("utf-8")
        candidate = contract_of(read_bundle(self.write(members, "no-listing-cand.tar.gz")))
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_a_listing_only_one_side_collected_is_still_a_difference(self) -> None:
        # The convergence is on the evidence, so the evidence still has to
        # agree: a listing one bundle carries and the other skipped is a real
        # divergence in what was collected, not a difference in bookkeeping.
        members = bundle_members(hosts=(self.HOST,))
        members[f"nodes/{self.HOST}/cephadm/var-lib-ceph-listing.txt"] = (
            b"SKIPPED: /var/lib/ceph is not a readable directory on this node\n"
        )
        members[f"nodes/{self.HOST}/manifest.jsonl"] = "".join(
            json.dumps(line) + "\n" for line in self.shared()
        ).encode("utf-8")
        reference = contract_of(read_bundle(self.write(members, "skipped-listing.tar.gz")))
        self.assertNotEqual(describe_differences(reference, self.candidate()), ())

    def test_an_entry_the_reference_claims_is_still_compared(self) -> None:
        reference = self.reference()
        candidate = self.bundle(
            [
                node_entry(self.HOST, "system/hostname.txt", "hostname"),
                node_entry(self.HOST, "kernel/dmesg.txt", "dmesg -T"),
                node_entry(
                    self.HOST,
                    "cephadm/var-lib-ceph-listing.txt",
                    "collect-node list /var/lib/ceph",
                ),
            ],
            "unprivileged.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_a_candidate_entry_outside_the_enumerated_classes_is_a_difference(self) -> None:
        reference = self.reference()
        candidate = self.candidate(
            [node_entry(self.HOST, "system/uptime.txt", "uptime")]
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_a_redaction_the_listing_does_not_explain_is_a_difference(self) -> None:
        reference = self.reference(extra=[BLANKED_LINE])
        self.assertNotEqual(describe_differences(reference, self.candidate()), ())

    def test_the_relaxation_does_not_reach_the_cluster_manifest(self) -> None:
        reference = self.contract_with_cluster_manifest([], "cluster-reference.tar.gz")
        candidate = self.contract_with_cluster_manifest(
            [
                {
                    "host": "monitor01",
                    "collector": "cluster-ceph",
                    "artifact": "cluster/ceph/json/crash-info/crash-02.json",
                    "command": "ssh -i /key monitor01 ceph crash info crash-02",
                    "exit_code": 0,
                    "started": "2026-07-31T01:00:00Z",
                    "ended": "2026-07-31T01:00:01Z",
                }
            ],
            "cluster-candidate.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def contract_with_cluster_manifest(
        self, extra: list[dict[str, object]], name: str
    ):
        members = bundle_members(hosts=(self.HOST,))
        members["manifest.jsonl"] += "".join(
            json.dumps(record) + "\n" for record in extra
        ).encode("utf-8")
        return contract_of(read_bundle(self.write(members, name)))


class VarLogDriftTests(BundleTestCase):
    """The `/var/log` payload file set belongs to the machine, not the collector.

    Two honest collects hours apart package different files there — a UTC day
    boundary births `sysstat/sa03`, journald renames its archived journals — so
    the member comparison stops at the tree (#52).  The boundary is exactly the
    `merged/`, `raw/` and `original/` subtrees: everything directly under
    `logs/var-log/` is still compared, and so is every other member path.
    """

    def contract(self, extra: dict[str, bytes], name: str):
        return contract_of(read_bundle(self.write(bundle_members(extra=extra), name)))

    def test_files_the_machine_grew_between_collects_are_not_a_difference(self) -> None:
        reference = self.contract({}, "drift-reference.tar.gz")
        candidate = self.contract(
            {
                # What the real lab produced: the day boundary's sysstat file and
                # a renamed archived journal, both candidate-only (#52).
                "nodes/monitor01/logs/var-log/raw/sysstat/sa03": b"\x00\x01",
                "nodes/monitor01/logs/var-log/raw/journal/3f0c/system@0006.journal": b"\x7fLPKSHHRH",
            },
            "drift-candidate.tar.gz",
        )
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_a_file_rotated_away_before_the_second_collect_is_not_a_difference(self) -> None:
        reference = self.contract(
            {"nodes/monitor01/logs/var-log/original/syslog.1": b"old line\n"},
            "rotated-reference.tar.gz",
        )
        candidate = self.contract({}, "rotated-candidate.tar.gz")
        self.assertEqual(describe_differences(reference, candidate), ())

    def test_a_member_directly_under_var_log_is_still_compared(self) -> None:
        # `INDEX.tsv` and the journal capture sit beside the payload trees, not
        # inside them: they are the implementation's output, so losing one is a
        # difference the drift allowance must not absorb.
        reference = self.contract(
            {"nodes/monitor01/logs/var-log/INDEX.tsv": b"path\tbytes\n"},
            "index-reference.tar.gz",
        )
        candidate = self.contract({}, "index-candidate.tar.gz")
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_the_drift_allowance_does_not_reach_other_node_evidence(self) -> None:
        reference = self.contract({}, "node-reference.tar.gz")
        candidate = self.contract(
            {"nodes/monitor01/cephadm/extra-evidence.txt": b"data\n"},
            "node-candidate.tar.gz",
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())

    def test_the_drift_allowance_does_not_reach_cluster_artifacts(self) -> None:
        reference = self.contract({}, "cluster-reference.tar.gz")
        candidate = self.contract(
            {"cluster/ceph/json/extra.json": b"{}\n"}, "cluster-candidate.tar.gz"
        )
        self.assertNotEqual(describe_differences(reference, candidate), ())


if __name__ == "__main__":
    unittest.main()
