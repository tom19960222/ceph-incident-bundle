"""The TOML Lab Profile: the only machine-readable connection source.

A Lab Profile records how to reach one lab, which cluster identity that lab is
expected to prove, and where its credentials live.  It never records credential
*contents*, and `CEPH-LAB-CONNECTION.md` is never parsed — see
`docs/read-only-safety.md` and `docs/lab-validation-runbook.md`.

Loading is fail-closed.  An unknown table or key is an error rather than
something tolerated, because a typo in an identity field would otherwise silently
disable the check it was meant to enable.  A profile in `active` or `candidate`
state must carry complete lab identity; only a `bootstrap` profile — the
hand-written input to discovery — may leave identity blank.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1
PROFILE_STATES = ("bootstrap", "candidate", "active")
# A profile is a short declarative document; anything larger is not one, and the
# cap keeps a hostile or accidentally concatenated file out of the parser.
PROFILE_MAX_BYTES = 64 * 1024
# One value can name a host, a path, a URL or a fingerprint.  Nothing legitimate
# needs more, and the cap stops a pasted credential blob from riding along.
VALUE_MAX_LENGTH = 512
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SAFE_SSH_USER = re.compile(r"[A-Za-z0-9._%+-]+\Z")
# The host map feeds argv words for ssh, so an address must not be readable as an
# option.  This mirrors the production SSH target grammar minus the user part,
# which the profile carries once in [ssh].
SAFE_ADDRESS = re.compile(r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._:-]+)\Z")
SAFE_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
SAFE_PROMETHEUS_URL = re.compile(r"https?://[^\s\x00-\x1f\x7f]+\Z")
# OpenSSH prints SHA256 host key fingerprints as unpadded base64 of 32 bytes.
SSH_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
FSID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
# Text that means credential material is present.  One list, shared by everything
# in the lab workflow that has to keep credentials out of a file it writes or a
# diagnostic it repeats: the profile loader rejects a profile carrying any of
# these, the probe layer redacts a diagnostic containing one, and the report
# writer refuses to persist a report with one.  Three copies would drift apart.
CREDENTIAL_MARKERS = (
    "-----BEGIN",
    "PRIVATE KEY",
    "Authorization:",
    "ssh-rsa AAAA",
    "ssh-ed25519 AAAA",
    "ecdsa-sha2-nistp256 AAAA",
    "client-key-data",
    "client-certificate-data",
)

TOP_LEVEL_KEYS = frozenset({"schema_version"})
TOP_LEVEL_TABLES = frozenset({"profile", "ssh", "ceph", "rook", "prometheus", "hosts"})
TABLE_KEYS: Mapping[str, frozenset[str]] = {
    "profile": frozenset({"name", "state"}),
    "ssh": frozenset({"user", "key_path"}),
    "ceph": frozenset({"seed", "fsid"}),
    "rook": frozenset({"kubeconfig_path", "namespace", "operator_namespace", "fsid"}),
    "prometheus": frozenset({"url"}),
}
HOST_KEYS = frozenset({"name", "address", "hostname", "ssh_fingerprints"})


class LabProfileError(Exception):
    """A Lab Profile could not be loaded, or is not usable as declared."""

    def __init__(self, message: str, *, failure_class: str = "profile-invalid") -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass(frozen=True)
class LabHost:
    """One host in the lab's host map, and the identity it must prove."""

    name: str
    address: str
    hostname: str | None = None
    ssh_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabProfile:
    """One lab's connection entry points, expected identity and credential paths."""

    path: Path
    name: str
    state: str
    ssh_user: str
    ssh_key_path: Path
    ceph_seed: str
    rook_kubeconfig_path: Path
    rook_namespace: str
    rook_operator_namespace: str
    prometheus_url: str
    hosts: tuple[LabHost, ...]
    ceph_fsid: str | None = None
    rook_fsid: str | None = None

    @property
    def seed_host(self) -> LabHost:
        return self.host(self.ceph_seed)

    def host(self, name: str) -> LabHost:
        for host in self.hosts:
            if host.name == name:
                return host
        raise KeyError(name)

    def ssh_target(self, host: LabHost) -> str:
        return f"{self.ssh_user}@{host.address}"

    def credential_paths(self) -> tuple[tuple[str, Path], ...]:
        """The credential files this profile references, labelled by profile key.

        A profile records paths, never content.  Both the preflight and the local
        status check the same set, so the set is defined once here.
        """

        return (
            ("ssh key_path", self.ssh_key_path),
            ("rook kubeconfig_path", self.rook_kubeconfig_path),
        )

    def missing_identity(self) -> tuple[str, ...]:
        """Name every identity field a trusted profile must carry but does not."""

        missing: list[str] = []
        if self.ceph_fsid is None:
            missing.append("ceph fsid")
        if self.rook_fsid is None:
            missing.append("rook fsid")
        for host in self.hosts:
            if not host.hostname:
                missing.append(f"hostname for {host.name}")
            if not host.ssh_fingerprints:
                missing.append(f"ssh fingerprints for {host.name}")
        return tuple(missing)

    @property
    def identity_complete(self) -> bool:
        return not self.missing_identity()

    @property
    def profile_hash(self) -> str:
        """A canonical content hash: formatting and comments do not change it."""

        digest = hashlib.sha256(self.render().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def render(self) -> str:
        """Serialise back to TOML.

        Only the validated schema is rendered, so no value here can need escaping
        or produce a document the loader would reject.
        """

        lines = [f"schema_version = {SCHEMA_VERSION}", ""]
        lines += ["[profile]", _string("name", self.name), _string("state", self.state), ""]
        lines += [
            "[ssh]",
            _string("user", self.ssh_user),
            _string("key_path", str(self.ssh_key_path)),
            "",
        ]
        lines += ["[ceph]", _string("seed", self.ceph_seed)]
        if self.ceph_fsid is not None:
            lines.append(_string("fsid", self.ceph_fsid))
        lines += [
            "",
            "[rook]",
            _string("kubeconfig_path", str(self.rook_kubeconfig_path)),
            _string("namespace", self.rook_namespace),
            _string("operator_namespace", self.rook_operator_namespace),
        ]
        if self.rook_fsid is not None:
            lines.append(_string("fsid", self.rook_fsid))
        lines += ["", "[prometheus]", _string("url", self.prometheus_url)]
        for host in self.hosts:
            lines += ["", "[[hosts]]", _string("name", host.name)]
            lines.append(_string("address", host.address))
            if host.hostname:
                lines.append(_string("hostname", host.hostname))
            if host.ssh_fingerprints:
                lines.append(_string_list("ssh_fingerprints", host.ssh_fingerprints))
        return "\n".join(lines) + "\n"

    def with_state(self, state: str, *, path: Path) -> LabProfile:
        """Return the same lab identity declared in another state."""

        if state not in PROFILE_STATES:
            raise LabProfileError(f"profile state must be one of {', '.join(PROFILE_STATES)}")
        derived = replace(self, state=state, path=path)
        _require_identity_for_state(derived)
        return derived

    def with_identity(
        self,
        *,
        state: str,
        ceph_fsid: str | None,
        rook_fsid: str | None,
        host_identity: Mapping[str, tuple[str | None, tuple[str, ...]]],
        path: Path | None = None,
    ) -> LabProfile:
        """Return a new profile carrying discovered identity; the source is untouched."""

        hosts: list[LabHost] = []
        for host in self.hosts:
            hostname, fingerprints = host_identity.get(host.name, (None, ()))
            hosts.append(
                replace(
                    host,
                    hostname=hostname or host.hostname,
                    ssh_fingerprints=fingerprints or host.ssh_fingerprints,
                )
            )
        derived = replace(
            self,
            state=state,
            path=path or self.path,
            ceph_fsid=ceph_fsid or self.ceph_fsid,
            rook_fsid=rook_fsid or self.rook_fsid,
            hosts=tuple(hosts),
        )
        _validate(derived)
        return derived


def safe_display_path(path: Path) -> str:
    """Render a path for reports without spelling out the operator's home."""

    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def load_profile(path: Path) -> LabProfile:
    """Read and validate one Lab Profile from an absolute path."""

    if not path.is_absolute():
        raise LabProfileError(f"lab profile path must be absolute: {path}")
    if not path.is_file():
        raise LabProfileError(f"missing lab profile: {safe_display_path(path)}")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise LabProfileError(f"cannot read lab profile: {safe_display_path(path)}") from error
    if size > PROFILE_MAX_BYTES:
        raise LabProfileError(
            f"lab profile is too large ({size} bytes): {safe_display_path(path)}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise LabProfileError(f"cannot read lab profile: {safe_display_path(path)}") from error
    return parse_profile(text, path)


def parse_profile(text: str, path: Path) -> LabProfile:
    """Validate one Lab Profile document that is already in memory."""

    _reject_credential_material(text, path)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LabProfileError(
            f"cannot parse lab profile {safe_display_path(path)}: {error}"
        ) from error
    _reject_unknown_shape(document)
    profile = LabProfile(
        path=path,
        name=_safe_scalar(document, "profile", "name", SAFE_NAME, "profile name"),
        state=_state(document),
        ssh_user=_safe_scalar(document, "ssh", "user", SAFE_SSH_USER, "ssh user"),
        ssh_key_path=_absolute_path(document, "ssh", "key_path"),
        ceph_seed=_safe_scalar(document, "ceph", "seed", SAFE_NAME, "ceph seed"),
        ceph_fsid=_optional_fsid(document, "ceph"),
        rook_kubeconfig_path=_absolute_path(document, "rook", "kubeconfig_path"),
        rook_namespace=_namespace(document, "namespace"),
        rook_operator_namespace=_operator_namespace(document),
        rook_fsid=_optional_fsid(document, "rook"),
        prometheus_url=_prometheus_url(document),
        hosts=_hosts(document),
    )
    _validate(profile)
    return profile


def _validate(profile: LabProfile) -> None:
    try:
        profile.seed_host
    except KeyError:
        raise LabProfileError(
            f"ceph seed is not in the host map: {profile.ceph_seed}"
        ) from None
    _require_identity_for_state(profile)


def _require_identity_for_state(profile: LabProfile) -> None:
    if profile.state == "bootstrap":
        return
    missing = profile.missing_identity()
    if missing:
        raise LabProfileError(
            f"{profile.state} profile is missing lab identity: {', '.join(missing)}",
            failure_class="profile-identity-incomplete",
        )


def _reject_credential_material(text: str, path: Path) -> None:
    for marker in CREDENTIAL_MARKERS:
        if marker in text:
            raise LabProfileError(
                f"profile carries credential material ({marker}): {safe_display_path(path)}",
                failure_class="profile-carries-credential",
            )


def _reject_unknown_shape(document: Mapping[str, object]) -> None:
    version = document.get("schema_version")
    if version is None:
        raise LabProfileError(f"missing schema_version (expected {SCHEMA_VERSION})")
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise LabProfileError(f"schema_version must be {SCHEMA_VERSION}")
    for key, value in document.items():
        if isinstance(value, dict) or key == "hosts":
            if key not in TOP_LEVEL_TABLES:
                raise LabProfileError(f"unknown profile table: {key}")
        elif key not in TOP_LEVEL_KEYS:
            raise LabProfileError(f"unknown profile key: {key}")
    for table, allowed in TABLE_KEYS.items():
        section = document.get(table)
        if section is None:
            raise LabProfileError(f"missing [{table}]")
        if not isinstance(section, dict):
            raise LabProfileError(f"[{table}] must be a table")
        for key in section:
            if key not in allowed:
                raise LabProfileError(f"unknown key in [{table}]: {key}")
    for entry in _host_entries(document):
        for key in entry:
            if key not in HOST_KEYS:
                raise LabProfileError(f"unknown key in [[hosts]]: {key}")
    _reject_long_values(document)


def _reject_long_values(value: object) -> None:
    if isinstance(value, str):
        if len(value) > VALUE_MAX_LENGTH:
            raise LabProfileError(
                f"profile value is too long ({len(value)} characters)",
                failure_class="profile-carries-credential",
            )
    elif isinstance(value, dict):
        for item in value.values():
            _reject_long_values(item)
    elif isinstance(value, list):
        for item in value:
            _reject_long_values(item)


def _host_entries(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries = document.get("hosts")
    if entries is None:
        raise LabProfileError("profile lists no hosts")
    if not isinstance(entries, list) or not entries:
        raise LabProfileError("profile lists no hosts")
    for entry in entries:
        if not isinstance(entry, dict):
            raise LabProfileError("each [[hosts]] entry must be a table")
    return entries


def _state(document: Mapping[str, object]) -> str:
    value = document["profile"].get("state")  # type: ignore[union-attr]
    if value not in PROFILE_STATES:
        raise LabProfileError(f"profile state must be one of {', '.join(PROFILE_STATES)}")
    return str(value)


def _required_string(document: Mapping[str, object], table: str, key: str) -> str:
    value = document[table].get(key)  # type: ignore[union-attr]
    if value is None:
        raise LabProfileError(f"missing {key} in [{table}]")
    if not isinstance(value, str):
        raise LabProfileError(f"{table} {key} must be a string")
    return value


def _safe_scalar(
    document: Mapping[str, object],
    table: str,
    key: str,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    value = _required_string(document, table, key)
    if value.startswith("-") or pattern.fullmatch(value) is None:
        raise LabProfileError(f"invalid {label}: {value}")
    return value


def _absolute_path(document: Mapping[str, object], table: str, key: str) -> Path:
    value = _required_string(document, table, key)
    path = Path(value)
    if not path.is_absolute():
        raise LabProfileError(f"{table} {key} must be absolute: {value}")
    return path


def _optional_fsid(document: Mapping[str, object], table: str) -> str | None:
    value = document[table].get("fsid")  # type: ignore[union-attr]
    if value is None:
        return None
    if not isinstance(value, str):
        raise LabProfileError(f"{table} fsid must be a string")
    normalised = value.lower()
    if FSID.fullmatch(normalised) is None:
        raise LabProfileError(f"invalid {table} fsid: {value}")
    return normalised


def _namespace(document: Mapping[str, object], key: str) -> str:
    return _safe_scalar(document, "rook", key, SAFE_NAMESPACE, f"rook {key}")


def _operator_namespace(document: Mapping[str, object]) -> str:
    """The Rook operator namespace, which defaults to the cluster namespace.

    This mirrors the inventory contract's `ROOK_OPERATOR_NAMESPACE`, so the
    harness can derive one shared inventory from the profile instead of asking an
    operator to keep a second host/namespace list in step.
    """

    if document["rook"].get("operator_namespace") is None:  # type: ignore[union-attr]
        return _namespace(document, "namespace")
    return _namespace(document, "operator_namespace")


def _prometheus_url(document: Mapping[str, object]) -> str:
    value = _required_string(document, "prometheus", "url").rstrip("/")
    if SAFE_PROMETHEUS_URL.fullmatch(value) is None:
        raise LabProfileError(f"invalid prometheus url: {value}")
    return value


def _hosts(document: Mapping[str, object]) -> tuple[LabHost, ...]:
    hosts: list[LabHost] = []
    names: set[str] = set()
    addresses: set[str] = set()
    for entry in _host_entries(document):
        name = _host_string(entry, "name")
        if name in (".", "..") or SAFE_NAME.fullmatch(name) is None:
            raise LabProfileError(f"invalid host name: {name}")
        # Host names become bundle directory names, so two names that differ only
        # by case would collide on a case-insensitive filesystem.
        if name.casefold() in names:
            raise LabProfileError(f"duplicate host name: {name}")
        names.add(name.casefold())
        address = _host_string(entry, "address")
        if address.startswith("-") or SAFE_ADDRESS.fullmatch(address) is None:
            raise LabProfileError(f"invalid host address: {address}")
        if address in addresses:
            raise LabProfileError(f"duplicate host address: {address}")
        addresses.add(address)
        hostname = entry.get("hostname")
        if hostname is not None:
            if not isinstance(hostname, str) or SAFE_NAME.fullmatch(hostname) is None:
                raise LabProfileError(f"invalid host hostname: {hostname}")
        hosts.append(
            LabHost(
                name=name,
                address=address,
                hostname=hostname,
                ssh_fingerprints=_fingerprints(entry, name),
            )
        )
    return tuple(hosts)


def _host_string(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if value is None:
        raise LabProfileError(f"missing {key} in [[hosts]]")
    if not isinstance(value, str):
        raise LabProfileError(f"host {key} must be a string")
    return value


def _fingerprints(entry: Mapping[str, object], host: str) -> tuple[str, ...]:
    value = entry.get("ssh_fingerprints")
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LabProfileError(f"ssh_fingerprints for {host} must be a list of strings")
    seen: set[str] = set()
    for fingerprint in value:
        if SSH_FINGERPRINT.fullmatch(fingerprint) is None:
            raise LabProfileError(f"invalid ssh fingerprint for {host}: {fingerprint}")
        if fingerprint in seen:
            raise LabProfileError(f"duplicate ssh fingerprint for {host}: {fingerprint}")
        seen.add(fingerprint)
    return tuple(value)


def _string(key: str, value: str) -> str:
    return f'{key} = "{value}"'


def _string_list(key: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f'"{value}"' for value in values)
    return f"{key} = [{rendered}]"
