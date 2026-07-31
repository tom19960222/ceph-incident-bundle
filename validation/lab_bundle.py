"""Reducing one real-lab bundle to the contract the two implementations must share.

What this compares, and why it is not a byte comparison
-------------------------------------------------------
The offline equivalence gate (`make test-differential`, issue #18) already proves
that the shell reference and the Python candidate turn *the same inputs* into the
same bytes; it can do that because its world is fake and frozen.  A real lab is
neither.  The two qualification collects run minutes apart against a live
cluster, so `ceph -s` genuinely reports different numbers and a journal genuinely
has more lines in the second run.  Demanding identical evidence bytes there would
not be strictness — it would be a gate that can only pass by accident.

So this module compares the part of a bundle that must not depend on when it was
taken:

- which members exist, at which paths;
- the manifest — every collector, every artifact, the exact command argv and the
  exact exit code, which is where CLI semantics, runner selection and source
  selection become observable;
- each captured artifact's header (host, collector, timeout, truncation) and
  whether its body parses as JSON at all;
- how each SKIPPED or partial outcome was classified;
- which of the four collector paths were covered.

Evidence *bodies*, `/var/log` payloads and recompressed metric dumps are recorded
as present-and-opaque — `_evidence_is_the_clusters_answer` below explains why
even their JSON key paths are the cluster's doing rather than the collector's.
A difference in any of the compared fields fails the gate; there is no allowance
list here, because the only differences deliberately ignored are the ones the
normalizer removes below — clocks, run directories, random temporary names and
invocation identifiers — and each is named.

Reading is untrusting.  A bundle is a tar archive, so every member is checked for
absolute paths, traversal, links and special files before anything is read, and
nothing is ever extracted to disk.
"""

from __future__ import annotations

import json
import re
import shlex
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from validation.lab_report import COLLECTOR_PATHS, CollectorCoverage


# Artifacts this module interprets are configuration, status and manifests: text
# a person could read.  Anything bigger is evidence payload, recorded as opaque.
ARTIFACT_READ_CAP_BYTES = 4 * 1024 * 1024
# A bundle with an implausible number of members is not one this comparison can
# describe; refusing beats spending an hour building an unreadable difference.
MEMBER_LIMIT = 500_000
COMPRESSED_SUFFIXES = (".gz", ".xz", ".bz2", ".zst")
MANIFEST_NAME = "manifest.jsonl"
ENVIRONMENT_NAME = "environment.txt"
SUMMARY_NAME = "summary.txt"
# `environment.txt` is where the run records which source and runner it chose, so
# these keys are the comparison's view of source and runner selection.
# `created_utc` is the clock, and the candidate-only keys (`node_target_*`,
# `node_invocation_id_*`, the Rook namespaces, `kube_context`) are the rewrite's
# declared additional observability — see `docs/differential-normalizer.md`.
ENVIRONMENT_SELECTION_KEYS = (
    "mode",
    "seed",
    "since",
    "timeout",
    "git_commit",
    "ceph_source",
    "ceph_runner",
    "rook_source",
    "prom_url",
    "prom_jobs",
)
# `summary.txt` is where partial collection becomes observable.
SUMMARY_KEYS = (
    "mode",
    "seed",
    "cluster_status",
    "node_ok",
    "node_failed",
    "final_status",
)
NODE_MANIFEST = re.compile(r"nodes/([^/]+)/manifest\.jsonl\Z")
VAR_LOG_PAYLOAD = re.compile(r"nodes/[^/]+/logs/var-log/(?:merged|raw|original)/")
CLUSTER_LAYERS = {"ceph": "cluster/ceph/", "rook": "cluster/rook/", "prometheus": "cluster/prometheus/"}

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
ARCHIVE_STAMP = re.compile(r"ceph-incident-\d{8}T\d{6}Z")
# Both implementations name their remote workspace after `ceph-incident-node`;
# the tail is a mktemp suffix or an invocation identifier, and neither is
# evidence.
NODE_WORKSPACE = re.compile(r"/[^\s\"']*ceph-incident-node[.-][A-Za-z0-9._-]+")
INVOCATION_ID = re.compile(r"\b[0-9a-f]{32}\b")
# A redaction scratch file `<dir>/.<name>.plain.XXXXXX` names the artifact the
# other implementation logs directly; only the random component differs.
REDACTION_SCRATCH = re.compile(r"/\.([^/]+)\.(?:plain|encoded)\.[A-Za-z0-9]{6,}")
# The workstation workdir's `mktemp` component.  Matched on its own rather than
# with a leading path, because by then the caller's rules have already replaced
# the directory it sits in.
LOCAL_WORKDIR = re.compile(r"tmp\.[A-Za-z0-9]{6,}")

