"""Reduce one Collect run to its observable contract.

The normalizer exists so a comparison can be strict.  It removes exactly the
differences the rewrite decision declared non-semantic — timestamps, random
temporary paths, JSON and shell-quoting serialization, tar member order, gzip
metadata and mtimes — and nothing else.  Human-readable failure prose is reduced
to a *documented class* only when the wording is recognised; unknown prose falls
through and is compared literally, so a genuinely new message can never be
mistaken for an approved difference.

`docs/differential-normalizer.md` is the reviewed statement of what this module
is allowed to hide, and every approved rule below points back at it.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import re
import shlex
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from tests.differential.environment import CollectRun


TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
ARCHIVE_STAMP = re.compile(r"ceph-incident-\d{8}T\d{6}Z\.tar\.gz")
# Epoch seconds are only ever normalized inside the metrics layer, where the
# query window and sample timestamps live.  Applying this to all evidence would
# blank real numbers (a byte total, an object count) that happen to look like a
# clock, which is exactly the masking the gate must not do.
EPOCH_SECONDS = re.compile(r"(?<![0-9.])1[0-9]{9}(?![0-9.])")
PROMETHEUS_LAYER = "cluster/prometheus/"
# The query window as it appears in a recorded request: only these named
# parameters are clocks, so the rule cannot reach a number that is evidence.
QUERY_WINDOW_PARAMETER = re.compile(r"\b(start|end|time)=1[0-9]{9}(?:\.[0-9]+)?\b")
NODE_TEMPORARY = re.compile(r"/tmp/ceph-incident-node\.[A-Za-z0-9._-]+")
# A redaction scratch file: `<dir>/.<name>.plain.XXXXXX` names the same artifact
# the other implementation logs directly, so the random component is removed.
REDACTION_SCRATCH = re.compile(r"/\.([^/]+)\.(?:plain|encoded)\.[A-Za-z0-9]{6,}$")

COMPRESSED_SUFFIXES = (".gz", ".xz", ".bz2", ".zst")
OPAQUE_PREFIX = re.compile(r"^nodes/[^/]+/logs/var-log/raw/")
PAYLOAD_PREFIX = re.compile(r"^nodes/([^/]+)/logs/var-log/(?:merged|raw|original)/")

# Candidate-only environment.txt keys: additional observability the rewrite plan
# introduced (#11 node invocation identifiers, #14 Rook namespaces).  They are
# recorded here rather than ignored silently, so an *unlisted* new key fails.
CANDIDATE_ENVIRONMENT_KEYS = re.compile(
    r"^(?:node_target_.+|node_invocation_id_.+|rook_namespace|"
    r"rook_operator_namespace|kube_context)$"
)

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
    (re.compile(r"timed out", re.I), "node-timeout"),
    (re.compile(r"missing manifest|no manifest\.jsonl|incomplete", re.I), "node-archive-missing-manifest"),
    (re.compile(r"no usable node archive|invalid or unreadable archive", re.I), "node-archive-unusable"),
    (re.compile(r"python 3\.11", re.I), "node-python-unsupported"),
    (re.compile(r"skip-logs", re.I), "var-log-skipped"),
    (re.compile(r"not reachable|not responding", re.I), "prometheus-unreachable"),
    (re.compile(r"no scrape job matched", re.I), "prometheus-no-job"),
    (re.compile(r"unable to parse crash list", re.I), "ceph-crash-list-unparsed"),
    (re.compile(r"post-redaction", re.I), "node-log-cap-post-redaction"),
)

ERROR_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"capability probe failed for (?P<target>\S+) \(ssh exit (?P<code>\d+)\)"),
        "capability-probe-failed:{target}:{code}",
    ),
    (re.compile(r"cluster collection exited (?P<code>\d+)"), "cluster-partial:{code}"),
    (re.compile(r"prometheus collection exited (?P<code>\d+)"), "prometheus-partial:{code}"),
    (
        re.compile(r"node (?P<alias>\S+) \((?P<target>[^)]+)\)"),
        "node-partial:{alias}:{target}",
    ),
    (re.compile(r"redaction failed"), "redaction-failed"),
    (
        re.compile(r"node (?P<alias>\S+) log payload exceeded cap"),
        "node-log-cap-exceeded:{alias}",
    ),
    (re.compile(r"bundle verification failed|VERIFY FAIL"), "verify-failed"),
)

STDERR_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"interrupted"), "interrupted"),
    (re.compile(r"VERIFY FAILED"), "verify-failed"),
    (re.compile(r"kept workdir|workdir kept"), "workdir-kept"),
)


class BundleContents:
    """The members of one bundle, whether it is a directory or an archive."""

    def __init__(
        self, names: list[str], payloads: dict[str, bytes], modes: dict[str, int]
    ) -> None:
        self.names = sorted(names)
        self.payloads = payloads
        self.modes = modes

    @classmethod
    def of_archive(cls, path: Path) -> "BundleContents":
        names: list[str] = []
        payloads: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name.removeprefix("./")).as_posix()
                if name in ("", "."):
                    continue
                names.append(name + ("/" if member.isdir() else ""))
                if member.isfile():
                    stream = archive.extractfile(member)
                    payloads[name] = b"" if stream is None else stream.read()
                    modes[name] = member.mode & 0o777
        return cls(names, payloads, modes)

    @classmethod
    def of_directory(cls, path: Path) -> "BundleContents":
        names: list[str] = []
        payloads: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for item in sorted(path.rglob("*")):
            name = item.relative_to(path).as_posix()
            if item.is_dir():
                names.append(name + "/")
            else:
                names.append(name)
                payloads[name] = item.read_bytes()
                modes[name] = item.stat().st_mode & 0o777
        return cls(names, payloads, modes)


def contract_of(run: CollectRun) -> dict[str, object]:
    """The full observable contract of one Collect run, ready to compare."""

    substitutions = _substitutions(run)
    contract: dict[str, object] = {
        "exit_code": run.exit_code,
        "stdout": _normalize(run.stdout, substitutions),
        # Progress prose is human-facing and deliberately not part of the bundle
        # contract; the lifecycle announcements it carries are.  One line may
        # carry several announcements, so this is the set of events, not of lines.
        "stderr_lifecycle": _event_classes(run.stderr, STDERR_CLASSES),
        "archive_produced": run.archive is not None,
        "workdir_retained": run.workdir is not None,
        "node_requests": _node_requests(run),
        "ssh_commands": _ssh_commands(run, substitutions),
        "kubectl_commands": _json_ledger(run, "kubectl-argv.jsonl"),
        "curl_commands": [
            [_normalize(word, substitutions) for word in argv]
            for argv in run.curl_commands
        ],
    }
    source = _bundle_of(run)
    if source is None:
        contract["bundle"] = None
        return contract
    contract["bundle"] = _bundle_contract(source, substitutions)
    return contract


def describe_differences(
    reference: object, candidate: object, path: str = ""
) -> list[str]:
    """Locate every contract difference, so a failure names the drift.

    A whole-contract equality assertion prints thousands of lines and hides which
    field actually moved; this walk reports one line per differing leaf, with the
    two values truncated to something a reviewer can read.
    """

    if type(reference) is not type(candidate):
        return [f"{path or '<contract>'}: type {type(reference).__name__} != {type(candidate).__name__}"]
    if isinstance(reference, dict):
        assert isinstance(candidate, dict)
        differences = []
        for key in sorted(set(reference) | set(candidate), key=str):
            if key not in reference:
                differences.append(f"{path}.{key}: only the candidate has it")
            elif key not in candidate:
                differences.append(f"{path}.{key}: only the reference has it")
            else:
                differences.extend(
                    describe_differences(reference[key], candidate[key], f"{path}.{key}")
                )
        return differences
    if isinstance(reference, list):
        assert isinstance(candidate, list)
        if len(reference) != len(candidate):
            only_reference = [item for item in reference if item not in candidate]
            only_candidate = [item for item in candidate if item not in reference]
            return [
                f"{path}: {len(reference)} entries != {len(candidate)}; "
                f"reference-only={_short(only_reference)} candidate-only={_short(only_candidate)}"
            ]
        return [
            difference
            for index, (left, right) in enumerate(zip(reference, candidate))
            for difference in describe_differences(left, right, f"{path}[{index}]")
        ]
    if reference != candidate:
        if isinstance(reference, str) and "\n" in (reference + str(candidate)):
            return [f"{path}: {_line_difference(reference, str(candidate))}"]
        return [f"{path}: {_short(reference)} != {_short(candidate)}"]
    return []


def _line_difference(reference: str, candidate: str, limit: int = 4) -> str:
    """Name the differing lines of a multi-line artifact, not the whole file."""

    left, right = reference.splitlines(), candidate.splitlines()
    report = [
        f"line {index + 1}: {_short(a, 160)} != {_short(b, 160)}"
        for index, (a, b) in enumerate(zip(left, right))
        if a != b
    ][:limit]
    if len(left) != len(right):
        report.append(f"{len(left)} lines != {len(right)} lines")
    return "; ".join(report) or "trailing content differs"


def _short(value: object, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _bundle_of(run: CollectRun) -> BundleContents | None:
    if run.archive is not None:
        return BundleContents.of_archive(run.archive)
    if run.workdir is not None:
        return BundleContents.of_directory(run.workdir)
    return None


def _bundle_contract(
    source: BundleContents, substitutions: list[tuple[re.Pattern[str], str]]
) -> dict[str, object]:
    compressed_payload_nodes = {
        match.group(1)
        for name in source.payloads
        if (match := PAYLOAD_PREFIX.match(name)) is not None
        and name.endswith(COMPRESSED_SUFFIXES)
    }
    contract: dict[str, object] = {"members": source.names, "artifacts": {}}
    artifacts: dict[str, object] = contract["artifacts"]  # type: ignore[assignment]
    for name in sorted(source.payloads):
        artifacts[name] = _artifact_contract(
            name, source.payloads[name], substitutions, compressed_payload_nodes
        )
    return contract


def _artifact_contract(
    name: str,
    payload: bytes,
    substitutions: list[tuple[re.Pattern[str], str]],
    compressed_payload_nodes: set[str],
) -> object:
    if name == "manifest.jsonl" or re.fullmatch(r"nodes/[^/]+/manifest\.jsonl", name):
        return _manifest_contract(payload, substitutions)
    if name == "redactions.log":
        return _redaction_contract(payload, substitutions)
    if name == "errors.log":
        # The set of classified failure events, not the line sequence: the two
        # implementations record the same failures at different granularity and
        # interleave their own diagnostics.  Every distinct event must still
        # appear on both sides.
        return sorted(
            set(
                _classified_lines(
                    payload.decode("utf-8", "replace"), ERROR_CLASSES, substitutions
                )
            )
        )
    if OPAQUE_PREFIX.match(name):
        # Opaque evidence is a byte-for-byte contract: compare the bytes.
        return {"opaque_bytes": len(payload), "opaque_sha256": _sha256(payload)}
    if name.endswith("PAYLOAD-BYTES.txt"):
        node = name.split("/")[1]
        if node in compressed_payload_nodes:
            # Recompressed payload sizes follow the compressor, not the evidence;
            # the decompressed payload is compared artifact by artifact instead.
            return "<compressor-dependent-bytes>"
        return _normalize(payload.decode("utf-8", "replace"), substitutions)
    if name.endswith(COMPRESSED_SUFFIXES):
        return _artifact_contract(
            name[: name.rfind(".")],
            _decompress(name, payload),
            substitutions,
            compressed_payload_nodes,
        )
    text = payload.decode("utf-8", "replace")
    if name.startswith("ssh-debug/"):
        return _debug_log_contract(text, substitutions)
    if name == "CONTENTS.md":
        return _catalog_contract(text, substitutions)
    if _is_skip_artifact(name):
        return _classified_lines(text, SKIP_CLASSES, substitutions)
    # Only the metrics layer carries a query window and sample clocks.
    metrics_layer = name.startswith(PROMETHEUS_LAYER)
    if metrics_layer:
        substitutions = [*substitutions, (EPOCH_SECONDS, "<epoch>")]
    header, body = _split_capture_header(text)
    contract: dict[str, object] = {}
    if header:
        contract["header"] = header
    if name == "environment.txt":
        contract["fields"] = _environment_fields(body, substitutions)
        return contract
    body_json = _canonical_json(body, normalize_epochs=metrics_layer)
    contract["body"] = (
        body_json if body_json is not None else _normalize(body, substitutions)
    )
    return contract


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
        header[key] = "<timestamp>" if key in ("started", "ended") else value
        index += 1
    return header, "".join(lines[index:])


def _canonical_json(text: str, *, normalize_epochs: bool = False) -> object | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return _without_epochs(parsed) if normalize_epochs else parsed


def _without_epochs(value: object) -> object:
    """Replace sample timestamps inside a parsed metrics dump, wherever they sit.

    A metrics dump carries its query window as numbers, not text, so the string
    timestamp rule cannot reach them; the sample values themselves are still
    compared.  Only the metrics layer is passed through here — elsewhere a number
    in this range is evidence, not a clock.
    """

    if isinstance(value, dict):
        return {key: _without_epochs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_epochs(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and 1_000_000_000 <= value < 10_000_000_000:
        return "<epoch>"
    return value


def _debug_log_contract(
    text: str, substitutions: list[tuple[re.Pattern[str], str]]
) -> list[object]:
    """An ssh-debug log, with its recorded command read as argv."""

    entries: list[object] = []
    for line in text.splitlines():
        if line.startswith("# command: "):
            entries.append(
                {
                    "command": [
                        _normalize(word, substitutions)
                        for word in _bounded_argv(line.removeprefix("# command: "))
                    ]
                }
            )
            continue
        entries.append(_normalize(line, substitutions))
    return entries


CATALOG_ROW = re.compile(r"^\| (?P<exit>\d+) \| `(?P<file>[^`]*)` \| `(?P<command>.*)` \|$")


def _catalog_contract(
    text: str, substitutions: list[tuple[re.Pattern[str], str]]
) -> list[object]:
    """CONTENTS.md line by line, with catalog rows compared as fields.

    The prose and structure are compared verbatim; only the command cell is read
    as argv, because it embeds whichever quoting style its producer used.
    """

    entries: list[object] = []
    for line in text.splitlines():
        match = CATALOG_ROW.match(line)
        if match is None:
            entries.append(_normalize(line, substitutions))
            continue
        entries.append(
            {
                "exit_code": int(match.group("exit")),
                "file": match.group("file"),
                "command": [
                    _normalize(word, substitutions)
                    for word in _argv(match.group("command").replace("\\|", "|"))
                ],
            }
        )
    return entries


def _manifest_contract(
    payload: bytes, substitutions: list[tuple[re.Pattern[str], str]]
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for line in payload.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        entries.append(
            {
                "host": entry["host"],
                "collector": entry["collector"],
                "artifact": _normalize(entry["artifact"], substitutions),
                "command": [
                    _normalize(word, substitutions) for word in _argv(entry["command"])
                ],
                "exit_code": entry["exit_code"],
            }
        )
    return entries


def _redaction_contract(
    payload: bytes, substitutions: list[tuple[re.Pattern[str], str]]
) -> dict[str, str]:
    """redactions.log as a per-file mapping: order follows each tree walk."""

    mapping: dict[str, str] = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        path, _, detail = line.partition(": ")
        mapping[_normalize(path, substitutions)] = detail
    return mapping


def _environment_fields(
    body: str, substitutions: list[tuple[re.Pattern[str], str]]
) -> dict[str, str]:
    """environment.txt as a mapping, minus the candidate's documented additions.

    The candidate records more than the reference (node invocation identifiers,
    Rook namespaces).  Those keys are checked against their documented pattern by
    `candidate_environment_additions`, so they neither fail the comparison nor
    escape review.
    """

    fields: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition("=")
        if not separator or CANDIDATE_ENVIRONMENT_KEYS.fullmatch(key):
            continue
        fields[key] = _normalize(value, substitutions)
    return fields


def environment_keys(run: CollectRun) -> list[str]:
    """Every environment.txt key one run recorded, in file order."""

    source = _bundle_of(run)
    if source is None or "environment.txt" not in source.payloads:
        return []
    return [
        line.partition("=")[0]
        for line in source.payloads["environment.txt"].decode("utf-8", "replace").splitlines()
        if "=" in line
    ]


def candidate_environment_additions(keys: list[str]) -> list[str]:
    return [key for key in keys if CANDIDATE_ENVIRONMENT_KEYS.fullmatch(key)]


def file_modes(run: CollectRun) -> dict[str, int]:
    """Every bundle file's permission mode.

    Compared one-way rather than for equality: the candidate is allowed to be
    stricter than the reference, never looser.  See
    `test_the_candidate_never_widens_a_file_mode`.
    """

    source = _bundle_of(run)
    return dict(sorted(source.modes.items())) if source is not None else {}


def _classified_lines(
    text: str,
    classes: tuple[tuple[re.Pattern[str], str], ...],
    substitutions: list[tuple[re.Pattern[str], str]],
) -> list[str]:
    return [
        _classify(stripped, classes, substitutions)
        for line in text.splitlines()
        if (stripped := line.strip())
    ]


def _event_classes(
    text: str, classes: tuple[tuple[re.Pattern[str], str], ...]
) -> list[str]:
    """Every announcement class the text carries, deduplicated and sorted."""

    found = {
        template
        for pattern, template in classes
        for line in text.splitlines()
        if pattern.search(line)
    }
    return sorted(found)


def _classify(
    line: str,
    classes: tuple[tuple[re.Pattern[str], str], ...],
    substitutions: list[tuple[re.Pattern[str], str]],
) -> str:
    structured = _structured_capture_failure(line, substitutions)
    if structured is not None:
        return structured
    for pattern, template in classes:
        match = pattern.search(line)
        if match is not None:
            return template.format(**{key: value or "" for key, value in match.groupdict().items()})
    return f"literal:{_normalize(line, substitutions)}"


def _structured_capture_failure(
    line: str, substitutions: list[tuple[re.Pattern[str], str]]
) -> str | None:
    """A run_capture failure line: compared field by field, never by prose."""

    match = re.search(
        r"host=(?P<host>\S+) collector=(?P<collector>\S+) artifact=(?P<artifact>\S+) "
        r"exit=(?P<exit>\d+) command=(?P<command>.*)$",
        line,
    )
    if match is None:
        return None
    artifact = _normalize(match.group("artifact"), substitutions)
    command = [_normalize(word, substitutions) for word in _argv(match.group("command"))]
    return (
        f"capture-failed:{match.group('host')}:{match.group('collector')}:"
        f"{artifact}:{match.group('exit')}:{json.dumps(command)}"
    )


def _argv(command: str) -> list[str]:
    """Compare commands as argv, not as one implementation's quoting style."""

    try:
        return shlex.split(command)
    except ValueError:
        return [command]


