from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from tempfile import TemporaryDirectory
from typing import Callable
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ceph_incident_bundle.collect.prometheus import (
    _deduplicate_metric_names,
    collect_prometheus,
)


class _PrometheusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _PrometheusServer)
        server.requests.append((self.command, self.path))
        status, chunks, declared_length, disconnect = server.response_for(self.path)
        self.send_response(status)
        if declared_length is not None:
            self.send_header("Content-Length", str(declared_length))
        self.end_headers()
        try:
            for delay, chunk in chunks:
                time.sleep(delay)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if disconnect:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def log_message(self, format: str, *args: object) -> None:
        pass


class _PrometheusServer(ThreadingHTTPServer):
    requests: list[tuple[str, str]]
    response_for: Callable[
        [str], tuple[int, list[tuple[float, bytes]], int | None, bool]
    ]


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
    else:
        body = b'{"status":"error","errorType":"bad_data","error":"unexpected"}'
    return 200, [(0, body)], len(body), False


@contextmanager
def _loopback_prometheus(
    response_for: Callable[
        [str], tuple[int, list[tuple[float, bytes]], int | None, bool]
    ] = _successful_controls,
):
    server = _PrometheusServer(("127.0.0.1", 0), _PrometheusHandler)
    server.requests = []
    server.response_for = response_for
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PrometheusCollectionTests(unittest.TestCase):
    def test_metric_names_are_deduplicated_exactly_in_first_occurrence_order(
        self,
    ) -> None:
        self.assertEqual(
            _deduplicate_metric_names(
                ["same", "Same", "same", "metric/../../name", "Same"]
            ),
            ("same", "Same", "metric/../../name"),
        )

    def test_fixed_controls_and_per_job_discovery_are_raw_ordered_and_path_safe(
        self,
    ) -> None:
        hostile_job = 'NoDe/../../bad"\\\n\r\t\x00\b\f\v\u0085name'
        with _loopback_prometheus() as (server, url), TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "private" / "prometheus"
            staging.parent.mkdir()
            admitted = root / "admitted" / "prometheus"
            admitted.parent.mkdir()

            problems = collect_prometheus(
                url=url + "/prometheus/",
                since_seconds=86400,
                request_timeout_seconds=5,
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
            staging_exists = staging.exists()

        self.assertEqual(problems, [])
        self.assertEqual([method for method, _ in requests], ["GET"] * 5)
        self.assertEqual(
            [urlsplit(request).path for _, request in requests],
            [
                "/prometheus/api/v1/status/buildinfo",
                "/prometheus/api/v1/targets",
                "/prometheus/api/v1/label/job/values",
                "/prometheus/api/v1/label/__name__/values",
                "/prometheus/api/v1/label/__name__/values",
            ],
        )
        metric_queries = [
            parse_qs(urlsplit(request).query) for _, request in requests[3:]
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

        with _loopback_prometheus(mixed_responses) as (
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
            third_metric_exists = (
                admitted / "metric-names" / "000003" / "response"
            ).is_file()

        self.assertEqual(len(requests), 6)
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
        self.assertEqual(second_metric_result["outcome"], "failed")
        self.assertEqual(second_metric_result["error"]["kind"], "invalid_response")
        self.assertTrue(third_metric_exists)

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

        with _loopback_prometheus(timed_responses) as (
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

        with _loopback_prometheus(interrupted_responses) as (
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

        with _loopback_prometheus(delayed_response) as (
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

    def test_existing_admission_or_failed_promotion_never_publishes_private_bytes(
        self,
    ) -> None:
        with _loopback_prometheus() as (server, url), TemporaryDirectory() as directory:
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
            requests_before_promotion_case = len(server.requests)

            sentinel.unlink()
            admitted.rmdir()
            with patch(
                "ceph_incident_bundle.collect.prometheus.os.rename",
                side_effect=OSError("injected promotion failure"),
            ):
                promotion_problems = collect_prometheus(
                    url=url,
                    since_seconds=60,
                    request_timeout_seconds=5,
                    staging_directory=staging,
                    contribution_directory=admitted,
                )

            admitted_exists = admitted.exists()
            private_response = staging / "contribution" / "buildinfo" / "response"
            private_response_exists = private_response.is_file()
            private_bytes = private_response.read_bytes()

        self.assertEqual(requests_before_promotion_case, 0)
        self.assertEqual(len(existing_problems), 1)
        self.assertIn("contribution already exists", existing_problems[0])
        self.assertEqual(len(promotion_problems), 1)
        self.assertIn("cannot atomically promote", promotion_problems[0])
        self.assertIn("private residue", promotion_problems[0])
        self.assertFalse(admitted_exists)
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

        with _loopback_prometheus(malformed_jobs) as (
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

        self.assertEqual(len(requests), 3)
        self.assertEqual(len(problems), 1)
        self.assertIn("job-values control response is malformed", problems[0])
        self.assertFalse(metric_directory_exists)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"]["kind"], "invalid_response")

    def test_embedded_credentials_remain_exact_without_content_special_cases(
        self,
    ) -> None:
        with _loopback_prometheus() as (server, url), TemporaryDirectory() as directory:
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

        with _loopback_prometheus() as (_, url), TemporaryDirectory() as directory:
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