SKIP_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"no cephadm-capable node", re.I), "ceph-source-missing"),
    (re.compile(r"no kubectl-capable node", re.I), "rook-source-missing"),
    (re.compile(r"namespaces? .*not found|rook namespace not found", re.I), "rook-namespace-missing"),
    (re.compile(r"kubectl command not found", re.I), "kubectl-missing"),
    (re.compile(r"kubectl cannot connect", re.I), "kubectl-unreachable"),
    (re.compile(r"kubectl context not found", re.I), "kube-context-missing"),
    (re.compile(r"kubectl exec disabled", re.I), "toolbox-exec-disabled"),
    (re.compile(r"toolbox Pod not found", re.I), "toolbox-missing"),
    (re.compile(r"operator Pod not found", re.I), "operator-missing"),
    (re.compile(r"node collection timed out", re.I), "node-timeout"),
    (re.compile(r"missing manifest|no manifest\.jsonl", re.I), "node-archive-missing-manifest"),
    (re.compile(r"no usable node archive|invalid or unreadable archive", re.I), "node-archive-unusable"),
    (re.compile(r"python 3\.11", re.I), "node-python-unsupported"),
    (re.compile(r"skip-logs", re.I), "var-log-skipped"),
    (re.compile(r"not reachable|not responding", re.I), "prometheus-unreachable"),
    (re.compile(r"no scrape job matched", re.I), "prometheus-no-job"),
)


class BundleUnreadable(Exception):
    """A bundle could not be read as a bundle, so it cannot be compared."""


@dataclass(frozen=True)
class BundleMember:
    """One archive member, already checked for the shapes we refuse to accept."""

    name: str
    is_directory: bool
    size: int


@dataclass(frozen=True)
class BundleContents:
    """One bundle's member list plus the payloads this comparison interprets."""

    path: Path
    members: tuple[BundleMember, ...]
    payloads: dict[str, bytes]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            member.name + ("/" if member.is_directory else "") for member in self.members
        )

    def text(self, name: str) -> str:
        return self.payloads.get(name, b"").decode("utf-8", "replace")


def read_bundle(path: Path) -> BundleContents:
    """Read one packaged bundle without extracting it, refusing unsafe members.

    The collector produced this archive, but "we made it" is not a reason to
    trust it: the read-only safety contract asks every archive to be validated
    before its content is used, and a bundle carrying a link or an absolute path
    is a finding rather than something to work around.
    """

    if not path.is_file():
        raise BundleUnreadable(f"missing bundle: {path}")
    members: list[BundleMember] = []
    payloads: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if len(members) >= MEMBER_LIMIT:
                    raise BundleUnreadable(
                        f"bundle has more than {MEMBER_LIMIT} members: {path}"
                    )
                name = _safe_member_name(member)
                if name is None:
                    continue
                members.append(BundleMember(name, member.isdir(), member.size))
                if member.isfile() and _is_interpreted(name, member.size):
                    stream = archive.extractfile(member)
                    payloads[name] = b"" if stream is None else stream.read(
                        ARTIFACT_READ_CAP_BYTES
                    )
    except (tarfile.TarError, OSError, EOFError) as error:
        raise BundleUnreadable(f"cannot read bundle {path}: {error}") from error
    if not members:
        raise BundleUnreadable(f"bundle is empty: {path}")
    return BundleContents(path, tuple(sorted(members, key=lambda item: item.name)), payloads)


