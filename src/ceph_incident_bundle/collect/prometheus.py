"""Collect workstation-local Prometheus control HTTP Evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_SELECTED_JOB = re.compile(r"ceph|node", re.IGNORECASE)
_READ_SIZE = 64 * 1024


def collect_prometheus(
    *,
    url: str,
    since_seconds: int,
    request_timeout_seconds: int,
    staging_directory: Path,
    contribution_directory: Path,
) -> list[str]:
    """Collect fixed controls and atomically admit one Prometheus contribution."""
    staging_directory = Path(staging_directory)
    contribution_directory = Path(contribution_directory)
    boundary_problem = _boundary_problem(staging_directory, contribution_directory)
    if boundary_problem is not None:
        return [boundary_problem]

    try:
        staging_directory.mkdir(mode=0o700)
        candidate = staging_directory / "contribution"
        candidate.mkdir()
    except OSError as error:
        return [
            _with_residue(
                f"Prometheus: cannot create private staging: {error}",
                staging_directory,
            )
        ]

    end = int(datetime.now(timezone.utc).timestamp())
    start = end - since_seconds
    timeout = request_timeout_seconds or None
    problems: list[str] = []

    controls = (
        ("buildinfo", "/api/v1/status/buildinfo"),
        ("targets", "/api/v1/targets"),
        ("job-values", "/api/v1/label/job/values"),
    )
    job_names: list[str] = []
    for name, endpoint in controls:
        capture = candidate / name
        try:
            document, problem = _capture_request(
                _request_url(url, endpoint),
                capture,
                timeout=timeout,
            )
        except OSError as error:
            return [
                *problems,
                _with_residue(
                    f"Prometheus: cannot preserve {name} request: {error}",
                    staging_directory,
                ),
            ]
        if problem is not None:
            problems.append(f"Prometheus {name} request failed: {problem}")
            continue
        if name == "job-values":
            values, parse_problem = _control_values(document)
            if parse_problem is not None:
                try:
                    _mark_invalid_control(capture, parse_problem)
                except OSError as error:
                    return [
                        *problems,
                        _with_residue(
                            f"Prometheus: cannot preserve job-values parse result: "
                            f"{error}",
                            staging_directory,
                        ),
                    ]
                problems.append(
                    f"Prometheus job-values control response is malformed: "
                    f"{parse_problem}"
                )
            else:
                job_names = [name for name in values if _SELECTED_JOB.search(name)]

    # #97's control-only artifact contract has no field for parsed metric names.
    # Keep the ordered, deduplicated handoff explicit so #98 can consume it when
    # it adds range scheduling without reparsing or changing the raw captures.
    metric_names_by_job: list[tuple[str, tuple[str, ...]]] = []
    for sequence, job_name in enumerate(job_names, 1):
        matcher = f'{{job="{_promql_string(job_name)}"}}'
        query = urlencode(
            (("match[]", matcher), ("start", str(start)), ("end", str(end)))
        )
        capture = candidate / "metric-names" / f"{sequence:06d}"
        try:
            document, problem = _capture_request(
                _request_url(url, "/api/v1/label/__name__/values", query),
                capture,
                timeout=timeout,
                extra_result={"job_name": job_name},
            )
        except OSError as error:
            return [
                *problems,
                _with_residue(
                    f"Prometheus: cannot preserve metric-names request "
                    f"for job {job_name!r}: {error}",
                    staging_directory,
                ),
            ]
        if problem is not None:
            problems.append(
                f"Prometheus metric-names request for job {job_name!r} failed: "
                f"{problem}"
            )
            continue
        values, parse_problem = _control_values(document)
        if parse_problem is not None:
            try:
                _mark_invalid_control(capture, parse_problem)
            except OSError as error:
                return [
                    *problems,
                    _with_residue(
                        f"Prometheus: cannot preserve metric-names parse result "
                        f"for job {job_name!r}: {error}",
                        staging_directory,
                    ),
                ]
            problems.append(
                f"Prometheus metric-names control response for job {job_name!r} "
                f"is malformed: {parse_problem}"
            )
            continue
        metric_names_by_job.append((job_name, _deduplicate_metric_names(values)))

    try:
        os.rename(candidate, contribution_directory)
    except OSError as error:
        problems.append(
            _with_residue(
                f"Prometheus: cannot atomically promote complete contribution: "
                f"{error}",
                staging_directory,
            )
        )
        return problems

    try:
        staging_directory.rmdir()
    except OSError as error:
        problems.append(
            f"Prometheus: admitted contribution but cannot remove private staging "
            f"{staging_directory}: {error}"
        )
    return problems


def _capture_request(
    request_url: str,
    capture: Path,
    *,
    timeout: int | None,
    extra_result: dict[str, str] | None = None,
) -> tuple[object | None, str | None]:
    capture.mkdir(parents=True)
    started_at = _utc_now()
    http_status: int | None = None
    error: dict[str, str] | None = None
    response: object | None = None
    response_path = capture / "response"

    with response_path.open("xb") as response_file:
        try:
            request = Request(request_url, method="GET")
            try:
                response = urlopen(request, timeout=timeout)
            except HTTPError as http_error:
                response = http_error
            except Exception as request_error:
                error = _request_error(request_error)
        except Exception as request_error:
            error = _request_error(request_error)
        if response is not None:
            http_status = response.status
            content_length = response.headers.get("Content-Length")
            try:
                expected_bytes = (
                    int(content_length)
                    if content_length is not None and content_length.isdecimal()
                    else None
                )
            except ValueError:
                expected_bytes = None
            received_bytes = 0
            with response:
                while True:
                    try:
                        chunk = response.read1(_READ_SIZE)
                    except Exception as request_error:
                        error = _request_error(request_error)
                        break
                    if not chunk:
                        if (
                            expected_bytes is not None
                            and received_bytes < expected_bytes
                        ):
                            error = {
                                "kind": "IncompleteRead",
                                "message": (
                                    "response ended after "
                                    f"{received_bytes} of {expected_bytes} bytes"
                                ),
                            }
                        break
                    response_file.write(chunk)
                    received_bytes += len(chunk)

    document: object | None = None
    if error is None and http_status is not None and not 200 <= http_status < 300:
        error = {
            "kind": "http_status",
            "message": f"HTTP status {http_status}",
        }
    if error is None:
        try:
            document = json.loads(response_path.read_bytes())
            if not isinstance(document, dict):
                raise TypeError("top-level response is not an object")
            if document.get("status") != "success":
                api_error = document.get("error")
                raise ValueError(
                    "Prometheus API status is not success"
                    + (f": {api_error}" if isinstance(api_error, str) else "")
                )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as parse_error:
            error = {
                "kind": "invalid_response",
                "message": str(parse_error),
            }
            document = None

    outcome = "received" if error is None else "failed"
    result: dict[str, object] = {
        "url": request_url,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "outcome": outcome,
        "http_status": http_status,
        "error": error,
    }
    if extra_result is not None:
        result.update(extra_result)
    (capture / "result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )
    if error is None:
        return document, None
    return None, f"{error['kind']}: {error['message']}"


def _control_values(document: object | None) -> tuple[list[str], str | None]:
    try:
        if not isinstance(document, dict):
            raise TypeError("top-level response is not an object")
        values = document["data"]
        if not isinstance(values, list):
            raise TypeError("data is not a list")
        if not all(isinstance(value, str) for value in values):
            raise TypeError("data contains a non-string value")
    except (KeyError, TypeError) as error:
        return [], str(error)
    return values, None


def _mark_invalid_control(capture: Path, problem: str) -> None:
    result_path = capture / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["finished_at"] = _utc_now()
    result["outcome"] = "failed"
    result["error"] = {"kind": "invalid_response", "message": problem}
    result_path.write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )


def _request_url(base: str, endpoint: str, query: str | None = None) -> str:
    url = base.rstrip("/") + endpoint
    return f"{url}?{query}" if query is not None else url


def _promql_string(value: str) -> str:
    simple_escapes = {
        "\\": "\\\\",
        '"': '\\"',
        "\a": "\\a",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    escaped: list[str] = []
    for character in value:
        replacement = simple_escapes.get(character)
        if replacement is not None:
            escaped.append(replacement)
        elif character.isprintable():
            escaped.append(character)
        else:
            codepoint = ord(character)
            if codepoint <= 0x7F:
                escaped.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)


def _deduplicate_metric_names(values: list[str]) -> tuple[str, ...]:
    """Keep exact metric names once, in their Prometheus response order."""
    return tuple(dict.fromkeys(values))


def _request_error(error: Exception) -> dict[str, str]:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return {"kind": "timeout", "message": str(reason) or "request timed out"}
    return {"kind": type(reason).__name__, "message": str(reason)}


def _boundary_problem(staging: Path, contribution: Path) -> str | None:
    try:
        contribution.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"Prometheus: cannot inspect admitted contribution: {error}"
    else:
        return (
            "Prometheus: admitted Prometheus contribution already exists: "
            f"{contribution}"
        )

    try:
        staging.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"Prometheus: cannot inspect private staging: {error}"
    else:
        return f"Prometheus: private staging already exists: {staging}"

    try:
        staging_parent = staging.parent.stat(follow_symlinks=False)
        contribution_parent = contribution.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(staging_parent.st_mode):
            return (
                "Prometheus: private staging parent is not an ordinary directory: "
                f"{staging.parent}"
            )
        if not stat.S_ISDIR(contribution_parent.st_mode):
            return (
                "Prometheus: admitted contribution parent is not an ordinary directory: "
                f"{contribution.parent}"
            )
        if staging_parent.st_dev != contribution_parent.st_dev:
            return (
                "Prometheus: private staging and admitted contribution must share "
                "a filesystem"
            )
    except OSError as error:
        return f"Prometheus: cannot validate private contribution boundaries: {error}"
    return None


def _with_residue(problem: str, staging: Path) -> str:
    try:
        staging.lstat()
    except OSError:
        return problem
    return f"{problem}; private residue: {staging}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