def _bounded_argv(command: str) -> list[str]:
    """An argv whose bounding wrapper is stripped, keeping the bounded command.

    The reference bounds its verbose SSH probe with `timeout <seconds> …` and
    records the whole line; the candidate bounds the same probe with a subprocess
    deadline, which leaves no token to record.  That mechanism difference is the
    one the rewrite sanctioned (inventory C21), so only the wrapper is dropped
    here — every remaining word is still compared, and this is used solely for
    the ssh-debug command line.
    """

    words = _argv(command)
    if len(words) > 2 and words[0] == "timeout" and words[1].isdecimal():
        return words[2:]
    return words


def _node_requests(run: CollectRun) -> list[dict[str, object]]:
    requests = []
    for line in run.ledgers.get("node-requests.jsonl", []):
        record = json.loads(line)
        record.pop("implementation", None)
        record.pop("payload_sha256", None)
        record.pop("payload_members", None)
        requests.append(record)
    return requests


def _ssh_commands(
    run: CollectRun, substitutions: list[tuple[re.Pattern[str], str]]
) -> list[list[str]]:
    """Every remote command, with the node invocation reduced to its shape.

    The node bootstrap differs by construction — a streamed shell script versus a
    streamed Python module — so it is compared as a labelled shape here and by
    the node request ledger field by field.
    """

    commands: list[list[str]] = []
    for line in run.ledgers.get("ssh-argv.jsonl", []):
        argv = [_normalize(word, substitutions) for word in json.loads(line)]
        commands.append(
            [*argv[:-1], "<node-collector-invocation>"]
            if _is_node_invocation(argv[-1:])
            else argv
        )
    return commands