def _safe_member_name(member: tarfile.TarInfo) -> str | None:
    if member.islnk() or member.issym():
        raise BundleUnreadable(f"bundle contains a link member: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise BundleUnreadable(f"bundle contains a special member: {member.name}")
    name = member.name.removeprefix("./")
    if name in ("", "."):
        return None
    if name.startswith("/") or PurePosixPath(name).is_absolute():
        raise BundleUnreadable(f"bundle contains an absolute member: {member.name}")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise BundleUnreadable(f"bundle contains a traversal member: {member.name}")
    return PurePosixPath(name).as_posix()


def _is_interpreted(name: str, size: int) -> bool:
    """Whether this member's bytes take part in the comparison.

    Evidence payload — `/var/log` files and recompressed metric dumps — is
    deliberately excluded: it is where two collects of a live cluster are
    *expected* to differ, and reading it would also mean holding gigabytes in
    memory to prove nothing.
    """

    if size > ARTIFACT_READ_CAP_BYTES:
        return False
    if VAR_LOG_PAYLOAD.match(name) or name.endswith(COMPRESSED_SUFFIXES):
        return False
    return True


def coverage_of(contents: BundleContents, host_aliases: Sequence[str]) -> CollectorCoverage:
    """Decide, per collector path, whether this single invocation covered it.

    Anything short of "collected" is a gap, including a path that only produced a
    SKIPPED marker: qualification requires one invocation to cover all four, so a
    documented reason for missing evidence is still missing evidence.
    """

    names = contents.names
    verdicts = {
        layer: _layer_verdict(names, prefix) for layer, prefix in CLUSTER_LAYERS.items()
    }
    verdicts["nodes"] = _per_node_verdict(names, host_aliases, "")
    verdicts["var_log"] = _per_node_verdict(names, host_aliases, "logs/var-log/")
    return CollectorCoverage(**{path: verdicts[path] for path in COLLECTOR_PATHS})


def _layer_verdict(names: Sequence[str], prefix: str) -> str:
    files = [name for name in names if name.startswith(prefix) and not name.endswith("/")]
    if not files:
        return "missing"
    if all(_is_skip_artifact(name) for name in files):
        return "skipped"
    return "collected"


def _per_node_verdict(names: Sequence[str], host_aliases: Sequence[str], suffix: str) -> str:
    gaps = [
        alias
        for alias in host_aliases
        if _layer_verdict(names, f"nodes/{alias}/{suffix}") != "collected"
    ]
    if not host_aliases:
        return "missing"
    return "collected" if not gaps else "missing: " + ", ".join(sorted(gaps))


def contract_of(
    contents: BundleContents, substitutions: Sequence[tuple[re.Pattern[str], str]] = ()
) -> dict[str, object]:
    """Reduce one bundle to the document the two implementations must agree on."""

    # Caller rules first: they are literal paths, and a general rule that rewrote
    # the timestamp inside one would stop the literal from ever matching.
    rules = [*substitutions, *_default_substitutions()]
    contract: dict[str, object] = {
        "members": list(contents.names),
        "manifests": _manifests(contents, rules),
        "artifacts": _artifacts(contents, rules),
        "errors": _error_classes(contents, rules),
    }
    return contract


def _default_substitutions() -> list[tuple[re.Pattern[str], str]]:
    """The only differences this comparison removes, each one a clock or a nonce."""

    return [
        (ARCHIVE_STAMP, "ceph-incident-<stamp>"),
        (TIMESTAMP, "<timestamp>"),
        (NODE_WORKSPACE, "<node-workspace>"),
        (REDACTION_SCRATCH, r"/\1"),
        (LOCAL_WORKDIR, "<workdir>"),
        (INVOCATION_ID, "<invocation>"),
    ]


def normalize(text: str, rules: Sequence[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def _manifests(
    contents: BundleContents, rules: Sequence[tuple[re.Pattern[str], str]]
) -> dict[str, object]:
    manifests: dict[str, object] = {}
    for name in sorted(contents.payloads):
        if name != MANIFEST_NAME and NODE_MANIFEST.fullmatch(name) is None:
            continue
        manifests[name] = _manifest_records(contents.text(name), rules)
    return manifests


def _manifest_records(
    text: str, rules: Sequence[tuple[re.Pattern[str], str]]
) -> list[dict[str, object]]:
    """The manifest as a sorted set of (collector, artifact, command, exit) facts.

    `started`/`ended` are dropped — they are the clock — and the order is
    normalised because the two implementations are free to schedule the same
    collectors differently.  The command is compared as an argv vector so shell
    quoting style cannot masquerade as a different command.
    """

    records: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            records.append({"unparseable": normalize(line, rules)})
            continue
        if not isinstance(entry, dict):
            records.append({"unparseable": normalize(line, rules)})
            continue
        records.append(
            {
                "host": entry.get("host"),
                "collector": entry.get("collector"),
                "artifact": normalize(str(entry.get("artifact", "")), rules),
                "command": _argv(str(entry.get("command", "")), rules),
                "exit_code": entry.get("exit_code"),
            }
        )
    return sorted(records, key=lambda record: json.dumps(record, sort_keys=True))


def _argv(command: str, rules: Sequence[tuple[re.Pattern[str], str]]) -> list[str]:
    normalized = normalize(command, rules)
    try:
        return shlex.split(normalized)
    except ValueError:
        # An unsplittable command string is itself a difference worth seeing.
        return [normalized]


def _artifacts(
    contents: BundleContents, rules: Sequence[tuple[re.Pattern[str], str]]
) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    for member in contents.members:
        if member.is_directory:
            continue
        name = member.name
        if name == MANIFEST_NAME or NODE_MANIFEST.fullmatch(name) is not None:
            continue
        artifacts[normalize(name, rules)] = _artifact_contract(contents, member, rules)
    return artifacts


def _artifact_contract(
    contents: BundleContents,
    member: BundleMember,
    rules: Sequence[tuple[re.Pattern[str], str]],
) -> dict[str, object]:
    name = member.name
    if name not in contents.payloads:
        # Evidence payload: present, and its bytes are the live cluster's, not
        # the collector's.  Presence and path are the contract here.
        return {"kind": "opaque"}
    text = contents.text(name)
    if _is_skip_artifact(name):
        return {"kind": "skip", "reason": _classify(text, SKIP_CLASSES, rules)}
    if name == ENVIRONMENT_NAME:
        return {
            "kind": "environment",
            "selection": _fields(text, "=", ENVIRONMENT_SELECTION_KEYS, rules),
        }
    if name == SUMMARY_NAME:
        return {"kind": "summary", "fields": _fields(text, ":", SUMMARY_KEYS, rules)}
    header, body = _split_capture_header(text)
    contract: dict[str, object] = {
        # Whether the captured body parses as JSON *is* the implementation's
        # doing — a candidate that wrapped, truncated or re-serialised evidence
        # shows up here.  What the JSON says is the cluster's doing; see
        # `_evidence_is_the_clusters_answer` for why it is not compared.
        "kind": "json" if _parses_as_json(body) else "text"
    }
    if header:
        contract["header"] = {
            key: normalize(value, rules)
            for key, value in header.items()
            # `started` and `ended` are the capture's clock, not its contract.
            if key not in ("started", "ended")
        }
    return contract


def _evidence_is_the_clusters_answer() -> None:
    """Why a captured artifact's body is not compared field by field.

    Neither implementation *transforms* cluster evidence: each runs a command and
    records its output verbatim.  The manifest already pins which command ran and
    what it exited with, so if both manifests agree, the two bodies came from the
    same question asked of the same cluster — and any difference between them is
    the cluster answering at two different moments.

    Comparing the body, even reduced to its JSON key paths, would therefore fail
    on things that are not the candidate's doing: `health.checks` is `{}` on a
    healthy cluster and grows a key the moment a slow op is reported, a counter
    that reads `0` in one run reads `0.5` in the next.  A gate that fails on a
    transient HEALTH_WARN is a gate people learn to re-run until it passes, which
    is worse than one that compares less and means it.

    Byte-level equivalence of evidence handling is the offline gate's job
    (`make test-differential`), where the inputs are frozen and it can be exact.
    """


def _fields(
    text: str,
    separator: str,
    keys: tuple[str, ...],
    rules: Sequence[tuple[re.Pattern[str], str]],
) -> dict[str, str | None]:
    """Read one `key<sep>value` document, keeping only the listed keys.

    A listed key that is absent is recorded as `None` rather than omitted, so one
    implementation dropping a field is a difference instead of a smaller
    document that happens to agree everywhere it overlaps.
    """

    found: dict[str, str] = {}
    for line in text.splitlines():
        key, marker, value = line.partition(separator)
        if marker and key.strip() in keys:
            found[key.strip()] = normalize(value.strip(), rules)
    return {key: found.get(key) for key in keys}


def _parses_as_json(body: str) -> bool:
    stripped = body.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def _error_classes(
    contents: BundleContents, rules: Sequence[tuple[re.Pattern[str], str]]
) -> list[str]:
    """The set of classified failure events, not the log's line sequence.

    The two implementations record the same failure at different granularity and
    interleave their own diagnostics, so an unrecognised line is kept normalised
    and compared literally while a recognised one collapses to its class.
    """

    text = contents.text("errors.log")
    classes = {_classify(line, SKIP_CLASSES, rules) for line in text.splitlines() if line.strip()}
    return sorted(classes)


def _classify(
    text: str, classes: tuple[tuple[re.Pattern[str], str], ...], rules: Sequence[tuple[re.Pattern[str], str]]
) -> str:
    for pattern, name in classes:
        if pattern.search(text):
            return name
    return normalize(" ".join(text.split()), rules)


def _is_skip_artifact(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return (
        base.startswith("SKIPPED")
        or base.endswith("-SKIPPED.txt")
        or base in ("OVER-LIMIT.txt", "SCAN-LIMIT.txt", "MANIFEST-LIMIT.txt")
        or base == "crash-info-skip.txt"
    )


def _split_capture_header(text: str) -> tuple[dict[str, str], str]:
    """Split a captured artifact's `# key: value` header from its body."""

    header: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].startswith("# "):
        key, separator, value = lines[index][2:].rstrip("\n").partition(": ")
        if not separator:
            break
        header[key] = value
        index += 1
    return header, "".join(lines[index:])
