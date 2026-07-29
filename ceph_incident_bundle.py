#!/usr/bin/env python3
"""Public entrypoint; the current Verify boundary is documented in the rewrite plan."""

from __future__ import annotations

import gzip
import mmap
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ceph_incident_collectors import (
    PROMETHEUS_DEFAULT_BUDGET_SECONDS,
    PROMETHEUS_DEFAULT_JOB_REGEX,
    CollectionInterrupted,
    PrometheusCollectionResult,
    collect_direct_ceph_cluster,
    collect_prometheus_cluster,
    collect_rook_cluster,
    collect_single_node,
    probe_node_capabilities,
    prometheus_duration_seconds,
    select_ceph_runner,
)


MODES = ("auto", "cephadm", "rook")
KUBE_MODES = ("local", "remote")
# The shell contract's kube-context allowlist: EKS-style ARNs contain @, : and
# /, but nothing here may become a shell metacharacter or an option prefix.
SAFE_KUBE_CONTEXT = re.compile(r"[A-Za-z0-9._@:/-]*\Z")
SAFE_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
SAFE_REMOTE_KUBECTL_SINCE = re.compile(r"[0-9]+[smhdw]?\Z")
# The Prometheus base URL becomes an argv word for curl, so it must be an HTTP
# endpoint that cannot be read as an option or carry a shell/argv surprise.
SAFE_PROMETHEUS_URL = re.compile(r"https?://[^\s\x00-\x1f\x7f]+\Z")
PROMETHEUS_STEP = re.compile(r"[1-9][0-9]*\Z")
PROMETHEUS_BUDGET = re.compile(r"[0-9]+\Z")
DEFAULT_ROOK_NAMESPACE = "rook-ceph"


USAGE = """Usage:
  ceph_incident_bundle.py collect --inventory PATH --ssh-key PATH [options]
    [--seed USER@HOST] [--mode auto|cephadm|rook] [--since RANGE]
    [--skip-logs] [--keep-original-logs]
    [--var-log-max-bytes BYTES|unlimited]
    [--trust-ssh-host-key|--no-trust-ssh-host-key]
    [--kube-mode local|remote] [--kube-context CTX]
    [--prom-url URL] [--prom-job-regex RE] [--prom-step SECONDS]
    [--prom-timeout SECONDS] [--redact|--no-redact]
    [--quiet] [--keep-workdir]
  ceph_incident_bundle.py verify <bundle-dir|bundle.tar.gz>"""
REQUIRED_FILES = ("manifest.jsonl", "summary.txt", "README-FIRST.txt")
REDACTABLE_SUFFIXES = (
    ".txt",
    ".log",
    ".merged",
    ".yaml",
    ".json",
    ".jsonl",
    ".conf",
    ".gz",
    ".xz",
    ".bz2",
    ".zst",
)
COMPRESSED_CODECS = {
    ".gz": (("gzip", "-dc", "--"), ("gzip", "-c", "--")),
    ".xz": (("xz", "-dc", "--"), ("xz", "-c", "--")),
    ".bz2": (("bzip2", "-dc", "--"), ("bzip2", "-c", "--")),
    ".zst": (("zstd", "-qdc", "--"), ("zstd", "-q", "-c", "--")),
}
PEM_BEGIN = re.compile(
    rb"-----BEGIN[\t\v\f\r ]+.*PRIVATE[\t\v\f\r ]+KEY-----", re.IGNORECASE
)
PEM_END = re.compile(
    rb"-----END[\t\v\f\r ]+.*PRIVATE[\t\v\f\r ]+KEY-----", re.IGNORECASE
)
SENSITIVE_LINE = re.compile(
    rb"password|secret|token|keyring|private(?:[\t\v\f\r _-]+)?key",
    re.IGNORECASE,
)
CEPH_KEY_LABEL = re.compile(
    rb"(?:^|[^A-Za-z0-9])key[\t\v\f\r ]*[:=]", re.IGNORECASE
)
BASE64_SECRET = re.compile(rb"[A-Za-z0-9+/]{38,}={1,2}")
PRIVATE_KEY_CONTENT = re.compile(
    rb"-----BEGIN[ A-Za-z]*PRIVATE KEY-----"
)
CEPH_KEY_CONTENT = re.compile(
    rb"(?:^|\n)[\t\v\f\r ]*key[\t\v\f\r ]*=[\t\v\f\r ]*"
    rb"[A-Za-z0-9+/]{20,}={0,2}"
)
FINAL_BUNDLE_SAFETY_CEILING_BYTES = 1024**4
REDACTION_DECODE_SAFETY_CEILING_BYTES = 10 * 1024**3
SAFE_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SAFE_SSH_USER = re.compile(r"[A-Za-z0-9._%+-]+\Z")
SAFE_SSH_TARGET = re.compile(
    r"(?:[A-Za-z0-9._%+-]+@)?(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._:-]+)\Z"
)
SCALAR_ASSIGNMENT = re.compile(
    r'\s*(SSH_USER|SEED_HOST|ROOK_NAMESPACE|ROOK_OPERATOR_NAMESPACE)="([^"]*)"\s*(?:#.*)?\Z'
)
HOST_ENTRY = re.compile(r'\s*"([^"]+)"\s*(?:#.*)?\Z')


class VerificationError(Exception):
    """An incident bundle failed structural verification."""


class CollectUsageError(Exception):
    """The public collect invocation or inventory is invalid."""


def _structural_payload_cap() -> int:
    test_cap = os.environ.get("CEPH_INCIDENT_TEST_BUNDLE_SAFETY_CAP_BYTES", "")
    if test_cap.isdecimal():
        return min(FINAL_BUNDLE_SAFETY_CEILING_BYTES, int(test_cap))
    return FINAL_BUNDLE_SAFETY_CEILING_BYTES


def _redaction_decode_cap(source: Path) -> int:
    test_cap = os.environ.get("CEPH_INCIDENT_TEST_REDACTION_DECODE_CAP_BYTES", "")
    policy_cap = REDACTION_DECODE_SAFETY_CEILING_BYTES
    if test_cap.isdecimal():
        policy_cap = min(policy_cap, int(test_cap))
    # Decoding, the atomic redaction rewrite, and recompression can each need
    # one file-sized allocation in sequence.  Keep headroom for the rewrite.
    available_cap = shutil.disk_usage(source.parent).free // 3
    return min(policy_cap, available_cap)


