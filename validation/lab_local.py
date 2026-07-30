"""Local-only helpers shared by the lab workflow: writes, time and code identity.

Everything written by the lab workflow — candidates, active profiles, activation
records, reports — is local-only output that must never be committed and must
stay readable only by its owner.  These helpers keep that decision in one place.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
GIT_TIMEOUT_SECONDS = 10


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime(RUN_ID_FORMAT)


def write_owner_only(path: Path, text: str) -> None:
    """Replace `path` atomically with owner-only permissions.

    The temporary file is created in the destination directory so the rename
    cannot cross a filesystem boundary and leave a half-written profile behind.
    """

    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def append_owner_only(path: Path, line: str) -> None:
    """Append one record to a local-only, owner-only ledger."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(line if line.endswith("\n") else line + "\n")


@dataclass(frozen=True)
class CodeIdentity:
    """Which code a lab workflow ran, so a report can be traced back to it."""

    commit: str
    dirty: bool

    @property
    def display(self) -> str:
        return f"{self.commit}{'-dirty' if self.dirty else ''}"


def code_identity(root: Path) -> CodeIdentity:
    """Read the repository commit and dirty state, or report them as unknown."""

    commit = _git(root, "rev-parse", "HEAD") or "unknown"
    status = _git(root, "status", "--porcelain")
    return CodeIdentity(commit=commit, dirty=bool(status))


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()
