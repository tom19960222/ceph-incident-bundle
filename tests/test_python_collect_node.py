from __future__ import annotations

import json
import gzip
import hashlib
import bz2
import lzma
import os
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"
NODE_COLLECTOR = ROOT / "ceph_incident_node.py"


# Every external command the node collector may reach for.  All of them are
# answered by the whitelisting fake, and the node's own PATH is replaced with the
# fake world (`FAKE_REMOTE_PATH`), so a tool that happens to exist on the
# developer's workstation can neither be executed nor make a "missing tool"
# assertion pass by accident.
FAKE_NODE_COMMANDS = (
    "hostname",
    "uname",
    "uptime",
    "free",
    "df",
    "ip",
    "systemctl",
    "dd",
    "journalctl",
    "find",
    "sudo",
    "lsblk",
    "dmesg",
    "iostat",
    "chronyc",
    "ntpq",
    "timedatectl",
    "pvs",
    "vgs",
    "lvs",
    "podman",
    "docker",
    "cephadm",
    "stat",
)
# The few genuinely real binaries the fake world is built out of: the node runs
# Python, packages its evidence with tar, and the fakes themselves are shell
# scripts.  Nothing else is reachable from the node's PATH.
REAL_NODE_COMMANDS = ("python3", "sh", "tar", "cat", "awk", "sed", "sleep")


