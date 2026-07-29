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


class CollectSingleNodeCliTests(unittest.TestCase):
    def make_fake_environment(self, root: Path) -> tuple[dict[str, str], Path, Path]:
        real_commands = {
            command: shutil.which(command)
            for command in ("gzip", "xz", "bzip2", "zstd", "find")
        }
        self.assertTrue(all(real_commands.values()))
        fake_bin = root / "bin"
        fake_bin.mkdir()
        ssh_log = root / "ssh-argv.json"
        payload_log = root / "ssh-stdin.py"
        remote_tmp = root / "remote-tmp"
        remote_tmp.mkdir()
        remote_bin = root / "remote-bin"
        remote_bin.mkdir()

        fixture_bin = ROOT / "tests" / "fixtures" / "python-node" / "bin"
        (fake_bin / "ssh").symlink_to(fixture_bin / "ssh")
        (fake_bin / "kubectl").symlink_to(
            ROOT / "tests" / "fixtures" / "python-rook" / "bin" / "kubectl"
        )
        for command in (
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
        ):
            (fake_bin / command).symlink_to(fixture_bin / "node-command")
        for command in ("gzip", "xz", "bzip2", "zstd"):
            (fake_bin / command).symlink_to(fixture_bin / "codec-command")
        (remote_bin / "tar").symlink_to(fixture_bin / "node-command")

        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(remote_tmp),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_SSH_PAYLOAD": str(payload_log),
            "FAKE_REMOTE_BIN": str(remote_bin),
            "CEPH_INCIDENT_VAR_LOG_DIR": str(root / "var-log"),
            "CEPH_INCIDENT_TEST_ALLOW_ATIME_READ": "1",
            "FAKE_NODE_COMMAND_LOG": str(root / "node-command.log"),
            "FAKE_REAL_GZIP": str(real_commands["gzip"]),
            "FAKE_REAL_XZ": str(real_commands["xz"]),
            "FAKE_REAL_BZIP2": str(real_commands["bzip2"]),
            "FAKE_REAL_ZSTD": str(real_commands["zstd"]),
            "FAKE_REAL_FIND": str(real_commands["find"]),
        }
        (root / "var-log").mkdir()
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
            (var_log / "server.pem").write_text(
                "private material\n", encoding="utf-8"
            )
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
                self.assertNotIn(
                    "./nodes/monitor01/logs/var-log/raw/server.pem", names
                )
                skipped = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/SKIPPED-sensitive.txt"
                )
                self.assertIsNotNone(skipped)
                self.assertIn(b"server.pem", skipped.read())
                opaque = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/UNREDACTED-OPAQUE.txt"
                )
                self.assertIsNotNone(opaque)
                opaque_payload = opaque.read()
                self.assertIn(b"support.zip", opaque_payload)
                self.assertIn(b"wtmp", opaque_payload)

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
            self.assertFalse(any("server.pem" in line for line in dd_invocations))

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
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _, _ = self.make_fake_environment(root)
            var_log = root / "var-log"
            (var_log / "messages").write_text("new\n", encoding="utf-8")
            with gzip.open(var_log / "messages.1.gz", "wb") as output:
                output.write(b"old\n")

            result = self.run_collect(
                root, environment, extra_arguments=("--keep-original-logs",)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = Path(result.stdout.removeprefix("bundle: ").strip())
            with tarfile.open(bundle, "r:gz") as archive:
                for name in ("messages", "messages.1.gz"):
                    original = archive.extractfile(
                        f"./nodes/monitor01/logs/var-log/original/{name}"
                    )
                    self.assertIsNotNone(original)
                    self.assertEqual(original.read(), (var_log / name).read_bytes())

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
                    expected_error = b"decode-failed"
                else:
                    source = var_log / "service.log.1.zst"
                    source.write_bytes(b"opaque zstd bytes\n")
                    environment["FAKE_REMOTE_PATH"] = (
                        f"{root / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin"
                    )
                    (root / "bin" / "zstd").unlink()
                    expected_error = b"missing-codec"
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
                expected_paths = (
                    "merged/tree/files/app.merged",
                    "merged/tree/dirs/app/files/service.log.merged",
                    "merged/tree/dirs/app.merged/files/service.log.merged",
                )
                for relative in expected_paths:
                    self.assertIsNotNone(
                        archive.extractfile(
                            f"./nodes/monitor01/logs/var-log/{relative}"
                        )
                    )
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

    def test_unsupported_node_is_skipped_in_a_partial_bundle(self) -> None:
        for mode, diagnostic in (
            ("unsupported", "Python 3.11 or newer is required"),
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
                        b"Python 3.11 or newer is unavailable", skipped.read()
                    )
                    summary = archive.extractfile("./summary.txt")
                    self.assertIsNotNone(summary)
                    self.assertIn(b"final_status=2", summary.read())
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


if __name__ == "__main__":
    unittest.main()
