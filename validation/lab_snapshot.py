"""The stable-state snapshot: proof that collecting evidence changed nothing.

Qualification takes one snapshot before the live Python collect and one after it,
and requires them to be identical. That comparison is only meaningful if
the snapshot holds *stable* state, so this module never compares a whole status
dump: each source is reduced by an explicit projection that keeps identity and
desired configuration and drops everything a healthy idle cluster changes on its
own — epochs, counters, uptimes, timestamps, health history, request statistics
and observed up/down status.

Whitelisting is the point.  A projection lists the fields it keeps, so a future
Ceph release that adds a new volatile field cannot quietly start failing the gate,
and — more importantly — a genuine desired-state mutation cannot hide inside a
field nobody enumerated.  `docs/lab-validation-runbook.md` is the reviewed
statement of this schema; widening or narrowing it needs the same review as
changing collector behaviour.

Every command here is a read: direct read-only `ceph` on the seed host and local
`kubectl get`.  Nothing in the snapshot path may write.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from validation.lab_contract import describe_differences
from validation.lab_probe import ROOK_CLUSTER_RESOURCE, LabProber
from validation.lab_profile import LabProfile


SNAPSHOT_SCHEMA_VERSION = 1
RESULT_UNCHANGED = "unchanged"
RESULT_CHANGED = "changed"
WORKLOAD_RESOURCES = ("deployments", "statefulsets", "daemonsets")


class SnapshotUnavailable(Exception):
    """A stable-state source could not be read, so no snapshot exists."""


@dataclass(frozen=True)
class StableStateSnapshot:
    """One reduced view of the lab's stable identity and desired configuration."""

    schema_version: int
    fields: dict[str, object]

    def document(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "fields": self.fields}


def capture_stable_state(
    prober: LabProber, profile: LabProfile, known_hosts: Path
) -> StableStateSnapshot:
    """Read every stable-state source and reduce it, or fail closed.

    A source that cannot be read is not "no change": it is a snapshot that cannot
    support the claim the gate makes, so it raises instead of yielding a partial
    document that would compare equal to another partial document.
    """

    fields: dict[str, object] = {}
    for name, subcommand, project in _ceph_sources():
        outcome = prober.run_ceph(known_hosts, subcommand)
        if not outcome.ok or outcome.value is None:
            raise SnapshotUnavailable(f"{name}: {outcome.detail}")
        fields[name] = project(_parse(name, outcome.value))
    for name, namespace, resource, project in _kube_sources(profile):
        outcome = prober.kubectl_get(namespace, resource)
        if not outcome.ok or outcome.value is None:
            raise SnapshotUnavailable(f"{name}: {outcome.detail}")
        fields[name] = project(_parse(name, outcome.value))
    return StableStateSnapshot(SNAPSHOT_SCHEMA_VERSION, fields)


def compare_snapshots(
    before: StableStateSnapshot, after: StableStateSnapshot
) -> tuple[str, str, tuple[str, ...]]:
    """Compare two snapshots and name every stable field that moved."""

    differences = describe_differences(before.document(), after.document())
    result = RESULT_UNCHANGED if not differences else RESULT_CHANGED
    return result, str(before.schema_version), differences


def _ceph_sources() -> tuple[tuple[str, str, Callable[[object], object]], ...]:
    return (
        ("ceph_monitors", "mon dump --format json", project_monitors),
        ("ceph_crush_topology", "osd tree --format json", project_crush_topology),
        ("ceph_pools", "osd pool ls detail --format json", project_pools),
        ("ceph_config", "config dump --format json", project_config),
    )


def _kube_sources(
    profile: LabProfile,
) -> tuple[tuple[str, str, str, Callable[[object], object]], ...]:
    """The Kubernetes sources, once per namespace the profile names.

    The operator namespace is usually the cluster namespace; reading it twice
    under two field names would report a difference that is really one object, so
    it is only added when the profile actually separates them.
    """

    namespaces = [profile.rook_namespace]
    if profile.rook_operator_namespace != profile.rook_namespace:
        namespaces.append(profile.rook_operator_namespace)
    sources: list[tuple[str, str, str, Callable[[object], object]]] = [
        (
            "rook_cephclusters",
            profile.rook_namespace,
            ROOK_CLUSTER_RESOURCE,
            project_cephclusters,
        )
    ]
    for namespace in namespaces:
        for resource in WORKLOAD_RESOURCES:
            sources.append(
                (f"k8s_{resource}_{namespace}", namespace, resource, project_workloads)
            )
    return tuple(sources)