class NodeCollectorFixture:
    """The offline fake world both node-collector test classes run inside."""

    def make_fake_environment(self, root: Path) -> tuple[dict[str, str], Path, Path]:
        real_commands = {
            command: shutil.which(command)
            for command in (
                "gzip",
                "xz",
                "bzip2",
                "zstd",
                "find",
                "stat",
                *REAL_NODE_COMMANDS,
            )
        }
        self.assertTrue(all(real_commands.values()), real_commands)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        real_bin = root / "real-bin"
        real_bin.mkdir()
        ssh_log = root / "ssh-argv.json"
        payload_log = root / "ssh-stdin.py"
        workstation_tmp = root / "workstation-tmp"
        workstation_tmp.mkdir()
        remote_tmp = root / "remote-tmp"
        remote_tmp.mkdir()
        remote_bin = root / "remote-bin"
        remote_bin.mkdir()

        fixture_bin = ROOT / "tests" / "fixtures" / "python-node" / "bin"
        (fake_bin / "ssh").symlink_to(fixture_bin / "ssh")
        (fake_bin / "kubectl").symlink_to(
            ROOT / "tests" / "fixtures" / "python-rook" / "bin" / "kubectl"
        )
        for command in FAKE_NODE_COMMANDS:
            (fake_bin / command).symlink_to(fixture_bin / "node-command")
        for command in ("gzip", "xz", "bzip2", "zstd"):
            (fake_bin / command).symlink_to(fixture_bin / "codec-command")
        for command in REAL_NODE_COMMANDS:
            (real_bin / command).symlink_to(real_commands[command])
        (remote_bin / "tar").symlink_to(fixture_bin / "node-command")

        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_REMOTE_PATH": f"{fake_bin}{os.pathsep}{real_bin}",
            "TMPDIR": str(workstation_tmp),
            "FAKE_REMOTE_TMPDIR": str(remote_tmp),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_SSH_PAYLOAD": str(payload_log),
            "FAKE_REMOTE_BIN": str(remote_bin),
            "CEPH_INCIDENT_VAR_LOG_DIR": str(root / "var-log"),
            "CEPH_INCIDENT_ETC_DIR": str(root / "etc"),
            "CEPH_INCIDENT_VAR_LIB_CEPH_DIR": str(root / "var-lib-ceph"),
            "CEPH_INCIDENT_TIMESYNCD_CONF": str(root / "timesyncd.conf"),
            "CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR": str(root / "timesyncd.conf.d"),
            "CEPH_INCIDENT_TEST_ALLOW_ATIME_READ": "1",
            "FAKE_NODE_COMMAND_LOG": str(root / "node-command.log"),
            "FAKE_NODE_ARGV_LOG": str(root / "node-argv.log"),
            "FAKE_REAL_GZIP": str(real_commands["gzip"]),
            "FAKE_REAL_XZ": str(real_commands["xz"]),
            "FAKE_REAL_BZIP2": str(real_commands["bzip2"]),
            "FAKE_REAL_ZSTD": str(real_commands["zstd"]),
            "FAKE_REAL_FIND": str(real_commands["find"]),
            "FAKE_REAL_STAT": str(real_commands["stat"]),
        }
        (root / "var-log").mkdir()
        (root / "etc").mkdir()
        return environment, ssh_log, payload_log

    def run_collect(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        node_timeout: int = 10,
        extra_arguments: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        command = self.prepare_collect(
            root, node_timeout=node_timeout, extra_arguments=extra_arguments
        )
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def prepare_collect(
        self,
        root: Path,
        *,
        node_timeout: int = 10,
        extra_arguments: tuple[str, ...] = (),
    ) -> list[str]:
        inventory = root / "inventory.env"
        inventory.write_text(
            'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n',
            encoding="utf-8",
        )
        ssh_key = root / "id_ed25519"
        ssh_key.write_text("fixture key path only\n", encoding="utf-8")
        output = root / "results"
        return [
            sys.executable,
            str(ENTRYPOINT),
            "collect",
            "--inventory",
            str(inventory),
            "--ssh-key",
            str(ssh_key),
            "--out",
            str(output),
            "--timeout",
            "3",
            "--node-timeout",
            str(node_timeout),
            "--mode",
            "rook",
            "--kube-mode",
            "local",
            "--no-trust-ssh-host-key",
            *extra_arguments,
        ]

    def var_log_index(self, archive: tarfile.TarFile) -> dict[str, list[str]]:
        """The /var/log evidence index, keyed by source path.

        Fields: 0 source, 1 family, 2 codec, 3 stored_bytes, 4 decoded_bytes,
        5 mtime_epoch, 6 disposition, 7 detail.
        """

        index = archive.extractfile("./nodes/monitor01/logs/var-log/INDEX.tsv")
        self.assertIsNotNone(index)
        lines = index.read().decode("utf-8").splitlines()
        rows = [line.split("\t") for line in lines[1:]]
        return {row[0]: row for row in rows}

    def stamp(self, path: Path, epoch: int) -> None:
        """Pin one source's mtime, which is what the Evidence Window reads."""

        os.utime(path, (epoch, epoch))

    def var_log_manifest_exit_codes(self, archive: tarfile.TarFile) -> dict[str, int]:
        """Manifest exit codes for the generated /var/log tree, keyed by artifact."""

        manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
        self.assertIsNotNone(manifest)
        tree = "/logs/var-log/"
        exit_codes: dict[str, int] = {}
        for line in manifest.read().splitlines():
            entry = json.loads(line)
            _, separator, relative = entry["artifact"].partition(tree)
            if separator:
                exit_codes[relative] = entry["exit_code"]
        return exit_codes


class CollectSingleNodeCliTests(NodeCollectorFixture, unittest.TestCase):
    def test_public_collect_streams_one_node_and_saves_basic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, ssh_log, payload_log = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^bundle: .+\.tar\.gz\n$")
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            self.assertTrue(bundle.is_file())
            self.assertEqual(payload_log.read_bytes(), NODE_COLLECTOR.read_bytes())
            ssh_arguments = json.loads(ssh_log.read_text(encoding="utf-8"))
            self.assertEqual(sum("10.0.0.1" in item for item in ssh_arguments), 1)
            self.assertIn("python3 -c", ssh_arguments[-1])

            with tarfile.open(bundle, "r:gz") as archive:
                names = {member.name.removeprefix("./") for member in archive}
                self.assertIn("cluster/rook/pods-wide.txt", names)
                self.assertIn("nodes/monitor01/manifest.jsonl", names)
                self.assertIn("nodes/monitor01/system/hostname.txt", names)
                hostname = archive.extractfile("./nodes/monitor01/system/hostname.txt")
                self.assertIsNotNone(hostname)
                self.assertIn(b"monitor01", hostname.read())

            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            app = var_log / "app"
            app.mkdir()
            (var_log / "syslog").write_text("current line\n", encoding="utf-8")
            (var_log / "syslog.1").write_text("middle line\n", encoding="utf-8")
            with gzip.open(var_log / "syslog.2.gz", "wb") as output:
                output.write(b"oldest line\n")
            (app / "service.log-20260721").write_text(
                "dated older\n", encoding="utf-8"
            )
            with lzma.open(app / "service.log-20260722.xz", "wb") as output:
                output.write(b"xz recent\n")
            with bz2.open(app / "service.log-20260723.bz2", "wb") as output:
                output.write(b"bz2 newer\n")
            zstd_source = root / "zstd-source"
            zstd_source.write_bytes(b"zstd newest rotation\n")
            subprocess.run(
                [
                    "zstd",
                    "-q",
                    "-f",
                    str(zstd_source),
                    "-o",
                    str(app / "service.log-20260724.zst"),
                ],
                check=True,
            )
            (app / "service.log").write_text("active\n", encoding="utf-8")
            # N6: a log far larger than any buffer is collected whole, never
            # truncated to a head or a tail.
            oversized = b"".join(
                f"bulk line {index:07d}\n".encode() for index in range(60_000)
            )
            (var_log / "bulk.log").write_bytes(oversized)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                syslog = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/syslog.merged"
                )
                self.assertIsNotNone(syslog)
                syslog_payload = syslog.read()
                self.assertLess(syslog_payload.index(b"oldest line"), syslog_payload.index(b"middle line"))
                self.assertLess(syslog_payload.index(b"middle line"), syslog_payload.index(b"current line"))
                service = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/dirs/app/files/service.log.merged"
                )
                self.assertIsNotNone(service)
                service_payload = service.read()
                self.assertLess(service_payload.index(b"dated older"), service_payload.index(b"active"))
                self.assertLess(service_payload.index(b"dated older"), service_payload.index(b"xz recent"))
                self.assertLess(service_payload.index(b"xz recent"), service_payload.index(b"bz2 newer"))
                self.assertLess(service_payload.index(b"bz2 newer"), service_payload.index(b"zstd newest"))
                self.assertLess(service_payload.index(b"zstd newest"), service_payload.index(b"active"))
                bulk = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/bulk.log.merged"
                )
                self.assertIsNotNone(bulk)
                # Merged families carry a provenance header, so the payload is
                # asserted whole rather than as the entire artifact.
                self.assertIn(oversized, bulk.read())
                index = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/INDEX.tsv"
                )
                self.assertIsNotNone(index)
                self.assertIn(b"syslog.2.gz\tsyslog\tgz", index.read())
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                artifacts = {
                    Path(json.loads(line)["artifact"]).name
                    for line in manifest.read().splitlines()
                }
                self.assertIn("syslog.merged", artifacts)
                self.assertIn("INDEX.tsv", artifacts)
                journal = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/journal-all-since.txt"
                )
                self.assertIsNotNone(journal)
                journal_payload = journal.read()
                self.assertIn(b"# timeout: 120s", journal_payload)
                self.assertIn(b"journal line", journal_payload)
                command_ledger = (root / "node-command.log").read_text(
                    encoding="utf-8"
                )
                self.assertIn("find ", command_ledger)
                self.assertIn("dd if=", command_ledger)
                self.assertIn("gzip -dc", command_ledger)
                self.assertIn("xz -dc", command_ledger)
                self.assertIn("bzip2 -dc", command_ledger)
                self.assertIn("zstd -qdc", command_ledger)
                self.assertIn("journalctl --since -24h --no-pager", command_ledger)
                forbidden_verbs = (
                    " create ",
                    " apply ",
                    " patch ",
                    " delete ",
                    " mount ",
                    " systemctl restart",
                )
                padded_ledger = f" {command_ledger.lower()} "
                self.assertFalse(
                    any(verb in padded_ledger for verb in forbidden_verbs)
                )
                self.assertFalse(
                    any("/logs/var-log/original/" in name for name in archive.getnames())
                )

    def test_the_evidence_window_bounds_var_log_and_indexes_what_it_left(self) -> None:
        """`--since` bounds `/var/log`, not just the journal query (ADR 0012).

        The fixture stamps distances from the window start rather than absolute
        dates, because the window start is `--since` before this node's clock.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            now = int(time.time())
            (var_log / "syslog").write_text("inside the window\n", encoding="utf-8")
            (var_log / "syslog.1").write_text("just before\n", encoding="utf-8")
            with gzip.open(var_log / "syslog.2.gz", "wb") as output:
                output.write(b"a month before\n")
            self.stamp(var_log / "syslog", now - 3600)
            self.stamp(var_log / "syslog.1", now - 25 * 3600)
            self.stamp(var_log / "syslog.2.gz", now - 30 * 86400)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                merged = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/syslog.merged"
                )
                self.assertIsNotNone(merged)
                payload = merged.read()
                self.assertIn(b"inside the window", payload)
                # The rotation that crosses the window start comes too: its
                # mtime is when the rotation happened, not what it spans.
                self.assertIn(b"just before", payload)
                self.assertNotIn(b"a month before", payload)
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/raw/syslog.2.gz",
                    archive.getnames(),
                )
                index = self.var_log_index(archive)
                self.assertEqual(index["syslog.2.gz"][6], "outside-window")
                self.assertEqual(
                    index["syslog.2.gz"][5], str(now - 30 * 86400)
                )
                self.assertEqual(
                    index["syslog.2.gz"][7],
                    "older than the evidence window start",
                )
                self.assertEqual(index["syslog.1"][6], "merge-candidate")
                self.assertEqual(
                    index["syslog.1"][7], "oldest-to-newest window-crossing"
                )
                self.assertEqual(index["syslog"][5], str(now - 3600))
                self.assertEqual(index["syslog"][7], "oldest-to-newest")

    def test_the_evidence_window_crosses_its_start_per_family_and_journal_stream(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            slow = var_log / "slow"
            slow.mkdir()
            machine_a = var_log / "journal" / "1111111111111111111111111111aaaa"
            machine_b = var_log / "journal" / "2222222222222222222222222222bbbb"
            machine_a.mkdir(parents=True)
            machine_b.mkdir(parents=True)
            now = int(time.time())
            # A family that rotated moments before the collect: the active file
            # is nearly empty and the incident is in the newest rotation.
            (var_log / "fast.log").write_text("post-rotation\n", encoding="utf-8")
            (var_log / "fast.log.1").write_text(
                "the incident is in here\n", encoding="utf-8"
            )
            self.stamp(var_log / "fast.log", now - 600)
            self.stamp(var_log / "fast.log.1", now - 25 * 3600)
            # A family that rotates far more slowly: nothing it has is inside.
            (slow / "slow.log").write_text("slow current\n", encoding="utf-8")
            (slow / "slow.log.1").write_text("slow rotation\n", encoding="utf-8")
            self.stamp(slow / "slow.log", now - 26 * 3600)
            self.stamp(slow / "slow.log.1", now - 40 * 86400)
            for name, directory, age in (
                ("system.journal", machine_a, 3600),
                ("system@0001.journal", machine_a, 25 * 3600),
                ("system@0002.journal", machine_a, 30 * 86400),
                ("user-1000@0001.journal", machine_a, 30 * 86400),
                ("system@0003.journal", machine_b, 30 * 86400),
            ):
                (directory / name).write_bytes(f"{name}\0\n".encode())
                self.stamp(directory / name, now - age)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                fast = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/fast.log.merged"
                )
                self.assertIsNotNone(fast)
                self.assertIn(b"the incident is in here", fast.read())
                slow_merged = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/dirs/slow/"
                    "files/slow.log.merged"
                )
                self.assertIsNotNone(slow_merged)
                slow_payload = slow_merged.read()
                # One family's rotation rhythm must not decide another's.
                self.assertIn(b"slow current", slow_payload)
                self.assertNotIn(b"slow rotation", slow_payload)
                names = set(archive.getnames())
                raw = "./nodes/monitor01/logs/var-log/raw/journal"
                self.assertIn(
                    f"{raw}/1111111111111111111111111111aaaa/system.journal", names
                )
                self.assertIn(
                    f"{raw}/1111111111111111111111111111aaaa/system@0001.journal",
                    names,
                )
                self.assertNotIn(
                    f"{raw}/1111111111111111111111111111aaaa/system@0002.journal",
                    names,
                )
                # A different stream on the same machine, and the same stream on
                # another machine, each get their own window-crossing file.
                self.assertIn(
                    f"{raw}/1111111111111111111111111111aaaa/user-1000@0001.journal",
                    names,
                )
                self.assertIn(
                    f"{raw}/2222222222222222222222222222bbbb/system@0003.journal",
                    names,
                )
                index = self.var_log_index(archive)
                self.assertEqual(index["slow/slow.log.1"][6], "outside-window")
                self.assertEqual(
                    index["journal/1111111111111111111111111111aaaa/"
                          "system@0002.journal"][6],
                    "outside-window",
                )
                self.assertEqual(
                    index["journal/2222222222222222222222222222bbbb/"
                          "system@0003.journal"][7],
                    "binary or unknown window-crossing",
                )

    def test_the_evidence_window_is_applied_before_the_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            now = int(time.time())
            (var_log / "big.log").write_text("current\n", encoding="utf-8")
            (var_log / "big.log.1").write_text("crossing\n", encoding="utf-8")
            (var_log / "big.log.2").write_bytes(b"a" * 300_000)
            (var_log / "big.log.3").write_bytes(b"b" * 300_000)
            self.stamp(var_log / "big.log", now - 600)
            self.stamp(var_log / "big.log.1", now - 25 * 3600)
            self.stamp(var_log / "big.log.2", now - 30 * 86400)
            self.stamp(var_log / "big.log.3", now - 31 * 86400)

            # The cap is far below the out-of-window bytes and far above what
            # the window keeps: it can only pass if the window was applied first.
            result = self.run_collect(
                root, environment, extra_arguments=("--var-log-max-bytes", "65536")
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/OVER-LIMIT.txt",
                    archive.getnames(),
                )
                merged = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/big.log.merged"
                )
                self.assertIsNotNone(merged)
                self.assertIn(b"crossing", merged.read())

    def test_a_source_the_window_cannot_date_is_collected_and_marked(self) -> None:
        """Quietly collecting less evidence is worse than collecting more."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            undatable = root / "var-log" / "messages"
            undatable.write_text("undatable evidence\n", encoding="utf-8")
            # Old enough that the window would drop it if it could read the date.
            self.stamp(undatable, int(time.time()) - 30 * 86400)
            environment["CEPH_INCIDENT_TEST_FORCE_SUDO"] = "1"
            environment["FAKE_STAT_UNDATABLE_PATH"] = str(undatable)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                merged = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/messages.merged"
                )
                self.assertIsNotNone(merged)
                self.assertIn(b"undatable evidence", merged.read())
                index = self.var_log_index(archive)
                self.assertEqual(index["messages"][5], "unknown")
                self.assertEqual(
                    index["messages"][7], "oldest-to-newest mtime-unknown"
                )

    def test_journal_failure_leaves_complete_log_entries_marked_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_JOURNALCTL_TIMEOUT"] = "1"
            var_log = root / "var-log"
            (var_log / "syslog").write_text("current line\n", encoding="utf-8")
            (var_log / "syslog.1").write_text("older line\n", encoding="utf-8")

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                exit_codes = self.var_log_manifest_exit_codes(archive)
            self.assertEqual(
                exit_codes,
                {
                    "journal-all-since.txt": 124,
                    "INDEX.tsv": 0,
                    "PAYLOAD-BYTES.txt": 0,
                    "merged/tree/files/syslog.merged": 0,
                },
            )

    def test_merge_read_failure_marks_only_the_affected_merged_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            real_gzip = shutil.which("gzip")
            self.assertIsNotNone(real_gzip)
            var_log = root / "var-log"
            with gzip.open(var_log / "app.log.1.gz", "wb") as output:
                output.write(b"rotated text\n")
            (var_log / "app.log").write_text("active text\n", encoding="utf-8")
            (var_log / "other.log").write_text("unaffected\n", encoding="utf-8")
            # app.log.1.gz is the only gz source, so its three `gzip -dc` runs
            # are measure, inspect, then merge. Failing the third means the
            # rotation is preserved raw and never reaches the merged family.
            fake_gzip = root / "bin" / "gzip"
            fake_gzip.unlink()
            fake_gzip.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "if [ \"${1:-}\" != \"-dc\" ]; then exec \"$REAL_GZIP\" \"$@\"; fi\n"
                "count=0\n"
                "[ ! -f \"$GZIP_COUNTER\" ] || count=$(sed -n '1p' \"$GZIP_COUNTER\")\n"
                "count=$((count + 1))\n"
                "printf '%s\\n' \"$count\" >\"$GZIP_COUNTER\"\n"
                "if [ \"$count\" -eq 3 ]; then printf 'partial decode\\n'; exit 7; fi\n"
                "exec \"$REAL_GZIP\" \"$@\"\n",
                encoding="utf-8",
            )
            fake_gzip.chmod(0o755)
            environment["REAL_GZIP"] = real_gzip
            environment["GZIP_COUNTER"] = str(root / "gzip-count")

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                errors = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/ERRORS.tsv"
                )
                self.assertIsNotNone(errors)
                self.assertIn(b"merge-read-failed", errors.read())
                exit_codes = self.var_log_manifest_exit_codes(archive)
            self.assertEqual(
                exit_codes["merged/tree/files/app.log.merged"], 2, exit_codes
            )
            self.assertEqual(
                {
                    relative: code
                    for relative, code in exit_codes.items()
                    if relative != "merged/tree/files/app.log.merged"
                },
                {
                    "journal-all-since.txt": 0,
                    "INDEX.tsv": 0,
                    "ERRORS.tsv": 0,
                    "PAYLOAD-BYTES.txt": 0,
                    "raw/app.log.1.gz": 0,
                    "merged/tree/files/other.log.merged": 0,
                },
            )

    def test_skip_logs_writes_only_the_explicit_skip_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "var-log" / "messages").write_text(
                "must remain unread\n", encoding="utf-8"
            )

            result = self.run_collect(
                root, environment, extra_arguments=("--skip-logs",)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                prefix = "./nodes/monitor01/logs/var-log/"
                log_files = {
                    name.removeprefix(prefix)
                    for name in archive.getnames()
                    if name.startswith(prefix) and name != prefix
                }
                self.assertEqual(log_files, {"SKIPPED.txt"})
                skipped = archive.extractfile(prefix + "SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"disabled by --skip-logs", skipped.read())
                # Pinned, not endorsed: whether an operator-declined collection
                # is indexed as complete evidence is open with #8.  An absent
                # `/var/log` is a different case and is indexed as incomplete —
                # see `test_an_absent_var_log_is_indexed_as_missing_evidence`.
                self.assertEqual(
                    self.var_log_manifest_exit_codes(archive), {"SKIPPED.txt": 0}
                )

    def test_an_absent_var_log_is_indexed_as_missing_evidence(self) -> None:
        """A node without `/var/log` stays complete, and says so honestly.

        ADR 0010: the marker is never indexed as exit 0.  The reference spends no
        journal budget when there was no payload to account for, so no journal
        artifact appears either and the node is not partial.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "var-log").rmdir()

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/SKIPPED.txt"
                )
                self.assertIsNotNone(skipped)
                self.assertIn(b"is not a directory", skipped.read())
                self.assertEqual(
                    self.var_log_manifest_exit_codes(archive), {"SKIPPED.txt": 2}
                )

    def test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            dd_log = root / "dd-argv.log"
            environment["FAKE_DD_LOG"] = str(dd_log)
            var_log = root / "var-log"
            journal = var_log / "journal"
            journal.mkdir()
            sources = {
                var_log / "support.zip": b"PK\x03\x04zip bytes\n",
                var_log / "support.tar.gz": b"tar-like bytes\n",
                var_log / "wtmp": b"\x00\x01\x02binary\n",
                journal / "system.journal": b"\x00journal bytes\n",
            }
            for path, payload in sources.items():
                path.write_bytes(payload)
                path.chmod(0o640)
                os.utime(path, (1_774_000_000, 1_774_000_000))
            outside = root / "outside-secret"
            outside.write_text("outside sentinel\n", encoding="utf-8")
            (var_log / "follow-me.log").symlink_to(outside)
            # The reference's forbidden-path rule is `*.<ext>` or `*.<ext>.*`,
            # so a sensitive name stays sensitive through the compressed and
            # numeric-rotation variants of every extension it names.
            for name in (
                "server.pem",
                "server.key.1",
                "client.crt",
                "store.p12",
            ):
                (var_log / name).write_bytes(b"private material\n")
            with gzip.open(var_log / "server.pem.gz", "wb") as output:
                output.write(b"compressed private material\n")
            sensitive = {
                var_log / "server.pem",
                var_log / "server.key.1",
                var_log / "client.crt",
                var_log / "store.p12",
                var_log / "server.pem.gz",
            }
            before_hashes = {
                path: hashlib.sha256(payload).hexdigest()
                for path, payload in sources.items()
            }
            before_metadata = {
                path: (
                    path.stat().st_mode,
                    path.stat().st_mtime_ns,
                    path.stat().st_atime_ns,
                )
                for path in sources
            }

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                for path, payload in sources.items():
                    relative = path.relative_to(var_log).as_posix()
                    raw = archive.extractfile(
                        f"./nodes/monitor01/logs/var-log/raw/{relative}"
                    )
                    self.assertIsNotNone(raw)
                    self.assertEqual(raw.read(), payload)
                names = set(archive.getnames())
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/raw/follow-me.log", names
                )
                skipped = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/SKIPPED-sensitive.txt"
                )
                self.assertIsNotNone(skipped)
                skipped_payload = skipped.read()
                for path in sorted(sensitive):
                    with self.subTest(sensitive=path.name):
                        # Neither copied anywhere in the tree, nor merged as
                        # text, but named as the path that was declined.
                        self.assertFalse(
                            any(path.name in name for name in names), sorted(names)
                        )
                        self.assertIn(path.name.encode(), skipped_payload)
                opaque = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/UNREDACTED-OPAQUE.txt"
                )
                self.assertIsNotNone(opaque)
                opaque_payload = opaque.read()
                for path in sources:
                    with self.subTest(opaque=path.name):
                        self.assertIn(path.name.encode(), opaque_payload)
                        # Opaque evidence is preserved raw, never merged.
                        self.assertFalse(
                            any(
                                name.startswith(
                                    "./nodes/monitor01/logs/var-log/merged/"
                                )
                                and path.name in name
                                for name in names
                            ),
                            sorted(names),
                        )

            after_metadata = {
                path: (
                    path.stat().st_mode,
                    path.stat().st_mtime_ns,
                    path.stat().st_atime_ns,
                )
                for path in sources
            }
            self.assertEqual(after_metadata, before_metadata)
            after_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sources
            }
            self.assertEqual(after_hashes, before_hashes)
            dd_invocations = dd_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(dd_invocations)
            self.assertTrue(
                all("iflag=noatime,nofollow" in line for line in dd_invocations)
            )
            self.assertFalse(any("follow-me.log" in line for line in dd_invocations))
            for path in sorted(sensitive):
                with self.subTest(unread=path.name):
                    self.assertFalse(
                        any(path.name in line for line in dd_invocations)
                    )

    def test_safe_read_failure_is_partial_without_an_unsafe_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            dd_log = root / "dd-argv.log"
            environment["FAKE_DD_LOG"] = str(dd_log)
            environment["FAKE_DD_FAIL_MATCH"] = "unreadable.log"
            (root / "var-log" / "unreadable.log").write_text(
                "must not escape through a fallback\n", encoding="utf-8"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertFalse(
                    any(name.endswith("/unreadable.log") for name in names)
                )
                errors = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/ERRORS.tsv"
                )
                self.assertIsNotNone(errors)
                self.assertIn(b"read-failed", errors.read())
            dd_invocations = dd_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(dd_invocations)
            self.assertTrue(
                all("iflag=noatime,nofollow" in line for line in dd_invocations)
            )

    def test_non_root_discovery_reads_and_journal_use_noninteractive_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_TEST_FORCE_SUDO"] = "1"
            (root / "var-log" / "messages").write_text(
                "privileged fixture\n", encoding="utf-8"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = (root / "node-command.log").read_text(encoding="utf-8")
            self.assertIn("sudo -n find ", ledger)
            self.assertIn("sudo -n dd if=", ledger)
            self.assertIn("sudo -n journalctl --since -24h --no-pager", ledger)
            blocked = subprocess.run(
                [str(root / "bin" / "sudo"), "-n", "mount", "/dev/fake", "/mnt"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 64)
            self.assertIn("unexpected fake sudo command: mount", blocked.stderr)

    def test_finite_cap_without_sudo_marks_missing_journal_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_TEST_FORCE_NO_SUDO"] = "1"
            (root / "var-log" / "messages").write_text(
                "bounded log\n", encoding="utf-8"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "4096"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                journal = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/journal-all-since.txt"
                )
                self.assertIsNotNone(journal)
                self.assertIn(b"sudo command not found", journal.read())

    def test_keep_original_logs_is_opt_in_and_preserves_stored_bytes(self) -> None:
        """V4: `original/` exists only with the flag, and then byte for byte."""

        for extra_arguments, expect_originals in (
            ((), False),
            (("--keep-original-logs",), True),
        ):
            with self.subTest(extra_arguments=extra_arguments), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                var_log = root / "var-log"
                (var_log / "messages").write_text("new\n", encoding="utf-8")
                with gzip.open(var_log / "messages.1.gz", "wb") as output:
                    output.write(b"old\n")

                result = self.run_collect(
                    root, environment, extra_arguments=extra_arguments
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    names = set(archive.getnames())
                    if not expect_originals:
                        self.assertFalse(
                            any(
                                "/logs/var-log/original/" in name for name in names
                            ),
                            sorted(names),
                        )
                        continue
                    # Both the plain file and the compressed rotation are kept
                    # exactly as they were stored on the node.
                    for name in ("messages", "messages.1.gz"):
                        original = archive.extractfile(
                            f"./nodes/monitor01/logs/var-log/original/{name}"
                        )
                        self.assertIsNotNone(original)
                        self.assertEqual(
                            original.read(), (var_log / name).read_bytes()
                        )

    def test_var_log_metadata_scan_limit_fails_closed_before_payload_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES"] = "5"
            (root / "var-log" / "a-very-long-log-name.log").write_text(
                "must not be collected\n", encoding="utf-8"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertIn(
                    "./nodes/monitor01/logs/var-log/SCAN-LIMIT.txt", names
                )

    def test_var_log_entry_count_limit_fails_closed_before_payload_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_VAR_LOG_MAX_ENTRIES"] = "1"
            for name in ("first.log", "second.log"):
                (root / "var-log" / name).write_text(
                    "must not be collected\n", encoding="utf-8"
                )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/SCAN-LIMIT.txt"
                )
                self.assertIsNotNone(marker)
                self.assertIn(b"max_entries=1", marker.read())
                names = set(archive.getnames())
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        for name in names
                    )
                )
                exit_codes = self.var_log_manifest_exit_codes(archive)
            # The scan stopped at the first entry, so the index that was
            # written is not a full listing of /var/log either.
            self.assertEqual(exit_codes["SCAN-LIMIT.txt"], 2, exit_codes)
            self.assertEqual(exit_codes["INDEX.tsv"], 2, exit_codes)

    def test_manifest_limit_discards_high_cardinality_payload_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_TEST_MANIFEST_MAX_BYTES"] = "16384"
            for index in range(40):
                name = f"service-{index:03d}-{'x' * 120}.log"
                (root / "var-log" / name).write_text(
                    f"payload {index}\n", encoding="utf-8"
                )

            result = self.run_collect(root, environment, node_timeout=60)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                diagnostic = b""
                if "./nodes/monitor01/SKIPPED.txt" in names:
                    skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                    diagnostic = skipped.read() if skipped is not None else b""
                self.assertIn(
                    "./nodes/monitor01/logs/var-log/MANIFEST-LIMIT.txt",
                    names,
                    (sorted(names), diagnostic),
                )
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/MANIFEST-LIMIT.txt"
                )
                self.assertIsNotNone(marker)
                self.assertIn(
                    b"status=partial-var-log-payload-discarded", marker.read()
                )
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        or "/logs/var-log/original/" in name
                        for name in names
                    )
                )
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                manifest_payload = manifest.read()
                self.assertLessEqual(len(manifest_payload), 16384)
                self.assertIn(b"MANIFEST-LIMIT.txt", manifest_payload)
                exit_codes = self.var_log_manifest_exit_codes(archive)
                self.assertEqual(exit_codes["MANIFEST-LIMIT.txt"], 2, exit_codes)
                self.assertEqual(exit_codes["INDEX.tsv"], 0, exit_codes)
                self.assertEqual(exit_codes["journal-all-since.txt"], 0, exit_codes)

    def test_insufficient_owned_workspace_space_is_partial_before_log_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_DF_AVAILABLE_KIB"] = "4"
            environment["CEPH_INCIDENT_VAR_LOG_FREE_RESERVE_BYTES"] = "4096"
            environment["CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES"] = "4096"
            (root / "var-log" / "messages").write_text(
                "must not be read\n", encoding="utf-8"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/INSUFFICIENT-SPACE.txt"
                )
                self.assertIsNotNone(marker)
                self.assertIn(b"status=not-collected", marker.read())
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        for name in names
                    )
                )
                exit_codes = self.var_log_manifest_exit_codes(archive)
            # Nothing was scanned, so neither the marker nor the header-only
            # index is complete evidence.
            self.assertEqual(exit_codes["INSUFFICIENT-SPACE.txt"], 2, exit_codes)
            self.assertEqual(exit_codes["INDEX.tsv"], 2, exit_codes)

    def test_insufficient_space_after_estimation_discards_log_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_VAR_LOG_FREE_RESERVE_BYTES"] = "0"
            environment["CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES"] = "4096"
            environment["FAKE_DF_AVAILABLE_FIRST_KIB"] = "1024"
            environment["FAKE_DF_AVAILABLE_SECOND_KIB"] = "0"
            environment["FAKE_DF_COUNTER"] = str(root / "df-counter")
            (root / "var-log" / "messages").write_text(
                "estimated payload\n", encoding="utf-8"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/INSUFFICIENT-SPACE.txt"
                )
                self.assertIsNotNone(marker)
                marker_payload = marker.read()
                self.assertIn(b"estimated_output_bytes=", marker_payload)
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        for name in names
                    )
                )

    def test_var_log_and_journal_share_one_fail_closed_payload_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_JOURNALCTL_LARGE"] = "1"
            (root / "var-log" / "messages").write_text(
                "small log\n", encoding="utf-8"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "1024"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/OVER-LIMIT.txt"
                )
                self.assertIsNotNone(marker)
                self.assertIn(
                    b"status=not-collected-journal-exceeded-remaining-cap",
                    marker.read(),
                )
                journal = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/journal-all-since.txt"
                )
                self.assertIsNotNone(journal)
                self.assertIn(b"combined /var/log and journal text exceeded", journal.read())
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/PAYLOAD-BYTES.txt", names
                )
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        or "/logs/var-log/original/" in name
                        for name in names
                    )
                )
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                entries = [json.loads(line) for line in manifest.read().splitlines()]
                journal_entry = next(
                    entry
                    for entry in entries
                    if entry["artifact"].endswith("journal-all-since.txt")
                )
                self.assertEqual(journal_entry["exit_code"], 75)
                exit_codes = self.var_log_manifest_exit_codes(archive)
                self.assertEqual(exit_codes["OVER-LIMIT.txt"], 2, exit_codes)
                self.assertEqual(exit_codes["INDEX.tsv"], 0, exit_codes)

    def test_combined_cap_discard_marks_the_journal_entry_incomplete(self) -> None:
        # The journal command itself succeeds here; only the combined total
        # breaks the cap, so the payload is discarded after a clean capture.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_JOURNALCTL_LARGE"] = "1"
            (root / "var-log" / "messages").write_text(
                "small log\n", encoding="utf-8"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "2200"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                marker = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/OVER-LIMIT.txt"
                )
                self.assertIsNotNone(marker)
                self.assertIn(
                    b"status=not-collected-combined-payload-exceeded-cap",
                    marker.read(),
                )
                journal = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/journal-all-since.txt"
                )
                self.assertIsNotNone(journal)
                self.assertIn(
                    b"combined /var/log and journal text exceeded", journal.read()
                )
                exit_codes = self.var_log_manifest_exit_codes(archive)
            self.assertEqual(exit_codes["journal-all-since.txt"], 2, exit_codes)
            self.assertEqual(exit_codes["OVER-LIMIT.txt"], 2, exit_codes)

    def test_journal_timeout_preserves_bounded_partial_output_and_manifest_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_JOURNALCTL_TIMEOUT"] = "1"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                journal = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/journal-all-since.txt"
                )
                self.assertIsNotNone(journal)
                journal_payload = journal.read()
                self.assertIn(b"partial journal before timeout", journal_payload)
                self.assertIn(b"# TRUNCATED: command timed out", journal_payload)
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                entries = [json.loads(line) for line in manifest.read().splitlines()]
                journal_entry = next(
                    entry
                    for entry in entries
                    if entry["artifact"].endswith("journal-all-since.txt")
                )
                self.assertEqual(journal_entry["exit_code"], 124)
                self.assertIn("journalctl --since -24h --no-pager", journal_entry["command"])

    def test_corrupt_and_missing_codecs_are_preserved_raw_and_partial(self) -> None:
        cases = ("corrupt", "missing")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                var_log = root / "var-log"
                if case == "corrupt":
                    source = var_log / "broken.log.1.gz"
                    source.write_bytes(b"not gzip\n")
                    expected_error = b"broken.log.1.gz\tdecode-failed"
                else:
                    source = var_log / "service.log.1.zst"
                    source.write_bytes(b"opaque zstd bytes\n")
                    environment["FAKE_REMOTE_PATH"] = (
                        f"{root / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin"
                    )
                    (root / "bin" / "zstd").unlink()
                    # The row names the codec that was missing, not merely that
                    # one was: a reader has to know what to install.
                    expected_error = b"service.log.1.zst\tmissing-codec:zstd"
                (var_log / "healthy.log").write_text(
                    "healthy\n", encoding="utf-8"
                )

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    raw = archive.extractfile(
                        f"./nodes/monitor01/logs/var-log/raw/{source.name}"
                    )
                    self.assertIsNotNone(raw)
                    self.assertEqual(raw.read(), source.read_bytes())
                    healthy = archive.extractfile(
                        "./nodes/monitor01/logs/var-log/merged/tree/files/healthy.log.merged"
                    )
                    self.assertIsNotNone(healthy)
                    self.assertIn(b"healthy", healthy.read())
                    errors = archive.extractfile(
                        "./nodes/monitor01/logs/var-log/ERRORS.tsv"
                    )
                    self.assertIsNotNone(errors)
                    self.assertIn(expected_error, errors.read())

    def test_var_log_cap_discards_all_payload_instead_of_truncating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "var-log" / "messages").write_text(
                "1234567890\n", encoding="utf-8"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "5"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertIn(
                    "./nodes/monitor01/logs/var-log/OVER-LIMIT.txt", names
                )
                self.assertFalse(
                    any(
                        "/logs/var-log/merged/" in name
                        or "/logs/var-log/raw/" in name
                        or "/logs/var-log/original/" in name
                        for name in names
                    )
                )

    def test_var_log_collision_safe_tree_base10_order_and_late_nul_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            (var_log / "app").mkdir()
            (var_log / "app.merged").mkdir()
            (var_log / "app.1").write_text("top level\n", encoding="utf-8")
            (var_log / "app" / "service.log").write_text(
                "nested\n", encoding="utf-8"
            )
            (var_log / "app.merged" / "service.log").write_text(
                "suffix directory\n", encoding="utf-8"
            )
            for suffix, payload in (
                ("010", "rotation ten\n"),
                ("09", "rotation nine\n"),
                ("08", "rotation eight\n"),
            ):
                (var_log / f"messages.{suffix}").write_text(payload, encoding="utf-8")
            (var_log / "messages").write_text("active\n", encoding="utf-8")
            mixed = b"A" * (1024 * 1024) + b"\0tail\n"
            (var_log / "mixed.log").write_bytes(mixed)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "2097152"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                # Each of the three colliding names keeps its own payload: a
                # collision that merely produced three files would still have
                # lost evidence.
                expected_paths = {
                    "merged/tree/files/app.merged": b"top level",
                    "merged/tree/dirs/app/files/service.log.merged": b"nested",
                    "merged/tree/dirs/app.merged/files/service.log.merged": (
                        b"suffix directory"
                    ),
                }
                for relative, expected in expected_paths.items():
                    with self.subTest(artifact=relative):
                        collected = archive.extractfile(
                            f"./nodes/monitor01/logs/var-log/{relative}"
                        )
                        self.assertIsNotNone(collected)
                        self.assertIn(expected, collected.read())
                messages = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/messages.merged"
                )
                self.assertIsNotNone(messages)
                payload = messages.read()
                self.assertLess(payload.index(b"rotation ten"), payload.index(b"rotation nine"))
                self.assertLess(payload.index(b"rotation nine"), payload.index(b"rotation eight"))
                self.assertLess(payload.index(b"rotation eight"), payload.index(b"active"))
                raw = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/raw/mixed.log"
                )
                self.assertIsNotNone(raw)
                self.assertEqual(raw.read(), mixed)
                # A NUL past the first block still makes the file binary, so
                # it is preserved raw and never merged as text.
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/mixed.log.merged",
                    set(archive.getnames()),
                )

    def test_later_decode_failures_roll_back_and_preserve_raw(self) -> None:
        for fail_at in (2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                real_gzip = shutil.which("gzip")
                self.assertIsNotNone(real_gzip)
                source = root / "var-log" / "app.log.1.gz"
                with gzip.open(source, "wb") as output:
                    output.write(b"archive text\n")
                counter = root / "gzip-count"
                fake_gzip = root / "bin" / "gzip"
                fake_gzip.unlink()
                fake_gzip.write_text(
                    "#!/bin/sh\n"
                    "set -eu\n"
                    "if [ \"${1:-}\" != \"-dc\" ]; then exec \"$REAL_GZIP\" \"$@\"; fi\n"
                    "count=0\n"
                    "[ ! -f \"$GZIP_COUNTER\" ] || count=$(sed -n '1p' \"$GZIP_COUNTER\")\n"
                    "count=$((count + 1))\n"
                    "printf '%s\\n' \"$count\" >\"$GZIP_COUNTER\"\n"
                    "if [ \"$count\" -ge \"$GZIP_FAIL_AT\" ]; then printf 'partial decode\\n'; exit 7; fi\n"
                    "exec \"$REAL_GZIP\" \"$@\"\n",
                    encoding="utf-8",
                )
                fake_gzip.chmod(0o755)
                environment["REAL_GZIP"] = real_gzip
                environment["GZIP_COUNTER"] = str(counter)
                environment["GZIP_FAIL_AT"] = str(fail_at)

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    raw = archive.extractfile(
                        "./nodes/monitor01/logs/var-log/raw/app.log.1.gz"
                    )
                    self.assertIsNotNone(raw)
                    self.assertEqual(raw.read(), source.read_bytes())
                    index = archive.extractfile(
                        "./nodes/monitor01/logs/var-log/INDEX.tsv"
                    )
                    self.assertIsNotNone(index)
                    index_payload = index.read()
                    if fail_at == 2:
                        self.assertIn(b"raw-partial\tdecode failed", index_payload)
                    else:
                        self.assertIn(b"merge-candidate\toldest-to-newest", index_payload)
                    names = set(archive.getnames())
                    merged_name = (
                        "./nodes/monitor01/logs/var-log/merged/tree/files/app.log.merged"
                    )
                    if merged_name in names:
                        merged = archive.extractfile(merged_name)
                        self.assertIsNotNone(merged)
                        self.assertNotIn(b"partial decode", merged.read())

    def test_python39_and_missing_python_are_skipped_in_a_partial_bundle(self) -> None:
        for mode, diagnostic in (
            ("python39", "Python 3.10 or newer is required"),
            ("missing-python", "python3: command not found"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                environment["FAKE_SSH_MODE"] = mode

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                    self.assertIsNotNone(skipped)
                    self.assertIn(
                        b"Python 3.10 or newer is unavailable", skipped.read()
                    )
                    summary = archive.extractfile("./summary.txt")
                    self.assertIsNotNone(summary)
                    self.assertIn(b"final_status: 2", summary.read())
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_valid_archive_is_preserved_when_node_collector_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_NODE_FAIL_COMMAND"] = "ip"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertNotIn("./nodes/monitor01/SKIPPED.txt", names)
                network = archive.extractfile("./nodes/monitor01/network/ip-addr.txt")
                self.assertIsNotNone(network)
                network_payload = network.read()
                self.assertIn(b"# host: monitor01\n", network_payload)
                self.assertIn(b"# collector: collect-node\n", network_payload)
                self.assertIn(b"# started: ", network_payload)
                self.assertIn(b"# timeout: ", network_payload)
                self.assertNotIn(b"# ended:", network_payload)
                self.assertNotIn(b"# exit_code:", network_payload)
                self.assertNotIn(b"# command:", network_payload)
                self.assertIn(b"simulated failure for ip", network_payload)
                manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
                self.assertIsNotNone(manifest)
                entries = [json.loads(line) for line in manifest.read().splitlines()]
                ip_entry = next(
                    entry for entry in entries if entry["command"] == "ip addr show"
                )
                self.assertEqual(ip_entry["exit_code"], 17)
                errors = archive.extractfile("./nodes/monitor01/errors.log")
                self.assertIsNotNone(errors)
                self.assertIn(b"command=ip addr show", errors.read())
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_untrusted_node_archives_are_rejected_before_extraction(self) -> None:
        expected_reasons = {
            "corrupt": "invalid or unreadable archive",
            "truncated": "invalid or unreadable archive",
            "missing-manifest": "missing manifest",
            "unsafe": "unsafe archive member",
            "unmanifested": "archive contains evidence without a manifest mapping",
            "duplicate-manifest": "duplicates an artifact mapping",
            "nonnumeric-exit-code": "invalid exit_code",
        }
        for mode, expected_reason in expected_reasons.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, _, _ = self.make_fake_environment(root)
                environment["FAKE_SSH_MODE"] = mode

                result = self.run_collect(root, environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                bundle = Path(result.stdout.removeprefix("bundle: ").strip())
                with tarfile.open(bundle, "r:gz") as archive:
                    names = set(archive.getnames())
                    self.assertIn("./nodes/monitor01/SKIPPED.txt", names)
                    self.assertNotIn(
                        "./nodes/monitor01/system/hostname.txt", names
                    )
                    skipped = archive.extractfile(
                        "./nodes/monitor01/SKIPPED.txt"
                    )
                    self.assertIsNotNone(skipped)
                    self.assertIn(expected_reason.encode(), skipped.read())
                self.assertFalse((root / "escape.txt").exists())
                self.assertFalse((root / "results" / "escape.txt").exists())
                self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_remote_fatal_failure_cleans_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "remote-failure"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"exit 74", skipped.read())
            self.assertIn("simulated archive failure", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_disconnect_signal_cleans_remote_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_NODE_SIGNAL_PARENT"] = "HUP"

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"no usable node archive", skipped.read())
            self.assertIn("node collector interrupted", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_timeout_cleans_remote_workspace_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "timeout"
            environment["FAKE_NODE_SLEEP_COMMAND"] = "hostname"

            result = self.run_collect(root, environment, node_timeout=3)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                skipped = archive.extractfile("./nodes/monitor01/SKIPPED.txt")
                self.assertIsNotNone(skipped)
                self.assertIn(b"timed out after 3s", skipped.read())
                # O21: a transport failure keeps its verbose re-probe in the
                # bundle, labelled with the node it belongs to.
                debug = archive.extractfile(
                    "./ssh-debug/node-monitor01-ceph_10.0.0.1.log"
                )
                self.assertIsNotNone(debug)
                debug_payload = debug.read()
                self.assertIn(b"# label: node-monitor01", debug_payload)
                self.assertIn(b"# target: ceph@10.0.0.1", debug_payload)
            self.assertIn("node collector interrupted", result.stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])

    def test_interruption_cleans_remote_and_workstation_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "timeout"
            environment["FAKE_NODE_SLEEP_COMMAND"] = "hostname"
            process = subprocess.Popen(
                self.prepare_collect(root, node_timeout=30),
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while not list((root / "remote-tmp").iterdir()):
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("node collector did not create its owned workspace")
                time.sleep(0.05)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("interrupted", stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])
            results = root / "results"
            self.assertEqual(list(results.glob("tmp.*")), [])
            self.assertEqual(list(results.glob("*.tar.gz")), [])

    def test_keep_workdir_preserves_the_workstation_workspace_on_interrupt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_SSH_MODE"] = "timeout"
            environment["FAKE_NODE_SLEEP_COMMAND"] = "hostname"
            process = subprocess.Popen(
                self.prepare_collect(
                    root,
                    node_timeout=30,
                    extra_arguments=("--keep-workdir",),
                ),
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while not list((root / "remote-tmp").iterdir()):
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("node collector did not create its owned workspace")
                time.sleep(0.05)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(stdout, "")
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])
            self.assertEqual(len(list((root / "results").glob("tmp.*"))), 1)

    def test_packaging_failure_rewrites_retained_summary_to_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "bin" / "tar").symlink_to("/usr/bin/false")

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 1, result.stderr)
            workdirs = list((root / "results").glob("tmp.*"))
            self.assertEqual(len(workdirs), 1)
            summary = (workdirs[0] / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("final_status: 1\n", summary)

    def test_packaging_interruption_removes_reserved_archive_and_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            fixture_bin = ROOT / "tests" / "fixtures" / "python-node" / "bin"
            (root / "bin" / "tar").symlink_to(fixture_bin / "tar-wrapper")
            marker = root / "packaging-started"
            environment["FAKE_LOCAL_TAR_MARKER"] = str(marker)
            process = subprocess.Popen(
                self.prepare_collect(root, node_timeout=30),
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 15
            while not marker.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("collector did not reach final bundle packaging")
                time.sleep(0.05)

            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("interrupted", stderr)
            self.assertEqual(list((root / "remote-tmp").iterdir()), [])
            results = root / "results"
            self.assertEqual(list(results.glob("tmp.*")), [])
            self.assertEqual(list(results.glob("*.tar.gz")), [])

    def test_unsafe_inventory_is_rejected_before_ssh_or_output_writes(self) -> None:
        cases = (
            'SSH_USER="$(touch should-not-exist)"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n',
            'SSH_USER="ceph"\nHOSTS=(\n  "../escape=10.0.0.1"\n)\n',
            'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=--ProxyCommand=bad"\n)\n',
        )
        for inventory_payload in cases:
            with self.subTest(inventory=inventory_payload), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                environment, ssh_log, _ = self.make_fake_environment(root)
                command = self.prepare_collect(root)
                (root / "inventory.env").write_text(
                    inventory_payload, encoding="utf-8"
                )

                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertFalse(ssh_log.exists())
                self.assertFalse((root / "should-not-exist").exists())
                self.assertFalse((root / "results").exists())


class NodeEvidenceSurfaceTests(NodeCollectorFixture, unittest.TestCase):
    """The node evidence surface beyond the basic commands and `/var/log`.

    Covers the shell scenarios N2, N3, N4, N5, N7, N8, N9, N10 and N13: the fixed
    artifact list, each artifact's provenance, the optional/privileged/absent
    dispositions, per-file config copies and the heavier timeout the kernel ring
    and the ceph journal get.
    """

    # Everything the node evidence surface must produce, `/var/log` aside.
    EXPECTED_ARTIFACTS = frozenset(
        {
            "system/hostname.txt",
            "system/uname.txt",
            "system/uptime.txt",
            "system/os-release",
            "system/hosts",
            "system/resolv.conf",
            "resources/free.txt",
            "resources/iostat.txt",
            "storage/df.txt",
            "storage/lsblk.txt",
            "storage/pvs.txt",
            "storage/vgs.txt",
            "storage/lvs.txt",
            "network/ip-addr.txt",
            "kernel/dmesg.txt",
            "systemd/failed-units.txt",
            "systemd/journal-ceph.txt",
            "time/chronyc-tracking.txt",
            "time/chronyc-sources.txt",
            "time/ntpq-peers.txt",
            "time/timedatectl-status.txt",
            "time/timedatectl-show-timesync.txt",
            "time/timedatectl-timesync-status.txt",
            "time/systemd-timesyncd-status.txt",
            "time/systemd-timesyncd-journal.txt",
            "time/systemd-timesyncd-config/timesyncd.conf",
            "time/systemd-timesyncd-config/timesyncd.conf.d/10-lab.conf",
            "containers/podman-ps.txt",
            "containers/docker-ps.txt",
            "cephadm/cephadm-ls.json",
            "cephadm/var-lib-ceph-listing.txt",
            "cephadm/var-lib-ceph-configs/fsid/mon.a/config",
        }
    )

    def make_fake_environment(self, root: Path) -> tuple[dict[str, str], Path, Path]:
        """The fake world plus the node-side files the evidence surface reads."""

        environment, ssh_log, payload_log = super().make_fake_environment(root)
        (root / "var-log" / "messages").write_text("current log\n", encoding="utf-8")
        etc = root / "etc"
        (etc / "os-release").write_text('NAME="Fake Linux"\n', encoding="utf-8")
        (etc / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
        # Most systemd hosts ship `/etc/resolv.conf` as a symlink, and the shell
        # reference follows it.
        (etc / "resolv-target").write_text("nameserver 10.0.0.53\n", encoding="utf-8")
        (etc / "resolv.conf").symlink_to(etc / "resolv-target")
        (root / "timesyncd.conf").write_text(
            "[Time]\nNTP=192.168.18.1\n", encoding="utf-8"
        )
        conf_directory = root / "timesyncd.conf.d"
        conf_directory.mkdir()
        (conf_directory / "10-lab.conf").write_text(
            "[Time]\nFallbackNTP=time.cloudflare.com\n", encoding="utf-8"
        )
        (conf_directory / "notes.txt").write_text(
            "not a drop-in\n", encoding="utf-8"
        )
        ceph_daemon = root / "var-lib-ceph" / "fsid" / "mon.a"
        ceph_daemon.mkdir(parents=True)
        (ceph_daemon / "config").write_text("fsid = fake\n", encoding="utf-8")
        (ceph_daemon / "keyring").write_text(
            "must never be collected\n", encoding="utf-8"
        )
        return environment, ssh_log, payload_log

    def node_evidence(self, bundle: Path) -> dict[str, bytes]:
        """Every node artifact outside the `/var/log` tree, keyed by relative path."""

        prefix = "./nodes/monitor01/"
        evidence: dict[str, bytes] = {}
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(prefix):
                    continue
                relative = member.name.removeprefix(prefix)
                if relative.startswith("logs/") or relative in (
                    "manifest.jsonl",
                    "errors.log",
                ):
                    continue
                payload = archive.extractfile(member)
                self.assertIsNotNone(payload)
                evidence[relative] = payload.read()
        return evidence

    def node_manifest(self, bundle: Path) -> dict[str, tuple[str, int]]:
        """Node manifest as artifact -> (command, exit_code), `/var/log` aside."""

        entries: dict[str, tuple[str, int]] = {}
        with tarfile.open(bundle, "r:gz") as archive:
            manifest = archive.extractfile("./nodes/monitor01/manifest.jsonl")
            self.assertIsNotNone(manifest)
            for line in manifest.read().splitlines():
                entry = json.loads(line)
                relative = entry["artifact"].split("/out/", 1)[-1]
                if relative.startswith("logs/"):
                    continue
                entries[relative] = (entry["command"], entry["exit_code"])
        return entries

    def collect_node_evidence(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        expected_exit: int = 0,
        extra_arguments: tuple[str, ...] = (),
    ) -> Path:
        result = self.run_collect(
            root, environment, extra_arguments=extra_arguments
        )
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        return Path(result.stdout.removeprefix("bundle: ").strip())

    def test_the_node_evidence_surface_is_complete_and_indexed(self) -> None:
        """N2, N3: the fixed artifact list, each carrying its command's output."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertEqual(set(evidence), self.EXPECTED_ARTIFACTS)
            # ADR 0010: the manifest indexes every artifact in the archive, so a
            # copied config and a skip marker are both traceable to a source.
            self.assertEqual(set(self.node_manifest(bundle)), self.EXPECTED_ARTIFACTS)
            self.assertIn(b'"style":"cephadm"', evidence["cephadm/cephadm-ls.json"])
            self.assertIn(b"fake kernel ring buffer", evidence["kernel/dmesg.txt"])
            self.assertIn(b"ceph journal line", evidence["systemd/journal-ceph.txt"])
            self.assertIn(b"FAKESERIAL", evidence["storage/lsblk.txt"])
            self.assertIn(
                b"System clock synchronized: yes",
                evidence["time/timedatectl-status.txt"],
            )
            self.assertIn(
                b"ServerName=192.168.18.1",
                evidence["time/timedatectl-show-timesync.txt"],
            )
            self.assertIn(
                b"Poll interval", evidence["time/timedatectl-timesync-status.txt"]
            )
            self.assertIn(
                b"Network Time Synchronization",
                evidence["time/systemd-timesyncd-status.txt"],
            )
            self.assertIn(
                b"systemd-timesyncd journal line",
                evidence["time/systemd-timesyncd-journal.txt"],
            )
            self.assertIn(b"nameserver 10.0.0.53", evidence["system/resolv.conf"])
            self.assertIn(b'NAME="Fake Linux"', evidence["system/os-release"])
            # An optional tool that fails is still evidence, and still leaves the
            # node complete: a node without a docker daemon is a normal node.
            self.assertIn(
                b"Cannot connect to the Docker daemon",
                evidence["containers/docker-ps.txt"],
            )
            self.assertEqual(
                self.node_manifest(bundle)["containers/docker-ps.txt"],
                ("docker ps -a", 1),
            )

    def test_optional_tools_receive_their_exact_argv(self) -> None:
        """N8: argument boundaries survive, including the single-space separator."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)

            self.collect_node_evidence(root, environment)

            ledger = (root / "node-argv.log").read_text(encoding="utf-8").splitlines()
            self.assertIn("iostat <-xz> <1> <3>", ledger)
            self.assertIn("chronyc <tracking>", ledger)
            self.assertIn("chronyc <sources> <-v>", ledger)
            self.assertIn("ntpq <-pn>", ledger)
            self.assertIn("timedatectl <show-timesync> <--all>", ledger)
            for report in ("pvs", "vgs", "lvs"):
                with self.subTest(report=report):
                    self.assertIn(
                        f"{report} <--noheadings> <--separator> < >", ledger
                    )
            self.assertIn("podman <ps> <-a>", ledger)
            self.assertIn("docker <ps> <-a>", ledger)
            self.assertIn("cephadm <ls> <--format> <json-pretty>", ledger)

    def test_a_missing_optional_tool_is_skipped_without_failing_the_node(self) -> None:
        """N4: an absent optional tool leaves a marker, not a partial node."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "bin" / "ntpq").unlink()
            (root / "bin" / "cephadm").unlink()

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertEqual(
                evidence["time/ntpq-peers.txt"],
                b"SKIPPED: command not found: ntpq\n",
            )
            self.assertEqual(
                evidence["cephadm/cephadm-ls.json"],
                b"SKIPPED: command not found: cephadm\n",
            )
            manifest = self.node_manifest(bundle)
            self.assertEqual(manifest["time/ntpq-peers.txt"], ("ntpq -pn", 127))
            self.assertEqual(
                manifest["cephadm/cephadm-ls.json"],
                ("cephadm ls --format json-pretty", 127),
            )
            ledger = (root / "node-argv.log").read_text(encoding="utf-8")
            self.assertNotIn("ntpq", ledger)
            self.assertNotIn("cephadm", ledger)

    def test_privileged_reads_go_through_noninteractive_sudo(self) -> None:
        """N9: the kernel ring and the ceph journal are read as root, or not at all."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)

            bundle = self.collect_node_evidence(root, environment)

            ledger = (root / "node-argv.log").read_text(encoding="utf-8").splitlines()
            self.assertIn("sudo <-n> <dmesg> <-T>", ledger)
            self.assertIn(
                "sudo <-n> <lsblk> <-a> <-o> "
                "<NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL>",
                ledger,
            )
            self.assertIn(
                "sudo <-n> <journalctl> <--since> <-24h> <-u> <ceph*> <--no-pager>",
                ledger,
            )
            # The `/var/lib/ceph` listing records a collector verb rather than
            # its find expression (ADR 0010), so the manifest cannot show that
            # the scan ran privileged.  The argv ledger has to, or a silent drop
            # to an unprivileged scan would go unnoticed.
            for scanned in (
                root / "var-lib-ceph",
                root / "timesyncd.conf.d",
            ):
                with self.subTest(scan_root=scanned):
                    self.assertTrue(
                        any(
                            line.startswith(f"sudo <-n> <find> <{scanned}>")
                            for line in ledger
                        ),
                        ledger,
                    )
            manifest = self.node_manifest(bundle)
            self.assertEqual(
                manifest["kernel/dmesg.txt"], ("sudo -n dmesg -T", 0)
            )
            self.assertEqual(
                manifest["systemd/journal-ceph.txt"],
                ("sudo -n journalctl --since -24h -u 'ceph*' --no-pager", 0),
            )

    def test_privileged_reads_without_sudo_are_skipped_not_faked(self) -> None:
        """A node that grants no sudo yields markers, never an unprivileged read."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["CEPH_INCIDENT_TEST_FORCE_NO_SUDO"] = "1"

            # An uncapped payload keeps the missing all-journal export out of the
            # node's status, so this asserts only the privileged-read policy.
            bundle = self.collect_node_evidence(
                root, environment, extra_arguments=("--var-log-max-bytes", "unlimited")
            )

            evidence = self.node_evidence(bundle)
            self.assertEqual(
                evidence["kernel/dmesg.txt"],
                b"SKIPPED: sudo command not found for privileged read: dmesg\n",
            )
            self.assertEqual(
                evidence["storage/lsblk.txt"],
                b"SKIPPED: sudo command not found for privileged read: lsblk\n",
            )
            self.assertEqual(
                evidence["systemd/journal-ceph.txt"],
                b"SKIPPED: command not found: sudo\n",
            )
            ledger = (root / "node-argv.log").read_text(encoding="utf-8")
            self.assertNotIn("dmesg", ledger)
            self.assertNotIn("lsblk", ledger)
            self.assertEqual(
                self.node_manifest(bundle)["kernel/dmesg.txt"],
                ("dmesg -T", 127),
            )

    def test_a_node_without_a_ceph_journal_is_not_partial(self) -> None:
        """N12: a node that runs no ceph unit still collects, and still passes."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_JOURNALCTL_NO_CEPH"] = "1"

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertIn(b"no entries", evidence["systemd/journal-ceph.txt"])
            self.assertEqual(
                self.node_manifest(bundle)["systemd/journal-ceph.txt"][1], 1
            )

    def test_dmesg_and_the_ceph_journal_get_the_heavier_timeout(self) -> None:
        """N10: a heavy read is bounded at 120s, not by the per-command timeout."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertIn(b"# timeout: 120s\n", evidence["kernel/dmesg.txt"])
            self.assertIn(b"# timeout: 120s\n", evidence["systemd/journal-ceph.txt"])
            # The per-command timeout still bounds everything else.
            self.assertIn(b"# timeout: 3s\n", evidence["storage/lsblk.txt"])
            self.assertIn(
                b"# timeout: 3s\n", evidence["time/systemd-timesyncd-journal.txt"]
            )

    def test_timesyncd_config_is_copied_file_by_file(self) -> None:
        """N5: the config and every `conf.d` drop-in land as their own artifacts."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            (root / "timesyncd.conf.d" / "20-pool.conf").write_text(
                "[Time]\nNTP=pool.example\n", encoding="utf-8"
            )

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            configuration = "time/systemd-timesyncd-config"
            self.assertEqual(
                evidence[f"{configuration}/timesyncd.conf"],
                b"[Time]\nNTP=192.168.18.1\n",
            )
            self.assertEqual(
                evidence[f"{configuration}/timesyncd.conf.d/10-lab.conf"],
                b"[Time]\nFallbackNTP=time.cloudflare.com\n",
            )
            self.assertEqual(
                evidence[f"{configuration}/timesyncd.conf.d/20-pool.conf"],
                b"[Time]\nNTP=pool.example\n",
            )
            # `conf.d` is a drop-in directory: only `*.conf` is configuration.
            self.assertNotIn(
                f"{configuration}/timesyncd.conf.d/notes.txt", evidence
            )
            manifest = self.node_manifest(bundle)
            self.assertEqual(
                manifest[f"{configuration}/timesyncd.conf"],
                (f"collect-node copy {root / 'timesyncd.conf'}", 0),
            )

    def test_a_node_without_timesyncd_stays_complete(self) -> None:
        """N13: no time daemon at all is a normal node, fully accounted for."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_TIMESYNCD_MISSING"] = "1"
            (root / "timesyncd.conf").unlink()
            shutil.rmtree(root / "timesyncd.conf.d")

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertIn(
                b"systemd-timesyncd unavailable",
                evidence["time/timedatectl-status.txt"],
            )
            self.assertIn(
                b"Unit systemd-timesyncd.service could not be found.",
                evidence["time/systemd-timesyncd-status.txt"],
            )
            self.assertIn(
                b"No journal files were found for systemd-timesyncd",
                evidence["time/systemd-timesyncd-journal.txt"],
            )
            skipped = evidence["time/systemd-timesyncd-config/SKIPPED.txt"]
            self.assertIn(b"systemd-timesyncd config not found at", skipped)
            self.assertIn(str(root / "timesyncd.conf").encode(), skipped)
            self.assertNotIn(
                "time/systemd-timesyncd-config/timesyncd.conf", evidence
            )
            self.assertEqual(
                self.node_manifest(bundle)[
                    "time/systemd-timesyncd-config/SKIPPED.txt"
                ][1],
                2,
            )

    def test_var_lib_ceph_configs_are_copied_without_credentials(self) -> None:
        """N7: configs are collected, keyrings are pruned before they are seen."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            osd = root / "var-lib-ceph" / "fsid" / "osd.1"
            osd.mkdir()
            (osd / "unit.run").write_text("podman run ...\n", encoding="utf-8")
            (osd / "ceph.conf").write_text("[global]\n", encoding="utf-8")
            (osd / "osd_private_key").write_text("never\n", encoding="utf-8")
            secrets = root / "var-lib-ceph" / "fsid" / ".ssh"
            secrets.mkdir()
            (secrets / "config").write_text("never\n", encoding="utf-8")

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            configs = "cephadm/var-lib-ceph-configs"
            self.assertEqual(
                evidence[f"{configs}/fsid/mon.a/config"], b"fsid = fake\n"
            )
            self.assertEqual(evidence[f"{configs}/fsid/osd.1/ceph.conf"], b"[global]\n")
            # `unit.run` is not configuration, and nothing under a pruned path or
            # matching a credential name is copied or even listed.
            self.assertNotIn(f"{configs}/fsid/osd.1/unit.run", evidence)
            for forbidden in (
                f"{configs}/fsid/mon.a/keyring",
                f"{configs}/fsid/osd.1/osd_private_key",
                f"{configs}/fsid/.ssh/config",
            ):
                with self.subTest(artifact=forbidden):
                    self.assertNotIn(forbidden, evidence)
            listing = evidence["cephadm/var-lib-ceph-listing.txt"]
            self.assertIn(b"d ", listing)
            self.assertIn(b"/fsid/mon.a/config\n", listing)
            # The prune stops at the credential itself: `.ssh` is named as a
            # directory, but nothing inside it is enumerated.
            for forbidden in (b"keyring", b"private_key", b"/.ssh/"):
                with self.subTest(entry=forbidden):
                    self.assertNotIn(forbidden, listing)
            self.assertEqual(
                self.node_manifest(bundle)["cephadm/var-lib-ceph-listing.txt"],
                (f"collect-node list {root / 'var-lib-ceph'}", 0),
            )

    def test_an_unstattable_config_costs_one_artifact_not_the_node(self) -> None:
        """#50: a source the collector cannot stat must not abort the node.

        The privileged scan finds a daemon directory this process cannot
        traverse. `Path.is_symlink()` raises EACCES there rather than answering,
        and that used to escape the node collector and lose every artifact for
        the host — where the shell reference simply falls through to its
        privileged read.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            # Depth 4 keeps it out of the `-maxdepth 3` listing scan, so only
            # the copy scan meets it.
            locked = root / "var-lib-ceph" / "fsid" / "mon.a" / "locked"
            locked.mkdir()
            config = locked / "config"
            config.write_text("fsid = unreadable\n", encoding="utf-8")
            environment["FAKE_FIND_EXTRA_PATH"] = str(config)
            dd_log = root / "dd-argv.log"
            environment["FAKE_DD_LOG"] = str(dd_log)
            if os.geteuid() == 0:
                self.skipTest("root can stat anything, so there is nothing to prove")
            locked.chmod(0o000)
            try:
                bundle = self.collect_node_evidence(
                    root, environment, expected_exit=2
                )
            finally:
                locked.chmod(0o700)

            evidence = self.node_evidence(bundle)
            artifact = "cephadm/var-lib-ceph-configs/fsid/mon.a/locked/config"
            self.assertIn(b"SKIPPED: copy failed: ", evidence[artifact])
            # The rest of the node is still there: the loss is one artifact
            # wide, not one host wide.
            self.assertIn(b"fsid = fake\n", evidence[
                "cephadm/var-lib-ceph-configs/fsid/mon.a/config"
            ])
            self.assertIn("system/hostname.txt", evidence)
            # The read is still attempted the way the shell reference attempts
            # it. A path the privileged scan found is reachable by a privileged
            # read, so refusing to try is its own way of losing evidence.
            self.assertIn(f"if={config}", dd_log.read_text(encoding="utf-8"))

    def test_an_unstattable_log_is_stated_with_sudo(self) -> None:
        """#50: `/var/log/ceph/<fsid>` is 0750, and the reference stats it anyway.

        Without a privileged fallback every ceph log on every node reads as
        `stat-failed`, which is the whole node's `/var/log` evidence gone for a
        permission the shell reference simply asks sudo about.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            locked = root / "var-log" / "ceph" / "fsid"
            locked.mkdir(parents=True)
            log = locked / "ceph-volume.log"
            log.write_text("volume line\n", encoding="utf-8")
            (root / "var-log" / "messages").write_text("m\n", encoding="utf-8")
            environment["FAKE_FIND_EXTRA_LOG_PATH"] = str(log)
            stat_log = root / "stat-argv.log"
            environment["FAKE_STAT_LOG"] = str(stat_log)
            if os.geteuid() == 0:
                self.skipTest("root can stat anything, so there is nothing to prove")
            locked.chmod(0o000)
            try:
                bundle = self.collect_node_evidence(
                    root, environment, expected_exit=2
                )
            finally:
                locked.chmod(0o700)

            self.assertIn(str(log), stat_log.read_text(encoding="utf-8"))
            # The rest of the node is untouched by one log the collector could
            # not size for itself.
            self.assertIn("system/hostname.txt", self.node_evidence(bundle))

    def test_an_absent_var_lib_ceph_is_a_marker_not_a_failure(self) -> None:
        """A kube-only node has no `/var/lib/ceph`, and that is not an error."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            shutil.rmtree(root / "var-lib-ceph")

            bundle = self.collect_node_evidence(root, environment)

            evidence = self.node_evidence(bundle)
            self.assertIn(
                b"is not a readable directory on this node",
                evidence["cephadm/var-lib-ceph-listing.txt"],
            )
            self.assertFalse(
                any(
                    name.startswith("cephadm/var-lib-ceph-configs/")
                    for name in evidence
                )
            )

    def test_a_failed_copy_names_the_evidence_it_lost(self) -> None:
        """ADR 0010: a copy that fails leaves a marker and a partial node."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            environment["FAKE_DD_FAIL_MATCH"] = "timesyncd.conf.d/10-lab.conf"

            bundle = self.collect_node_evidence(root, environment, expected_exit=2)

            evidence = self.node_evidence(bundle)
            artifact = (
                "time/systemd-timesyncd-config/timesyncd.conf.d/10-lab.conf"
            )
            self.assertIn(b"SKIPPED: copy failed: ", evidence[artifact])
            command, exit_code = self.node_manifest(bundle)[artifact]
            self.assertEqual(
                command,
                f"collect-node copy "
                f"{root / 'timesyncd.conf.d' / '10-lab.conf'}",
            )
            self.assertEqual(exit_code, 2)
            # The rest of the surface is still collected.
            self.assertEqual(
                evidence["time/systemd-timesyncd-config/timesyncd.conf"],
                b"[Time]\nNTP=192.168.18.1\n",
            )

    def test_a_failed_symlinked_copy_reports_the_path_it_was_asked_for(self) -> None:
        """A lost `/etc/resolv.conf` is named once, not once per indirection."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            # The read lands on the symlink's target, so that is what fails.
            environment["FAKE_DD_FAIL_MATCH"] = "resolv-target"

            bundle = self.collect_node_evidence(root, environment, expected_exit=2)

            source = root / "etc" / "resolv.conf"
            self.assertEqual(
                self.node_evidence(bundle)["system/resolv.conf"],
                f"SKIPPED: copy failed: {source}\n".encode(),
            )
            self.assertEqual(
                self.node_manifest(bundle)["system/resolv.conf"],
                (f"collect-node copy {source}", 2),
            )


if __name__ == "__main__":
    unittest.main()
