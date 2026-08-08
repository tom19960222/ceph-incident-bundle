from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ceph_incident_collectors import (  # noqa: E402
    _prometheus_auto_step,
    mask_prometheus_url,
    prometheus_duration_seconds,
)

ENTRYPOINT = ROOT / "ceph_incident_bundle.py"
# The Prometheus HTTP boundary is faked by the very same whitelist curl the
# shell reference is tested with, so an argv assertion here is an assertion of
# shell equivalence rather than of a Python-only convention.
FAKE_CURL = ROOT / "tests" / "fixtures" / "bin" / "curl"
FAKE_SSH = ROOT / "tests" / "fixtures" / "python-prometheus" / "bin" / "ssh"
FAKE_GREP = ROOT / "tests" / "fixtures" / "python-prometheus" / "bin" / "grep"
FAKE_KUBECTL = (
    ROOT / "tests" / "fixtures" / "python-prometheus" / "bin" / "kubectl"
)
PROM_URL = "http://prom.example:9090"


class PrometheusFixture:
    """Black-box helpers: fake curl/ssh on PATH, public CLI, bundle reading."""

    def make_fake_environment(
        self, root: Path, **knobs: str
    ) -> tuple[dict[str, str], Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "curl").symlink_to(FAKE_CURL)
        (fake_bin / "ssh").symlink_to(FAKE_SSH)
        (fake_bin / "grep").symlink_to(FAKE_GREP)
        (fake_bin / "kubectl").symlink_to(FAKE_KUBECTL)
        curl_ledger = root / "curl-argv.nul"
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CURL_LOG": str(root / "curl.log"),
            "FAKE_CURL_ARGV_LOG": str(curl_ledger),
            "FAKE_GREP_LOG": str(root / "grep-argv.jsonl"),
            "FAKE_SSH_LOG": str(root / "ssh-argv.jsonl"),
            **knobs,
        }
        return environment, curl_ledger

    def run_collect(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        extra_arguments: tuple[str, ...] = ("--prom-url", PROM_URL),
    ) -> subprocess.CompletedProcess[str]:
        inventory = root / "inventory.env"
        inventory.write_text(
            'SSH_USER="ceph"\nHOSTS=(\n  "monitor01=10.0.0.1"\n)\n', encoding="utf-8"
        )
        ssh_key = root / "id_ed25519"
        ssh_key.write_text("fixture key path only\n", encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(ENTRYPOINT),
                "collect",
                "--inventory",
                str(inventory),
                "--ssh-key",
                str(ssh_key),
                "--out",
                str(root / "results"),
                "--timeout",
                "5",
            "--node-timeout",
            "20",
            "--mode",
            "rook",
            "--kube-mode",
            "local",
            "--no-trust-ssh-host-key",
                *extra_arguments,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def bundle_of(self, result: subprocess.CompletedProcess[str]) -> Path:
        self.assertRegex(result.stdout, r"^bundle: .+\.tar\.gz\n$")
        bundle = Path(result.stdout.removeprefix("bundle: ").strip())
        self.assertTrue(bundle.is_file())
        return bundle

    def extract(self, bundle: Path) -> dict[str, bytes]:
        contents: dict[str, bytes] = {}
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                self.assertIsNotNone(stream)
                contents[member.name.removeprefix("./")] = stream.read()
        return contents

    def text_of(self, contents: dict[str, bytes], name: str) -> str:
        self.assertIn(name, sorted(contents))
        return contents[name].decode("utf-8", errors="replace")

    def curl_commands(self, curl_ledger: Path) -> list[list[str]]:
        """Read the fake curl's lossless NUL-delimited argv ledger."""

        if not curl_ledger.exists():
            return []
        invocations = curl_ledger.read_bytes().split(b"\0\0")
        return [
            [argument.decode("utf-8") for argument in invocation.split(b"\0")]
            for invocation in invocations
            if invocation
        ]

    def manifest_entries(self, contents: dict[str, bytes]) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.text_of(contents, "manifest.jsonl").splitlines()
            if line.strip()
        ]

    def prometheus_entries(
        self, contents: dict[str, bytes]
    ) -> list[dict[str, object]]:
        return [
            entry
            for entry in self.manifest_entries(contents)
            if entry["collector"] == "collect-prometheus"
        ]

    def dump_info(self, contents: dict[str, bytes]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in self.text_of(
            contents, "cluster/prometheus/dump-info.txt"
        ).splitlines():
            key, _, value = line.partition("=")
            fields[key] = value
        return fields

    def assert_bundle_verifies(self, bundle: Path) -> None:
        python_verify = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "verify", str(bundle)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(python_verify.returncode, 0, python_verify.stderr)
        self.assertIn("VERIFY PASS", python_verify.stdout)

class PrometheusGrammarTests(unittest.TestCase):
    """P1, P2, P3 at the seam the shell reference tests them: pure functions.

    The end-to-end cases each exercise one window, one step and one URL shape,
    so the conversion table, the step floor and the no-credential case of the
    masker are only covered here.
    """

    def test_the_duration_grammar_converts_every_documented_unit(self) -> None:
        for value, expected in (
            ("90", 90),
            ("45s", 45),
            ("30m", 1800),
            ("24h", 86400),
            ("7d", 604800),
            ("2w", 1209600),
            # A pasted leading zero is base ten, not octal.
            ("010h", 36000),
            ("008", 8),
        ):
            with self.subTest(since=value):
                self.assertEqual(prometheus_duration_seconds(value), expected)

    def test_the_duration_grammar_rejects_what_it_cannot_mean(self) -> None:
        for value in ("yesterday", "5x", "", "0", "000", "-1", "1.5h", "24 h"):
            with self.subTest(since=value):
                self.assertIsNone(prometheus_duration_seconds(value))

    def test_the_auto_step_keeps_a_floor_and_bounds_the_point_count(self) -> None:
        # Short windows would ask for a sub-second step, so the floor applies.
        for window in (60, 3600, 86400):
            with self.subTest(window=window):
                self.assertEqual(_prometheus_auto_step(window), 15)
        # ceil(604800 / 10000) keeps a week under the 11k point limit.
        self.assertEqual(_prometheus_auto_step(604800), 61)
        self.assertEqual(_prometheus_auto_step(1209600), 121)

    def test_masking_hides_a_password_and_leaves_everything_else_alone(self) -> None:
        self.assertEqual(
            mask_prometheus_url("http://u:sekrit@h"), "http://u:***@h"
        )
        self.assertEqual(
            mask_prometheus_url("http://u:s3cr@t@h:9090/x"),
            "http://u:***@h:9090/x",
        )
        # A URL without credentials is recorded exactly as it was given.
        for url in (
            "http://prom.example:9090",
            "https://prom.example/prefix",
            "http://prom.example:9090/path@notauth",
        ):
            with self.subTest(url=url):
                self.assertEqual(mask_prometheus_url(url), url)


class PrometheusHappyPathTests(PrometheusFixture, unittest.TestCase):
    def test_prom_url_collects_metrics_evidence_for_matching_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)

            build_info = json.loads(
                self.text_of(contents, "cluster/prometheus/buildinfo.json")
            )
            self.assertEqual(build_info["data"]["version"], "2.51.0")
            self.assertIn("activeTargets", self.text_of(
                contents, "cluster/prometheus/targets.json"
            ))

            # Metric dumps land gzipped, one per metric of each matching job.
            for artifact in (
                "cluster/prometheus/ceph/ceph_health_status.json.gz",
                "cluster/prometheus/ceph/ceph_osd_up.json.gz",
                "cluster/prometheus/node-exporter/node_load1.json.gz",
            ):
                self.assertIn(artifact, contents)
            dump = gzip.decompress(
                contents["cluster/prometheus/ceph/ceph_health_status.json.gz"]
            )
            self.assertIn(b'"status":"success"', dump)
            # A job the regex does not match is never collected at all.
            self.assertFalse(
                [name for name in contents if name.startswith("cluster/prometheus/grafana/")]
            )

            index = self.text_of(contents, "cluster/prometheus/ceph/index.txt")
            self.assertIn("ok ceph_health_status ceph_health_status.json.gz\n", index)
            self.assertIn("ok ceph_osd_up ceph_osd_up.json.gz\n", index)

            fields = self.dump_info(contents)
            self.assertEqual(fields["url"], PROM_URL)
            self.assertEqual(fields["since"], "24h")
            self.assertEqual(
                int(fields["window_end_epoch"]) - int(fields["window_start_epoch"]),
                86400,
            )
            self.assertEqual(fields["step_seconds"], "15")
            self.assertEqual(fields["job_regex"], "ceph|node")
            self.assertEqual(fields["jobs_seen"], "ceph node-exporter grafana")
            self.assertEqual(fields["jobs_matched"], "ceph node-exporter")
            self.assertEqual(fields["metrics_ok"], "3")
            self.assertEqual(fields["metrics_failed"], "0")
            self.assertEqual(fields["truncated"], "0")

            # The layer's own scratch responses are never bundled as evidence.
            self.assertFalse(
                [name for name in contents if Path(name).name.startswith(".")]
            )
            # …and the metrics layer does not displace the node evidence.
            self.assertIn("nodes/monitor01/system/hostname.txt", contents)

            environment_text = self.text_of(contents, "environment.txt")
            self.assertIn(f"prom_url={PROM_URL}\n", environment_text)
            self.assertIn("prom_jobs=ceph node-exporter\n", environment_text)

            entries = self.prometheus_entries(contents)
            self.assertEqual(len(entries), 4)
            for entry in entries:
                self.assertEqual(entry["host"], "prometheus")
                self.assertEqual(entry["exit_code"], 0)
                self.assertTrue(str(entry["command"]).startswith(f"GET {PROM_URL}/"))
            self.assertEqual(
                [Path(str(entry["artifact"])).name for entry in entries],
                ["buildinfo.json", "targets.json", "index.txt", "index.txt"],
            )

            self.assert_bundle_verifies(bundle)

    def test_requests_match_the_shell_curl_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            fields = self.dump_info(contents)
            start = fields["window_start_epoch"]
            end = fields["window_end_epoch"]
            commands = self.curl_commands(curl_ledger)

            def fixed(url: str, *params: str) -> list[str]:
                return [
                    "-q",
                    "-fsS",
                    "-G",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "5",
                    "-o",
                    "<out>",
                    url,
                    *[word for param in params for word in ("--data-urlencode", param)],
                ]

            normalised = []
            for argv in commands:
                copy = list(argv)
                copy[copy.index("-o") + 1] = "<out>"
                normalised.append(copy)

            self.assertEqual(
                normalised[:3],
                [
                    fixed(f"{PROM_URL}/api/v1/status/buildinfo"),
                    fixed(f"{PROM_URL}/api/v1/targets"),
                    fixed(f"{PROM_URL}/api/v1/label/job/values"),
                ],
            )
            self.assertIn(
                fixed(
                    f"{PROM_URL}/api/v1/label/__name__/values",
                    'match[]={job="ceph"}',
                    f"start={start}",
                    f"end={end}",
                ),
                normalised,
            )
            self.assertIn(
                fixed(
                    f"{PROM_URL}/api/v1/query_range",
                    'query={__name__="ceph_osd_up",job="ceph"}',
                    f"start={start}",
                    f"end={end}",
                    "step=15",
                ),
                normalised,
            )


