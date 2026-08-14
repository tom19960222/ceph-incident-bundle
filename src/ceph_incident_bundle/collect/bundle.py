"""Validate admitted state and atomically publish one Incident Bundle."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import secrets
import stat
import tarfile
from typing import TypeAlias
import unicodedata


OWNERSHIP_MARKER = ".ceph-incident-bundle-owned"

# An admitted source is reopened from the pinned workspace descriptor using its
# relative components, then checked against the device and inode seen during the
# complete validation pass.
_SourceIdentity: TypeAlias = tuple[tuple[str, ...], int, int]
_ArchiveEntry: TypeAlias = tuple[str, _SourceIdentity | None, bool]


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
    candidate_name: str | None = None
    cleanup_problem: str | None = None
    workspace_cleanup_attempted = False
    workspace_marker_path: str | None = None
    workspace_parent_descriptor: int | None = None
    workspace_descriptor: int | None = None
    output_descriptor: int | None = None
    entries: list[_ArchiveEntry] = []
    try:
        _require_posix_file_protection()
        workspace_marker_path = str(workspace.resolve())
        workspace_parent_descriptor = _open_workspace_parent(workspace)
        workspace_descriptor = _open_directory_at(
            workspace_parent_descriptor,
            workspace.name,
            "owned workstation workspace",
        )
        _require_owned_workspace_at(
            workspace_descriptor, workspace_marker_path
        )
        bundle_root = _validate_lifecycle_paths(workspace, final_path, started_at)
        output_descriptor = _open_output_directory(final_path.parent)
        _require_final_absent_at(output_descriptor, final_path)
        _validated_entries(workspace_descriptor, bundle_root, entries)
        descriptor, candidate_name, candidate = _create_private_candidate(
            output_descriptor, final_path
        )
        with os.fdopen(descriptor, "wb") as candidate_file:
            with tarfile.open(fileobj=candidate_file, mode="w:gz") as archive:
                for archive_name, source, is_directory in entries:
                    _add_validated_entry(
                        archive,
                        workspace_descriptor,
                        archive_name,
                        source,
                        is_directory,
                        started_at,
                    )
                # Ordering is load-bearing: each workspace-backed member is closed
                # before cleanup.  The root descriptor remains only as the anchor
                # that confines deletion to this exact owned workspace.  Cleanup
                # and both anchor closes finish before final metadata because their
                # residue decides complete vs partial.
                assert workspace_parent_descriptor is not None
                assert workspace_marker_path is not None
                cleanup_problem = _cleanup_workspace(
                    workspace,
                    workspace_parent_descriptor,
                    workspace_descriptor,
                    workspace_marker_path,
                )
                workspace_cleanup_attempted = True
                workspace_close_problem = _close_workspace_input(
                    workspace_descriptor
                )
                workspace_descriptor = None
                if workspace_close_problem is not None:
                    raise BundlePublicationError(workspace_close_problem)
                parent_close_problem = _close_workspace_parent(
                    workspace_parent_descriptor
                )
                workspace_parent_descriptor = None
                if parent_close_problem is not None:
                    raise BundlePublicationError(parent_close_problem)
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
        _require_output_directory_unchanged(
            output_descriptor, final_path.parent
        )
        os.link(
            candidate_name,
            final_path.name,
            src_dir_fd=output_descriptor,
            dst_dir_fd=output_descriptor,
            follow_symlinks=False,
        )
        try:
            os.unlink(candidate_name, dir_fd=output_descriptor)
        except OSError as cleanup_error:
            try:
                os.unlink(final_path.name, dir_fd=output_descriptor)
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
        if (
            not workspace_cleanup_attempted
            and workspace_parent_descriptor is not None
            and workspace_descriptor is not None
            and workspace_marker_path is not None
        ):
            _cleanup_workspace(
                workspace,
                workspace_parent_descriptor,
                workspace_descriptor,
                workspace_marker_path,
            )
        if workspace_descriptor is not None:
            _close_workspace_input(workspace_descriptor)
            workspace_descriptor = None
        if workspace_parent_descriptor is not None:
            _close_workspace_parent(workspace_parent_descriptor)
            workspace_parent_descriptor = None
        if (
            candidate is not None
            and candidate_name is not None
            and output_descriptor is not None
        ):
            _remove_candidate_at(output_descriptor, candidate_name, candidate)
        raise
    except Exception as error:
        if not workspace_cleanup_attempted:
            if (
                workspace_parent_descriptor is not None
                and workspace_descriptor is not None
                and workspace_marker_path is not None
            ):
                cleanup_problem = _cleanup_workspace(
                    workspace,
                    workspace_parent_descriptor,
                    workspace_descriptor,
                    workspace_marker_path,
                )
            else:
                cleanup_problem = (
                    "refusing to clean unpinned workstation workspace "
                    f"{workspace}"
                )
            workspace_cleanup_attempted = True
        workspace_close_problem = None
        if workspace_descriptor is not None:
            workspace_close_problem = _close_workspace_input(workspace_descriptor)
            workspace_descriptor = None
        parent_close_problem = None
        if workspace_parent_descriptor is not None:
            parent_close_problem = _close_workspace_parent(
                workspace_parent_descriptor
            )
            workspace_parent_descriptor = None
        candidate_cleanup_problem = None
        if (
            candidate is not None
            and candidate_name is not None
            and output_descriptor is not None
        ):
            candidate_cleanup_problem = _remove_candidate_at(
                output_descriptor, candidate_name, candidate
            )
        message = str(error)
        if workspace_close_problem:
            message = f"{message}; {workspace_close_problem}"
        if parent_close_problem:
            message = f"{message}; {parent_close_problem}"
        if candidate_cleanup_problem:
            message = f"{message}; {candidate_cleanup_problem}"
        if cleanup_problem:
            message = f"{message}; {cleanup_problem}"
        raise BundlePublicationError(message) from error
    finally:
        if output_descriptor is not None:
            # Issue #100 owns the post-link close-failure delivery/residue matrix.
            # Keep #87's existing propagation instead of guessing whether a linked
            # final path should be rolled back or reported as delivered.
            os.close(output_descriptor)


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
    workspace_descriptor: int,
    bundle_root: str,
    entries: list[_ArchiveEntry],
) -> None:
    admitted_descriptor = _open_directory_at(
        workspace_descriptor, "admitted", "admitted state"
    )
    try:
        inventory_descriptor = _open_regular_at(
            admitted_descriptor, "inventory.ini", "admitted Inventory Snapshot"
        )
        try:
            inventory_source = _source_identity(
                ("admitted", "inventory.ini"),
                os.fstat(inventory_descriptor),
            )
        finally:
            os.close(inventory_descriptor)
        evidence_descriptors: list[int] = []
        try:
            contributions_descriptor = _open_directory_at(
                admitted_descriptor, "node-contributions", "node contributions"
            )
            evidence_descriptors.append(contributions_descriptor)
            kubernetes_descriptor = _open_directory_at(
                admitted_descriptor, "kubernetes", "Kubernetes contribution"
            )
            evidence_descriptors.append(kubernetes_descriptor)
            prometheus_descriptor = _open_directory_at(
                admitted_descriptor, "prometheus", "Prometheus contribution"
            )
            evidence_descriptors.append(prometheus_descriptor)
            entries.extend(
                [
                    (bundle_root, None, True),
                    (f"{bundle_root}/inventory.ini", inventory_source, False),
                    (f"{bundle_root}/nodes", None, True),
                    (f"{bundle_root}/ceph", None, True),
                    (
                        f"{bundle_root}/kubernetes",
                        _source_identity(
                            ("admitted", "kubernetes"),
                            os.fstat(kubernetes_descriptor),
                        ),
                        True,
                    ),
                    (
                        f"{bundle_root}/prometheus",
                        _source_identity(
                            ("admitted", "prometheus"),
                            os.fstat(prometheus_descriptor),
                        ),
                        True,
                    ),
                ]
            )
            _append_node_contributions(
                entries,
                contributions_descriptor,
                ("admitted", "node-contributions"),
                bundle_root,
            )
            _append_children_at(
                entries,
                kubernetes_descriptor,
                ("admitted", "kubernetes"),
                f"{bundle_root}/kubernetes",
                "Kubernetes contribution",
            )
            _append_children_at(
                entries,
                prometheus_descriptor,
                ("admitted", "prometheus"),
                f"{bundle_root}/prometheus",
                "Prometheus contribution",
            )
        finally:
            for descriptor in reversed(evidence_descriptors):
                os.close(descriptor)
    finally:
        os.close(admitted_descriptor)
    _validate_archive_paths(entries)


def _append_node_contributions(
    entries: list[_ArchiveEntry],
    contributions_descriptor: int,
    contributions_components: tuple[str, ...],
    bundle_root: str,
) -> None:
    ceph_seen = False
    for contribution_name, contribution_facts in _list_entries(
        contributions_descriptor, "node contributions"
    ):
        _require_safe_component(contribution_name)
        contribution_descriptor = _open_directory_at(
            contributions_descriptor,
            contribution_name,
            f"admitted node contribution {contribution_name}",
            observed=_file_identity(contribution_facts),
        )
        contribution_components = contributions_components + (contribution_name,)
        try:
            children = dict(
                _list_entries(
                    contribution_descriptor,
                    f"admitted node contribution {contribution_name}",
                )
            )
            if set(children) - {"node", "ceph"}:
                raise BundlePublicationError(
                    f"unknown admitted member in contribution {contribution_name}"
                )
            node_facts = children.get("node")
            if node_facts is None:
                raise BundlePublicationError(
                    f"admitted contribution lacks node/: {contribution_name}"
                )
            node_descriptor = _open_directory_at(
                contribution_descriptor,
                "node",
                f"admitted node evidence {contribution_name}",
                observed=_file_identity(node_facts),
            )
            try:
                node_archive_name = f"{bundle_root}/nodes/{contribution_name}"
                node_components = contribution_components + ("node",)
                entries.append(
                    (
                        node_archive_name,
                        _source_identity(node_components, os.fstat(node_descriptor)),
                        True,
                    )
                )
                node_children = _append_children_at(
                    entries,
                    node_descriptor,
                    node_components,
                    node_archive_name,
                    f"admitted node evidence {contribution_name}",
                )
            finally:
                os.close(node_descriptor)
            for required in ("probes", "files"):
                if not node_children.get(required, False):
                    raise BundlePublicationError(
                        f"admitted node evidence lacks {required}/: {contribution_name}"
                    )

            ceph_facts = children.get("ceph")
            if ceph_facts is not None:
                if ceph_seen:
                    raise BundlePublicationError(
                        "more than one admitted Ceph contribution"
                    )
                ceph_seen = True
                ceph_descriptor = _open_directory_at(
                    contribution_descriptor,
                    "ceph",
                    f"admitted Ceph evidence {contribution_name}",
                    observed=_file_identity(ceph_facts),
                )
                try:
                    ceph_children = _append_children_at(
                        entries,
                        ceph_descriptor,
                        contribution_components + ("ceph",),
                        f"{bundle_root}/ceph",
                        f"admitted Ceph evidence {contribution_name}",
                    )
                finally:
                    os.close(ceph_descriptor)
                if not ceph_children.get("probes", False):
                    raise BundlePublicationError(
                        f"admitted Ceph evidence lacks probes/: {contribution_name}"
                    )
        finally:
            os.close(contribution_descriptor)


def _append_children_at(
    entries: list[_ArchiveEntry],
    directory_descriptor: int,
    directory_components: tuple[str, ...],
    archive_name: str,
    label: str,
) -> dict[str, bool]:
    child_types: dict[str, bool] = {}
    for child_name, child_facts in _list_entries(directory_descriptor, label):
        _require_safe_component(child_name)
        child_archive_name = f"{archive_name}/{child_name}"
        child_components = directory_components + (child_name,)
        if stat.S_ISDIR(child_facts.st_mode):
            child_descriptor = _open_directory_at(
                directory_descriptor,
                child_name,
                f"{label}/{child_name}",
                observed=_file_identity(child_facts),
            )
            entries.append(
                (
                    child_archive_name,
                    _source_identity(child_components, child_facts),
                    True,
                )
            )
            try:
                _append_children_at(
                    entries,
                    child_descriptor,
                    child_components,
                    child_archive_name,
                    f"{label}/{child_name}",
                )
            finally:
                os.close(child_descriptor)
            child_types[child_name] = True
        elif stat.S_ISREG(child_facts.st_mode):
            child_descriptor = _open_regular_at(
                directory_descriptor,
                child_name,
                f"{label}/{child_name}",
                observed=_file_identity(child_facts),
            )
            os.close(child_descriptor)
            entries.append(
                (
                    child_archive_name,
                    _source_identity(child_components, child_facts),
                    False,
                )
            )
            child_types[child_name] = False
        else:
            raise BundlePublicationError(
                f"inadmissible final-tree object: {label}/{child_name}"
            )
    return child_types


def _validate_archive_paths(entries: list[_ArchiveEntry]) -> None:
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


def _require_posix_file_protection() -> None:
    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.link not in os.supports_dir_fd
        or os.link not in os.supports_follow_symlinks
        or os.unlink not in os.supports_dir_fd
        or os.rmdir not in os.supports_dir_fd
        or os.listdir not in os.supports_fd
    ):
        raise BundlePublicationError(
            "safe bundle publication requires POSIX no-follow directory access"
        )


def _open_output_directory(output_directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(output_directory, flags)
    except OSError as error:
        raise BundlePublicationError(
            f"cannot pin final destination parent without following links: {error}"
        ) from error
    try:
        facts = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise BundlePublicationError(
            f"cannot inspect pinned final destination parent: {error}"
        ) from error
    if not stat.S_ISDIR(facts.st_mode):
        os.close(descriptor)
        raise BundlePublicationError(
            "final destination parent must be an ordinary directory"
        )
    return descriptor


def _require_final_absent_at(
    output_descriptor: int, final_path: Path
) -> None:
    _require_safe_component(final_path.name)
    try:
        os.stat(
            final_path.name,
            dir_fd=output_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise BundlePublicationError(
            f"cannot inspect final destination {final_path}: {error}"
        ) from error
    raise BundlePublicationError(f"final destination already exists: {final_path}")


def _require_output_directory_unchanged(
    output_descriptor: int, output_directory: Path
) -> None:
    try:
        path_facts = os.stat(output_directory, follow_symlinks=False)
        opened_facts = os.fstat(output_descriptor)
    except OSError as error:
        raise BundlePublicationError(
            f"cannot confirm final destination parent before publication: {error}"
        ) from error
    if (
        not stat.S_ISDIR(path_facts.st_mode)
        or _file_identity(path_facts) != _file_identity(opened_facts)
    ):
        raise BundlePublicationError(
            "final destination parent changed during bundle publication"
        )


def _create_private_candidate(
    output_descriptor: int, final_path: Path
) -> tuple[int, str, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(100):
        candidate_name = (
            f".{final_path.name}.candidate.{secrets.token_hex(16)}"
        )
        try:
            descriptor = os.open(
                candidate_name,
                flags,
                0o600,
                dir_fd=output_descriptor,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise BundlePublicationError(
                f"cannot create private Incident Bundle candidate: {error}"
            ) from error
        return descriptor, candidate_name, final_path.parent / candidate_name
    raise BundlePublicationError(
        "cannot choose a unique private Incident Bundle candidate name"
    )


def _open_workspace_parent(workspace: Path) -> int:
    _require_safe_component(workspace.name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(workspace.parent, flags)
    except OSError as error:
        raise BundlePublicationError(
            "cannot pin workstation workspace parent without following links: "
            f"{error}"
        ) from error
    try:
        facts = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise BundlePublicationError(
            f"cannot inspect pinned workstation workspace parent: {error}"
        ) from error
    if not stat.S_ISDIR(facts.st_mode):
        os.close(descriptor)
        raise BundlePublicationError(
            "workstation workspace parent must be an ordinary directory"
        )
    return descriptor


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    label: str,
    *,
    observed: tuple[int, int] | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise BundlePublicationError(
            f"cannot open {label} without following links: {error}"
        ) from error
    try:
        facts = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise BundlePublicationError(
            f"cannot inspect opened {label}: {error}"
        ) from error
    if not stat.S_ISDIR(facts.st_mode) or (
        observed is not None
        and _file_identity(facts) != observed
    ):
        os.close(descriptor)
        raise BundlePublicationError(f"{label} changed during bundle validation")
    return descriptor


def _open_regular_at(
    parent_descriptor: int,
    name: str,
    label: str,
    *,
    observed: tuple[int, int] | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise BundlePublicationError(
            f"cannot open {label} without following links: {error}"
        ) from error
    try:
        facts = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise BundlePublicationError(
            f"cannot inspect opened {label}: {error}"
        ) from error
    if not stat.S_ISREG(facts.st_mode) or (
        observed is not None
        and _file_identity(facts) != observed
    ):
        os.close(descriptor)
        raise BundlePublicationError(f"{label} changed during bundle validation")
    return descriptor


def _file_identity(facts: os.stat_result) -> tuple[int, int]:
    return facts.st_dev, facts.st_ino


def _require_absent_at(
    parent_descriptor: int, name: str, label: str
) -> None:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BundlePublicationError(
            f"cannot confirm removal of {label}: {error}"
        ) from error
    raise BundlePublicationError(f"cannot confirm removal of {label}")


def _source_identity(
    components: tuple[str, ...], facts: os.stat_result
) -> _SourceIdentity:
    device, inode = _file_identity(facts)
    return components, device, inode


def _list_entries(
    directory_descriptor: int, label: str
) -> list[tuple[str, os.stat_result]]:
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as error:
        raise BundlePublicationError(f"cannot list {label}: {error}") from error
    entries: list[tuple[str, os.stat_result]] = []
    for name in names:
        _require_safe_component(name)
        try:
            facts = os.stat(
                name, dir_fd=directory_descriptor, follow_symlinks=False
            )
        except OSError as error:
            raise BundlePublicationError(
                f"cannot inspect {label}/{name}: {error}"
            ) from error
        entries.append((name, facts))
    return entries


def _require_owned_workspace_at(
    workspace_descriptor: int, expected_path: str
) -> None:
    marker_descriptor = _open_regular_at(
        workspace_descriptor, OWNERSHIP_MARKER, "workspace ownership marker"
    )
    expected = (expected_path + "\n").encode("utf-8")
    try:
        with os.fdopen(marker_descriptor, "rb") as marker:
            recorded = marker.read(len(expected) + 1)
    except OSError as error:
        raise BundlePublicationError(
            f"cannot read workspace ownership marker: {error}"
        ) from error
    if recorded != expected:
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


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise BundlePublicationError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISDIR(mode):
        raise BundlePublicationError(f"{label} must be an ordinary directory")


def _add_validated_entry(
    archive: tarfile.TarFile,
    workspace_descriptor: int,
    archive_name: str,
    source: _SourceIdentity | None,
    is_directory: bool,
    started_at: datetime,
) -> None:
    if source is None:
        if not is_directory:
            raise BundlePublicationError(
                f"regular final-tree member lacks an admitted source: {archive_name}"
            )
        _add_directory(archive, archive_name, started_at)
        return

    source_descriptor = _reopen_admitted_source(
        workspace_descriptor, source, is_directory=is_directory
    )
    try:
        if is_directory:
            _add_directory(archive, archive_name, started_at)
        else:
            _add_regular_file(
                archive, archive_name, source_descriptor, started_at
            )
    finally:
        os.close(source_descriptor)


def _reopen_admitted_source(
    workspace_descriptor: int,
    source: _SourceIdentity,
    *,
    is_directory: bool,
) -> int:
    components, expected_device, expected_inode = source
    if not components:
        raise BundlePublicationError("admitted source path has no components")
    for component in components:
        _require_safe_component(component)

    parent_descriptor = workspace_descriptor
    try:
        for component in components[:-1]:
            next_descriptor = _open_directory_at(
                parent_descriptor,
                component,
                f"admitted source {'/'.join(components)}",
            )
            if parent_descriptor != workspace_descriptor:
                os.close(parent_descriptor)
            parent_descriptor = next_descriptor

        final_component = components[-1]
        expected_identity = expected_device, expected_inode
        if is_directory:
            return _open_directory_at(
                parent_descriptor,
                final_component,
                f"admitted source {'/'.join(components)}",
                observed=expected_identity,
            )
        return _open_regular_at(
            parent_descriptor,
            final_component,
            f"admitted source {'/'.join(components)}",
            observed=expected_identity,
        )
    finally:
        if parent_descriptor != workspace_descriptor:
            os.close(parent_descriptor)


def _add_directory(archive: tarfile.TarFile, name: str, started_at: datetime) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o700
    member.mtime = int(started_at.timestamp())
    archive.addfile(member)


def _add_regular_file(
    archive: tarfile.TarFile,
    name: str,
    source_descriptor: int,
    started_at: datetime,
) -> None:
    with os.fdopen(os.dup(source_descriptor), "rb") as contents:
        facts = os.fstat(contents.fileno())
        if not stat.S_ISREG(facts.st_mode):
            raise BundlePublicationError(
                f"final-tree source changed type while writing {name}"
            )
        member = tarfile.TarInfo(name)
        member.size = facts.st_size
        member.mode = 0o600
        member.mtime = int(started_at.timestamp())
        archive.addfile(member, contents)


def _close_workspace_input(descriptor: int) -> str | None:
    try:
        os.close(descriptor)
    except OSError as error:
        return f"cannot close admitted workspace input: {error}"
    return None


def _close_workspace_parent(descriptor: int) -> str | None:
    try:
        os.close(descriptor)
    except OSError as error:
        return f"cannot close pinned workstation workspace parent: {error}"
    return None


def _add_bytes(
    archive: tarfile.TarFile, name: str, contents: bytes, started_at: datetime
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mode = 0o600
    member.mtime = int(started_at.timestamp())
    archive.addfile(member, io.BytesIO(contents))


def _cleanup_workspace(
    workspace: Path,
    workspace_parent_descriptor: int,
    workspace_descriptor: int,
    expected_marker_path: str,
) -> str | None:
    """Remove only the exact pinned workspace after publication handoff.

    The top-level flow deliberately keeps its pre-handoff cleanup local because it
    still owns failures that happen before publication starts.  Issue #87 has one
    collector owner for this private tree and no concurrent workspace writer.  A
    complete same-uid interleaving and cancellation matrix belongs to issue #100.
    """
    try:
        _require_owned_workspace_at(workspace_descriptor, expected_marker_path)
        _remove_directory_contents_at(
            workspace_descriptor, "owned workstation workspace"
        )
        opened_facts = os.fstat(workspace_descriptor)
        current_facts = os.stat(
            workspace.name,
            dir_fd=workspace_parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current_facts.st_mode)
            or _file_identity(current_facts) != _file_identity(opened_facts)
        ):
            return (
                "refusing to clean replaced workstation workspace "
                f"{workspace}"
            )
        os.rmdir(workspace.name, dir_fd=workspace_parent_descriptor)
        _require_absent_at(
            workspace_parent_descriptor,
            workspace.name,
            f"exact workstation workspace {workspace}",
        )
    except (BundlePublicationError, OSError) as error:
        return f"cannot remove workstation workspace {workspace}: {error}"
    return None


def _remove_directory_contents_at(
    directory_descriptor: int, label: str
) -> None:
    for name, observed_facts in _list_entries(directory_descriptor, label):
        child_label = f"{label}/{name}"
        observed_identity = _file_identity(observed_facts)
        if stat.S_ISDIR(observed_facts.st_mode):
            child_descriptor = _open_directory_at(
                directory_descriptor,
                name,
                child_label,
                observed=observed_identity,
            )
            try:
                _remove_directory_contents_at(child_descriptor, child_label)
                current_facts = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(current_facts.st_mode)
                    or _file_identity(current_facts) != _file_identity(
                        os.fstat(child_descriptor)
                    )
                ):
                    raise BundlePublicationError(
                        f"refusing to remove replaced workspace directory {child_label}"
                    )
                os.rmdir(name, dir_fd=directory_descriptor)
                _require_absent_at(directory_descriptor, name, child_label)
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(observed_facts.st_mode):
            child_descriptor = _open_regular_at(
                directory_descriptor,
                name,
                child_label,
                observed=observed_identity,
            )
            try:
                os.unlink(name, dir_fd=directory_descriptor)
            finally:
                os.close(child_descriptor)
        else:
            raise BundlePublicationError(
                f"refusing to remove inadmissible workspace object {child_label}"
            )


def _remove_candidate_at(
    output_descriptor: int, candidate_name: str, candidate_path: Path
) -> str | None:
    try:
        os.unlink(candidate_name, dir_fd=output_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        return (
            "cannot remove private Incident Bundle candidate "
            f"{candidate_path}: {error}"
        )
    return None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
