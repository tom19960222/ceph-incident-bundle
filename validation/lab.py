"""The fixed lab workflow entry points behind the `lab-*` make targets.

    python3 -m validation.lab status    --profile PATH [--runs-dir PATH] [--json]
    python3 -m validation.lab discover  --profile PATH [--candidate-out PATH]
                                        [--replace-candidate] [--json]
    python3 -m validation.lab activate  --profile PATH --candidate PATH
                                        [--replace-active] [--json]
    python3 -m validation.lab preflight --profile PATH [--runs-dir PATH] [--json]
    python3 -m validation.lab qualify   --profile PATH [--runs-dir PATH]
                                        [--collect-timeout SECONDS] [--json]

`status` is local-only.  `discover` and `preflight` reach the lab with read-only
queries, and `preflight` additionally requires `CEPH_INCIDENT_LAB_CONFIRM=1`
because it is part of the qualification path.  `activate` writes the trusted
profile and requires `CEPH_INCIDENT_LAB_ACTIVATE=1`.  `qualify` is the full
dual-run gate behind `make validate-lab`; it runs two real collects and therefore
requires the same explicit confirmation as the preflight.

Exit codes: 0 when the command reached its intended state, 1 for a usage or
configuration error, 2 when the workflow is blocked and the printed `next_action`
must be followed.  Every command prints exactly one next action.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from validation.lab_activation import activate
from validation.lab_commands import (
    ACTIVATION_CONFIRMATION_VARIABLE,
    PREFLIGHT_CONFIRMATION_VARIABLE,
    preflight_command,
    status_command,
)
from validation.lab_discovery import discover
from validation.lab_preflight import preflight
from validation.lab_profile import LabProfileError
from validation.lab_qualify import DEFAULT_COLLECT_TIMEOUT_SECONDS, qualify
from validation.lab_report import (
    ReportRejected,
    code_identity,
    report_from_preflight,
    report_from_qualification,
    report_from_unusable_profile,
    reserve_run_directory,
    write_report,
)
from validation.lab_status import lab_status


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIRECTORY = REPOSITORY_ROOT / "results" / "lab-validation"
COMMANDS = ("status", "discover", "activate", "preflight", "qualify")
USAGE = """Usage:
  python3 -m validation.lab status --profile PATH [--runs-dir PATH] [--json]
  python3 -m validation.lab discover --profile PATH [--candidate-out PATH]
      [--replace-candidate] [--json]
  python3 -m validation.lab activate --profile PATH --candidate PATH
      [--replace-active] [--json]
  python3 -m validation.lab preflight --profile PATH [--runs-dir PATH] [--json]
  python3 -m validation.lab qualify --profile PATH [--runs-dir PATH]
      [--collect-timeout SECONDS] [--json]"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BLOCKED = 2


class UsageError(Exception):
    """The command line was not usable as given."""