class PrometheusQueryShapeTests(PrometheusFixture, unittest.TestCase):
    """Window, step, job filter, URL and timeout become the request argv."""

    def test_a_long_window_raises_the_auto_step_above_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--since", "7d"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            fields = self.dump_info(contents)
            self.assertEqual(
                int(fields["window_end_epoch"]) - int(fields["window_start_epoch"]),
                604800,
            )
            # ceil(604800 / 10000) keeps every series under the 11k point limit.
            self.assertEqual(fields["step_seconds"], "61")
            self.assertTrue(
                any(
                    "step=61" in argv
                    for argv in self.curl_commands(curl_ledger)
                )
            )

    def test_an_explicit_step_overrides_the_automatic_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    PROM_URL,
                    "--since",
                    "7d",
                    "--prom-step",
                    "300",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            for argv in self.curl_commands(curl_ledger):
                self.assertNotIn("step=61", argv)
            self.assertTrue(
                any("step=300" in argv for argv in self.curl_commands(curl_ledger))
            )

    def test_a_trailing_slash_never_doubles_in_a_request_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", f"{PROM_URL}//"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.curl_commands(curl_ledger)
            self.assertTrue(commands)
            for argv in commands:
                for word in argv:
                    self.assertNotIn("9090//api", word)
            contents = self.extract(self.bundle_of(result))
            self.assertEqual(self.dump_info(contents)["url"], PROM_URL)

    def test_the_command_timeout_bounds_every_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--timeout", "9"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.curl_commands(curl_ledger)
            self.assertTrue(commands)
            for argv in commands:
                self.assertEqual(argv[argv.index("--connect-timeout") + 1], "9")
                self.assertEqual(argv[argv.index("--max-time") + 1], "9")

    def test_the_job_filter_is_case_insensitive_and_never_an_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", "CEPH"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            fields = self.dump_info(contents)
            self.assertEqual(fields["job_regex"], "CEPH")
            self.assertEqual(fields["jobs_matched"], "ceph")
            self.assertIn("cluster/prometheus/ceph/index.txt", contents)
            self.assertFalse(
                [
                    name
                    for name in contents
                    if name.startswith("cluster/prometheus/node-exporter/")
                ]
            )

    def test_a_dash_leading_job_filter_matches_nothing_without_an_option_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", "-zzz"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "no scrape job matched",
                self.text_of(contents, "cluster/prometheus/SKIPPED.txt"),
            )
            # The filter is a pattern, never an option: the matcher receives it
            # after `--`, so a dash-leading regex matches nothing instead of
            # being read as an option.  Asserting the argv is the only way to
            # see this — the matcher's own diagnostics are discarded, so a
            # "stderr says nothing about grep" assertion could never fail.
            grep_invocations = [
                json.loads(line)
                for line in (root / "grep-argv.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(grep_invocations)
            for arguments in grep_invocations:
                self.assertEqual(arguments[:2], ["-qiE", "--"])
                self.assertEqual(arguments[2], "-zzz")

    def test_the_job_filter_uses_the_shell_posix_ere_dialect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    PROM_URL,
                    "--prom-job-regex",
                    "[[:alpha:]]+",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            fields = self.dump_info(self.extract(self.bundle_of(result)))
            self.assertEqual(fields["jobs_matched"], "ceph node-exporter grafana")


class PrometheusUnavailableTests(PrometheusFixture, unittest.TestCase):
    """Collecting no metrics evidence is a partial bundle naming its cause."""

    def test_an_unreachable_server_skips_the_layer_with_the_curl_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_DOWN="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
            self.assertTrue(skipped.startswith("SKIPPED: "), skipped)
            self.assertIn("prometheus not reachable", skipped)
            self.assertIn(PROM_URL, skipped)
            self.assertIn("curl exit 7", skipped)
            self.assertIn("Failed to connect", skipped)

            # The probe's own truncated output must not survive as evidence,
            # and nothing is claimed in the manifest that was never collected.
            self.assertNotIn("cluster/prometheus/buildinfo.json", contents)
            self.assertNotIn("cluster/prometheus/dump-info.txt", contents)
            self.assertEqual(self.prometheus_entries(contents), [])

            errors = self.text_of(contents, "errors.log")
            self.assertIn("prometheus dump skipped", errors)
            self.assertIn("prometheus collection exited 2", errors)
            self.assertIn("final_status: 2", self.text_of(contents, "summary.txt"))
            # Only the connectivity probe runs; nothing else is attempted.
            self.assertEqual(len(self.curl_commands(curl_ledger)), 1)
            self.assert_bundle_verifies(bundle)

    def test_a_failed_job_listing_skips_the_layer_after_keeping_what_worked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_FAIL_PATHS="/api/v1/label/job/values"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
            self.assertIn("prometheus job listing failed", skipped)
            self.assertIn("curl exit 22", skipped)

            self.assertIn("cluster/prometheus/buildinfo.json", contents)
            self.assertIn("cluster/prometheus/targets.json", contents)
            self.assertEqual(len(self.prometheus_entries(contents)), 2)
            self.assert_bundle_verifies(bundle)

    def test_a_malformed_job_listing_is_a_skip_not_a_crash(self) -> None:
        for label, body in (
            ("not-json", "<html>gateway</html>"),
            ("error-status", '{"status":"error","errorType":"bad_data"}'),
            ("not-an-object", '["ceph"]'),
        ):
            with self.subTest(response=label):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, curl_ledger = self.make_fake_environment(
                        root, FAKE_CURL_JOBS_JSON=body
                    )

                    result = self.run_collect(root, environment)

                    self.assertEqual(result.returncode, 2, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
                    self.assertIn("prometheus job listing failed", skipped)
                    self.assertIn("unparseable JSON", skipped)

    def test_an_empty_job_listing_names_the_jobs_it_saw(self) -> None:
        for label, body in (
            ("empty-data", '{"status":"success","data":[]}'),
            # A success without data[] is an empty listing, not a malformed one.
            ("absent-data", '{"status":"success"}'),
        ):
            with self.subTest(response=label):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    environment, curl_ledger = self.make_fake_environment(
                        root, FAKE_CURL_JOBS_JSON=body
                    )

                    result = self.run_collect(root, environment)

                    self.assertEqual(result.returncode, 2, result.stderr)
                    contents = self.extract(self.bundle_of(result))
                    skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
                    self.assertIn("no scrape job matched regex 'ceph|node'", skipped)
                    self.assertIn("jobs seen: <none>", skipped)


class PrometheusPartialCollectionTests(PrometheusFixture, unittest.TestCase):
    """A failed request degrades its own artifact, never the whole layer."""

    def index_of(self, contents: dict[str, bytes], job: str) -> str:
        return self.text_of(contents, f"cluster/prometheus/{job}/index.txt")

    def job_entry(self, contents: dict[str, bytes], job: str) -> dict[str, object]:
        entries = [
            entry
            for entry in self.prometheus_entries(contents)
            if str(entry["artifact"]).endswith(f"/{job}/index.txt")
        ]
        self.assertEqual(len(entries), 1, entries)
        return entries[0]

    def test_a_failed_targets_fetch_keeps_the_metrics_dump_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_FAIL_PATHS="/api/v1/targets"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertNotIn("cluster/prometheus/targets.json", contents)
            self.assertIn("cluster/prometheus/buildinfo.json", contents)
            self.assertIn(
                "cluster/prometheus/ceph/ceph_health_status.json.gz", contents
            )
            self.assertIn(
                "prometheus targets fetch failed", self.text_of(contents, "errors.log")
            )
            targets_entry = [
                entry
                for entry in self.prometheus_entries(contents)
                if str(entry["artifact"]).endswith("targets.json")
            ]
            self.assertEqual([entry["exit_code"] for entry in targets_entry], [22])
            self.assert_bundle_verifies(bundle)

    def test_a_failed_metric_listing_marks_its_job_and_keeps_going(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_FAIL_PATHS="/api/v1/label/__name__/values"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            errors = self.text_of(contents, "errors.log")
            for job in ("ceph", "node-exporter"):
                self.assertIn(
                    f"FAILED: metric listing for job {job}",
                    self.index_of(contents, job),
                )
                self.assertEqual(self.job_entry(contents, job)["exit_code"], 2)
                self.assertIn(
                    f"prometheus metric listing failed for job {job}", errors
                )
            # A listing that failed leaves no metric dump and no query behind.
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            for argv in self.curl_commands(curl_ledger):
                self.assertNotIn(f"{PROM_URL}/api/v1/query_range", argv)
            # Everything collected before the failure is still evidence.
            self.assertIn("cluster/prometheus/buildinfo.json", contents)
            self.assertIn("cluster/prometheus/targets.json", contents)
            self.assert_bundle_verifies(bundle)

    def test_a_failed_metric_query_leaves_no_dump_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_FAIL_METRICS="ceph_osd_up"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertNotIn("cluster/prometheus/ceph/ceph_osd_up.json.gz", contents)
            self.assertNotIn("cluster/prometheus/ceph/ceph_osd_up.json", contents)
            self.assertIn(
                "cluster/prometheus/ceph/ceph_health_status.json.gz", contents
            )
            index = self.index_of(contents, "ceph")
            self.assertIn("failed ceph_osd_up -\n", index)
            self.assertIn("ok ceph_health_status ceph_health_status.json.gz\n", index)
            self.assertIn(
                "prometheus query_range failed job=ceph metric=ceph_osd_up",
                self.text_of(contents, "errors.log"),
            )
            fields = self.dump_info(contents)
            self.assertEqual(fields["metrics_ok"], "2")
            self.assertEqual(fields["metrics_failed"], "1")
            self.assertEqual(self.job_entry(contents, "ceph")["exit_code"], 2)
            self.assert_bundle_verifies(bundle)

    def test_a_malformed_metric_response_is_not_kept_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_MALFORMED_PATHS="/api/v1/query_range"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            # A 200 that is not a Prometheus success response is a failure.
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            index = self.index_of(contents, "ceph")
            self.assertIn("failed ceph_health_status -\n", index)
            self.assertIn("failed ceph_osd_up -\n", index)
            self.assertEqual(self.dump_info(contents)["metrics_failed"], "3")
            self.assert_bundle_verifies(bundle)

    def test_a_non_string_metric_name_makes_the_listing_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_NAMES_JSON='{"status":"success","data":[42]}',
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", "ceph"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "FAILED: metric listing for job ceph\n",
                self.text_of(contents, "cluster/prometheus/ceph/index.txt"),
            )
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            self.assert_bundle_verifies(bundle)

    def test_a_timed_out_request_is_recorded_with_its_curl_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_TIMEOUT_PATHS="/api/v1/query_range"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            errors = self.text_of(contents, "errors.log")
            self.assertIn("curl exit 28", errors)
            self.assertIn("Operation timed out", errors)
            self.assert_bundle_verifies(bundle)

    def test_a_timed_out_connectivity_probe_skips_the_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_TIMEOUT_PATHS="/api/v1/"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
            self.assertIn("prometheus not reachable", skipped)
            self.assertIn("curl exit 28", skipped)
            self.assertEqual(len(self.curl_commands(curl_ledger)), 1)

    def test_a_job_without_metrics_is_an_empty_but_complete_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", "grafana"),
            )

            # An empty result set is not a collection failure.
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertEqual(self.index_of(contents, "grafana"), "")
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            entry = self.job_entry(contents, "grafana")
            self.assertEqual(entry["exit_code"], 0)
            self.assertIn("(0 metrics)", str(entry["command"]))
            fields = self.dump_info(contents)
            self.assertEqual(fields["jobs_matched"], "grafana")
            self.assertEqual(fields["metrics_ok"], "0")
            self.assert_bundle_verifies(bundle)


class PrometheusUnsafeNameTests(PrometheusFixture, unittest.TestCase):
    """Names read back from the server never become argv or path surprises."""

    def test_an_unsafe_job_name_is_skipped_and_the_safe_jobs_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_JOBS_JSON=(
                    json.dumps(
                        {
                            "status": "success",
                            "data": ["ceph", 'node"x', "node\\back", "node\x00x"],
                        }
                    )
                ),
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            errors = self.text_of(contents, "errors.log")
            self.assertIn('prometheus job skipped (unsafe name): node"x', errors)
            self.assertIn(
                "prometheus job skipped (unsafe name): node\\\\back", errors
            )
            self.assertIn(
                "prometheus job skipped (unsafe name): node\\x00x", errors
            )
            self.assertIn("cluster/prometheus/ceph/index.txt", contents)
            for argv in self.curl_commands(curl_ledger):
                for word in argv:
                    self.assertNotIn('node"x', word)
                    self.assertNotIn("node\\back", word)
                    self.assertNotIn("node\x00x", word)
            self.assert_bundle_verifies(bundle)

    def test_an_unsafe_metric_name_never_becomes_a_file_or_a_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_NAMES_JSON=(
                    '{"status":"success","data":["../../escape","ceph_ok",'
                    '"with space","ceph:sub:metric"]}'
                ),
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", "ceph"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            index = self.text_of(contents, "cluster/prometheus/ceph/index.txt")
            self.assertIn("skipped ../../escape unsafe-name\n", index)
            self.assertIn("skipped with space unsafe-name\n", index)
            self.assertIn("ok ceph_ok ceph_ok.json.gz\n", index)
            # A colon is legal in PromQL but not wanted in a filename.
            self.assertIn(
                "ok ceph:sub:metric ceph__sub__metric.json.gz\n", index
            )
            self.assertIn("cluster/prometheus/ceph/ceph__sub__metric.json.gz", contents)
            self.assertFalse([name for name in contents if "escape" in name])
            for argv in self.curl_commands(curl_ledger):
                for word in argv:
                    self.assertNotIn("../../escape", word)
            self.assertIn(
                "prometheus metric skipped (unsafe name) job=ceph metric=../../escape",
                self.text_of(contents, "errors.log"),
            )
            self.assert_bundle_verifies(bundle)

    def test_distinct_safe_names_never_overwrite_each_others_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_JOBS_JSON=json.dumps(
                    {
                        "status": "success",
                        "data": ["ceph/a", "ceph:a", "CEPH_A", "."],
                    }
                ),
                FAKE_CURL_NAMES_JSON=json.dumps(
                    {
                        "status": "success",
                        "data": ["foo:bar", "foo__bar", "FOO__BAR"],
                    }
                ),
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    PROM_URL,
                    "--prom-job-regex",
                    "ceph|^[.]$",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            indexes = sorted(
                name
                for name in contents
                if name.startswith("cluster/prometheus/") and name.endswith("/index.txt")
            )
            dumps = sorted(
                name
                for name in contents
                if name.startswith("cluster/prometheus/") and name.endswith(".json.gz")
            )
            self.assertEqual(len(indexes), 4, indexes)
            self.assertNotIn("cluster/prometheus/index.txt", indexes)
            self.assertEqual(len(dumps), 12, dumps)
            job_entries = [
                entry
                for entry in self.prometheus_entries(contents)
                if str(entry["artifact"]).endswith("/index.txt")
            ]
            self.assertEqual(len({entry["artifact"] for entry in job_entries}), 4)

    def test_job_names_never_collide_with_fixed_prometheus_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixed_names = [
                "buildinfo.json",
                "targets.json",
                "dump-info.txt",
                "SKIPPED.txt",
            ]
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_JOBS_JSON=json.dumps(
                    {"status": "success", "data": fixed_names}
                ),
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", ".*"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            for fixed_name in ("buildinfo.json", "targets.json", "dump-info.txt"):
                self.assertIn(f"cluster/prometheus/{fixed_name}", contents)
            indexes = [
                name
                for name in contents
                if name.startswith("cluster/prometheus/") and name.endswith("/index.txt")
            ]
            self.assertEqual(len(indexes), len(fixed_names), indexes)
            self.assert_bundle_verifies(self.bundle_of(result))

    def test_curl_ledger_preserves_a_space_inside_one_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_JOBS_JSON=json.dumps(
                    {"status": "success", "data": ["ceph exporter"]}
                ),
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-job-regex", ".*"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.curl_commands(curl_ledger)
            names_command = next(
                argv
                for argv in commands
                if f"{PROM_URL}/api/v1/label/__name__/values" in argv
            )
            self.assertIn('match[]={job="ceph exporter"}', names_command)
            self.assertNotIn('match[]={job="ceph', names_command)
            self.assertNotIn('exporter"}', names_command)


class FakeCurlArgvContractTests(unittest.TestCase):
    def test_an_unexpected_curl_option_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "response.json"
            environment = {
                **os.environ,
                "FAKE_CURL_LOG": str(root / "curl-argv.log"),
            }

            result = subprocess.run(
                [
                    str(FAKE_CURL),
                    "-q",
                    "-fsS",
                    "-G",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "5",
                    "-o",
                    str(output),
                    f"{PROM_URL}/api/v1/status/buildinfo",
                    "-k",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 99, result.stderr)
            self.assertFalse(output.exists())


class FakeGrepArgvContractTests(unittest.TestCase):
    def test_an_unexpected_grep_argument_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                **os.environ,
                "FAKE_GREP_LOG": str(root / "grep-argv.jsonl"),
            }

            result = subprocess.run(
                [str(FAKE_GREP), "-qiE", "--", "ceph", "unexpected"],
                cwd=ROOT,
                env=environment,
                input="ceph",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 99, result.stderr)


class PrometheusBudgetTests(PrometheusFixture, unittest.TestCase):
    def test_an_exhausted_budget_truncates_the_dump_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=("--prom-url", PROM_URL, "--prom-timeout", "0"),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertIn(
                "TRUNCATED: budget 0s exceeded",
                self.text_of(contents, "cluster/prometheus/ceph/index.txt"),
            )
            self.assertFalse(
                [name for name in contents if name.endswith(".json.gz")]
            )
            # Truncation stops the whole dump, so no later job is even started.
            self.assertFalse(
                [
                    name
                    for name in contents
                    if name.startswith("cluster/prometheus/node-exporter/")
                ]
            )
            fields = self.dump_info(contents)
            self.assertEqual(fields["truncated"], "1")
            self.assertEqual(fields["jobs_matched"], "ceph node-exporter")
            self.assertIn(
                "prometheus dump truncated: budget 0s exceeded at job ceph",
                self.text_of(contents, "errors.log"),
            )
            self.assert_bundle_verifies(bundle)


class PrometheusWorkstationDependencyTests(PrometheusFixture, unittest.TestCase):
    def test_a_workstation_without_curl_skips_the_layer_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            toolbox = root / "toolbox"
            toolbox.mkdir()
            (toolbox / "ssh").symlink_to(FAKE_SSH)
            real_tar = shutil.which("tar")
            self.assertIsNotNone(real_tar)
            (toolbox / "tar").symlink_to(str(real_tar))
            environment = {
                **os.environ,
                "PATH": str(toolbox),
                "FAKE_SSH_LOG": str(root / "ssh-argv.jsonl"),
            }
            self.assertIsNone(shutil.which("curl", path=environment["PATH"]))

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            skipped = self.text_of(contents, "cluster/prometheus/SKIPPED.txt")
            self.assertIn("curl not found on this workstation", skipped)
            self.assertNotIn("cluster/prometheus/dump-info.txt", contents)
            self.assertIn(
                "prometheus dump skipped", self.text_of(contents, "errors.log")
            )
            self.assert_bundle_verifies(bundle)


class PrometheusEnvironmentRecordTests(PrometheusFixture, unittest.TestCase):
    """environment.txt records the dump only once there is a dump to record."""

    def test_a_skipped_layer_records_no_prom_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_DOWN="1"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            environment_text = self.text_of(contents, "environment.txt")
            self.assertNotIn("prom_url", environment_text)
            self.assertNotIn("prom_jobs", environment_text)

    def test_a_partial_dump_still_records_the_jobs_it_matched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root, FAKE_CURL_FAIL_METRICS="ceph_osd_up"
            )

            result = self.run_collect(root, environment)

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            environment_text = self.text_of(contents, "environment.txt")
            self.assertIn(f"prom_url={PROM_URL}\n", environment_text)
            self.assertIn("prom_jobs=ceph node-exporter\n", environment_text)


class PrometheusCredentialMaskingTests(PrometheusFixture, unittest.TestCase):
    def test_url_credentials_never_reach_an_artifact_or_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(
                root,
                FAKE_CURL_DOWN="1",
                FAKE_CURL_ECHO_URL_ON_ERROR="1",
            )

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    "http://reader:s3cr@t-piece@prom.example:9090",
                ),
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            contents = self.extract(self.bundle_of(result))
            for name, payload in contents.items():
                self.assertNotIn(b"s3cr", payload, name)
                self.assertNotIn(b"t-piece", payload, name)
            self.assertIn(
                "http://reader:***@prom.example:9090",
                self.text_of(contents, "cluster/prometheus/SKIPPED.txt"),
            )
            self.assertNotIn("s3cr", result.stdout + result.stderr)
            self.assertNotIn("t-piece", result.stdout + result.stderr)
            # curl itself still receives the credentials it needs to connect.
            self.assertTrue(
                any(
                    "http://reader:s3cr@t-piece@prom.example:9090" in word
                    for argv in self.curl_commands(curl_ledger)
                    for word in argv
                )
            )

    def test_invalid_url_credentials_are_not_echoed_in_usage_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    "http://reader:sekrit@bad host",
                ),
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertNotIn("sekrit", result.stdout + result.stderr)
            self.assertIn("--prom-url must be an http(s) URL with a host", result.stderr)
            self.assertEqual(self.curl_commands(curl_ledger), [])

    def test_a_masked_url_is_recorded_in_the_bundle_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    "http://reader:sekrit@prom.example:9090",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertIn(
                "prom_url=http://reader:***@prom.example:9090\n",
                self.text_of(contents, "environment.txt"),
            )
            for name, payload in contents.items():
                self.assertNotIn(b"sekrit", payload, name)
            for entry in self.prometheus_entries(contents):
                self.assertNotIn("sekrit", str(entry["command"]))


