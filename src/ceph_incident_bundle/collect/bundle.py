"""Validate admitted state and atomically publish one Incident Bundle."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import unicodedata


OWNERSHIP_MARKER = ".ceph-incident-bundle-owned"


class BundlePublicationError(Exception):
    """The final Incident Bundle was not delivered."""


def publish_bundle(
    workspace: Path,
    final_path: Path,
    *,
    collector_version: str,
    started_at: datetime,
    since: str,
    prior_partial: bool,
) -> str | None:
    """Validate admitted state and publish one bundle without replacing a path.

    ``workspace`` must contain its ownership marker plus this admitted layout::

        admitted/inventory.ini
        admitted/node-contributions/<inventory-name>/node/
        admitted/node-contributions/<inventory-name>/ceph/  # optional
        admitted/kubernetes/
        admitted/prometheus/

    The caller owns the workspace until this function is called.  At that handoff,
    publication assumes responsibility for workspace cleanup and exact residue
    reporting on every return or raised publication error.
    """
    workspace = Path(workspace)
    final_path = Path(final_path)
    candidate: Path | None = None
    cleanup_problem: str | None = None
    try:
        bundle_root = _validate_lifecycle_paths(workspace, final_path, started_at)
        entries = _validated_entries(workspace, bundle_root)
        descriptor, candidate_name = tempfile.mkstemp(
            prefix=f".{final_path.name}.candidate.", dir=final_path.parent
        )
        candidate = Path(candidate_name)
        with os.fdopen(descriptor, "wb") as candidate_file:
            with tarfile.open(fileobj=candidate_file, mode="w:gz") as archive:
                for archive_name, source, is_directory in entries:
                    if is_directory:
                        _add_directory(archive, archive_name, started_at)
                    else:
                        assert source is not None
                        _add_regular_file(archive, archive_name, source, started_at)
                cleanup_problem = _cleanup_workspace(workspace)
                metadata = {
                    "collector_version": collector_version,
                    "started_at": _rfc3339(started_at),
                    "finished_at": _rfc3339(datetime.now(timezone.utc)),
                    "since": since,
                    "outcome": "partial"
                    if prior_partial or cleanup_problem is not None
                    else "complete",
                }
                _add_bytes(
                    archive,
                    f"{bundle_root}/collection.json",
                    (
                        json.dumps(metadata, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8"),
                    started_at,
                )
            candidate_file.flush()
            os.fsync(candidate_file.fileno())
        os.link(candidate, final_path)
        try:
            candidate.unlink()
        except OSError as cleanup_error:
            try:
                final_path.unlink()
            except OSError as rollback_error:
                raise BundlePublicationError(
                    f"cannot remove private candidate {candidate}: {cleanup_error}; "
                    f"cannot roll back final destination {final_path}: {rollback_error}"
                ) from cleanup_error
            raise BundlePublicationError(
                f"cannot remove private candidate {candidate}: {cleanup_error}; "
                "final publication was rolled back"
            ) from cleanup_error
        return cleanup_problem
    except KeyboardInterrupt:
        if candidate is not None:
            _remove_candidate(candidate)
        _cleanup_workspace(workspace)
        raise
    except Exception as error:
        candidate_cleanup_problem = None
        if candidate is not None:
            candidate_cleanup_problem = _remove_candidate(candidate)
        cleanup_problem = cleanup_problem or _cleanup_workspace(workspace)
        message = str(error)
        if candidate_cleanup_problem:
            message = f"{message}; {candidate_cleanup_problem}"
        if cleanup_problem:
            message = f"{message}; {cleanup_problem}"
        raise BundlePublicationError(message) from error


def _validate_lifecycle_paths(
    workspace: Path, final_path: Path, started_at: datetime
) -> str:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise BundlePublicationError("collection start time must be timezone-aware")
    if started_at.microsecond:
        raise BundlePublicationError("collection start time must use a whole second")
    started_utc = started_at.astimezone(timezone.utc)
    bundle_root = f"ceph-incident-bundle-{started_utc:%Y%m%dT%H%M%SZ}"
    if final_path.name != f"{bundle_root}.tar.gz":
        raise BundlePublicationError(
            "final destination does not match collection start time"
        )
    _require_directory(workspace, "owned workstation workspace")
    _require_owned_workspace(workspace)
    _require_directory(final_path.parent, "final destination parent")
    try:
        final_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise BundlePublicationError(f"final destination already exists: {final_path}")
    resolved_workspace = workspace.resolve()
    resolved_final = final_path.resolve()
    if (
        resolved_workspace == resolved_final
        or resolved_workspace in resolved_final.parents
        or resolved_final in resolved_workspace.parents
    ):
        raise BundlePublicationError(
            "workspace and final destination must be distinct non-containing paths"
        )
    return bundle_root


def _validated_entries(
    workspace: Path, bundle_root: str
) -> list[tuple[str, Path | None, bool]]:
    admitted = workspace / "admitted"
    inventory = admitted / "inventory.ini"
    contributions = admitted / "node-contributions"
    kubernetes = admitted / "kubernetes"
    prometheus = admitted / "prometheus"
    _require_regular(inventory, "admitted Inventory Snapshot")
    _require_directory(contributions, "node contributions")
    _require_directory(kubernetes, "Kubernetes contribution")
    _require_directory(prometheus, "Prometheus contribution")
    entries: list[tuple[str, Path | None, bool]] = [
        (bundle_root, None, True),
        (f"{bundle_root}/inventory.ini", inventory, False),
        (f"{bundle_root}/nodes", None, True),
        (f"{bundle_root}/ceph", None, True),
        (f"{bundle_root}/kubernetes", None, True),
        (f"{bundle_root}/prometheus", None, True),
    ]
    ceph_seen = False
    for contribution in sorted(contributions.iterdir(), key=lambda path: path.name):
        _require_safe_component(contribution.name)
        _require_directory(contribution, "admitted node contribution")
        children = {child.name: child for child in contribution.iterdir()}
        if set(children) - {"node", "ceph"}:
            raise BundlePublicationError(f"unknown admitted member: {contribution}")
        node = children.get("node")
        if node is None:
            raise BundlePublicationError(
                f"admitted contribution lacks node/: {contribution}"
            )
        _require_directory(node, "admitted node evidence")
        _require_directory(node / "probes", "admitted node probes")
        _require_directory(node / "files", "admitted node files")
        _append_tree(entries, node, f"{bundle_root}/nodes/{contribution.name}")
        ceph = children.get("ceph")
        if ceph is not None:
            if ceph_seen:
                raise BundlePublicationError("more than one admitted Ceph contribution")
            ceph_seen = True
            _require_directory(ceph, "admitted Ceph evidence")
            _require_directory(ceph / "probes", "admitted Ceph probes")
            _append_children(entries, ceph, f"{bundle_root}/ceph")
    _append_children(entries, kubernetes, f"{bundle_root}/kubernetes")
    _append_children(entries, prometheus, f"{bundle_root}/prometheus")
    _validate_archive_paths(entries)
    return entries


def _append_tree(
    entries: list[tuple[str, Path | None, bool]], source: Path, archive_name: str
) -> None:
    entries.append((archive_name, source, True))
    _append_children(entries, source, archive_name)


def _append_children(
    entries: list[tuple[str, Path | None, bool]], source: Path, archive_name: str
) -> None:
    for child in sorted(source.iterdir(), key=lambda path: path.name):
        _require_safe_component(child.name)
        mode = child.stat(follow_symlinks=False).st_mode
        child_name = f"{archive_name}/{child.name}"
        if stat.S_ISDIR(mode):
            _append_tree(entries, child, child_name)
        elif stat.S_ISREG(mode):
            entries.append((child_name, child, False))
        else:
            raise BundlePublicationError(f"inadmissible final-tree object: {child}")


def _validate_archive_paths(entries: list[tuple[str, Path | None, bool]]) -> None:
    exact: dict[tuple[str, ...], bool] = {}
    portable: dict[tuple[str, ...], tuple[str, ...]] = {}
    for archive_name, _source, is_directory in entries:
        components = tuple(archive_name.split("/"))
        if any(
            component in {"", ".", ".."} or "\\" in component
            for component in components
        ):
            raise BundlePublicationError(f"unsafe final archive path: {archive_name}")
        if components in exact:
            raise BundlePublicationError(
                f"duplicate final archive path: {archive_name}"
            )
        portable_key = tuple(_portable_component(component) for component in components)
        previous = portable.get(portable_key)
        if previous is not None:
            raise BundlePublicationError(
                "portable final-path collision: "
                f"{'/'.join(previous)} and {archive_name}"
            )
        exact[components] = is_directory
        portable[portable_key] = components
    for components in exact:
        for length in range(1, len(components)):
            parent = components[:length]
            if parent in exact and not exact[parent]:
                raise BundlePublicationError(
                    f"regular file is an ancestor in final tree: {'/'.join(parent)}"
                )


def _require_owned_workspace(workspace: Path) -> None:
    marker = workspace / OWNERSHIP_MARKER
    _require_regular(marker, "workspace ownership marker")
    try:
        recorded_path = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise BundlePublicationError(
            f"cannot read workspace ownership marker: {error}"
        ) from error
    if recorded_path != str(workspace.resolve()):
        raise BundlePublicationError(
            "workspace ownership marker does not match workspace"
        )


def _require_safe_component(component: str) -> None:
    if component in {"", ".", ".."} or "/" in component or "\\" in component:
        raise BundlePublicationError(f"unsafe final-tree component: {component!r}")


def _portable_component(component: str) -> str:
    """Return the portable identity used only for final-tree collisions.

    Archive admission repeats this small rule independently so each peer retains a
    complete fail-closed validation boundary without importing the other.
    """
    normalized = unicodedata.normalize("NFC", component)
    return unicodedata.normalize("NFC", normalized.casefold())


def _require_regular(path: Path, label: str) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise BundlePublicationError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISREG(mode):
        raise BundlePublicationError(f"{label} must be an ordinary regular file")


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise BundlePublicationError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISDIR(mode):
        raise BundlePublicationError(f"{label} must be an ordinary directory")


def _add_directory(archive: tarfile.TarFile, name: str, started_at: datetime) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o700
    member.mtime = int(started_at.timestamp())
    archive.addfile(member)


def _add_regular_file(
    archive: tarfile.TarFile, name: str, source: Path, started_at: datetime
) -> None:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as contents:
        facts = os.fstat(contents.fileno())
        if not stat.S_ISREG(facts.st_mode):
            raise BundlePublicationError(f"final-tree source changed type: {source}")
        member = tarfile.TarInfo(name)
        member.size = facts.st_size
        member.mode = 0o600
        member.mtime = int(started_at.timestamp())
        archive.addfile(member, contents)


def _add_bytes(
    archive: tarfile.TarFile, name: str, contents: bytes, started_at: datetime
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mode = 0o600
    member.mtime = int(started_at.timestamp())
    archive.addfile(member, io.BytesIO(contents))


def _cleanup_workspace(workspace: Path) -> str | None:
    """Clean the workspace after publication has accepted ownership.

    The top-level flow deliberately keeps its pre-handoff cleanup local because it
    still owns failures that happen before publication starts.
    """
    if not workspace.exists():
        return None
    try:
        _require_owned_workspace(workspace)
    except BundlePublicationError as error:
        return f"refusing to clean unowned workstation workspace {workspace}: {error}"
    try:
        shutil.rmtree(workspace)
    except OSError as error:
        return f"cannot remove workstation workspace {workspace}: {error}"
    return None


def _remove_candidate(candidate: Path) -> str | None:
    try:
        candidate.unlink(missing_ok=True)
    except OSError as error:
        return f"cannot remove private Incident Bundle candidate {candidate}: {error}"
    return None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
