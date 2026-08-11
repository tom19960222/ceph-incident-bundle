"""Reduce real-lab bundles to the preserved cross-implementation contract.

What this compares, and why it is not a byte comparison
-------------------------------------------------------
The retired offline equivalence gate (issue #18) proved that the shell reference
and Python candidate turned *the same inputs* into the same bytes because its
world was fake and frozen. A real lab is neither. The preserved shell baseline
and the post-cutover Python collect were taken at different times, so `ceph -s`
genuinely reports different numbers and a journal genuinely has more lines in the
later run. Demanding identical evidence bytes there would not be strictness — it
would be a gate that can only pass by accident.

So this module compares the part of a bundle that must not depend on when it was
taken:

- which members exist, at which paths — except inside the `/var/log` payload
  trees, whose file set is the live machine's doing rather than either
  implementation's: a collect on the far side of a UTC day boundary finds a
  `sysstat/sa03` the earlier one could not, and journald renames its archived
  files between two runs (#52);
- the manifest — every collector, every artifact, the exact command argv and the
  exact exit code, which is where CLI semantics, runner selection and source
  selection become observable.  Node manifests are compared over the surface
  both implementations claim rather than entry-for-entry, because ADR 0010
  deliberately diverged their coverage; `_node_manifest` below is where that
  divergence is enumerated, and it is the only place the manifest comparison
  gives anything up;
- each captured artifact's header (host, collector, timeout, truncation) and
  whether its body parses as JSON at all;
- the collector decisions recorded in top-level metadata and Prometheus
  `dump-info.txt`, selected field by field so live observations stay out;
- how each SKIPPED or partial outcome was classified;
- which of the four collector paths were covered.

Evidence *bodies*, `/var/log` payloads and recompressed metric dumps are recorded
as present-and-opaque, down to their JSON key paths.  Collector-authored control
documents are the exception: the explicit field lists below retain decisions
without turning live observations into contract.  Neither implementation
*transforms* cluster evidence: each runs a command and records its output
verbatim, and the manifest above already pins which command ran and what it
exited with.  If both manifests agree, the two evidence bodies came from the same
question asked of the same cluster, and any difference between them is the
cluster answering at two different moments — `health.checks` is `{}` on a healthy
cluster and grows a key the moment a slow op is reported.  A gate that fails on a
transient HEALTH_WARN is one people learn to re-run until it passes, which is
worse than one that compares less and means it.

A difference in any of the compared fields fails the gate.  Four surfaces are
deliberately ignored, and each is enumerated rather than hidden behind a
catch-all: the clocks, run directories, random temporary names and invocation
identifiers the normalizer removes below; unselected live fields in
collector-authored control documents; the node manifest entries ADR 0010 already
adjudicated as divergent; and which files the `/var/log` payload trees hold,
which belongs to the machine and the hour rather than to either implementation.

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
PROMETHEUS_DUMP_INFO_NAME = "cluster/prometheus/dump-info.txt"
# `environment.txt` is where the run records which source and runner it chose, so
# these keys are the comparison's view of source and runner selection. `git_commit`
# remains bundle provenance but is intentionally compared by the pinned baseline
# record and current report: a post-cutover bundle must come from a newer commit.
# `created_utc` is the clock, and the candidate-only keys (`node_target_*`,
# `node_invocation_id_*`, the Rook namespaces, `kube_context`) are the rewrite's
# declared additional observability — see `docs/differential-normalizer.md`.
ENVIRONMENT_SELECTION_KEYS = (
    "mode",
    "seed",
    "since",
    "timeout",
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
# `dump-info.txt` records both the Prometheus collector's decisions and values
# observed from a live target.  Only the former belong in a two-run contract.
PROMETHEUS_DUMP_INFO_KEYS = (
    "since",
    "step_seconds",
    "job_regex",
    "jobs_matched",
    "truncated",
)
NODE_MANIFEST = re.compile(r"nodes/([^/]+)/manifest\.jsonl\Z")
# The `/var/log` payload trees.  Their bytes are never interpreted, and their
# *file set* is excluded from the member and artifact comparison outright: which
# files `/var/log` holds is the live machine's doing — `sysstat/sa03` is born at
# a UTC day boundary, journald renames archived journals between two collects
# (#52) — so two honest collects hours apart legitimately package different
# files here.  The boundary is exactly these three subtrees: members directly
# under `logs/var-log/` (the journal capture, the INDEX) are still compared, and
# per-bundle coverage still requires every node's `/var/log` path collected.
VAR_LOG_PAYLOAD = re.compile(r"nodes/[^/]+/logs/var-log/(?:merged|raw|original)/")
SKIP_MARKER = "SKIPPED:"
# The collector's own index verbs (ADR 0010).  `collect-node copy` marks evidence
# the reference duplicates without recording, and `collect-var-log /var/log` the
# generated `/var/log` tree.  Neither names a command that ran: they exist so an
# archive-wide index can point at evidence nobody executed a command for.  Each
# is matched as an argv prefix, and no wider than the contract document
# enumerates it — the candidate writes `collect-var-log /var/log` verbatim, so
# the `/var/log` argument is part of the verb, not an example.
INDEX_VERBS = (["collect-node", "copy"], ["collect-var-log", "/var/log"])
# The one artifact whose entry both implementations record but name differently.
VAR_LIB_CEPH_LISTING = "cephadm/var-lib-ceph-listing.txt"
CAPTURE_HEADER = "# host: "
# ADR 0010 gives the SKIPPED markers it enumerates one of two exit codes: 127 for
# a command that does not exist, 2 for evidence that does not.  The reference
# writes a marker of its own outside that list — the over-limit journal, entry
# code 75 — and that one it *does* record, so the code has to be part of the
# recognition or the relaxation would swallow a reference entry.
MARKER_EXIT_CODES = (2, 127)
# A node manifest entry names its artifact by the absolute path it had inside the
# node's workspace, which the normalizer has already collapsed to this marker.
NODE_WORKSPACE_MARK = "<node-workspace>/"
# Both node collectors write their evidence to `<workspace>/out` — the reference
# is passed `--out "$tmp/out"`, the candidate derives `workspace / "out"` itself —
# so every artifact path their manifests record carries that segment.  Packing
# drops it: the same evidence is `nodes/<alias>/<relative>` in the bundle.
# Stripping it is what lets the rules below ask the bundle about the member the
# entry actually names.
NODE_WORKSPACE_OUT = "out/"
# All that survives of a manifest line whose command matched content safety.
BLANKED_LINE = "[REDACTED]"
CLUSTER_LAYERS = {"ceph": "cluster/ceph/", "rook": "cluster/rook/", "prometheus": "cluster/prometheus/"}
# A manifest entry's artifact under the Prometheus layer.  Matched anywhere in
# the path rather than at its start, because the cluster manifest records the
# workstation-absolute path and only the normalizer's `<workdir>` sits in front.
PROMETHEUS_ARTIFACT = re.compile(r"(?:\A|/)" + CLUSTER_LAYERS["prometheus"])

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
#
# The two implementations build the name differently and the rule has to cover
# both, or the difference it exists to erase comes back: the reference uses
# `tmp.<stamp>.$$`, so a pattern ending at the stamp leaves the pid behind as
# `<workdir>.61493` (#52), and `tempfile.mkdtemp` draws its suffix from an
# alphabet that includes `_`, so an alphanumeric-only run can stop short of the
# candidate's whole name — intermittently, which is worse than never.
LOCAL_WORKDIR = re.compile(r"tmp\.[A-Za-z0-9_]{6,}(?:\.\d+)?")

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

    def is_skip(self, name: str) -> bool:
        """Whether this member records a skipped capture rather than evidence.

        Name alone is not enough.  A collector may write `SKIPPED: <reason>` into
        the artifact the evidence *would* have occupied — the `/var/log`
        over-limit path does exactly that to `journal-all-since.txt` — so a
        coverage check that only looked at filenames would count a node whose
        whole log payload was dropped as covered.
        """

        return _is_skip_name(name) or self.text(name).startswith(SKIP_MARKER)


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

    verdicts = {
        layer: _layer_verdict(contents, prefix)
        for layer, prefix in CLUSTER_LAYERS.items()
    }
    verdicts["nodes"] = _per_node_verdict(contents, host_aliases, "")
    verdicts["var_log"] = _per_node_verdict(contents, host_aliases, "logs/var-log/")
    return CollectorCoverage(**{path: verdicts[path] for path in COLLECTOR_PATHS})


def _layer_verdict(contents: BundleContents, prefix: str) -> str:
    files = [
        member.name
        for member in contents.members
        if not member.is_directory and member.name.startswith(prefix)
    ]
    if not files:
        return "missing"
    if all(contents.is_skip(name) for name in files):
        return "skipped"
    return "collected"


def _per_node_verdict(
    contents: BundleContents, host_aliases: Sequence[str], suffix: str
) -> str:
    if not host_aliases:
        return "missing"
    gaps = sorted(
        f"{alias}={verdict}"
        for alias in host_aliases
        if (verdict := _layer_verdict(contents, f"nodes/{alias}/{suffix}")) != "collected"
    )
    # Named per node and per verdict: "the gate did not cover everything" is not
    # actionable, "osd01 skipped its logs while mon02 produced none" is.
    return "collected" if not gaps else ", ".join(gaps)


def contract_of(
    contents: BundleContents, substitutions: Sequence[tuple[re.Pattern[str], str]] = ()
) -> dict[str, object]:
    """Reduce one bundle to the document the two implementations must agree on."""

    # Caller rules first: they are literal paths, and a general rule that rewrote
    # the timestamp inside one would stop the literal from ever matching.
    rules = [*substitutions, *_default_substitutions()]
    contract: dict[str, object] = {
        # The `/var/log` payload trees are the one member-set exception: their
        # file set drifts naturally between two collects, so their contents stay
        # out of the comparison — see VAR_LOG_PAYLOAD for the boundary.
        "members": [name for name in contents.names if not VAR_LOG_PAYLOAD.match(name)],
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
        if name == MANIFEST_NAME:
            manifests[name] = _manifest_records(contents.text(name), rules)
            continue
        node = NODE_MANIFEST.fullmatch(name)
        if node is not None:
            manifests[name] = _node_manifest(contents, node.group(1), name, rules)
    return manifests


def _node_manifest(
    contents: BundleContents,
    alias: str,
    name: str,
    rules: Sequence[tuple[re.Pattern[str], str]],
) -> dict[str, object]:
    """One node manifest, reduced to the surface both implementations claim.

    ADR 0010 deliberately diverged this document: the Python node manifest is an
    index of *every* evidence in the archive, while the reference records only
    the commands it ran.  Comparing entry-for-entry would make this gate overturn
    an adjudication the project already made — and the counts say so plainly, 26
    entries against 248 on one lab node.

    What it drops is exactly what ADR 0010 enumerates: an entry that names copied
    evidence or the generated `/var/log` tree, and an entry whose artifact is a
    SKIPPED marker rather than evidence.  The first two announce themselves with
    an index verb, so the bundle is asked to corroborate — an entry wearing an
    index verb over an artifact that carries a capture header is a command that
    really ran, and it stays in the comparison.  The third is read from the
    bundle outright.

    What it does *not* drop is the rest of `logs/var-log/`: the journal capture
    there records the real `journalctl` argv on both sides, which is where
    `sudo -n` and the `--since` window stay observable.
    """

    records = _manifest_records(contents.text(name), rules)
    listing_present = _has_listing(contents, alias)
    entries: list[dict[str, object]] = []
    blanked = 0
    listing = "absent"
    for record in records:
        if record.get("unparseable") == BLANKED_LINE:
            blanked += 1
            continue
        if "unparseable" in record:
            entries.append(record)
            continue
        relative = _node_relative(alias, str(record.get("artifact", "")))
        # The enumerated classes are tested first, including over the listing's
        # own artifact.  A node with no readable `/var/lib/ceph` gets the same
        # SKIPPED marker from both sides and an index entry over it from the
        # candidate alone — that entry is a marker index, the third row of the
        # table, and reading it as "this node recorded a listing" would have the
        # gate claim the two disagree about evidence they wrote identically
        # (#52).  Order is the whole fix: the listing branch below then only
        # ever sees an entry over evidence.
        if _is_index_only(contents, alias, relative, record):
            continue
        if relative == VAR_LIB_CEPH_LISTING:
            # Both implementations record this entry; only the command differs,
            # and ADR 0010 moved that command's policy to the N9 argv ledger.
            # Keyed on the artifact rather than on the candidate's verb, so the
            # reference's `find` entry collapses the same way if content safety
            # ever stops blanking it (#44).
            listing = "recorded"
            continue
        entries.append(record)
    if listing == "absent" and listing_present and blanked:
        # The reference records this same entry, but its real `find` expression
        # names `*keyring*`, so content safety blanks the whole line before the
        # bundle is packed.  One blanked line against a listing that is in the
        # archive is that entry; ADR 0010 already moved its command policy to the
        # N9 argv ledger.  A second blanked line is a redaction nothing here
        # accounts for, so it stays visible below.
        listing = "recorded"
        blanked -= 1
    return {
        "entries": entries,
        "var_lib_ceph_listing": listing,
        "unaccounted_redacted_entries": blanked,
    }


def _has_listing(contents: BundleContents, alias: str) -> bool:
    """Whether this node's `/var/lib/ceph` listing is in the bundle as evidence.

    Membership, not readability: a listing over the interpretation cap is still a
    listing, and asking `payloads` would quietly stop recognising it.
    """

    member = f"nodes/{alias}/{VAR_LIB_CEPH_LISTING}"
    present = any(entry.name == member for entry in contents.members)
    return present and not contents.is_skip(member)


def _is_index_only(
    contents: BundleContents, alias: str, relative: str | None, record: dict[str, object]
) -> bool:
    """Whether ADR 0010 declares this entry one the reference never records."""

    if relative is None:
        return False
    member = f"nodes/{alias}/{relative}"
    if contents.is_skip(member) and record.get("exit_code") in MARKER_EXIT_CODES:
        return True
    command = record.get("command", [])
    if not isinstance(command, list):
        return False
    if not any(command[: len(verb)] == verb for verb in INDEX_VERBS):
        return False
    # An index verb claims nobody ran a command for this artifact.  A capture
    # header says otherwise, and a capture is exactly what the reference records
    # too — so the claim is refused rather than taken at its word.
    return not contents.text(member).startswith(CAPTURE_HEADER)


def _node_relative(alias: str, artifact: str) -> str | None:
    """Where one node manifest entry's artifact sits inside the node's archive.

    The answer has to be the *bundle* member path, because every rule above uses
    it to ask the bundle a question.  A workspace-absolute artifact therefore
    loses both the workspace and the `out/` directory the collector was pointed
    at: keeping `out/` names a member that cannot exist, and a bundle asked about
    a member it does not have answers "not a skip marker" and "no capture
    header" — the two answers that make the ADR 0010 rules do nothing.  That is
    how a reduction with tests over it still let 13 entries through on the real
    lab (#52): the fixtures wrote the artifact without the `out/` real collectors
    put there.
    """

    marker = artifact.rfind(NODE_WORKSPACE_MARK)
    if marker >= 0:
        relative = artifact[marker + len(NODE_WORKSPACE_MARK) :]
        return relative[len(NODE_WORKSPACE_OUT) :] if relative.startswith(NODE_WORKSPACE_OUT) else relative
    prefix = f"nodes/{alias}/"
    return artifact[len(prefix) :] if artifact.startswith(prefix) else None


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
        artifact = normalize(str(entry.get("artifact", "")), rules)
        records.append(
            {
                "host": entry.get("host"),
                "collector": entry.get("collector"),
                "artifact": artifact,
                "command": _argv(str(entry.get("command", "")), rules, artifact),
                "exit_code": entry.get("exit_code"),
            }
        )
    return sorted(records, key=lambda record: json.dumps(record, sort_keys=True))


def _argv(
    command: str, rules: Sequence[tuple[re.Pattern[str], str]], artifact: str = ""
) -> list[str]:
    normalized = normalize(command, rules)
    try:
        argv = shlex.split(normalized)
    except ValueError:
        # An unsplittable command string is itself a difference worth seeing.
        return [normalized]
    return _relative_query_window(argv, artifact)


def _relative_query_window(argv: list[str], artifact: str) -> list[str]:
    """Restate a Prometheus query window relative to its own end.

    `start=`/`end=` are absolute epoch seconds taken from the moment the collect
    began, so two collects minutes apart can never write the same pair — the
    only argv in either implementation that is a clock rather than a decision.
    Erasing both would also erase `--since`, which is exactly the decision this
    gate has to keep watching, so the end becomes the origin and the start keeps
    its distance from it: a candidate that queried a different window still says
    so, and one that queried the same window twenty minutes later does not.

    Scoped to the Prometheus artifacts whose collector computes those epochs.
    A `start=`/`end=` pair anywhere else is some other command's argument, and
    this rewrite has no business deciding it is a clock.
    """

    if not PROMETHEUS_ARTIFACT.search(artifact):
        return argv
    start = _epoch_argument(argv, "start=")
    end = _epoch_argument(argv, "end=")
    if start is None or end is None or end < start:
        return argv
    return [
        f"start=<epoch-{end - start}s>"
        if argument.startswith("start=")
        else "end=<epoch>"
        if argument.startswith("end=")
        else argument
        for argument in argv
    ]


def _epoch_argument(argv: Sequence[str], prefix: str) -> int | None:
    """The one `<prefix><digits>` argument in `argv`, or None if it is not that."""

    found = [argument for argument in argv if argument.startswith(prefix)]
    if len(found) != 1:
        return None
    value = found[0][len(prefix) :]
    return int(value) if value.isdigit() else None


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
        if VAR_LOG_PAYLOAD.match(name):
            # Which files the payload trees hold is the machine's doing, not the
            # implementation's — see VAR_LOG_PAYLOAD for the boundary.
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
    if contents.is_skip(name):
        return {"kind": "skip", "reason": _classify(text, SKIP_CLASSES, rules)}
    if name == ENVIRONMENT_NAME:
        return {
            "kind": "environment",
            "selection": _fields(text, "=", ENVIRONMENT_SELECTION_KEYS, rules),
        }
    if name == SUMMARY_NAME:
        return {"kind": "summary", "fields": _fields(text, ":", SUMMARY_KEYS, rules)}
    if name == PROMETHEUS_DUMP_INFO_NAME:
        return {
            "kind": "prometheus-dump-info",
            "decisions": _fields(text, "=", PROMETHEUS_DUMP_INFO_KEYS, rules),
        }
    header, body = _split_capture_header(text)
    contract: dict[str, object] = {
        # Whether the captured body parses as JSON *is* the implementation's
        # doing — a candidate that wrapped, truncated or re-serialised evidence
        # shows up here.  What the JSON says is the cluster's doing; the module
        # docstring explains why that is not compared.
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


def _is_skip_name(name: str) -> bool:
    """The artifact names that exist only to record a skip."""

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
