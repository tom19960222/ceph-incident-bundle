"""How the lab workflow writes and stamps its local-only output.

Candidates, active profiles, the activation ledger and Lab Validation Reports are
all local-only artifacts that must never be committed and must stay readable only
by their owner.  That single decision — owner-only mode, atomic replacement, one
UTC timestamp format — lives here so no writer can drift from it.

Nothing else belongs in this module: report identity and run identifiers live with
the report that defines them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_timestamp() -> str:
    """The one timestamp format every lab artifact is stamped with."""

    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


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
