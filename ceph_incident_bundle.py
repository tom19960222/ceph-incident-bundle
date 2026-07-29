#!/usr/bin/env python3
"""Public entrypoint; the current Verify boundary is documented in the rewrite plan."""

from __future__ import annotations

import gzip
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ceph_incident_collectors import (
    CollectionInterrupted,
    collect_direct_ceph_cluster,
    collect_single_node,
)


# Public collect stays fixed to the direct Ceph CLI runner; choosing a runner
# (and the source it belongs to) is multi-source orchestration work (#16).
CEPH_RUNNER = "direct"


USAGE = """Usage:
  ceph_incident_bundle.py collect --inventory PATH --ssh-key PATH [options]
    [--seed USER@HOST] [--since RANGE] [--skip-logs] [--keep-original-logs]
    [--var-log-max-bytes BYTES|unlimited]
  ceph_incident_bundle.py verify <bundle-dir|bundle.tar.gz>"""
REQUIRED_FILES = ("manifest.jsonl", "summary.txt", "README-FIRST.txt")
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
        "skip_logs": False,
        "keep_original_logs": False,
        "var_log_max_bytes": 10 * 1024**3,
        "since": "24h",
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
        if argument in ("--skip-logs", "--keep-original-logs"):
            values[argument.removeprefix("--").replace("-", "_")] = True
            index += 1
            continue
        option_names = {
            "--inventory": "inventory",
            "--ssh-key": "ssh_key",
            "--out": "out",
            "--seed": "seed",
            "--timeout": "timeout",
            "--node-timeout": "node_timeout",
            "--var-log-max-bytes": "var_log_max_bytes",
            "--since": "since",
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
        else:
            values[key] = _positive_integer(raw_value, argument)
        index += 2
    if values.get("help"):
        return values
    for required in ("inventory", "ssh_key"):
        if required not in values:
            raise CollectUsageError(f"missing required option: --{required.replace('_', '-')}")
    return values


def _validated_ssh_target(value: str, message: str) -> str:
    if not value or value.startswith("-") or SAFE_SSH_TARGET.fullmatch(value) is None:
        raise CollectUsageError(message)
    return value


def _ssh_target_for_host(host: str, ssh_user: str) -> str:
    return host if "@" in host or not ssh_user else f"{ssh_user}@{host}"


def _read_single_node_inventory(path: Path) -> tuple[str, str, str]:
    if not path.is_file():
        raise CollectUsageError(f"missing inventory: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CollectUsageError(f"cannot read inventory: {path}") from error
    ssh_user = ""
    seed_host = ""
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
    if in_hosts:
        raise CollectUsageError("inventory HOSTS block is not closed")
    if len(hosts) != 1:
        raise CollectUsageError("this Python candidate requires exactly one inventory node")
    if ssh_user and (
        ssh_user.startswith("-") or SAFE_SSH_USER.fullmatch(ssh_user) is None
    ):
        raise CollectUsageError("invalid SSH_USER")
    entry = hosts[0]
    if "=" not in entry:
        raise CollectUsageError("invalid HOSTS entry")
    alias, host = entry.split("=", 1)
    if alias in (".", "..") or SAFE_ALIAS.fullmatch(alias) is None:
        raise CollectUsageError("invalid host alias")
    target = _validated_ssh_target(
        _ssh_target_for_host(host, ssh_user), "invalid SSH target"
    )
    seed = (
        _validated_ssh_target(
            _ssh_target_for_host(seed_host, ssh_user), "invalid SSH seed target"
        )
        if seed_host
        else ""
    )
    return alias, target, seed


def _write_initial_bundle_files(workdir: Path, *, ceph_seed: str) -> None:
    (workdir / "cluster").mkdir(mode=0o700)
    (workdir / "nodes").mkdir(mode=0o700)
    uncollected = (
        "rook and prometheus collectors are outside the current Python slice"
        if ceph_seed
        else "cluster collectors are outside the current Python node slice"
    )
    (workdir / "cluster" / "SKIPPED.txt").write_text(
        f"SKIPPED: {uncollected}\n",
        encoding="utf-8",
    )
    (workdir / "README-FIRST.txt").write_text(
        "Operationally read-only incident evidence. Review errors.log and summary.txt.\n",
        encoding="utf-8",
    )
    (workdir / "manifest.jsonl").touch(mode=0o600)
    (workdir / "errors.log").touch(mode=0o600)


def _verify_bundle_path(target: Path) -> None:
    if target.is_dir():
        _verify_directory(target)
    else:
        files = _read_archive(target)
        _verify_file_set(files)


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
        host_alias, target, inventory_seed = _read_single_node_inventory(inventory)
        # --seed overrides the inventory SEED_HOST, as in the shell contract.
        ceph_seed = str(options.get("seed") or inventory_seed)
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
    keep_workdir = False
    previous_term_handler = signal.getsignal(signal.SIGTERM)

    def interrupt_handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_handler)
    try:
        _write_initial_bundle_files(workdir, ceph_seed=ceph_seed)
        known_hosts: Path | None = None
        if options["trust_ssh_host_key"]:
            known_hosts = workdir / ".runtime-known-hosts"
            known_hosts.touch(mode=0o600)
        cluster_status: int | None = None
        if ceph_seed:
            cluster_status = collect_direct_ceph_cluster(
                workdir=workdir,
                seed=ceph_seed,
                ssh_key=ssh_key,
                connection_timeout=int(options["timeout"]),
                command_timeout=int(options["timeout"]),
                known_hosts_file=known_hosts,
                runner=CEPH_RUNNER,
            )
            if cluster_status != 0:
                with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                    errors.write(f"cluster collection exited {cluster_status}\n")
        node_source = (Path(__file__).resolve().parent / "ceph_incident_node.py").read_bytes()
        result = collect_single_node(
            workspace=workdir,
            destination=workdir / "nodes" / host_alias,
            host_alias=host_alias,
            target=target,
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
        if result.reason is not None:
            with (workdir / "errors.log").open("a", encoding="utf-8") as errors:
                errors.write(f"node {host_alias} ({target}): {result.reason}\n")
        if known_hosts is not None:
            known_hosts.unlink(missing_ok=True)
        exit_code = 2 if (result.exit_code != 0 or cluster_status) else 0
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        environment = [
            f"created_utc={created}",
            f"mode=python-{'direct-ceph' if ceph_seed else 'node'}-slice",
            f"node_target={target}",
            f"node_invocation_id={result.invocation_id}",
        ]
        summary = [
            f"created_utc={created}",
            f"node_ok={1 if result.exit_code == 0 else 0}",
            f"node_failed={1 if result.exit_code != 0 else 0}",
        ]
        if cluster_status is not None:
            environment.extend(
                [f"ceph_source={ceph_seed}", f"ceph_runner={CEPH_RUNNER}"]
            )
            summary.append(f"cluster_status={cluster_status}")
        summary.append(f"final_status={exit_code}")
        (workdir / "environment.txt").write_text(
            "".join(f"{line}\n" for line in environment), encoding="utf-8"
        )
        (workdir / "summary.txt").write_text(
            "".join(f"{line}\n" for line in summary), encoding="utf-8"
        )
        _verify_bundle_path(workdir)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = _reserve_archive_path(output_root, timestamp)
        packaged_candidate = workdir / ".incident-bundle.tar.gz"
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
        return exit_code
    except CollectionInterrupted:
        if archive is not None:
            archive.unlink(missing_ok=True)
        print("interrupted — stopping and cleaning up…", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        if archive is not None:
            archive.unlink(missing_ok=True)
        print("interrupted — stopping and cleaning up…", file=sys.stderr)
        return 130
    except (OSError, VerificationError) as error:
        keep_workdir = True
        if archive is not None:
            archive.unlink(missing_ok=True)
        print(f"VERIFY FAILED: workdir kept at {workdir}: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term_handler)
        if not keep_workdir:
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
        # Consume the gzip stream to EOF before opening the tar.  This verifies
        # the gzip trailer/CRC even when every tar member itself is readable.
        with gzip.open(target, mode="rb") as compressed_stream:
            while compressed_stream.read(1024 * 1024):
                pass

        with tarfile.open(target, mode="r:gz") as archive:
            members = archive.getmembers()
            normalised_members: list[tuple[tarfile.TarInfo, str]] = []
            seen_names: dict[str, str] = {}
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

            for member, normalised_name in normalised_members:
                if member.isdir():
                    continue
                files.add(normalised_name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise VerificationError(f"cannot read archive member: {member.name}")
                with stream:
                    while stream.read(1024 * 1024):
                        pass
    except (OSError, tarfile.TarError, EOFError, zlib.error) as error:
        raise VerificationError(f"invalid archive: {target}") from error
    return files


def _verify_directory(target: Path) -> None:
    files: set[str] = set()
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