def main(arguments: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    try:
        options = _parse(argv)
    except UsageError as error:
        sys.stderr.write(f"error: {error}\n{USAGE}\n")
        return EXIT_USAGE
    try:
        return _dispatch(options)
    except LabProfileError as error:
        profile: Path = options["profile"]  # type: ignore[assignment]
        sys.stderr.write(f"error: {error}\n")
        sys.stderr.write(
            f"next action: fix {profile} ({error}), then run {status_command(profile)}\n"
        )
        return EXIT_BLOCKED
    except ReportRejected as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_BLOCKED


def _dispatch(options: dict[str, object]) -> int:
    command = options["command"]
    if command == "status":
        return _status(options)
    if command == "discover":
        return _discover(options)
    if command == "activate":
        return _activate(options)
    if command == "preflight":
        return _preflight(options)
    return _qualify(options)


def _status(options: dict[str, object]) -> int:
    status = lab_status(
        options["profile"],  # type: ignore[arg-type]
        runs_directory=options["runs_directory"],  # type: ignore[arg-type]
    )
    _emit(status.summary(), status.text(), as_json=bool(options["as_json"]))
    return EXIT_OK if status.ready else EXIT_BLOCKED


def _discover(options: dict[str, object]) -> int:
    result = discover(
        options["profile"],  # type: ignore[arg-type]
        candidate_path=options["candidate"],  # type: ignore[arg-type]
        replace_candidate=bool(options["replace_candidate"]),
    )
    lines = ["Lab profile discovery", f"  input:     {result.input_path}"]
    for finding in result.findings:
        verdict = "ok" if finding.ok else "FAILED"
        observed = f" -> {finding.observed}" if finding.observed else ""
        lines.append(f"  {finding.subject}: {verdict}{observed} ({finding.detail})")
    lines.append(f"  candidate: {result.candidate_path}")
    lines.append(f"  written:   {'yes' if result.written else 'no'}")
    if result.candidate is not None:
        lines.append(f"  hash:      {result.candidate.profile_hash}")
    if result.comparison_note:
        lines.append(f"  comparison: {result.comparison_note}")
    for difference in result.differences:
        lines.append(f"  difference: {difference}")
    lines.append(f"  status:    {result.status}")
    if result.blocked_reason:
        lines.append(f"  blocked by: {result.blocked_reason}")
    lines += ["", f"next action: {result.next_action}"]
    _emit(result.summary(), "\n".join(lines) + "\n", as_json=bool(options["as_json"]))
    return EXIT_OK if result.ok else EXIT_BLOCKED


def _activate(options: dict[str, object]) -> int:
    result = activate(
        options["candidate"],  # type: ignore[arg-type]
        options["profile"],  # type: ignore[arg-type]
        confirmed=os.environ.get(ACTIVATION_CONFIRMATION_VARIABLE) == "1",
        replace_active=bool(options["replace_active"]),
    )
    lines = [
        "Lab profile activation",
        f"  candidate: {result.candidate_path}",
        f"  active:    {result.active_path}",
        f"  activated: {'yes' if result.activated else 'no'}",
    ]
    if result.active_hash:
        lines.append(f"  hash:      {result.active_hash}")
    if result.previous_hash:
        lines.append(f"  replaced:  {result.previous_hash}")
    if result.log_path:
        lines.append(f"  audit log: {result.log_path}")
    lines.append(f"  status:    {result.status}")
    if result.blocked_reason:
        lines.append(f"  blocked by: {result.blocked_reason}")
    lines += ["", f"next action: {result.next_action}"]
    _emit(result.summary(), "\n".join(lines) + "\n", as_json=bool(options["as_json"]))
    return EXIT_OK if result.ok else EXIT_BLOCKED


def _preflight(options: dict[str, object]) -> int:
    if os.environ.get(PREFLIGHT_CONFIRMATION_VARIABLE) != "1":
        sys.stderr.write(
            "error: the identity preflight connects to the lab and must be "
            f"confirmed explicitly\nnext action: re-run with "
            f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1\n"
        )
        return EXIT_USAGE
    profile_path: Path = options["profile"]  # type: ignore[assignment]
    runs_directory: Path = options["runs_directory"]  # type: ignore[assignment]
    try:
        result = preflight(profile_path)
    except LabProfileError as error:
        # An unusable profile is still an attempt, so it still leaves a report for
        # the next agent rather than only a message on this terminal.
        return _report_unusable_profile(profile_path, error, runs_directory)
    report = report_from_preflight(
        result,
        code=code_identity(REPOSITORY_ROOT),
        hosts=tuple(host.name for host in result.profile.hosts),
    )
    location = write_report(runs_directory, report)
    lines = ["Lab identity preflight"]
    for check in result.checks:
        lines.append(
            f"  {check.name}: {'ok' if check.ok else 'FAILED'} ({check.detail})"
        )
    lines += [
        f"  status:    {result.status}",
        f"  report:    {location.directory}",
    ]
    if result.blocked_reason:
        lines.append(f"  blocked by: {result.blocked_reason}")
    lines += ["", f"next action: {result.next_action}"]
    summary = result.summary()
    summary["report_directory"] = str(location.directory)
    _emit(summary, "\n".join(lines) + "\n", as_json=bool(options["as_json"]))
    return EXIT_OK if result.ok else EXIT_BLOCKED


def _qualify(options: dict[str, object]) -> int:
    if os.environ.get(PREFLIGHT_CONFIRMATION_VARIABLE) != "1":
        sys.stderr.write(
            "error: the real-lab gate runs two full collects against the lab and "
            f"must be confirmed explicitly\nnext action: re-run with "
            f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1\n"
        )
        return EXIT_USAGE
    profile_path: Path = options["profile"]  # type: ignore[assignment]
    runs_directory: Path = options["runs_directory"]  # type: ignore[assignment]
    try:
        # The run directory is reserved before anything runs: the two bundles and
        # both command ledgers live beside the report they are evidence for.
        run_directory = reserve_run_directory(runs_directory)
    except OSError as error:
        sys.stderr.write(f"error: cannot create a run directory: {error}\n")
        return EXIT_USAGE
    try:
        result = qualify(
            profile_path,
            run_directory=run_directory,
            collect_timeout=int(options["collect_timeout"]),  # type: ignore[arg-type]
        )
    except LabProfileError as error:
        return _report_unusable_profile(
            profile_path, error, runs_directory, directory=run_directory
        )
    report = report_from_qualification(result, code=code_identity(REPOSITORY_ROOT))
    location = write_report(runs_directory, report, directory=run_directory)
    summary = result.summary()
    summary["report_directory"] = str(location.directory)
    _emit(summary, result.text(), as_json=bool(options["as_json"]))
    return EXIT_OK if result.ok else EXIT_BLOCKED


def _report_unusable_profile(
    profile_path: Path,
    error: LabProfileError,
    runs_directory: Path,
    *,
    directory: Path | None = None,
) -> int:
    next_action = (
        f"Fix {profile_path} ({error}), then re-run {preflight_command(profile_path)}"
    )
    location = write_report(
        runs_directory,
        report_from_unusable_profile(
            profile_path,
            str(error),
            code=code_identity(REPOSITORY_ROOT),
            status=error.failure_class,
            next_action=next_action,
        ),
        directory=directory,
    )
    sys.stderr.write(f"error: {error}\n")
    sys.stdout.write(
        f"Lab identity preflight\n"
        f"  profile-load: FAILED ({error})\n"
        f"  status:    {error.failure_class}\n"
        f"  report:    {location.directory}\n"
        f"\nnext action: {next_action}\n"
    )
    return EXIT_BLOCKED


def _emit(document: dict[str, object], text: str, *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(text)


def _parse(argv: list[str]) -> dict[str, object]:
    if not argv:
        raise UsageError("missing command")
    command = argv[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    values: dict[str, object] = {
        "command": command,
        "profile": None,
        "candidate": None,
        "runs_directory": DEFAULT_RUNS_DIRECTORY,
        "replace_candidate": False,
        "replace_active": False,
        "collect_timeout": DEFAULT_COLLECT_TIMEOUT_SECONDS,
        "as_json": False,
    }
    rest = argv[1:]
    index = 0
    while index < len(rest):
        option = rest[index]
        if option == "--json":
            values["as_json"] = True
            index += 1
            continue
        if option == "--replace-candidate":
            if command != "discover":
                raise UsageError(f"--replace-candidate is not valid for {command}")
            values["replace_candidate"] = True
            index += 1
            continue
        if option == "--replace-active":
            if command != "activate":
                raise UsageError(f"--replace-active is not valid for {command}")
            values["replace_active"] = True
            index += 1
            continue
        if option == "--collect-timeout":
            if command != "qualify":
                raise UsageError(f"{option} is not valid for {command}")
            if index + 1 >= len(rest):
                raise UsageError(f"{option} requires a value")
            value = rest[index + 1]
            if not value.isdecimal() or int(value) <= 0:
                raise UsageError(f"{option} must be a positive number of seconds")
            values["collect_timeout"] = int(value)
            index += 2
            continue
        if option in ("--profile", "--candidate", "--candidate-out", "--runs-dir"):
            if index + 1 >= len(rest):
                raise UsageError(f"{option} requires a value")
            value = rest[index + 1]
            index += 2
            if option == "--profile":
                values["profile"] = _absolute(option, value)
            elif option == "--runs-dir":
                values["runs_directory"] = Path(value).expanduser()
            else:
                if option == "--candidate-out" and command != "discover":
                    raise UsageError(f"{option} is not valid for {command}")
                if option == "--candidate" and command != "activate":
                    raise UsageError(f"{option} is not valid for {command}")
                values["candidate"] = _absolute(option, value)
            continue
        raise UsageError(f"unknown option: {option}")
    if values["profile"] is None:
        raise UsageError("--profile is required")
    if command == "activate" and values["candidate"] is None:
        raise UsageError("--candidate is required for activate")
    return values


def _absolute(option: str, value: str) -> Path:
    """Require absolute paths: a lab profile is referenced, not discovered by cwd."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        raise UsageError(f"{option} must be an absolute path: {value}")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