def _positive_integer(value: str, option: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise CollectUsageError(f"{option} must be a positive integer") from error
    if parsed <= 0:
        raise CollectUsageError(f"{option} must be a positive integer")
    return parsed


def _parse_collect_arguments(arguments: Sequence[str]) -> dict[str, object]:
    values: dict[str, object] = {
        "out": Path(__file__).resolve().parent / "results",
        "timeout": 20,
        "node_timeout": 300,
        "trust_ssh_host_key": True,
        "redact": True,
        "skip_logs": False,
        "keep_original_logs": False,
        "keep_workdir": False,
        "quiet": False,
        "var_log_max_bytes": 10 * 1024**3,
        "since": "24h",
        "mode": "auto",
        "kube_mode": "remote",
        "prom_url": "",
        "prom_job_regex": PROMETHEUS_DEFAULT_JOB_REGEX,
        "prom_step": "",
        "prom_timeout": PROMETHEUS_DEFAULT_BUDGET_SECONDS,
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("--help", "-h"):
            values["help"] = True
            index += 1
            continue
        if argument in ("--trust-ssh-host-key", "--no-trust-ssh-host-key"):
            values["trust_ssh_host_key"] = argument == "--trust-ssh-host-key"
            index += 1
            continue
        if argument in ("--redact", "--no-redact"):
            values["redact"] = argument == "--redact"
            index += 1
            continue
        if argument in (
            "--skip-logs",
            "--keep-original-logs",
            "--keep-workdir",
            "--quiet",
        ):
            values[argument.removeprefix("--").replace("-", "_")] = True
            index += 1
            continue
        option_names = {
            "--inventory": "inventory",
            "--ssh-key": "ssh_key",
            "--out": "out",
            "--seed": "seed",
            "--mode": "mode",
            "--timeout": "timeout",
            "--node-timeout": "node_timeout",
            "--var-log-max-bytes": "var_log_max_bytes",
            "--since": "since",
            "--kube-mode": "kube_mode",
            "--kube-context": "kube_context",
            "--prom-url": "prom_url",
            "--prom-job-regex": "prom_job_regex",
            "--prom-step": "prom_step",
            "--prom-timeout": "prom_timeout",
        }
        key = option_names.get(argument)
        if key is None:
            raise CollectUsageError(f"unknown collect option: {argument}")
        if index + 1 >= len(arguments):
            raise CollectUsageError(f"missing value for {argument}")
        raw_value = arguments[index + 1]
        if key in ("inventory", "ssh_key", "out"):
            values[key] = Path(raw_value)
        elif key == "var_log_max_bytes":
            if raw_value == "unlimited":
                values[key] = raw_value
            elif raw_value.isdecimal():
                values[key] = int(raw_value)
            else:
                raise CollectUsageError(
                    "--var-log-max-bytes must be a non-negative integer or unlimited"
                )
        elif key == "since":
            if not raw_value:
                raise CollectUsageError("--since must not be empty")
            values[key] = raw_value
        elif key == "seed":
            values[key] = _validated_ssh_target(raw_value, "invalid SSH seed target")
        elif key == "mode":
            if raw_value not in MODES:
                raise CollectUsageError(f"unsupported mode: {raw_value}")
            values[key] = raw_value
        elif key == "kube_mode":
            if raw_value not in KUBE_MODES:
                raise CollectUsageError(f"unsupported kube-mode: {raw_value}")
            values[key] = raw_value
        elif key in ("prom_url", "prom_job_regex", "prom_step", "prom_timeout"):
            # Prometheus values are validated below, and only when the layer is
            # actually enabled, exactly as the shell contract does.
            values[key] = raw_value
        elif key == "kube_context":
            if SAFE_KUBE_CONTEXT.fullmatch(raw_value) is None:
                raise CollectUsageError(
                    "--kube-context may only contain A-Za-z0-9._@:/-"
                )
            values[key] = raw_value
        else:
            values[key] = _positive_integer(raw_value, argument)
        index += 2
    if (
        values.get("mode") in ("auto", "rook")
        and values.get("kube_mode") == "remote"
    ):
        since = str(values["since"])
        duration = since[:-1] if since[-1:] in "smhdw" else since
        if (
            SAFE_REMOTE_KUBECTL_SINCE.fullmatch(since) is None
            or not duration.strip("0")
        ):
            raise CollectUsageError(
                "remote Rook --since must be N, Ns, Nm, Nh, Nd, or Nw "
                "with N greater than 0"
            )
    if values["prom_url"]:
        _validate_prometheus_options(values)
    if values.get("help"):
        return values
    for required in ("inventory", "ssh_key"):
        if required not in values:
            raise CollectUsageError(f"missing required option: --{required.replace('_', '-')}")
    return values


def _validate_prometheus_options(values: dict[str, object]) -> None:
    """Check the metrics-dump options, only ever when the dump is enabled.

    The shell contract leaves these unchecked without a --prom-url, so an unused
    value stays harmless; an enabled dump fails closed before any request.
    """

    url = str(values["prom_url"])
    try:
        parsed_url = urlsplit(url)
        # Accessing .port also validates numeric syntax and the 0..65535 range.
        _ = parsed_url.port
    except ValueError:
        parsed_url = None
    if (
        SAFE_PROMETHEUS_URL.fullmatch(url) is None
        or parsed_url is None
        or parsed_url.scheme not in ("http", "https")
        or not parsed_url.netloc
        or parsed_url.hostname is None
        or parsed_url.query
        or parsed_url.fragment
        or "?" in url
        or "#" in url
    ):
        # Do not echo an invalid value: it may still contain basic-auth
        # credentials even though it cannot be used as an endpoint.
        raise CollectUsageError("--prom-url must be an http(s) URL with a host")
    since = str(values["since"])
    if prometheus_duration_seconds(since) is None:
        raise CollectUsageError(
            f"--since must be N/Ns/Nm/Nh/Nd/Nw when using --prom-url: {since}"
        )
    step = str(values["prom_step"])
    if step and PROMETHEUS_STEP.fullmatch(step) is None:
        raise CollectUsageError(f"invalid --prom-step (positive seconds): {step}")
    budget = str(values["prom_timeout"])
    if PROMETHEUS_BUDGET.fullmatch(budget) is None:
        raise CollectUsageError(f"invalid --prom-timeout (seconds): {budget}")
    values["prom_timeout"] = int(budget)


def _validated_ssh_target(value: str, message: str) -> str:
    if not value or value.startswith("-") or SAFE_SSH_TARGET.fullmatch(value) is None:
        raise CollectUsageError(message)
    return value


def _ssh_target_for_host(host: str, ssh_user: str) -> str:
    return host if "@" in host or not ssh_user else f"{ssh_user}@{host}"


@dataclass(frozen=True)
class InventoryNode:
    host_alias: str
    target: str


@dataclass(frozen=True)
class Inventory:
    nodes: tuple[InventoryNode, ...]
    seed: str
    rook_namespace: str
    rook_operator_namespace: str
    rejected_entries: tuple[str, ...]


def _validated_namespace(value: str, option: str) -> str:
    if SAFE_NAMESPACE.fullmatch(value) is None:
        raise CollectUsageError(f"invalid {option}: {value}")
    return value


def _read_inventory(path: Path) -> Inventory:
    if not path.is_file():
        raise CollectUsageError(f"missing inventory: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CollectUsageError(f"cannot read inventory: {path}") from error
    ssh_user = ""
    seed_host = ""
    rook_namespace = ""
    rook_operator_namespace = ""
    hosts: list[str] = []
    in_hosts = False
    hosts_closed = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "$(" in line or "${" in line or "`" in line:
            raise CollectUsageError("inventory contains forbidden shell expression")
        if in_hosts:
            if re.fullmatch(r"\s*\)\s*(?:#.*)?", line):
                in_hosts = False
                hosts_closed = True
                continue
            match = HOST_ENTRY.fullmatch(line)
            if match is None:
                raise CollectUsageError("inventory contains an invalid HOSTS entry")
            hosts.append(match.group(1))
            continue
        if re.fullmatch(r"\s*HOSTS=\(\s*\)\s*(?:#.*)?", line):
            hosts_closed = True
            continue
        if re.fullmatch(r"\s*HOSTS=\(\s*(?:#.*)?", line):
            if hosts_closed:
                raise CollectUsageError("inventory contains multiple HOSTS blocks")
            in_hosts = True
            continue
        assignment = SCALAR_ASSIGNMENT.fullmatch(line)
        if assignment is None:
            raise CollectUsageError(
                "inventory must contain only supported quoted assignments and HOSTS entries"
            )
        if assignment.group(1) == "SSH_USER":
            ssh_user = assignment.group(2)
        elif assignment.group(1) == "SEED_HOST":
            seed_host = assignment.group(2)
        elif assignment.group(1) == "ROOK_NAMESPACE":
            rook_namespace = assignment.group(2)
        elif assignment.group(1) == "ROOK_OPERATOR_NAMESPACE":
            rook_operator_namespace = assignment.group(2)
    if in_hosts:
        raise CollectUsageError("inventory HOSTS block is not closed")
    if not hosts:
        raise CollectUsageError("inventory HOSTS is empty")
    if ssh_user and (
        ssh_user.startswith("-") or SAFE_SSH_USER.fullmatch(ssh_user) is None
    ):
        raise CollectUsageError("invalid SSH_USER")
    nodes: list[InventoryNode] = []
    rejected_entries: list[str] = []
    for entry in hosts:
        if "=" not in entry:
            rejected_entries.append(f"skipped malformed HOSTS entry: {entry}")
            continue
        alias, host = entry.split("=", 1)
        if not alias or not host:
            rejected_entries.append(f"skipped malformed HOSTS entry: {entry}")
            continue
        if alias in (".", "..") or SAFE_ALIAS.fullmatch(alias) is None:
            rejected_entries.append(f"skipped unsafe host alias: {alias}")
            continue
        try:
            target = _validated_ssh_target(
                _ssh_target_for_host(host, ssh_user), "invalid SSH target"
            )
        except CollectUsageError:
            rejected_entries.append(f"skipped unsafe SSH target for alias {alias}")
            continue
        nodes.append(InventoryNode(alias, target))
    if not nodes:
        raise CollectUsageError("inventory contains no valid HOSTS entries")
    seed = (
        _validated_ssh_target(
            _ssh_target_for_host(seed_host, ssh_user), "invalid SSH seed target"
        )
        if seed_host
        else ""
    )
    # An empty assignment falls back to the default, as in the shell contract.
    namespace = _validated_namespace(
        rook_namespace or DEFAULT_ROOK_NAMESPACE, "ROOK_NAMESPACE"
    )
    operator_namespace = _validated_namespace(
        rook_operator_namespace or DEFAULT_ROOK_NAMESPACE, "ROOK_OPERATOR_NAMESPACE"
    )
    return Inventory(
        tuple(nodes), seed, namespace, operator_namespace, tuple(rejected_entries)
    )


def _write_initial_bundle_files(workdir: Path) -> None:
    (workdir / "cluster").mkdir(mode=0o700)
    (workdir / "nodes").mkdir(mode=0o700)
    (workdir / "README-FIRST.txt").write_text(
        "Operationally read-only incident evidence. Review errors.log and summary.txt.\n",
        encoding="utf-8",
    )
    (workdir / "manifest.jsonl").touch(mode=0o600)
    (workdir / "errors.log").touch(mode=0o600)


def _write_cluster_skip_once(workdir: Path, layer: str, reason: str) -> None:
    destination = workdir / "cluster" / layer / "SKIPPED.txt"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text(f"SKIPPED: {reason}\n", encoding="utf-8")


def _is_redaction_target(workdir: Path, path: Path) -> bool:
    relative = path.relative_to(workdir)
    parts = relative.parts
    if (
        len(parts) >= 5
        and parts[0] == "nodes"
        and parts[2:5] == ("logs", "var-log", "raw")
    ):
        return False
    if (
        len(parts) == 4
        and parts[:2] == ("cluster", "prometheus")
        and path.name.endswith(".json.gz")
    ):
        return False
    name = path.name
    return (
        name == "config"
        or ".log." in name
        or any(name.endswith(suffix) for suffix in REDACTABLE_SUFFIXES)
    )


def _redact_plain_file(
    source: Path, redaction_log: Path, *, display_path: Path | None = None
) -> None:
    mode = source.stat().st_mode & 0o7777
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.", dir=source.parent
    )
    temporary = Path(temporary_name)
    count = 0
    in_pem = False
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            size = os.fstat(input_file.fileno()).st_size
            if size:
                with mmap.mmap(
                    input_file.fileno(), length=0, access=mmap.ACCESS_READ
                ) as mapped:
                    start = 0
                    while start < size:
                        newline = mapped.find(b"\n", start)
                        content_end = size if newline < 0 else newline
                        line_end = size if newline < 0 else newline + 1
                        redact_line = False
                        if PEM_BEGIN.search(mapped, start, content_end):
                            in_pem = True
                        if in_pem:
                            redact_line = True
                            if PEM_END.search(mapped, start, content_end):
                                in_pem = False
                        elif (
                            SENSITIVE_LINE.search(mapped, start, content_end)
                            or CEPH_KEY_LABEL.search(mapped, start, content_end)
                            or BASE64_SECRET.search(mapped, start, content_end)
                        ):
                            redact_line = True
                        if redact_line:
                            output.write(b"[REDACTED]\n")
                            count += 1
                        else:
                            offset = start
                            while offset < line_end:
                                chunk_end = min(offset + 64 * 1024, line_end)
                                output.write(mapped[offset:chunk_end])
                                offset = chunk_end
                            if newline < 0:
                                output.write(b"\n")
                        start = line_end
        os.chmod(temporary, mode)
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)
    with redaction_log.open("a", encoding="utf-8") as output:
        output.write(f"{display_path or source}: {count} line(s) redacted\n")


def _redact_compressed_file(source: Path, redaction_log: Path) -> bool:
    decoder, encoder = COMPRESSED_CODECS[source.suffix]
    mode = source.stat().st_mode & 0o7777
    plain_descriptor, plain_name = tempfile.mkstemp(
        prefix=f".{source.name}.plain.", dir=source.parent
    )
    encoded_descriptor, encoded_name = tempfile.mkstemp(
        prefix=f".{source.name}.encoded.", dir=source.parent
    )
    os.close(plain_descriptor)
    os.close(encoded_descriptor)
    plain = Path(plain_name)
    encoded = Path(encoded_name)
    try:
        decoded_returncode: int | None = None
        decode_over_cap = False
        decoder_process: subprocess.Popen[bytes] | None = None
        decode_cap = _redaction_decode_cap(source)
        try:
            with plain.open("wb") as output:
                decoder_process = subprocess.Popen(
                    [*decoder, str(source)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if decoder_process.stdout is None:
                    raise OSError("decoder stdout pipe was not created")
                decoded_bytes = 0
                while True:
                    chunk = decoder_process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    decoded_bytes += len(chunk)
                    if decoded_bytes > decode_cap:
                        decode_over_cap = True
                        decoder_process.kill()
                        break
                    output.write(chunk)
                decoder_process.stdout.close()
                decoded_returncode = decoder_process.wait()
        except OSError:
            decoded_returncode = None
        finally:
            if decoder_process is not None:
                if decoder_process.stdout is not None:
                    decoder_process.stdout.close()
                if decoder_process.poll() is None:
                    decoder_process.kill()
                    decoder_process.wait()
        if decode_over_cap:
            with redaction_log.open("a", encoding="utf-8") as output:
                output.write(
                    f"{source}: decompressed payload exceeds safety cap, "
                    "original left as-is (NOT redacted)\n"
                )
            return False
        if decoded_returncode is None or decoded_returncode != 0:
            with redaction_log.open("a", encoding="utf-8") as output:
                output.write(
                    f"{source}: {source.suffix.removeprefix('.')} decompress failed, "
                    "left as-is (NOT redacted)\n"
                )
            return True

        _redact_plain_file(plain, redaction_log, display_path=source)
        try:
            with encoded.open("wb") as output:
                encoded_result = subprocess.run(
                    [*encoder, str(plain)],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        except OSError:
            encoded_result = None
        if encoded_result is None or encoded_result.returncode != 0:
            with redaction_log.open("a", encoding="utf-8") as output:
                output.write(
                    f"{source}: {source.suffix.removeprefix('.')} recompress failed, "
                    "original left as-is (NOT redacted)\n"
                )
            return False
        os.chmod(encoded, mode)
        os.replace(encoded, source)
        return True
    finally:
        plain.unlink(missing_ok=True)
        encoded.unlink(missing_ok=True)


def _redact_bundle_text(workdir: Path) -> bool:
    redaction_log = workdir / "redactions.log"
    redaction_log.touch(mode=0o600)
    complete = True
    for root_name in ("cluster", "nodes"):
        for root, directories, filenames in os.walk(workdir / root_name):
            directories[:] = sorted(directories)
            for filename in sorted(filenames):
                path = Path(root, filename)
                if path.is_symlink() or not _is_redaction_target(workdir, path):
                    continue
                if path.suffix in COMPRESSED_CODECS:
                    if not _redact_compressed_file(path, redaction_log):
                        complete = False
                else:
                    _redact_plain_file(path, redaction_log)
    return complete


def _forbidden_content_path(name: str) -> bool:
    return (
        "keyring" in name
        or ".ssh" in name
        or "id_ed25519" in name
        or "private_key" in name
        or name.endswith((".pem", ".key", ".crt", ".pfx", ".p12"))
    )


def _scan_ceph_key_prefix(
    chunk: bytes, state: int, base64_count: int
) -> tuple[int, int, bool]:
    """Scan the line-anchored Ceph key regex without buffering whole lines."""

    whitespace = b" \t\v\f\r"
    base64_alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    position = 0
    while position < len(chunk):
        if state < 0:
            newline = chunk.find(b"\n", position)
            if newline < 0:
                break
            state = 0
            base64_count = 0
            position = newline + 1
            continue
        byte = chunk[position]
        position += 1
        if byte == ord("\n"):
            state = 0
            base64_count = 0
        elif state == 0:
            if byte in whitespace:
                continue
            state = 1 if byte == ord("k") else -1
        elif state == 1:
            state = 2 if byte == ord("e") else -1
        elif state == 2:
            state = 3 if byte == ord("y") else -1
        elif state == 3:
            if byte in whitespace:
                continue
            state = 4 if byte == ord("=") else -1
        elif state == 4:
            if byte in whitespace:
                continue
            if byte in base64_alphabet:
                state = 5
                base64_count = 1
            else:
                state = -1
        else:
            if byte not in base64_alphabet:
                state = -1
                continue
            base64_count += 1
            if base64_count >= 20:
                return state, base64_count, True
    return state, base64_count, False


def _scan_private_key_marker(
    chunk: bytes, active: bool, begin_tail: bytes, target_progress: int
) -> tuple[bool, bytes, int, bool]:
    """Scan the unbounded shell private-key regex with bounded state."""

    begin = b"-----BEGIN"
    target = b"PRIVATE KEY-----"
    allowed = b" ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    position = 0
    while position < len(chunk):
        newline = chunk.find(b"\n", position)
        line_end = len(chunk) if newline < 0 else newline
        while position < line_end:
            if active:
                byte = chunk[position]
                position += 1
                if byte == target[target_progress]:
                    target_progress += 1
                    if target_progress == len(target):
                        return False, b"", 0, True
                elif byte == target[0]:
                    target_progress = 1
                else:
                    target_progress = 0
                if byte not in allowed and not (
                    byte == ord("-") and target_progress > 0
                ):
                    active = False
                    begin_tail = b"-" if byte == ord("-") else b""
                continue

            if begin_tail:
                boundary_end = min(line_end, position + len(begin))
                boundary = begin_tail + chunk[position:boundary_end]
                boundary_match = boundary.find(begin)
                if 0 <= boundary_match < len(begin_tail):
                    consumed = boundary_match + len(begin) - len(begin_tail)
                    position += consumed
                    active = True
                    begin_tail = b""
                    target_progress = 0
                    continue
            begin_at = chunk.find(begin, position, line_end)
            if begin_at < 0:
                begin_tail = (begin_tail + chunk[position:line_end])[-(len(begin) - 1) :]
                position = line_end
                break
            position = begin_at + len(begin)
            active = True
            begin_tail = b""
            target_progress = 0

        if newline < 0:
            break
        active = False
        begin_tail = b""
        target_progress = 0
        position = newline + 1
    return active, begin_tail, target_progress, False


def _scan_secret_stream(evidence: object, name: str) -> None:
    tail = b""
    secret_found = False
    binary = False
    ceph_state = 0
    ceph_base64_count = 0
    private_active = False
    private_begin_tail = b""
    private_target_progress = 0
    while True:
        chunk = evidence.read(1024 * 1024)
        if not chunk:
            break
        binary = binary or b"\0" in chunk
        window = tail + chunk
        ceph_state, ceph_base64_count, ceph_key_found = _scan_ceph_key_prefix(
            chunk, ceph_state, ceph_base64_count
        )
        (
            private_active,
            private_begin_tail,
            private_target_progress,
            private_key_found,
        ) = _scan_private_key_marker(
            chunk,
            private_active,
            private_begin_tail,
            private_target_progress,
        )
        if (
            PRIVATE_KEY_CONTENT.search(window)
            or CEPH_KEY_CONTENT.search(window)
            or ceph_key_found
            or private_key_found
        ):
            secret_found = True
        tail = window[-512:]
    if secret_found and not binary:
        raise VerificationError(f"unredacted PRIVATE KEY / key material in: {name}")


def _verify_content_safety(target: Path) -> None:
    try:
        if target.is_dir():
            for root, directories, filenames in os.walk(
                target, topdown=True, onerror=_raise_walk_error
            ):
                for name in (*directories, *filenames):
                    relative = Path(root, name).relative_to(target).as_posix()
                    if _forbidden_content_path(relative):
                        raise VerificationError(f"forbidden path: {relative}")
                for filename in filenames:
                    path = Path(root, filename)
                    if path.is_symlink() or not path.is_file():
                        continue
                    with path.open("rb") as evidence:
                        _scan_secret_stream(
                            evidence, path.relative_to(target).as_posix()
                        )
            return

        with tarfile.open(target, mode="r:gz") as archive:
            for member in archive.getmembers():
                normalised = _normalise_member_name(member.name)
                if _forbidden_content_path(normalised):
                    raise VerificationError(f"forbidden path: {normalised}")
                if not member.isfile():
                    continue
                evidence = archive.extractfile(member)
                if evidence is None:
                    raise VerificationError(f"cannot read archive member: {member.name}")
                with evidence:
                    _scan_secret_stream(evidence, normalised)
    except VerificationError:
        raise
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        raise VerificationError(f"cannot scan bundle content: {target}") from error


def _run_content_safety(target: Path, *, redact: bool | None) -> bool:
    """Temporary cutover policy behind one removable public-entrypoint seam."""

    complete = True
    if redact is True:
        if not target.is_dir():
            raise VerificationError("redaction requires a bundle workdir")
        complete = _redact_bundle_text(target)
    elif redact is False and target.is_dir():
        (target / "redactions.log").touch(mode=0o600)
    else:
        _verify_content_safety(target)
    return complete


def _enforce_node_log_caps(workdir: Path, max_bytes: int | str) -> bool:
    """Recheck the durable node-log payload after the content-safety phase."""

    if max_bytes == "unlimited":
        return True
    assert isinstance(max_bytes, int)
    complete = True
    nodes_root = workdir / "nodes"
    for node_dir in sorted(nodes_root.iterdir()):
        if node_dir.is_symlink() or not node_dir.is_dir():
            continue
        var_log = node_dir / "logs" / "var-log"
        if not var_log.is_dir() or var_log.is_symlink():
            continue
        total = 0
        for payload_root in ("merged", "raw", "original"):
            root = var_log / payload_root
            if not root.exists():
                continue
            if root.is_symlink():
                raise VerificationError("symlink is not allowed in node log payload")
            if root.is_file():
                total += root.stat().st_size
                continue
            if not root.is_dir():
                raise VerificationError(
                    "non-regular node log payload is not allowed"
                )
            for current_root, directories, filenames in os.walk(root):
                for directory in directories:
                    if Path(current_root, directory).is_symlink():
                        raise VerificationError(
                            "symlink is not allowed in node log payload"
                        )
                for filename in filenames:
                    path = Path(current_root, filename)
                    if path.is_symlink() or not path.is_file():
                        raise VerificationError(
                            "non-regular node log payload is not allowed"
                        )
                    total += path.stat().st_size
        journal = var_log / "journal-all-since.txt"
        payload_marker = var_log / "PAYLOAD-BYTES.txt"
        over_limit_marker = var_log / "OVER-LIMIT.txt"
        for writable_path in (journal, payload_marker, over_limit_marker):
            if writable_path.is_symlink():
                raise VerificationError(
                    f"symlink is not allowed in node log payload: {writable_path.name}"
                )
        if journal.is_file():
            total += journal.stat().st_size
        if total > max_bytes:
            for payload_root in ("merged", "raw", "original"):
                path = var_log / payload_root
                if path.is_symlink():
                    raise VerificationError(
                        "symlink is not allowed in node log payload"
                    )
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()
                elif path.exists():
                    raise VerificationError(
                        "non-regular node log payload is not allowed"
                    )
            journal.write_text(
                "SKIPPED: not collected because post-redaction node log payload "
                "exceeded the per-node cap\n",
                encoding="utf-8",
            )
            payload_marker.unlink(missing_ok=True)
            over_limit_marker.write_text(
                f"actual_payload_bytes={total}\n"
                f"max_bytes={max_bytes}\n"
                "status=not-collected-post-redaction-cap\n",
                encoding="utf-8",
            )
            with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                errors.write(
                    f"node {node_dir.name} log payload exceeded cap after redaction\n"
                )
            complete = False
        elif payload_marker.is_file():
            payload_marker.write_text(f"{total}\n", encoding="utf-8")
    return complete


def _verify_structural_bundle_path(target: Path) -> None:
    if target.is_dir():
        _verify_directory(target)
    else:
        files = _read_archive(target)
        _verify_file_set(files)


def _verify_bundle_path(target: Path) -> None:
    if target.is_symlink():
        raise VerificationError(f"symlink is not allowed as bundle target: {target}")
    if target.is_dir():
        _verify_structural_bundle_path(target)
        _run_content_safety(target, redact=None)
        return

    cap = _structural_payload_cap()
    with tempfile.TemporaryDirectory(prefix="ceph-incident-verify.") as temporary:
        snapshot = Path(temporary) / "bundle.tar.gz"
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags)
        except OSError as error:
            raise VerificationError(f"cannot read archive: {target}") from error
        copied = 0
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationError(f"expected a regular archive: {target}")
            with os.fdopen(descriptor, "rb", closefd=False) as source, snapshot.open(
                "xb"
            ) as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > cap:
                        raise VerificationError(
                            "archive exceeds structural payload cap"
                        )
                    destination.write(chunk)
        finally:
            os.close(descriptor)
        _verify_structural_bundle_path(snapshot)
        _run_content_safety(snapshot, redact=None)


def _reserve_archive_path(output_root: Path, timestamp: str) -> Path:
    for suffix in ("", *(f"-{index}" for index in range(1, 1000))):
        archive = output_root / f"ceph-incident-{timestamp}{suffix}.tar.gz"
        try:
            descriptor = os.open(
                archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            continue
        os.close(descriptor)
        return archive
    raise OSError("cannot reserve a unique incident bundle output path")


def _rewrite_summary_final_status(workdir: Path, final_status: int) -> None:
    """Make a retained summary describe the fatal workstation result."""

    summary = workdir / "summary.txt"
    if not summary.is_file():
        return
    lines = summary.read_text(encoding="utf-8").splitlines()
    replacement = f"final_status={final_status}"
    rewritten = [
        replacement if line.startswith("final_status=") else line for line in lines
    ]
    if not any(line.startswith("final_status=") for line in lines):
        rewritten.append(replacement)
    temporary = workdir / ".summary.txt.tmp"
    temporary.write_text(
        "".join(f"{line}\n" for line in rewritten), encoding="utf-8"
    )
    os.replace(temporary, summary)


def _collect(arguments: Sequence[str]) -> int:
    try:
        options = _parse_collect_arguments(arguments)
        if options.get("help"):
            print(USAGE)
            return 0
        inventory = options["inventory"]
        ssh_key = options["ssh_key"]
        output_root = options["out"]
        assert isinstance(inventory, Path)
        assert isinstance(ssh_key, Path)
        assert isinstance(output_root, Path)
        if not ssh_key.is_file():
            raise CollectUsageError(f"missing ssh key: {ssh_key}")
        inventory_data = _read_inventory(inventory)
        # --seed overrides the inventory SEED_HOST, as in the shell contract.
        ceph_seed = str(options.get("seed") or inventory_data.seed)
        mode = str(options["mode"])
        kube_mode = str(options["kube_mode"])
        kube_context = str(options.get("kube_context") or "")
        prom_url = str(options["prom_url"])
    except CollectUsageError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1

    try:
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="tmp.", dir=output_root)).resolve(
            strict=True
        )
    except OSError as error:
        print(f"FATAL: cannot create collector-owned workspace: {error}", file=sys.stderr)
        return 1
    archive: Path | None = None
    packaged_candidate: Path | None = None
    retain_workdir = bool(options["keep_workdir"])
    previous_term_handler = signal.getsignal(signal.SIGTERM)

    def progress(message: str) -> None:
        if not options["quiet"]:
            print(message, file=sys.stderr)

    def interrupt_handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_handler)
    try:
        _write_initial_bundle_files(workdir)
        progress(f"starting: mode={mode}, {len(inventory_data.nodes)} hosts")
        for rejection in inventory_data.rejected_entries:
            with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                errors.write(f"{rejection}\n")
        known_hosts: Path | None = None
        if options["trust_ssh_host_key"]:
            known_hosts = workdir / ".runtime-known-hosts"
            known_hosts.touch(mode=0o600)
        want_ceph = mode in ("auto", "cephadm")
        want_rook = mode in ("auto", "rook")
        ceph_source = ceph_seed if want_ceph else ""
        ceph_runner: str | None = None
        rook_source = "local" if want_rook and kube_mode == "local" else ""

        if want_ceph and ceph_source:
            progress(f"probing Ceph runner on {ceph_source}")
            ceph_runner = select_ceph_runner(
                workdir=workdir,
                target=ceph_source,
                ssh_key=ssh_key,
                connection_timeout=int(options["timeout"]),
                known_hosts_file=known_hosts,
            )

        need_capabilities = (want_ceph and not ceph_source) or (
            want_rook and kube_mode == "remote"
        )
        if need_capabilities:
            progress(
                f"probing {len(inventory_data.nodes)} nodes for cluster capabilities"
            )
            for inventory_node in inventory_data.nodes:
                capabilities = probe_node_capabilities(
                    workdir=workdir,
                    target=inventory_node.target,
                    ssh_key=ssh_key,
                    connection_timeout=int(options["timeout"]),
                    known_hosts_file=known_hosts,
                )
                if want_ceph and not ceph_source and capabilities.intersection(
                    {"ceph", "cephadm"}
                ):
                    selected = select_ceph_runner(
                        workdir=workdir,
                        target=inventory_node.target,
                        ssh_key=ssh_key,
                        connection_timeout=int(options["timeout"]),
                        known_hosts_file=known_hosts,
                    )
                    if selected is not None:
                        ceph_source = inventory_node.target
                        ceph_runner = selected
                if (
                    want_rook
                    and kube_mode == "remote"
                    and not rook_source
                    and "kubectl" in capabilities
                ):
                    rook_source = inventory_node.target
                if (not want_ceph or bool(ceph_source)) and (
                    not want_rook or kube_mode == "local" or bool(rook_source)
                ):
                    break

        ceph_status: int | None = None
        ceph_done = False
        if want_ceph and ceph_source and ceph_runner:
            progress(f"collecting Ceph cluster from {ceph_source} via {ceph_runner}")
            ceph_status = collect_direct_ceph_cluster(
                workdir=workdir,
                seed=ceph_source,
                ssh_key=ssh_key,
                connection_timeout=int(options["timeout"]),
                command_timeout=int(options["timeout"]),
                known_hosts_file=known_hosts,
                runner=ceph_runner,
            )
            ceph_done = True
            if ceph_status != 0:
                with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(f"cluster collection exited {ceph_status}\n")
        rook_status: int | None = None
        rook_done = False
        if want_rook and rook_source:
            progress(f"collecting Rook cluster through {rook_source}")
            rook_status = collect_rook_cluster(
                workdir=workdir,
                namespace=inventory_data.rook_namespace,
                operator_namespace=inventory_data.rook_operator_namespace,
                since=str(options["since"]),
                command_timeout=int(options["timeout"]),
                kube_context=kube_context,
                ssh_target=rook_source if kube_mode == "remote" else None,
                ssh_key=ssh_key,
                connection_timeout=int(options["timeout"]),
                known_hosts_file=known_hosts,
                allow_skip=mode == "auto",
            )
            rook_done = (workdir / "cluster" / "rook" / "pods-wide.txt").is_file()
            if rook_status != 0:
                with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(f"rook collection exited {rook_status}\n")
        missing_cluster_source = False
        if mode == "cephadm" and not ceph_done:
            _write_cluster_skip_once(
                workdir,
                "ceph",
                "no cephadm-capable node found (or --seed unreachable)",
            )
            missing_cluster_source = True
        elif mode == "rook" and not rook_done:
            _write_cluster_skip_once(
                workdir, "rook", "no kubectl-capable node found"
            )
            missing_cluster_source = True
        elif mode == "auto":
            if not ceph_done:
                _write_cluster_skip_once(
                    workdir, "ceph", "no cephadm-capable node in inventory (auto)"
                )
            if not rook_done:
                _write_cluster_skip_once(
                    workdir, "rook", "no kubectl-capable node in inventory (auto)"
                )
            missing_cluster_source = not ceph_done and not rook_done

        prometheus: PrometheusCollectionResult | None = None
        if prom_url:
            progress("collecting Prometheus evidence")
            prometheus = collect_prometheus_cluster(
                workdir=workdir,
                url=prom_url,
                since=str(options["since"]),
                job_regex=str(options["prom_job_regex"]),
                step=str(options["prom_step"]),
                command_timeout=int(options["timeout"]),
                budget=int(options["prom_timeout"]),
            )
            if prometheus.exit_code != 0:
                with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(
                        f"prometheus collection exited {prometheus.exit_code}\n"
                    )
        node_source = (
            Path(__file__).resolve().parent / "ceph_incident_node.py"
        ).read_bytes()
        node_results = []
        for index, inventory_node in enumerate(inventory_data.nodes, start=1):
            progress(
                f"[{index}/{len(inventory_data.nodes)}] collecting node "
                f"{inventory_node.host_alias}"
            )
            result = collect_single_node(
                workspace=workdir,
                destination=workdir / "nodes" / inventory_node.host_alias,
                host_alias=inventory_node.host_alias,
                target=inventory_node.target,
                ssh_key=ssh_key,
                node_source=node_source,
                connection_timeout=int(options["timeout"]),
                node_timeout=int(options["node_timeout"]),
                command_timeout=int(options["timeout"]),
                known_hosts_file=known_hosts,
                skip_logs=bool(options["skip_logs"]),
                keep_original_logs=bool(options["keep_original_logs"]),
                var_log_max_bytes=options["var_log_max_bytes"],
                since=str(options["since"]),
            )
            node_results.append((inventory_node, result))
            if result.reason is not None:
                with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(
                        f"node {inventory_node.host_alias} "
                        f"({inventory_node.target}): {result.reason}\n"
                    )
        progress("redacting collected evidence" if options["redact"] else "checking collected evidence")
        content_safety_complete = _run_content_safety(
            workdir, redact=bool(options["redact"])
        )
        if not content_safety_complete:
            with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                errors.write(
                    "redaction failed; original collected artifact was preserved\n"
                )
        log_caps_complete = _enforce_node_log_caps(
            workdir, options["var_log_max_bytes"]
        )
        if known_hosts is not None:
            known_hosts.unlink(missing_ok=True)
        prometheus_status = prometheus.exit_code if prometheus is not None else None
        node_ok = sum(result.exit_code == 0 for _, result in node_results)
        node_failed = len(node_results) - node_ok
        cluster_exit_code = (
            2 if ceph_status or rook_status or missing_cluster_source else 0
        )
        exit_code = (
            2
            if (
                bool(inventory_data.rejected_entries)
                or node_failed
                or ceph_status
                or rook_status
                or prometheus_status
                or missing_cluster_source
                or not content_safety_complete
                or not log_caps_complete
            )
            else 0
        )
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        environment = [
            f"created_utc={created}",
            f"mode={mode}",
            f"seed={ceph_seed}",
            f"since={options['since']}",
            f"timeout={options['timeout']}",
            f"ceph_source={ceph_source or '<none>'}",
            f"ceph_runner={ceph_runner or '<none>'}",
            f"rook_source={rook_source or '<none>'}",
        ]
        for inventory_node, result in node_results:
            environment.extend(
                [
                    f"node_target_{inventory_node.host_alias}={inventory_node.target}",
                    f"node_invocation_id_{inventory_node.host_alias}={result.invocation_id}",
                ]
            )
        summary = [
            f"created_utc={created}",
            f"mode={mode}",
            f"seed={ceph_seed}",
            f"cluster_status={cluster_exit_code}",
            f"node_ok={node_ok}",
            f"node_failed={node_failed}",
        ]
        if rook_status is not None:
            environment.extend(
                [
                    f"rook_namespace={inventory_data.rook_namespace}",
                    f"rook_operator_namespace={inventory_data.rook_operator_namespace}",
                ]
            )
            if kube_context:
                environment.append(f"kube_context={kube_context}")
        if prometheus is not None:
            if prometheus.dump_completed:
                environment.extend(
                    [
                        f"prom_url={prometheus.masked_url}",
                        f"prom_jobs={' '.join(prometheus.jobs_matched) or '<none>'}",
                    ]
                )
        summary.append(f"final_status={exit_code}")
        (workdir / "environment.txt").write_text(
            "".join(f"{line}\n" for line in environment), encoding="utf-8"
        )
        (workdir / "summary.txt").write_text(
            "".join(f"{line}\n" for line in summary), encoding="utf-8"
        )
        progress("verifying collected evidence")
        _verify_bundle_path(workdir)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = _reserve_archive_path(output_root, timestamp)
        packaged_candidate = workdir / ".incident-bundle.tar.gz"
        progress("packaging incident bundle")
        packaged = subprocess.run(
            [
                "tar",
                "--exclude=./.incident-bundle.tar.gz",
                "-czf",
                str(packaged_candidate),
                "-C",
                str(workdir),
                ".",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "COPYFILE_DISABLE": "1"},
            check=False,
        )
        if packaged.returncode != 0:
            raise VerificationError(
                f"failed to package incident bundle: {packaged.stderr.decode(errors='replace')}"
            )
        _verify_bundle_path(packaged_candidate)
        os.replace(packaged_candidate, archive)
        print(f"bundle: {archive}")
        if retain_workdir:
            print(f"kept workdir: {workdir}", file=sys.stderr)
        return exit_code
    except CollectionInterrupted:
        if archive is not None:
            archive.unlink(missing_ok=True)
        print(
            f"interrupted — workdir kept at {workdir}"
            if retain_workdir
            else "interrupted — stopping and cleaning up…",
            file=sys.stderr,
        )
        return 130
    except KeyboardInterrupt:
        if archive is not None:
            archive.unlink(missing_ok=True)
        print(
            f"interrupted — workdir kept at {workdir}"
            if retain_workdir
            else "interrupted — stopping and cleaning up…",
            file=sys.stderr,
        )
        return 130
    except (OSError, VerificationError) as error:
        retain_workdir = True
        if archive is not None:
            archive.unlink(missing_ok=True)
        if packaged_candidate is not None:
            packaged_candidate.unlink(missing_ok=True)
        try:
            _rewrite_summary_final_status(workdir, 1)
        except OSError:
            pass
        try:
            with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                errors.write(f"bundle verification failed: {error}\n")
        except OSError:
            pass
        print(f"VERIFY FAILED: workdir kept at {workdir}: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term_handler)
        if not retain_workdir:
            shutil.rmtree(workdir)


def _normalise_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise VerificationError(f"unsafe archive member: {name}")
    return path.as_posix()


def _verify_required_files(files: set[str]) -> None:
    for required in REQUIRED_FILES:
        if required not in files:
            raise VerificationError(f"missing required file: {required}")


def _verify_artifact_prefix(files: set[str], prefix: str) -> None:
    if not any(name.startswith(f"{prefix}/") for name in files):
        raise VerificationError(f"missing {prefix}/ artifact")


def _verify_file_set(files: set[str]) -> None:
    _verify_required_files(files)
    _verify_artifact_prefix(files, "cluster")
    _verify_artifact_prefix(files, "nodes")


def _raise_walk_error(error: OSError) -> None:
    raise error


def _read_archive(target: Path) -> set[str]:
    files: set[str] = set()
    try:
        payload_cap = _structural_payload_cap()
        if target.stat().st_size > payload_cap:
            raise VerificationError("archive exceeds structural payload cap")
        # Snapshot the fully consumed gzip payload before parsing the tar.  The
        # cap bounds gzip expansion, and both passes inspect the same bytes.
        with tempfile.TemporaryFile() as snapshot:
            expanded_bytes = 0
            with gzip.open(target, mode="rb") as compressed_stream:
                while True:
                    chunk = compressed_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    expanded_bytes += len(chunk)
                    if expanded_bytes > payload_cap:
                        raise VerificationError(
                            "archive exceeds structural payload cap"
                        )
                    snapshot.write(chunk)
            if expanded_bytes < 1024 or expanded_bytes % 512:
                raise VerificationError("invalid archive: missing tar end markers")
            snapshot.seek(0)
            with tarfile.open(fileobj=snapshot, mode="r:") as archive:
                members = archive.getmembers()
                normalised_members: list[tuple[tarfile.TarInfo, str]] = []
                seen_names: dict[str, str] = {}
                member_kinds: dict[str, str] = {}
                for member in members:
                    normalised_name = _normalise_member_name(member.name)
                    if normalised_name in seen_names:
                        raise VerificationError(
                            "duplicate archive member after normalisation: "
                            f"{member.name} collides with {seen_names[normalised_name]}"
                        )
                    seen_names[normalised_name] = member.name
                    normalised_members.append((member, normalised_name))
                    if not (member.isfile() or member.isdir()):
                        if member.issym():
                            member_kind = "symlink"
                        elif member.islnk():
                            member_kind = "hardlink"
                        else:
                            member_kind = "non-file/non-directory"
                        raise VerificationError(
                            f"archive contains {member_kind} member: {member.name}"
                        )
                    if normalised_name == "." and not member.isdir():
                        raise VerificationError(
                            "archive member hierarchy collision: . is a file ancestor"
                        )
                    member_kinds[normalised_name] = (
                        "directory" if member.isdir() else "file"
                    )

                for name in member_kinds:
                    parts = PurePosixPath(name).parts
                    for index in range(1, len(parts)):
                        parent = PurePosixPath(*parts[:index]).as_posix()
                        if member_kinds.get(parent) == "file":
                            raise VerificationError(
                                "archive member hierarchy collision: "
                                f"{name} below {parent}"
                            )

                payload_end = max(
                    (
                        member.offset_data + ((member.size + 511) // 512) * 512
                        for member in members
                    ),
                    default=0,
                )
                snapshot.seek(payload_end)
                trailing = snapshot.read()
                if len(trailing) < 1024 or any(trailing):
                    raise VerificationError("invalid archive: missing tar end markers")

                for member, normalised_name in normalised_members:
                    if member.isdir():
                        continue
                    files.add(normalised_name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise VerificationError(
                            f"cannot read archive member: {member.name}"
                        )
                    with stream:
                        while stream.read(1024 * 1024):
                            pass
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        raise VerificationError(f"invalid archive: {target}") from error
    return files


def _verify_directory(target: Path) -> None:
    files: set[str] = set()
    payload_bytes = 0
    payload_cap = _structural_payload_cap()
    try:
        for root, directories, filenames in os.walk(
            target, topdown=True, onerror=_raise_walk_error
        ):
            for name in (*directories, *filenames):
                path = Path(root, name)
                relative_name = path.relative_to(target).as_posix()
                if path.is_symlink():
                    raise VerificationError(
                        f"symlink is not allowed in bundle: {relative_name}"
                    )
                if path.is_dir():
                    continue
                if path.is_file():
                    payload_bytes += path.stat().st_size
                    if payload_bytes > payload_cap:
                        raise VerificationError(
                            "bundle directory exceeds structural payload cap"
                        )
                    files.add(relative_name)
                    continue
                raise VerificationError(
                    f"non-file/non-directory entry in bundle: {relative_name}"
                )
    except OSError as error:
        raise VerificationError(f"cannot read bundle directory: {target}") from error
    _verify_file_set(files)


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args and args[0] == "collect":
        return _collect(args[1:])
    if len(args) != 2 or args[0] != "verify":
        print(USAGE, file=sys.stderr)
        return 1

    target = Path(args[1])
    try:
        if not target.is_dir() and not (
            target.is_file() and args[1].endswith(".tar.gz")
        ):
            raise VerificationError(
                f"expected a directory or .tar.gz bundle: {args[1]}"
            )
        _verify_bundle_path(target)
    except VerificationError as error:
        print(f"VERIFY FAIL: {error}", file=sys.stderr)
        return 1

    print(f"VERIFY PASS: {args[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