def _parse(name: str, text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as error:
        raise SnapshotUnavailable(f"{name}: unparseable JSON ({error})") from error


def project_monitors(document: object) -> object:
    """Monitor identity: which monitors exist, at which rank and address.

    `epoch`, `modified`, `created`, `election_epoch`, `quorum` and the feature
    bitmaps are dropped: a quorum re-election is normal cluster life, not a
    configuration change collection could have caused.
    """

    table = _table(document)
    return {
        "fsid": _text(table.get("fsid")),
        "monitors": sorted(
            (
                {
                    "name": _text(entry.get("name")),
                    "rank": entry.get("rank"),
                    "public_addr": _text(entry.get("public_addr")),
                }
                for entry in _entries(table.get("mons"))
            ),
            key=_sort_key,
        ),
    }


def project_crush_topology(document: object) -> object:
    """CRUSH placement: the tree's shape and weights, not any node's health.

    `status`, `reweight` and `exists` are dropped because an OSD going down is an
    operational event; its position and CRUSH weight are configuration.
    """

    table = _table(document)
    return sorted(
        (
            {
                "id": entry.get("id"),
                "name": _text(entry.get("name")),
                "type": _text(entry.get("type")),
                "device_class": _text(entry.get("device_class")),
                "crush_weight": entry.get("crush_weight"),
                "children": sorted(
                    child for child in _entries_of(entry.get("children")) if _scalar(child)
                ),
            }
            for entry in _entries(table.get("nodes"))
        ),
        key=_sort_key,
    )


def project_pools(document: object) -> object:
    """Pool configuration: identity, redundancy and placement rule.

    `last_change`, `last_force_op_resend*`, the epoch fields and every usage
    statistic are dropped: they move whenever the cluster serves I/O.
    """

    return sorted(
        (
            {
                "pool_id": entry.get("pool_id"),
                "pool_name": _text(entry.get("pool_name")),
                "type": entry.get("type"),
                "size": entry.get("size"),
                "min_size": entry.get("min_size"),
                "pg_num": entry.get("pg_num"),
                "pg_num_target": entry.get("pg_num_target"),
                "crush_rule": entry.get("crush_rule"),
                "erasure_code_profile": _text(entry.get("erasure_code_profile")),
                "applications": sorted(_table(entry.get("application_metadata"))),
            }
            for entry in _entries(document)
        ),
        key=_sort_key,
    )


def project_config(document: object) -> object:
    """Persistent configuration: which option is set where, and to what.

    This is the field the gate cares about most — a collector that persisted a
    setting would show up here — so the value is kept literally.
    """

    return sorted(
        (
            {
                "section": _text(entry.get("section")),
                "name": _text(entry.get("name")),
                "value": _text(entry.get("value")),
            }
            for entry in _entries(document)
        ),
        key=_sort_key,
    )


def project_cephclusters(document: object) -> object:
    """The Rook CephCluster desired state: `spec`, never `status`.

    `spec` is kept whole rather than field by field: it *is* the desired state a
    read-only collector must not touch, and enumerating its keys would let a new
    Rook field carry a mutation past the gate.
    """

    table = _table(document)
    return sorted(
        (
            {
                "name": _text(_table(entry.get("metadata")).get("name")),
                "namespace": _text(_table(entry.get("metadata")).get("namespace")),
                "spec": entry.get("spec"),
            }
            for entry in _entries(table.get("items"))
        ),
        key=_sort_key,
    )


def project_workloads(document: object) -> object:
    """Workload desired state: name, replica count and container images.

    `status`, `metadata.resourceVersion`, `metadata.generation`, annotations and
    creation timestamps are dropped — they change as pods are rescheduled, which
    a cluster does without being asked.
    """

    table = _table(document)
    return sorted(
        (
            {
                "kind": _text(entry.get("kind")),
                "name": _text(_table(entry.get("metadata")).get("name")),
                "namespace": _text(_table(entry.get("metadata")).get("namespace")),
                "replicas": _table(entry.get("spec")).get("replicas"),
                "images": sorted(_images(entry)),
            }
            for entry in _entries(table.get("items"))
        ),
        key=_sort_key,
    )


def _images(entry: Mapping[str, object]) -> list[str]:
    pod_spec = _table(_table(_table(entry.get("spec")).get("template")).get("spec"))
    images: list[str] = []
    for key in ("initContainers", "containers"):
        for container in _entries(pod_spec.get(key)):
            image = _text(container.get("image"))
            if image:
                images.append(image)
    return images


def _table(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _entries(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _entries_of(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(value)


def _scalar(value: object) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sort_key(entry: object) -> str:
    """Order projected entries deterministically.

    Ceph and Kubernetes both return unordered collections, so the snapshot sorts
    by the entry's own canonical form: two captures that list the same objects in a
    different order are the same stable state, and the gate must not call that a
    change.
    """

    return json.dumps(entry, sort_keys=True, default=str)