def _is_node_invocation(tail: list[str]) -> bool:
    return bool(tail) and ("collect-node.sh" in tail[0] or tail[0].startswith("python3 -c "))


def _json_ledger(run: CollectRun, name: str) -> list[object]:
    return [json.loads(line) for line in run.ledgers.get(name, [])]


def _decompress(name: str, payload: bytes) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(payload)
    if name.endswith(".xz"):
        return lzma.decompress(payload)
    if name.endswith(".bz2"):
        return bz2.decompress(payload)
    try:
        return subprocess.run(
            ["zstd", "-qdc"], input=payload, stdout=subprocess.PIPE, check=True
        ).stdout
    except FileNotFoundError as error:  # pragma: no cover - host without zstd
        raise RuntimeError(
            "comparing zstd evidence needs the zstd command on this host"
        ) from error


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _substitutions(run: CollectRun) -> list[tuple[re.Pattern[str], str]]:
    """Volatile-value rules: paths and identifiers this run happened to pick."""

    # One implementation resolves symlinks in its workspace path (macOS
    # /var -> /private/var), so both spellings of the same directory normalize.
    def variants(path: Path) -> str:
        return "(?:/private)?" + re.escape(str(path))

    output_root = variants(run.output_root)
    rules = [
        (re.compile(rf"{output_root}/tmp\.[^\s\"']+"), "<workdir>"),
        (re.compile(output_root), "<out>"),
        (re.compile(variants(run.output_root.parent)), "<runroot>"),
        (NODE_TEMPORARY, "<node-tmp>"),
        (ARCHIVE_STAMP, "ceph-incident-<timestamp>.tar.gz"),
        (TIMESTAMP, "<timestamp>"),
        (QUERY_WINDOW_PARAMETER, r"\1=<epoch>"),
        (REDACTION_SCRATCH, r"/\1"),
    ]
    return rules


def _normalize(text: str, substitutions: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in substitutions:
        text = pattern.sub(replacement, text)
    return text