class PrometheusDisabledTests(PrometheusFixture, unittest.TestCase):
    """Without --prom-url the layer is not merely empty: it never runs."""

    def test_no_prom_url_collects_no_metrics_evidence_and_issues_no_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(root, environment, extra_arguments=())

            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = self.bundle_of(result)
            contents = self.extract(bundle)
            self.assertFalse(
                [name for name in contents if name.startswith("cluster/prometheus/")]
            )
            self.assertEqual(self.prometheus_entries(contents), [])
            self.assertNotIn("prom_url", self.text_of(contents, "environment.txt"))
            self.assertEqual(self.curl_commands(curl_ledger), [])
            self.assert_bundle_verifies(bundle)

    def test_prometheus_options_without_the_url_stay_unused_and_unvalidated(
        self,
    ) -> None:
        """The shell validates the dump options only when the dump is enabled."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-step",
                    "0",
                    "--prom-timeout",
                    "not-a-number",
                    "--since",
                    "yesterday",
                    # `--since` is no longer a Prometheus-only concern: the
                    # Evidence Window claims the same grammar whenever
                    # `/var/log` is collected (ADR 0012), so this scenario has
                    # to opt out of logs to keep asking its own question.
                    "--skip-logs",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            self.assertEqual(self.prometheus_entries(contents), [])
            self.assertEqual(self.curl_commands(curl_ledger), [])


class PrometheusOptionValidationTests(PrometheusFixture, unittest.TestCase):
    def assert_rejected_before_any_request(
        self, extra_arguments: tuple[str, ...], expected: str
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root, environment, extra_arguments=extra_arguments
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(result.stdout, "")
            self.assertIn(expected, result.stderr)
            self.assertEqual(self.curl_commands(curl_ledger), [])

    def test_unparseable_since_is_rejected_when_the_dump_is_enabled(self) -> None:
        for since in ("yesterday", "5x", "0", "000", ""):
            with self.subTest(since=since):
                self.assert_rejected_before_any_request(
                    ("--prom-url", PROM_URL, "--since", since), "--since"
                )

    def test_non_positive_step_is_rejected(self) -> None:
        for step in ("0", "-15", "15s", "abc"):
            with self.subTest(step=step):
                self.assert_rejected_before_any_request(
                    ("--prom-url", PROM_URL, "--prom-step", step), "--prom-step"
                )

    def test_non_numeric_timeout_is_rejected(self) -> None:
        for timeout in ("abc", "-1", "6.5"):
            with self.subTest(timeout=timeout):
                self.assert_rejected_before_any_request(
                    ("--prom-url", PROM_URL, "--prom-timeout", timeout),
                    "--prom-timeout",
                )

    def test_a_url_that_is_not_an_http_endpoint_is_rejected(self) -> None:
        for url in (
            "-X",
            "prom.example:9090",
            "file:///etc/passwd",
            "http://a b",
            "http:///",
            "https://",
            "http://prom.example:9090?query=wrong-base",
            "http://prom.example:9090#fragment",
            "http://prom.example:9090?",
            "http://prom.example:9090#",
            "http://prom.example:not-a-port",
            "http://prom.example:70000",
        ):
            with self.subTest(url=url):
                self.assert_rejected_before_any_request(
                    ("--prom-url", url), "--prom-url"
                )

    def test_a_missing_value_is_a_usage_error(self) -> None:
        self.assert_rejected_before_any_request(("--prom-url",), "--prom-url")

    def test_accepted_windows_reach_the_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, curl_ledger = self.make_fake_environment(root)

            result = self.run_collect(
                root,
                environment,
                extra_arguments=(
                    "--prom-url",
                    PROM_URL,
                    "--since",
                    "008",
                    "--prom-step",
                    "30",
                    "--prom-timeout",
                    "60",
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            contents = self.extract(self.bundle_of(result))
            fields = self.dump_info(contents)
            # A leading zero is base ten, not octal, and never a parse failure.
            self.assertEqual(
                int(fields["window_end_epoch"]) - int(fields["window_start_epoch"]), 8
            )
            self.assertEqual(fields["step_seconds"], "30")


if __name__ == "__main__":
    unittest.main()
