"""The one Node Evidence Archive both implementations receive.

The differential gate proves observable equivalence, so the archive a node
returns must be produced once and handed to whichever workstation asked for it.
Every payload here is deterministic: fixed bytes, fixed mtimes, fixed gzip
mtime, so the only differences a comparison can find belong to the collector
under test rather than to the fixture.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile


NODE_TMP_ROOT = "/tmp/ceph-incident-node.differential/out"

# Redaction bait. Each line is a documented secret shape from the content-safety
# contract, so a redact/no-redact pair of runs is decided by the collector's
# policy rather than by the fixture withholding material.
PLAIN_SECRET_PAYLOAD = (
    b"mon.a evidence line\n"
    b"key = AQBnodemergedsecret==\n"
    b"password = merged-must-redact\n"
)
COMPRESSED_SECRET_PAYLOAD = (
    b"osd.0 evidence line\n"
    b"token = compressed-must-redact\n"
)
OPAQUE_SECRET_PAYLOAD = b"secret = opaque-must-stay-byte-for-byte\n"
NODE_CONFIG_PAYLOAD = (
    b"[global]\n"
    b"mon_host = 10.0.0.1\n"
    b"key = AQBnodeconfigsecret==\n"
)


def _opaque_tar_gz() -> bytes:
    """An opaque archive member: raw evidence that must never be rewritten."""

    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as archive:
        member = tarfile.TarInfo("journal.export")
        member.size = len(OPAQUE_SECRET_PAYLOAD)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(OPAQUE_SECRET_PAYLOAD))
    return gzip.compress(inner.getvalue(), mtime=0)


def node_evidence_files(alias: str, *, skip_logs: bool) -> dict[str, bytes]:
    """The evidence tree one node returns, before it is packed into an archive."""

    files: dict[str, bytes] = {
        "system/hostname.txt": (
            f"# host: {alias}\n"
            f"# collector: collect-node\n"
            f"# started: 2026-07-29T00:00:00Z\n"
            f"# timeout: 20s\n"
            f"{alias}\n"
        ).encode(),
        "system/uname.txt": (
            "# host: {alias}\n"
            "# collector: collect-node\n"
            "# started: 2026-07-29T00:00:00Z\n"
            "# timeout: 20s\n"
            "Linux {alias} 6.1.0 x86_64\n"
        ).format(alias=alias).encode(),
        "cephadm/var-lib-ceph-configs/fsid-1/mon.a/config": NODE_CONFIG_PAYLOAD,
    }
    if skip_logs:
        files["logs/var-log/SKIPPED.txt"] = (
            b"SKIPPED: /var/log collection disabled by --skip-logs\n"
        )
    else:
        payload = {
            "logs/var-log/merged/tree/files/ceph.merged": PLAIN_SECRET_PAYLOAD,
            "logs/var-log/merged/tree/files/ceph-osd.0.log.gz": gzip.compress(
                COMPRESSED_SECRET_PAYLOAD, mtime=0
            ),
            "logs/var-log/raw/journal.tar.gz": _opaque_tar_gz(),
        }
        files.update(payload)
        files["logs/var-log/INDEX.tsv"] = (
            b"source\tfamily\tcodec\tbytes\tdisposition\n"
            b"/var/log/ceph/ceph.log\tceph\tplain\t"
            + str(len(PLAIN_SECRET_PAYLOAD)).encode()
            + b"\tmerged\n"
            b"/var/log/ceph/ceph-osd.0.log.gz\tceph-osd.0\tgz\t"
            + str(len(payload["logs/var-log/merged/tree/files/ceph-osd.0.log.gz"])).encode()
            + b"\tmerged\n"
            b"/var/log/journal/journal.export\tjournal\topaque\t"
            + str(len(payload["logs/var-log/raw/journal.tar.gz"])).encode()
            + b"\traw\n"
        )
        files["logs/var-log/UNREDACTED-OPAQUE.txt"] = (
            b"raw/journal.tar.gz: opaque evidence kept byte-for-byte, not redacted\n"
        )
        files["logs/var-log/PAYLOAD-BYTES.txt"] = (
            f"{sum(len(value) for value in payload.values())}\n".encode()
        )
    return files


def node_archive_bytes(alias: str, *, skip_logs: bool, case: str = "normal") -> bytes:
    """Pack one node's evidence, applying the requested archive-level case."""

    if case == "not-tar":
        return b"this is not a tar archive\n"

    files = node_evidence_files(alias, skip_logs=skip_logs)
    if case == "pem-leak":
        files["system/leak.pem"] = b"-----BEGIN OPENSSH PRIVATE KEY-----\nleaked\n"

    if case != "missing-manifest":
        entries = [
            {
                "host": alias,
                "collector": "collect-node",
                "artifact": f"{NODE_TMP_ROOT}/{name}",
                "command": _command_for(name),
                "exit_code": 0,
                "started": "2026-07-29T00:00:00Z",
                "ended": "2026-07-29T00:00:01Z",
            }
            for name in sorted(files)
        ]
        files["manifest.jsonl"] = "".join(
            json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries
        ).encode()

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        for name in sorted(files):
            member = tarfile.TarInfo(name)
            member.size = len(files[name])
            member.mtime = 0
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(files[name]))
    return buffer.getvalue()


def _command_for(name: str) -> str:
    if name == "system/hostname.txt":
        return "hostname"
    if name == "system/uname.txt":
        return "uname -a"
    if name.startswith("cephadm/"):
        return "cp -p /var/lib/ceph/fsid-1/mon.a/config"
    return "collect_var_logs /var/log"
