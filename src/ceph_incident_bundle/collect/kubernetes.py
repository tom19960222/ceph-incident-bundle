"""Collect one workstation-side external-Rook snapshot with direct kubectl."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import NamedTuple


class _KubernetesProbe(NamedTuple):
    name: str
    argv: tuple[str, ...]
    pod_control_namespace: str | None = None


_ROOK_RESOURCES = (
    "cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,"
    "cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io"
)
_TERMINATION_GRACE_SECONDS = 1


def collect_kubernetes(
    *,
    context: str,
    consumer_namespace: str,
    operator_namespace: str,
    since: str,
    probe_timeout_seconds: int,
    staging_directory: Path,
    contribution_directory: Path,
) -> list[str]:
    """Collect and atomically admit one configured Kubernetes contribution."""
    staging_directory = Path(staging_directory)
    contribution_directory = Path(contribution_directory)
    boundary_problem = _boundary_problem(staging_directory, contribution_directory)
    if boundary_problem is not None:
        return [boundary_problem]

    try:
        staging_directory.mkdir(mode=0o700)
        candidate = staging_directory / "contribution"
        probes_directory = candidate / "probes"
        probes_directory.mkdir(parents=True)
    except OSError as error:
        return [
            _with_residue(
                f"Kubernetes: cannot create private staging: {error}",
                staging_directory,
            )
        ]

    problems: list[str] = []
    pending_probes = list(
        _probe_catalog(
            context=context,
            consumer_namespace=consumer_namespace,
            operator_namespace=operator_namespace,
        )
    )
    next_log_sequence = 1
    probe_index = 0
    while probe_index < len(pending_probes):
        probe = pending_probes[probe_index]
        probe_index += 1
        capture = probes_directory / probe.name
        try:
            succeeded, process_residue = _run_probe(
                probe,
                capture,
                probe_timeout_seconds=probe_timeout_seconds,
            )
        except OSError as error:
            cleanup_errors = [str(error)]
            interrupted = error.__context__
            while interrupted is not None and not isinstance(
                interrupted, KeyboardInterrupt
            ):
                if isinstance(interrupted, OSError):
                    cleanup_errors.append(str(interrupted))
                interrupted = interrupted.__context__
            if isinstance(interrupted, KeyboardInterrupt):
                details: list[str] = []
                if isinstance(interrupted.__cause__, OSError):
                    details.append(str(interrupted.__cause__))
                details.extend(
                    f"Kubernetes {probe.name}: interruption cleanup failed: {message}"
                    for message in reversed(cleanup_errors)
                )
                # Preserve the interrupt as the outcome; the standard cause is
                # only the concrete cleanup detail the top-level owner reports.
                raise KeyboardInterrupt from OSError("; ".join(details))
            return [
                *problems,
                _with_residue(
                    f"Kubernetes: cannot preserve {probe.name} Probe: {error}",
                    staging_directory,
                ),
            ]
        if not succeeded:
            result = json.loads((capture / "result.json").read_text(encoding="utf-8"))
            problems.append(
                f"Kubernetes {probe.name} Probe failed: "
                f"outcome={result['outcome']} exit_code={result['exit_code']}"
                + _error_suffix(result["error"])
            )
        if process_residue is not None:
            problems.append(
                _with_residue(
                    f"Kubernetes {probe.name} process-group cleanup failed: "
                    f"{process_residue}",
                    staging_directory,
                )
            )
            return problems
        if not succeeded:
            continue
        if probe.pod_control_namespace is not None:
            log_probes, parse_problem = _pod_log_probes(
                capture / "stdout",
                probe.name,
                context=context,
                namespace=probe.pod_control_namespace,
                since=since,
                first_sequence=next_log_sequence,
            )
            if parse_problem is not None:
                problems.append(parse_problem)
            else:
                pending_probes[probe_index:probe_index] = log_probes
                next_log_sequence += len(log_probes)

    try:
        os.rename(candidate, contribution_directory)
    except OSError as error:
        problems.append(
            _with_residue(
                f"Kubernetes: cannot atomically promote complete contribution: {error}",
                staging_directory,
            )
        )
        return problems

    try:
        staging_directory.rmdir()
    except OSError as error:
        problems.append(
            f"Kubernetes: admitted contribution but cannot remove private staging "
            f"{staging_directory}: {error}"
        )
    return problems


def _probe_catalog(
    *, context: str, consumer_namespace: str, operator_namespace: str
) -> tuple[_KubernetesProbe, ...]:
    def probe(
        name: str,
        namespace: str,
        *arguments: str,
        pod_control_namespace: str | None = None,
    ) -> _KubernetesProbe:
        return _KubernetesProbe(
            name=name,
            argv=(
                "kubectl",
                f"--context={context}",
                f"--namespace={namespace}",
                *arguments,
            ),
            pod_control_namespace=pod_control_namespace,
        )

    catalog = [
        probe("consumer-pods-wide", consumer_namespace, "get", "pods", "--output=wide"),
        probe(
            "consumer-events",
            consumer_namespace,
            "get",
            "events",
            "--sort-by=.lastTimestamp",
        ),
        probe(
            "consumer-rook-resources-yaml",
            consumer_namespace,
            "get",
            _ROOK_RESOURCES,
            "--output=yaml",
        ),
        probe(
            "consumer-pods-json",
            consumer_namespace,
            "get",
            "pods",
            "--output=json",
            pod_control_namespace=consumer_namespace,
        ),
    ]
    if operator_namespace != consumer_namespace:
        catalog.append(
            probe(
                "operator-pods-json",
                operator_namespace,
                "get",
                "pods",
                "--output=json",
                pod_control_namespace=operator_namespace,
            )
        )
    return tuple(catalog)


def _run_probe(
    probe: _KubernetesProbe,
    capture: Path,
    *,
    probe_timeout_seconds: int,
) -> tuple[bool, str | None]:
    capture.mkdir()
    started_at = _utc_now()
    outcome = "exited"
    exit_code: int | None = None
    error: dict[str, str] | None = None
    process_residue: str | None = None

    with (capture / "stdout").open("xb") as stdout_file, (
        capture / "stderr"
    ).open("xb") as stderr_file:
        try:
            process = subprocess.Popen(
                list(probe.argv),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=True,
            )
        except OSError as process_error:
            outcome = "failed_to_start"
            error = {
                "kind": type(process_error).__name__,
                "message": str(process_error),
            }
        else:
            try:
                exit_code = process.wait(timeout=probe_timeout_seconds or None)
            except KeyboardInterrupt:
                process_residue = _terminate_process_group(process)
                if process_residue is not None:
                    raise KeyboardInterrupt from OSError(
                        f"Kubernetes {probe.name}: {process_residue}"
                    )
                raise
            except subprocess.TimeoutExpired:
                outcome = "timed_out"
                process_residue = _terminate_process_group(process)
                error = {
                    "kind": "timeout",
                    "message": (
                        f"{probe.name} exceeded {probe_timeout_seconds} seconds"
                        + _termination_suffix(process_residue)
                    ),
                }
            except (OverflowError, ValueError) as wait_error:
                exit_code = process.poll()
                if exit_code is None:
                    outcome = "timed_out"
                    process_residue = _terminate_process_group(process)
                    error = {
                        "kind": "timeout",
                        "message": (
                            f"cannot apply {probe.name} timeout: {wait_error}"
                            + _termination_suffix(process_residue)
                        ),
                    }

    result = {
        "argv": list(probe.argv),
        "started_at": started_at,
        "finished_at": _utc_now(),
        "outcome": outcome,
        "exit_code": exit_code,
        "error": error,
    }
    (capture / "result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )
    return outcome == "exited" and exit_code == 0, process_residue


def _terminate_process_group(process: subprocess.Popen[bytes]) -> str | None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if _wait_for_process_group_exit(process):
        return None

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if _wait_for_process_group_exit(process):
        return None
    return f"process group {process.pid} did not stop after SIGKILL"


def _wait_for_process_group_exit(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while True:
        leader_exited = process.poll() is not None
        group_exited = not _process_group_exists(process.pid)
        if leader_exited and group_exited:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _termination_suffix(problem: str | None) -> str:
    if problem is None:
        return ""
    return f"; {problem}"


def _pod_log_probes(
    capture: Path,
    probe_name: str,
    *,
    context: str,
    namespace: str,
    since: str,
    first_sequence: int,
) -> tuple[list[_KubernetesProbe], str | None]:
    try:
        document = json.loads(capture.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [], f"Kubernetes {probe_name} control response is not valid JSON: {error}"
    containers_to_collect: list[tuple[str, str, bool]] = []
    try:
        items = document["items"]
        if not isinstance(items, list):
            raise TypeError("items is not a list")
        for pod in items:
            if not isinstance(pod, dict):
                raise TypeError("Pod entry is not an object")
            metadata = pod["metadata"]
            spec = pod["spec"]
            status = pod.get("status", {})
            pod_name = _plain_text(metadata["name"], "Pod name")
            _plain_text(metadata["namespace"], "Pod namespace")
            for spec_key, status_key in (
                ("containers", "containerStatuses"),
                ("initContainers", "initContainerStatuses"),
                ("ephemeralContainers", "ephemeralContainerStatuses"),
            ):
                containers = spec.get(spec_key, [])
                statuses = status.get(status_key, [])
                if not isinstance(containers, list) or not isinstance(statuses, list):
                    raise TypeError(f"{spec_key} or {status_key} is not a list")
                restart_counts: dict[str, int] = {}
                for container_status in statuses:
                    name = _plain_text(container_status["name"], "container name")
                    restart_count = container_status["restartCount"]
                    if type(restart_count) is not int or restart_count < 0:
                        raise TypeError("container restartCount is not a nonnegative integer")
                    restart_counts[name] = restart_count
                for container in containers:
                    name = _plain_text(container["name"], "container name")
                    containers_to_collect.append(
                        (pod_name, name, restart_counts.get(name, 0) > 0)
                    )
    except (AttributeError, KeyError, TypeError) as error:
        return [], f"Kubernetes {probe_name} control response is malformed: {error}"

    probes: list[_KubernetesProbe] = []
    sequence = first_sequence
    for pod_name, container_name, has_previous in containers_to_collect:
        arguments = (
            "kubectl",
            f"--context={context}",
            f"--namespace={namespace}",
            "logs",
            pod_name,
            f"--container={container_name}",
            f"--since={since}",
        )
        probes.append(
            _KubernetesProbe(
                _log_probe_name(sequence, previous=False),
                arguments,
            )
        )
        sequence += 1
        if has_previous:
            probes.append(
                _KubernetesProbe(
                    _log_probe_name(sequence, previous=True),
                    (*arguments, "--previous"),
                )
            )
            sequence += 1
    return probes, None


def _log_probe_name(sequence: int, *, previous: bool) -> str:
    prefix = "pod-previous-log" if previous else "pod-log"
    return f"{prefix}-{sequence:06d}"


def _plain_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise TypeError(f"{label} is not plain nonempty text")
    return value


def _boundary_problem(staging: Path, contribution: Path) -> str | None:
    try:
        contribution.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"Kubernetes: cannot inspect admitted contribution: {error}"
    else:
        return (
            "Kubernetes: admitted Kubernetes contribution already exists: "
            f"{contribution}"
        )

    try:
        staging.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"Kubernetes: cannot inspect private staging: {error}"
    else:
        return f"Kubernetes: private staging already exists: {staging}"

    try:
        staging_parent_mode = staging.parent.stat(follow_symlinks=False).st_mode
        contribution_parent_mode = contribution.parent.stat(
            follow_symlinks=False
        ).st_mode
        if not stat.S_ISDIR(staging_parent_mode):
            return (
                "Kubernetes: private staging parent is not an ordinary directory: "
                f"{staging.parent}"
            )
        if not stat.S_ISDIR(contribution_parent_mode):
            return (
                "Kubernetes: admitted contribution parent is not an ordinary directory: "
                f"{contribution.parent}"
            )
        if staging.parent.stat(follow_symlinks=False).st_dev != contribution.parent.stat(
            follow_symlinks=False
        ).st_dev:
            return (
                "Kubernetes: private staging and admitted contribution must share "
                "a filesystem"
            )
    except OSError as error:
        return f"Kubernetes: cannot validate private contribution boundaries: {error}"
    return None


def _with_residue(problem: str, staging: Path) -> str:
    try:
        staging.lstat()
    except OSError:
        return problem
    return f"{problem}; private residue: {staging}"


def _error_suffix(error: object) -> str:
    if not isinstance(error, dict):
        return ""
    return f" error={error.get('kind')}: {error.get('message')}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
