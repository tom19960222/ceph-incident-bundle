"""Draft and validate the Node Inventory."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


_INVENTORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z", re.ASCII)
_SECTIONS = {"common", "nodes", "ceph", "kubernetes", "prometheus"}
_FIXED_KEYS = {
    "common": {"ssh_user", "probe_timeout", "ssh_connect_timeout"},
    "ceph": {"source"},
    "kubernetes": {"context", "consumer_namespace", "operator_namespace"},
    "prometheus": {"url", "metrics_filter_regex", "query_step", "request_timeout"},
}
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class InventoryRejected(Exception):
    """The Inventory could not be read or did not completely validate."""

    def __init__(self, problems: list[str] | tuple[str, ...]):
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class TargetNode:
    inventory_name: str
    ssh_address: str


@dataclass(frozen=True)
class Inventory:
    snapshot: bytes
    nodes: tuple[TargetNode, ...]
    ssh_user: str
    probe_timeout_seconds: int
    ssh_connect_timeout_seconds: int
    ceph_source: str | None
    kubernetes_context: str | None
    consumer_namespace: str
    operator_namespace: str
    prometheus_url: str | None
    metrics_filter_regex: str
    query_step: str
    request_timeout_seconds: int


def draft_inventory(hosts_path: Path) -> tuple[bytes, tuple[str, ...]]:
    """Return a reviewable Node Inventory draft and collision problems."""
    hostnames: list[str] = []
    seen_hostnames: set[str] = set()

    path = Path(hosts_path)
    try:
        hosts_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise InventoryRejected([f"cannot read hosts file {path}: {error}"]) from error

    for raw_line in hosts_text.splitlines():
        fields = raw_line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError:
            continue
        hostname = fields[1]
        if (
            address.is_loopback
            or _is_conventional_local_name(hostname)
            or not _is_hostname(hostname)
        ):
            continue
        if hostname in seen_hostnames:
            continue
        seen_hostnames.add(hostname)
        hostnames.append(hostname)

    entries = [(hostname.split(".", 1)[0], hostname) for hostname in hostnames]
    entries_by_portable_name: dict[str, list[tuple[str, str]]] = {}
    for name, hostname in entries:
        portable_name = _portable_name(name)
        entries_by_portable_name.setdefault(portable_name, []).append((name, hostname))

    colliding_portable_names = {
        name
        for name, grouped_entries in entries_by_portable_name.items()
        if len(grouped_entries) > 1
    }
    node_lines: list[str] = []
    for name, hostname in entries:
        portable_name = _portable_name(name)
        if portable_name in colliding_portable_names:
            node_lines.append(
                f'# ACTION REQUIRED: rename colliding Inventory Name "{name}"'
            )
        node_lines.append(f"{name} = {hostname}")

    problems: list[str] = []
    for grouped_entries in entries_by_portable_name.values():
        if len(grouped_entries) < 2:
            continue
        for first_index, (first_name, first_hostname) in enumerate(grouped_entries):
            for name, hostname in grouped_entries[first_index + 1 :]:
                problems.append(
                    f"Inventory Name collision: '{first_name}' ({first_hostname}) "
                    f"conflicts with '{name}' ({hostname})"
                )
    ceph_source = next(
        (
            hostname.split(".", 1)[0]
            for hostname in hostnames
            if any(marker in hostname.casefold() for marker in ("mon", "cp", "cm"))
        ),
        None,
    )

    lines = [
        "[common]",
        "ssh_user = root",
        "probe_timeout = 30m",
        "ssh_connect_timeout = 15s",
        "",
        "[nodes]",
        *node_lines,
        "",
        "[ceph]",
        f"source = {ceph_source}" if ceph_source else "# source =",
        "",
        "[kubernetes]",
        "# context =",
        "consumer_namespace = rook-ceph-external",
        "operator_namespace = rook-ceph",
        "",
        "[prometheus]",
        "# url =",
        "metrics_filter_regex =",
        "query_step = 15s",
        "request_timeout = 5m",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8"), tuple(problems)


def load_inventory(inventory_path: Path) -> Inventory:
    """Read and completely validate one Node Inventory."""
    path = Path(inventory_path)
    try:
        snapshot = path.read_bytes()
    except OSError as error:
        raise InventoryRejected([f"cannot read Inventory {path}: {error}"]) from error
    try:
        text = snapshot.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InventoryRejected([f"Inventory is not valid UTF-8: {error}"]) from error

    sections, problems = _parse_inventory(text)
    values: dict[str, dict[str, str]] = {}
    for section_name, entries in sections.items():
        values[section_name] = {}
        seen_keys: set[str] = set()
        for key, value, line_number in entries:
            if key in seen_keys:
                problems.append(
                    f"line {line_number}: duplicate key '{key}' in [{section_name}]"
                )
            else:
                seen_keys.add(key)
                values[section_name][key] = value
            if section_name != "nodes" and section_name in _FIXED_KEYS:
                if key not in _FIXED_KEYS[section_name]:
                    problems.append(
                        f"line {line_number}: unknown key '{key}' in [{section_name}]"
                    )

    common = values.get("common", {})
    if "common" not in sections:
        problems.append("missing required section [common]")
    ssh_user = common.get("ssh_user", "")
    if "ssh_user" not in common:
        problems.append("[common] is missing required key 'ssh_user'")
    elif ssh_user != "root":
        problems.append("ssh_user must be exactly 'root'")

    probe_timeout = common.get("probe_timeout", "30m")
    probe_timeout_seconds = _duration_seconds(
        "probe_timeout", probe_timeout, "mhdw", allow_zero=True, problems=problems
    )
    ssh_connect_timeout = common.get("ssh_connect_timeout", "15s")
    ssh_connect_timeout_seconds = _duration_seconds(
        "ssh_connect_timeout",
        ssh_connect_timeout,
        "smhdw",
        allow_zero=True,
        problems=problems,
    )

    node_entries = sections.get("nodes", [])
    if "nodes" not in sections:
        problems.append("missing required section [nodes]")
    elif not node_entries:
        problems.append("[nodes] must contain at least one Target Node")

    nodes: list[TargetNode] = []
    names_by_portable_key: dict[str, list[str]] = {}
    for name, address, line_number in node_entries:
        if not _INVENTORY_NAME.fullmatch(name):
            problems.append(f"line {line_number}: invalid Inventory Name '{name}'")
        if not _is_ssh_address(address):
            problems.append(f"line {line_number}: invalid SSH Address '{address}'")
        portable_name = _portable_name(name)
        names_by_portable_key.setdefault(portable_name, []).append(name)
        nodes.append(TargetNode(name, address))
    for grouped_names in names_by_portable_key.values():
        for first_index, first_name in enumerate(grouped_names):
            for name in grouped_names[first_index + 1 :]:
                problems.append(
                    f"Inventory Name collision: '{first_name}' conflicts with '{name}'"
                )

    ceph = values.get("ceph", {})
    ceph_source = ceph.get("source")
    if ceph_source is not None:
        if not ceph_source:
            problems.append("[ceph] source must be nonempty when present")
        elif ceph_source not in {node.inventory_name for node in nodes}:
            problems.append(
                f"Ceph source '{ceph_source}' does not reference a Target Node"
            )

    kubernetes = values.get("kubernetes", {})
    kubernetes_context = kubernetes.get("context")
    if kubernetes_context is not None and not _is_nonempty_plain_value(kubernetes_context):
        problems.append("[kubernetes] context must be nonempty when present")
    consumer_namespace = kubernetes.get("consumer_namespace", "rook-ceph-external")
    operator_namespace = kubernetes.get("operator_namespace", "rook-ceph")
    for key, namespace in (
        ("consumer_namespace", consumer_namespace),
        ("operator_namespace", operator_namespace),
    ):
        if not _is_nonempty_plain_value(namespace):
            problems.append(f"invalid {key} '{namespace}'")

    prometheus = values.get("prometheus", {})
    prometheus_url = prometheus.get("url")
    if prometheus_url is not None and not _is_prometheus_url(prometheus_url):
        problems.append(f"invalid Prometheus URL '{prometheus_url}'")
    metrics_filter_regex = prometheus.get("metrics_filter_regex", "")
    try:
        re.compile(metrics_filter_regex)
    except re.error as error:
        problems.append(f"invalid metrics_filter_regex: {error}")
    query_step = prometheus.get("query_step", "15s")
    _duration_seconds(
        "query_step", query_step, "smhdw", allow_zero=False, problems=problems
    )
    request_timeout = prometheus.get("request_timeout", "5m")
    request_timeout_seconds = _duration_seconds(
        "request_timeout",
        request_timeout,
        "smhdw",
        allow_zero=True,
        problems=problems,
    )

    if problems:
        raise InventoryRejected(problems)
    return Inventory(
        snapshot=snapshot,
        nodes=tuple(nodes),
        ssh_user=ssh_user,
        probe_timeout_seconds=probe_timeout_seconds,
        ssh_connect_timeout_seconds=ssh_connect_timeout_seconds,
        ceph_source=ceph_source,
        kubernetes_context=kubernetes_context,
        consumer_namespace=consumer_namespace,
        operator_namespace=operator_namespace,
        prometheus_url=prometheus_url,
        metrics_filter_regex=metrics_filter_regex,
        query_step=query_step,
        request_timeout_seconds=request_timeout_seconds,
    )


def _parse_inventory(
    text: str,
) -> tuple[dict[str, list[tuple[str, str, int]]], list[str]]:
    sections: dict[str, list[tuple[str, str, int]]] = {}
    problems: list[str] = []
    current_section: str | None = None
    seen_sections: set[str] = set()

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        section_match = re.fullmatch(r"\[([^]]+)]", line)
        if section_match:
            current_section = section_match.group(1)
            if current_section in seen_sections:
                problems.append(
                    f"line {line_number}: duplicate section [{current_section}]"
                )
            else:
                seen_sections.add(current_section)
            sections.setdefault(current_section, [])
            if current_section not in _SECTIONS:
                problems.append(f"line {line_number}: unknown section [{current_section}]")
            continue
        if current_section is None:
            problems.append(f"line {line_number}: entry appears before any section")
            continue
        if "=" not in raw_line:
            problems.append(f"line {line_number}: expected 'key = value'")
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            problems.append(f"line {line_number}: empty key in [{current_section}]")
            continue
        sections[current_section].append((key, value, line_number))
    return sections, problems


def _duration_seconds(
    key: str,
    value: str,
    units: str,
    *,
    allow_zero: bool,
    problems: list[str],
) -> int:
    if allow_zero and value == "0":
        return 0
    match = re.fullmatch(r"([1-9][0-9]*)([a-z])", value, re.ASCII)
    if match is None or match.group(2) not in units:
        problems.append(f"invalid {key} '{value}'")
        return 0
    return int(match.group(1)) * _SECONDS_PER_UNIT[match.group(2)]


def _is_ssh_address(value: str) -> bool:
    if not value or "@" in value or any(character.isspace() for character in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return _is_hostname(value)


def _is_hostname(value: str) -> bool:
    hostname = value[:-1] if value.endswith(".") else value
    if not hostname or len(value) > 253 or ":" in hostname:
        return False
    return all(
        len(label) <= 63 and _HOST_LABEL.fullmatch(label) is not None
        for label in hostname.split(".")
    )


def _is_conventional_local_name(hostname: str) -> bool:
    first_label = hostname.rstrip(".").split(".", 1)[0].casefold()
    return first_label in {
        "localhost",
        "localhost4",
        "localhost6",
        "loopback",
        "ip6-loopback",
    }


def _portable_name(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name)
    return unicodedata.normalize("NFC", normalized.casefold())


def _is_nonempty_plain_value(value: str) -> bool:
    return bool(value) and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _is_prometheus_url(value: str) -> bool:
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.query == ""
        and parsed.fragment == ""
    )
