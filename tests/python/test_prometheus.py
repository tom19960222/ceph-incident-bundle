import json
from pathlib import Path
import time
from tempfile import TemporaryDirectory
from typing import Callable
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ceph_incident_bundle.collect.prometheus import collect_prometheus
from prometheus_test_support import loopback_http_server


def _successful_controls(
    request_path: str,
) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
    path = urlsplit(request_path).path
    if path.startswith("/prometheus/"):
        path = path.removeprefix("/prometheus")
    if path == "/api/v1/status/buildinfo":
        body = b'{"status":"success","data":{"version":"3.0"}}\n'
    elif path == "/api/v1/targets":
        body = b'{"status":"success","data":{"activeTargets":[]}}\n'
    elif path == "/api/v1/label/job/values":
        body = json.dumps(
            {
                "status": "success",
                "data": [
                    "other",
                    'NoDe/../../bad"\\\n\r\t\x00\b\f\v\u0085name',
                    "ceph-exporter",
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")
    elif path == "/api/v1/label/__name__/values":
        body = b'{"status":"success","data":["metric_a","metric_a","metric_b"]}'
    elif path == "/api/v1/query_range":
        body = b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
    else:
        body = b'{"status":"error","errorType":"bad_data","error":"unexpected"}'
    return 200, [(0, body)], len(body), False


class PrometheusCollectionTests(unittest.TestCase):
    def test_fixed_controls_and_per_job_discovery_are_raw_ordered_and_path_safe(
        self,
    ) -> None:
        hostile_job = 'NoDe/../../bad"\\\n\r\t\x00\b\f\v\u0085name'
        with loopback_http_server(_successful_controls) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url + "/prometheus/",
                since_seconds=86400,
                request_timeout_seconds=5,
                metrics_filter_regex=r"^metric_[ab]$",
                query_step="31s",
                staging_directory=staging,
                contribution_directory=admitted,
            )

            requests = list(server.requests)
            captures = sorted(
                path.relative_to(admitted).as_posix()
                for path in admitted.rglob("*")
                if path.is_file()
            )
            job_result = json.loads(
                (admitted / "metric-names" / "000001" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            raw_job_values = (admitted / "job-values" / "response").read_bytes()
            range_result = json.loads(
                (admitted / "query-range" / "000001" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            staging_exists = staging.exists()

        self.assertEqual(problems, [])
        self.assertEqual([method for method, _ in requests], ["GET"] * 9)
        self.assertEqual(
            [urlsplit(request).path for _, request in requests],
            [
                "/prometheus/api/v1/status/buildinfo",
                "/prometheus/api/v1/targets",
                "/prometheus/api/v1/label/job/values",
                "/prometheus/api/v1/label/__name__/values",
                "/prometheus/api/v1/label/__name__/values",
                "/prometheus/api/v1/query_range",
                "/prometheus/api/v1/query_range",
                "/prometheus/api/v1/query_range",
                "/prometheus/api/v1/query_range",
            ],
        )
        metric_queries = [
            parse_qs(urlsplit(request).query) for _, request in requests[3:5]
        ]
        self.assertEqual(
            [query["match[]"] for query in metric_queries],
            [
                [r'{job="NoDe/../../bad\"\\\n\r\t\x00\b\f\v\u0085name"}'],
                [r'{job="ceph-exporter"}'],
            ],
        )
        starts = [query["start"][0] for query in metric_queries]
        ends = [query["end"][0] for query in metric_queries]
        self.assertEqual(len(set(starts)), 1)
        self.assertEqual(len(set(ends)), 1)
        self.assertEqual(float(ends[0]) - float(starts[0]), 86400)
        range_queries = [
            parse_qs(urlsplit(request).query) for _, request in requests[5:]
        ]
        self.assertEqual(
            [query["query"] for query in range_queries],
            [
                [
                    r'{job="NoDe/../../bad\"\\\n\r\t\x00\b\f\v\u0085name",'
                    r'__name__="metric_a"}'
                ],
                [
                    r'{job="NoDe/../../bad\"\\\n\r\t\x00\b\f\v\u0085name",'
                    r'__name__="metric_b"}'
                ],
                [r'{job="ceph-exporter",__name__="metric_a"}'],
                [r'{job="ceph-exporter",__name__="metric_b"}'],
            ],
        )
        self.assertEqual([query["start"] for query in range_queries], [[starts[0]]] * 4)
        self.assertEqual([query["end"] for query in range_queries], [[ends[0]]] * 4)
        self.assertEqual([query["step"] for query in range_queries], [["31s"]] * 4)
        self.assertEqual(
            captures,
            [
                "buildinfo/response",
                "buildinfo/result.json",
                "job-values/response",
                "job-values/result.json",
                "metric-names/000001/response",
                "metric-names/000001/result.json",
                "metric-names/000002/response",
                "metric-names/000002/result.json",
                "query-range/000001/response",
                "query-range/000001/result.json",
                "query-range/000002/response",
                "query-range/000002/result.json",
                "query-range/000003/response",
                "query-range/000003/result.json",
                "query-range/000004/response",
                "query-range/000004/result.json",
                "targets/response",
                "targets/result.json",
            ],
        )
        self.assertEqual(
            set(job_result),
            {
                "url",
                "started_at",
                "finished_at",
                "outcome",
                "http_status",
                "error",
                "job_name",
            },
        )
        self.assertEqual(job_result["job_name"], hostile_job)
        self.assertEqual(job_result["outcome"], "received")
        self.assertEqual(job_result["http_status"], 200)
        self.assertIsNone(job_result["error"])
        self.assertEqual(
            set(range_result),
            {
                "url",
                "started_at",
                "finished_at",
                "outcome",
                "http_status",
                "error",
                "job_name",
                "metric_name",
            },
        )
        self.assertEqual(range_result["job_name"], hostile_job)
        self.assertEqual(range_result["metric_name"], "metric_a")
        self.assertIn(b'NoDe/../../bad', raw_job_values)
        self.assertFalse(any(hostile_job in path for path in captures))
        self.assertFalse(staging_exists)

    def test_control_failures_preserve_bodies_and_block_only_dependents(self) -> None:
        def mixed_responses(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            query = parse_qs(urlsplit(request_path).query)
            if path == "/api/v1/status/buildinfo":
                body = b"service unavailable\x00\xff"
                return 503, [(0, body)], len(body), False
            if path == "/api/v1/targets":
                body = b"{not-json"
                return 200, [(0, body)], len(body), False
            if path == "/api/v1/label/job/values":
                body = b'{"status":"success","data":["node-a","ceph-b","node-c"]}'
                return 200, [(0, body)], len(body), False
            if query.get("match[]") == ['{job="node-a"}']:
                body = b'{"status":"error","errorType":"bad_data","error":"bad matcher"}'
            elif query.get("match[]") == ['{job="ceph-b"}']:
                body = b'{"status":"success","data":{"metric":"not-a-list"}}'
            else:
                body = b'{"status":"success","data":["metric_ok"]}'
            return 200, [(0, body)], len(body), False

        with loopback_http_server(mixed_responses) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=5,
                staging_directory=staging,
                contribution_directory=admitted,
            )

            requests = list(server.requests)
            buildinfo_body = (admitted / "buildinfo" / "response").read_bytes()
            buildinfo_result = json.loads(
                (admitted / "buildinfo" / "result.json").read_text(encoding="utf-8")
            )
            invalid_targets = (admitted / "targets" / "response").read_bytes()
            first_metric_result = json.loads(
                (admitted / "metric-names" / "000001" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            second_metric_result = json.loads(
                (admitted / "metric-names" / "000002" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            second_metric_response = (
                admitted / "metric-names" / "000002" / "response"
            ).read_bytes()
            third_metric_exists = (
                admitted / "metric-names" / "000003" / "response"
            ).is_file()
            third_range_exists = (
                admitted / "query-range" / "000001" / "response"
            ).is_file()

        self.assertEqual(len(requests), 7)
        self.assertEqual(len(problems), 4)
        self.assertIn(
            "buildinfo request failed: http_status: HTTP status 503", problems[0]
        )
        self.assertIn("targets request failed: invalid_response", problems[1])
        self.assertIn("metric-names request for job 'node-a' failed", problems[2])
        self.assertIn("metric-names control response for job 'ceph-b'", problems[3])
        self.assertEqual(buildinfo_body, b"service unavailable\x00\xff")
        self.assertEqual(buildinfo_result["outcome"], "failed")
        self.assertEqual(buildinfo_result["http_status"], 503)
        self.assertEqual(invalid_targets, b"{not-json")
        self.assertEqual(first_metric_result["outcome"], "failed")
        self.assertEqual(first_metric_result["error"]["kind"], "invalid_response")
        self.assertEqual(
            second_metric_response,
            b'{"status":"success","data":{"metric":"not-a-list"}}',
        )
        self.assertEqual(second_metric_result["outcome"], "received")
        self.assertIsNone(second_metric_result["error"])
        self.assertTrue(third_metric_exists)
        self.assertTrue(third_range_exists)

    def test_filtered_ranges_keep_pair_order_raw_failures_and_complete_admission(
        self,
    ) -> None:
        hostile_metric = 'hostile/../../metric"\\\n\r\t\x00\b\f\v\u0085name'
        large_body = json.dumps(
            {
                "status": "success",
                "data": {"resultType": "matrix", "padding": "x" * 1048576},
            },
            separators=(",", ":"),
        ).encode("utf-8")

        def range_responses(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            query = parse_qs(urlsplit(request_path).query)
            if path == "/api/v1/label/job/values":
                body = b'{"status":"success","data":["node-a","ceph-b"]}'
                return 200, [(0, body)], len(body), False
            if path == "/api/v1/label/__name__/values":
                if query["match[]"] == ['{job="node-a"}']:
                    body = json.dumps(
                        {
                            "status": "success",
                            "data": [
                                "drop",
                                "same",
                                "same",
                                hostile_metric,
                                "large",
                                "drop",
                            ],
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                else:
                    body = b'{"status":"success","data":["same","later"]}'
                return 200, [(0, body)], len(body), False
            if path == "/api/v1/query_range":
                selector = query["query"][0]
                if selector == '{job="node-a",__name__="same"}':
                    body = b"range unavailable\x00\xff"
                    return 503, [(0, body)], len(body), False
                if "hostile/../../metric" in selector:
                    body = b'{"status":"success",'
                    return 200, [(0, body)], len(body) + 17, True
                if selector == '{job="node-a",__name__="large"}':
                    return 200, [(0, large_body)], len(large_body), False
                body = b'{"status":"success","data":{"resultType":"matrix"}}'
                return 200, [(0, body)], len(body), False
            body = b'{"status":"success","data":{}}'
            return 200, [(0, body)], len(body), False

        with loopback_http_server(range_responses) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url,
                since_seconds=300,
                request_timeout_seconds=5,
                metrics_filter_regex=r"^(?:same|hostile/|large|later)",
                query_step="17s",
                staging_directory=staging,
                contribution_directory=admitted,
            )

            range_requests = [
                request
                for _, request in server.requests
                if urlsplit(request).path == "/api/v1/query_range"
            ]
            selectors = [
                parse_qs(urlsplit(request).query)["query"][0]
                for request in range_requests
            ]
            range_directories = sorted(
                path.name for path in (admitted / "query-range").iterdir()
            )
            results = [
                json.loads(
                    (admitted / "query-range" / f"{sequence:06d}" / "result.json")
                    .read_text(encoding="utf-8")
                )
                for sequence in range(1, 6)
            ]
            raw_responses = [
                (admitted / "query-range" / f"{sequence:06d}" / "response").read_bytes()
                for sequence in range(1, 6)
            ]
            staging_exists = staging.exists()

        self.assertEqual(
            selectors,
            [
                '{job="node-a",__name__="same"}',
                r'{job="node-a",__name__="hostile/../../metric\"\\\n\r\t'
                r'\x00\b\f\v\u0085name"}',
                '{job="node-a",__name__="large"}',
                '{job="ceph-b",__name__="same"}',
                '{job="ceph-b",__name__="later"}',
            ],
        )
        self.assertIn("%2F..%2F..%2F", range_requests[1])
        self.assertIn("%5C%22", range_requests[1])
        self.assertNotIn(hostile_metric, range_requests[1])
        self.assertEqual(
            [(result["job_name"], result["metric_name"]) for result in results],
            [
                ("node-a", "same"),
                ("node-a", hostile_metric),
                ("node-a", "large"),
                ("ceph-b", "same"),
                ("ceph-b", "later"),
            ],
        )
        self.assertEqual(
            range_directories,
            ["000001", "000002", "000003", "000004", "000005"],
        )
        self.assertEqual(len(problems), 2)
        self.assertIn("job 'node-a' metric 'same' failed: http_status", problems[0])
        self.assertIn("metric 'hostile/../../metric", problems[1])
        self.assertEqual(raw_responses[0], b"range unavailable\x00\xff")
        self.assertEqual(raw_responses[1], b'{"status":"success",')
        self.assertEqual(results[0]["error"]["kind"], "http_status")
        self.assertEqual(results[1]["error"]["kind"], "IncompleteRead")
        self.assertTrue(
            all(
                set(result)
                == {
                    "url",
                    "started_at",
                    "finished_at",
                    "outcome",
                    "http_status",
                    "error",
                    "job_name",
                    "metric_name",
                }
                for result in results
            )
        )
        self.assertEqual(raw_responses[2], large_body)
        self.assertEqual(results[2]["outcome"], "received")
        self.assertFalse(staging_exists)

    def test_timeout_is_per_blocking_progress_and_preserves_partial_bytes(self) -> None:
        progressing_body = (
            b'{"status":"success","data":{"padding":"'
            + b"x" * 1048576
            + b'"}}'
        )

        def timed_responses(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            if path == "/api/v1/status/buildinfo":
                chunks = [(0, b'{"status":"success",'), (1.2, b'"data":{}}')]
                return 200, chunks, sum(len(chunk) for _, chunk in chunks), False
            if path == "/api/v1/targets":
                thirds = len(progressing_body) // 3
                chunks = [
                    (0.4, progressing_body[:thirds]),
                    (0.4, progressing_body[thirds : thirds * 2]),
                    (0.4, progressing_body[thirds * 2 :]),
                ]
                return 200, chunks, len(progressing_body), False
            body = b'{"status":"success","data":[]}'
            return 200, [(0, body)], len(body), False

        with loopback_http_server(timed_responses) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            started = time.monotonic()
            problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=1,
                staging_directory=staging,
                contribution_directory=admitted,
            )
            elapsed = time.monotonic() - started

            requests = list(server.requests)
            partial_body = (admitted / "buildinfo" / "response").read_bytes()
            timeout_result = json.loads(
                (admitted / "buildinfo" / "result.json").read_text(encoding="utf-8")
            )
            progressing_bytes = (admitted / "targets" / "response").read_bytes()
            progressing_result = json.loads(
                (admitted / "targets" / "result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(requests), 3)
        self.assertGreater(elapsed, 1.1)
        self.assertEqual(len(problems), 1)
        self.assertIn("buildinfo request failed: timeout", problems[0])
        self.assertEqual(partial_body, b'{"status":"success",')
        self.assertEqual(timeout_result["outcome"], "failed")
        self.assertEqual(timeout_result["error"]["kind"], "timeout")
        self.assertEqual(progressing_bytes, progressing_body)
        self.assertEqual(progressing_result["outcome"], "received")

    def test_delayed_first_byte_and_interrupted_body_are_truthful_failures(
        self,
    ) -> None:
        def interrupted_responses(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            if path == "/api/v1/status/buildinfo":
                body = b'{"status":"success","data":{}}'
                return 200, [(1.2, body)], len(body), False
            if path == "/api/v1/targets":
                partial = b'{"status":"success",'
                return 200, [(0, partial)], len(partial) + 20, True
            body = b'{"status":"success","data":[]}'
            return 200, [(0, body)], len(body), False

        with loopback_http_server(interrupted_responses) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=1,
                staging_directory=staging,
                contribution_directory=admitted,
            )

            requests = list(server.requests)
            delayed_bytes = (admitted / "buildinfo" / "response").read_bytes()
            delayed_result = json.loads(
                (admitted / "buildinfo" / "result.json").read_text(encoding="utf-8")
            )
            interrupted_bytes = (admitted / "targets" / "response").read_bytes()
            interrupted_result = json.loads(
                (admitted / "targets" / "result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(requests), 3)
        self.assertEqual(len(problems), 2)
        self.assertEqual(delayed_bytes, b"")
        self.assertEqual(delayed_result["error"]["kind"], "timeout")
        self.assertEqual(interrupted_bytes, b'{"status":"success",')
        self.assertEqual(interrupted_result["error"]["kind"], "IncompleteRead")

    def test_zero_timeout_waits_for_progress_without_a_total_deadline(self) -> None:
        def delayed_response(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            body = b'{"status":"success","data":[]}'
            delay = 1.1 if path == "/api/v1/status/buildinfo" else 0
            return 200, [(delay, body)], len(body), False

        with loopback_http_server(delayed_response) as (
            _,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=0,
                staging_directory=staging,
                contribution_directory=admitted,
            )

            buildinfo_result = json.loads(
                (admitted / "buildinfo" / "result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(problems, [])
        self.assertEqual(buildinfo_result["outcome"], "received")

    def test_existing_admission_fails_real_promotion_without_publishing_private_bytes(
        self,
    ) -> None:
        with loopback_http_server(_successful_controls) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()
            admitted.mkdir()
            sentinel = admitted / "sentinel"
            sentinel.write_bytes(b"unchanged")

            existing_problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=5,
                staging_directory=staging,
                contribution_directory=admitted,
            )
            request_count = len(server.requests)
            sentinel_bytes = sentinel.read_bytes()
            private_response = staging / "contribution" / "buildinfo" / "response"
            private_response_exists = private_response.is_file()
            private_bytes = private_response.read_bytes()

        self.assertGreater(request_count, 0)
        self.assertEqual(sentinel_bytes, b"unchanged")
        self.assertEqual(len(existing_problems), 1)
        self.assertIn("cannot atomically promote", existing_problems[0])
        self.assertIn("private residue", existing_problems[0])
        self.assertTrue(private_response_exists)
        self.assertTrue(private_bytes)

    def test_malformed_job_values_blocks_only_metric_discovery(self) -> None:
        def malformed_jobs(
            request_path: str,
        ) -> tuple[int, list[tuple[float, bytes]], int | None, bool]:
            path = urlsplit(request_path).path
            if path == "/api/v1/label/job/values":
                body = b'{"status":"success","data":{"node":"not-a-list"}}'
            else:
                body = b'{"status":"success","data":{}}'
            return 200, [(0, body)], len(body), False

        with loopback_http_server(malformed_jobs) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url,
                since_seconds=60,
                request_timeout_seconds=5,
                staging_directory=staging,
                contribution_directory=admitted,
            )

            requests = list(server.requests)
            metric_directory_exists = (admitted / "metric-names").exists()
            result = json.loads(
                (admitted / "job-values" / "result.json").read_text(encoding="utf-8")
            )
            response = (admitted / "job-values" / "response").read_bytes()

        self.assertEqual(len(requests), 3)
        self.assertEqual(len(problems), 1)
        self.assertIn("job-values control response is malformed", problems[0])
        self.assertFalse(metric_directory_exists)
        self.assertEqual(
            response, b'{"status":"success","data":{"node":"not-a-list"}}'
        )
        self.assertEqual(result["outcome"], "received")
        self.assertIsNone(result["error"])

    def test_embedded_credentials_remain_exact_without_content_special_cases(
        self,
    ) -> None:
        with loopback_http_server(_successful_controls) as (
            server,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()
            credential_url = url.replace("http://", "http://user:cleartext@", 1)

            problems = collect_prometheus(
                url=credential_url,
                since_seconds=60,
                request_timeout_seconds=1,
                staging_directory=staging,
                contribution_directory=admitted,
            )

            requests = list(server.requests)
            result = json.loads(
                (admitted / "buildinfo" / "result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            result["url"], credential_url + "/api/v1/status/buildinfo"
        )
        self.assertIn("user:cleartext@", result["url"])
        self.assertNotIn("REDACT", result["url"])
        self.assertEqual(result["outcome"], "failed")
        self.assertIsNone(result["http_status"])
        self.assertEqual(len(problems), 3)
        self.assertEqual(requests, [])

    def test_capture_write_failure_reports_private_residue_without_admission(
        self,
    ) -> None:
        original_open = Path.open

        def fail_response_open(path: Path, *args: object, **kwargs: object):
            if path.name == "response":
                raise OSError("injected response write failure")
            return original_open(path, *args, **kwargs)

        with loopback_http_server(_successful_controls) as (
            _,
            url,
        ), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            with patch("pathlib.Path.open", new=fail_response_open):
                problems = collect_prometheus(
                    url=url,
                    since_seconds=60,
                    request_timeout_seconds=5,
                    staging_directory=staging,
                    contribution_directory=admitted,
                )

            admitted_exists = admitted.exists()
            staging_exists = staging.exists()

        self.assertEqual(len(problems), 1)
        self.assertIn("cannot preserve buildinfo request", problems[0])
        self.assertIn("injected response write failure", problems[0])
        self.assertIn("private residue", problems[0])
        self.assertFalse(admitted_exists)
        self.assertTrue(staging_exists)


if __name__ == "__main__":
    unittest.main()
