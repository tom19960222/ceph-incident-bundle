from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ceph_incident_bundle as bundle_entry

from tests.test_python_collect_ceph import DirectCephFixture


ROOT = Path(__file__).resolve().parents[1]


class CollectContentSafetyTests(DirectCephFixture, unittest.TestCase):
    def test_collect_redacts_sensitive_text_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_CEPH_SECRET_CONFIG="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            config_dump = contents["cluster/ceph/json/config-dump.json"]
            self.assertIn("[REDACTED]", config_dump)
            self.assertNotIn("must-not-leak", config_dump)
            self.assertIn(
                "cluster/ceph/json/config-dump.json",
                contents["redactions.log"],
            )

    def test_redaction_handles_ascii_whitespace_and_a_long_single_line(self) -> None:
        cases = (
            ("FAKE_CEPH_WHITESPACE_SECRET", "whitespace-must-redact"),
            ("FAKE_CEPH_LONG_GAP_SECRET", "long-gap-must-redact"),
        )
        for knob, secret in cases:
            with self.subTest(knob=knob):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, _ = self.make_fake_environment(root, **{knob: "1"})

                    result = self.run_collect(root, environment)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    config_dump = contents["cluster/ceph/json/config-dump.json"]
                    self.assertEqual(config_dump.splitlines()[-1], "[REDACTED]")
                    self.assertNotIn(secret, config_dump)

    def test_ceph_key_material_and_private_key_blocks_are_redacted(self) -> None:
        """Key material, both spellings, plus a whole PEM block; safe lines stay."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_CEPH_KEY_MATERIAL_CONFIG="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            config_dump = contents["cluster/ceph/json/config-dump.json"]
            for secret in (
                "AQBn0123456789012345678901234567890123456==",
                "BEGIN OPENSSH PRIVATE KEY",
                "END OPENSSH PRIVATE KEY",
                "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB",
            ):
                with self.subTest(secret=secret):
                    self.assertNotIn(secret, config_dump)
            self.assertIn("mon_host = 10.0.0.1", config_dump)
            self.assertIn("osd_pool_default_size = 3", config_dump)
            self.assertEqual(config_dump.count("[REDACTED]"), 5)

    def test_no_redact_keeps_sensitive_text_and_still_writes_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_CEPH_SECRET_CONFIG="1"
            )

            result = self.run_collect(
                root, environment, extra_arguments=("--no-redact",)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "must-not-leak", contents["cluster/ceph/json/config-dump.json"]
            )
            self.assertEqual(contents["redactions.log"], "")

    def test_redaction_flag_precedence_is_independent_from_host_key_trust(
        self,
    ) -> None:
        cases = (
            (
                ("--trust-ssh-host-key", "--no-redact"),
                "must-not-leak",
                True,
            ),
            (("--no-redact", "--redact"), "[REDACTED]", False),
        )
        for extra_arguments, expected_content, expected_trust in cases:
            with self.subTest(extra_arguments=extra_arguments):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, ledger = self.make_fake_environment(
                        root, FAKE_CEPH_SECRET_CONFIG="1"
                    )

                    result = self.run_collect(
                        root, environment, extra_arguments=extra_arguments
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    self.assertIn(
                        expected_content,
                        contents["cluster/ceph/json/config-dump.json"],
                    )
                    invocations = [
                        json.loads(line)
                        for line in ledger.read_text(encoding="utf-8").splitlines()
                    ]
                    trusted = any(
                        "StrictHostKeyChecking=accept-new" in arguments
                        for arguments in invocations
                    )
                    self.assertEqual(trusted, expected_trust)

    def test_compressed_text_is_redacted_but_opaque_raw_evidence_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_COMPRESSED_SECRET="1"
            )
            expected_opaque = gzip.compress(
                b"secret = intentionally opaque\n", mtime=0
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                compressed = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/ceph.log.gz"
                )
                opaque = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/raw/opaque.tar.gz"
                )
                self.assertIsNotNone(compressed)
                self.assertIsNotNone(opaque)
                redacted_payload = gzip.decompress(compressed.read())
                opaque_payload = opaque.read()

            self.assertIn(b"[REDACTED]", redacted_payload)
            self.assertNotIn(b"must-not-leak", redacted_payload)
            self.assertEqual(
                hashlib.sha256(opaque_payload).digest(),
                hashlib.sha256(expected_opaque).digest(),
            )

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required for codec parity")
    def test_all_supported_compressed_text_codecs_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_ALL_CODECS_SECRET="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            payloads: dict[str, bytes] = {}
            modes: dict[str, int] = {}
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                for suffix in ("gz", "xz", "bz2", "zst"):
                    name = (
                        "./nodes/monitor01/logs/var-log/merged/tree/files/"
                        f"ceph.log.{suffix}"
                    )
                    stream = archive.extractfile(name)
                    self.assertIsNotNone(stream)
                    payloads[suffix] = stream.read()
                    modes[suffix] = archive.getmember(name).mode

            decoded = {
                "gz": gzip.decompress(payloads["gz"]),
                "xz": lzma.decompress(payloads["xz"]),
                "bz2": bz2.decompress(payloads["bz2"]),
                "zst": subprocess.run(
                    ["zstd", "-qdc"],
                    input=payloads["zst"],
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout,
            }
            for suffix, payload in decoded.items():
                with self.subTest(suffix=suffix):
                    self.assertIn(b"[REDACTED]", payload)
                    self.assertNotIn(b"must-not-leak", payload)
                    self.assertEqual(modes[suffix], 0o600)

    def test_compressed_decode_failure_preserves_opaque_bytes_and_stays_successful(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_CORRUPT_GZIP="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                corrupt = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/corrupt.log.gz"
                )
                redactions = archive.extractfile("./redactions.log")
                self.assertIsNotNone(corrupt)
                self.assertIsNotNone(redactions)
                corrupt_payload = corrupt.read()
                redaction_log = redactions.read().decode()

            self.assertEqual(corrupt_payload, b"not a gzip stream\n")
            self.assertIn("decompress failed", redaction_log)
            self.assertIn("NOT redacted", redaction_log)

    def test_compressed_expansion_over_safety_cap_is_preserved_and_partial(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_COMPRESSED_BOMB="1"
            )
            environment["CEPH_INCIDENT_TEST_REDACTION_DECODE_CAP_BYTES"] = "4096"
            expected = gzip.compress(
                b"safe evidence\n"
                + b"x" * (64 * 1024)
                + b"\nsecret = must-not-leak\n",
                mtime=0,
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                compressed = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/bomb.log.gz"
                )
                self.assertIsNotNone(compressed)
                preserved = compressed.read()
            self.assertEqual(
                hashlib.sha256(preserved).digest(), hashlib.sha256(expected).digest()
            )
            self.assertIn("decompressed payload exceeds safety cap", contents["redactions.log"])
            self.assertIn("NOT redacted", contents["redactions.log"])
            self.assertIn(
                "redaction failed; original collected artifact was preserved",
                contents["errors.log"],
            )

    def test_recompress_failure_preserves_original_and_continues_other_redactions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root,
                FAKE_NODE_COMPRESSED_SECRET="1",
                FAKE_CEPH_SECRET_CONFIG="1",
            )
            fake_gzip = root / "bin" / "gzip"
            fake_gzip.write_text(
                "#!/bin/sh\n"
                "case \"$1:$3\" in\n"
                "  -c:*/.ceph.log.gz.plain.*) exit 9 ;;\n"
                "esac\n"
                "exec /usr/bin/gzip \"$@\"\n",
                encoding="utf-8",
            )
            fake_gzip.chmod(0o755)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "[REDACTED]", contents["cluster/ceph/json/config-dump.json"]
            )
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                compressed = archive.extractfile(
                    "./nodes/monitor01/logs/var-log/merged/tree/files/ceph.log.gz"
                )
                self.assertIsNotNone(compressed)
                preserved = gzip.decompress(compressed.read())
            self.assertIn(b"must-not-leak", preserved)
            self.assertIn("recompress failed", contents["redactions.log"])
            self.assertIn(
                "redaction failed; original collected artifact was preserved",
                contents["errors.log"],
            )

    def test_prometheus_metric_exclusion_is_anchored_to_the_cluster_layer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root,
                FAKE_CURL_SECRET_METRIC="1",
                FAKE_NODE_PROMETHEUS_LOOKALIKE="1",
            )
            fake_bin = root / "bin"
            (fake_bin / "curl").symlink_to(ROOT / "tests/fixtures/bin/curl")
            (fake_bin / "grep").symlink_to(
                ROOT / "tests/fixtures/python-prometheus/bin/grep"
            )
            environment.update(
                {
                    "FAKE_CURL_LOG": str(root / "curl.log"),
                    "FAKE_CURL_ARGV_LOG": str(root / "curl-argv.nul"),
                    "FAKE_GREP_LOG": str(root / "grep-argv.jsonl"),
                }
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    "http://prom.example:9090",
                    "--prom-job-regex",
                    "ceph|secret",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn("[REDACTED]", contents["cluster/prometheus/dump-info.txt"])
            with tarfile.open(self.bundle_of(result), "r:gz") as archive:
                metric = archive.extractfile(
                    "./cluster/prometheus/ceph/ceph_health_status.json.gz"
                )
                lookalike = archive.extractfile(
                    "./nodes/monitor01/cluster/prometheus/fake-job/lookalike.json.gz"
                )
                self.assertIsNotNone(metric)
                self.assertIsNotNone(lookalike)
                metric_payload = gzip.decompress(metric.read())
                lookalike_payload = gzip.decompress(lookalike.read())

            self.assertIn(b"metric-must-stay", metric_payload)
            self.assertNotIn(b"[REDACTED]", metric_payload)
            self.assertIn(b"[REDACTED]", lookalike_payload)
            self.assertNotIn(b"must-redact-lookalike", lookalike_payload)

    def test_post_redaction_payload_cap_discards_node_log_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_POST_REDACTION_CAP="1"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "8"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertNotIn(
                "nodes/monitor01/logs/var-log/merged/tree/files/ceph.merged",
                contents,
            )
            marker = contents["nodes/monitor01/logs/var-log/OVER-LIMIT.txt"]
            self.assertIn("status=not-collected-post-redaction-cap", marker)
            self.assertIn("node monitor01 log payload exceeded cap", contents["errors.log"])

    def test_post_redaction_cap_counts_a_regular_payload_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_NODE_PAYLOAD_ROOT_FILE="1"
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--var-log-max-bytes", "8"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertNotIn("nodes/monitor01/logs/var-log/merged", contents)
            marker = contents["nodes/monitor01/logs/var-log/OVER-LIMIT.txt"]
            self.assertIn("actual_payload_bytes=10", marker)

    def test_post_redaction_payload_accounting_updates_below_cap_and_skips_unlimited(
        self,
    ) -> None:
        for cap, expected_bytes in (("20", "11\n"), ("unlimited", "6\n")):
            with self.subTest(cap=cap):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, _ = self.make_fake_environment(
                        root, FAKE_NODE_POST_REDACTION_CAP="1"
                    )

                    result = self.run_collect(
                        root,
                        environment,
                        extra_arguments=("--var-log-max-bytes", cap),
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    self.assertIn(
                        "nodes/monitor01/logs/var-log/merged/tree/files/ceph.merged",
                        contents,
                    )
                    self.assertEqual(
                        contents["nodes/monitor01/logs/var-log/PAYLOAD-BYTES.txt"],
                        expected_bytes,
                    )
                    self.assertNotIn(
                        "nodes/monitor01/logs/var-log/OVER-LIMIT.txt", contents
                    )

    def test_pre_package_verification_failure_keeps_a_diagnostic_workdir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(
                root, FAKE_CEPH_PRIVATE_KEY_CONFIG="1"
            )

            result = self.run_collect(
                root, environment, extra_arguments=("--no-redact",)
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            workdirs = list((root / "results").glob("tmp.*"))
            self.assertEqual(len(workdirs), 1)
            self.assertIn(
                "final_status: 1\n",
                (workdirs[0] / "summary.txt").read_text(encoding="utf-8"),
            )
            errors = (workdirs[0] / "errors.log").read_text(encoding="utf-8")
            self.assertIn("bundle verification failed", errors)
            self.assertIn("key material", errors)
            self.assertEqual(list((root / "results").glob("*.tar.gz")), [])

    def test_packaged_archive_verification_failure_removes_the_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, _ = self.make_fake_environment(root)
            fake_tar = root / "bin" / "tar"
            fake_tar.write_text(
                "#!/bin/sh\nprintf 'not a tar archive\\n' > \"$3\"\n",
                encoding="utf-8",
            )
            fake_tar.chmod(0o755)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            workdirs = list((root / "results").glob("tmp.*"))
            self.assertEqual(len(workdirs), 1)
            self.assertFalse((workdirs[0] / ".incident-bundle.tar.gz").exists())
            errors = (workdirs[0] / "errors.log").read_text(encoding="utf-8")
            self.assertIn("bundle verification failed", errors)
            self.assertEqual(list((root / "results").glob("*.tar.gz")), [])


class RedactionFilePermissionTests(unittest.TestCase):
    """Ledger C9 and C12: redaction rewrites evidence without widening its mode.

    The end-to-end cases cannot prove preservation, because every artifact they
    see was created by a collector that already fixes the mode at 0600.  These
    call the redaction seam directly on evidence that arrives with a different
    mode, which is the only way the claim is actually tested.
    """

    def _log(self, root: Path) -> Path:
        log = root / "redactions.log"
        log.touch()
        return log

    def test_plain_redaction_keeps_the_original_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "ceph.conf"
            artifact.write_text(
                "mon_host = 10.0.0.1\nkey = AQBn0123456789012345678901234567890123456==\n",
                encoding="utf-8",
            )
            artifact.chmod(0o640)

            bundle_entry._redact_plain_file(artifact, self._log(root))

            self.assertEqual(artifact.stat().st_mode & 0o7777, 0o640)
            redacted = artifact.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", redacted)
            self.assertIn("mon_host = 10.0.0.1", redacted)

    def test_compressed_redaction_keeps_the_original_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "ceph.log.gz"
            artifact.write_bytes(
                gzip.compress(b"safe evidence\nsecret = must-not-leak\n", mtime=0)
            )
            artifact.chmod(0o640)

            complete = bundle_entry._redact_compressed_file(artifact, self._log(root))

            self.assertTrue(complete)
            self.assertEqual(artifact.stat().st_mode & 0o7777, 0o640)
            payload = gzip.decompress(artifact.read_bytes())
            self.assertIn(b"[REDACTED]", payload)
            self.assertNotIn(b"must-not-leak", payload)

    def test_recompress_failure_keeps_the_original_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "ceph.log.gz"
            original = gzip.compress(b"secret = must-not-leak\n", mtime=0)
            artifact.write_bytes(original)
            artifact.chmod(0o640)
            log = self._log(root)
            failing_encoder = {".gz": (("gzip", "-dc", "--"), ("false",))}

            with mock.patch.dict(
                bundle_entry.COMPRESSED_CODECS, failing_encoder, clear=False
            ):
                complete = bundle_entry._redact_compressed_file(artifact, log)

            self.assertFalse(complete)
            self.assertEqual(artifact.read_bytes(), original)
            self.assertEqual(artifact.stat().st_mode & 0o7777, 0o640)
            self.assertIn("recompress failed", log.read_text(encoding="utf-8"))
            self.assertEqual(sorted(item.name for item in root.iterdir()),
                             ["ceph.log.gz", "redactions.log"])


if __name__ == "__main__":
    unittest.main()
