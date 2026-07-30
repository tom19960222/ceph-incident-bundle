"""A fake lab for the Lab Profile workflow tests.

Nothing here connects to a real lab.  `tests/fixtures/lab/bin` holds whitelist
fakes for `ssh-keyscan`, `ssh`, `kubectl` and `curl`; putting that directory
first on PATH is what makes discovery, preflight and status testable offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAB_BIN = ROOT / "tests" / "fixtures" / "lab" / "bin"
CEPH_FSID = "3f2b1c8e-0000-4a1d-8b7e-000000000001"
ROOK_FSID = "3f2b1c8e-0000-4a1d-8b7e-000000000002"
OTHER_FSID = "3f2b1c8e-0000-4a1d-8b7e-00000000ffff"
DEFAULT_HOSTS: tuple[tuple[str, str], ...] = (
    ("monitor01", "10.0.0.11"),
    ("mon02", "10.0.0.12"),
    ("osd01", "10.0.0.21"),
)


def fingerprint_of_seed(seed: str) -> str:
    """The SHA256 fingerprint of a fake host key built from `seed`."""

    blob = base64.b64encode(seed.encode("utf-8"))
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def host_fingerprint(address: str) -> str:
    """The fingerprint the fake `ssh-keyscan` offers for `address` by default."""

    return fingerprint_of_seed(f"fake-host-key-{address}")


@dataclass
class FakeLab:
    """One temporary lab: credential files, profiles and the fake PATH."""

    root: Path

    def __post_init__(self) -> None:
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(parents=True, exist_ok=True)
        self.ssh_key = self.credentials / "id_ed25519"
        self.kubeconfig = self.credentials / "kubeconfig"
        self._write_credential(self.ssh_key, "fake private key placeholder\n")
        self._write_credential(self.kubeconfig, "fake kubeconfig placeholder\n")
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.runs = self.root / "runs"

    def _write_credential(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def profile_text(
        self,
        *,
        state: str = "active",
        name: str = "fake-lab",
        hosts: tuple[tuple[str, str], ...] = DEFAULT_HOSTS,
        identity: bool = True,
        ceph_fsid: str = CEPH_FSID,
        rook_fsid: str = ROOK_FSID,
        prometheus_url: str = "http://10.0.0.11:9095",
        ssh_key: Path | None = None,
        kubeconfig: Path | None = None,
        fingerprints: dict[str, tuple[str, ...]] | None = None,
        hostnames: dict[str, str] | None = None,
        seed: str = "monitor01",
    ) -> str:
        lines = [
            "schema_version = 1",
            "",
            "[profile]",
            f'name = "{name}"',
            f'state = "{state}"',
            "",
            "[ssh]",
            'user = "operator"',
            f'key_path = "{ssh_key or self.ssh_key}"',
            "",
            "[ceph]",
            f'seed = "{seed}"',
        ]
        if identity:
            lines.append(f'fsid = "{ceph_fsid}"')
        lines += [
            "",
            "[rook]",
            f'kubeconfig_path = "{kubeconfig or self.kubeconfig}"',
            'namespace = "rook-ceph"',
            'operator_namespace = "rook-ceph"',
        ]
        if identity:
            lines.append(f'fsid = "{rook_fsid}"')
        lines += ["", "[prometheus]", f'url = "{prometheus_url}"']
        for host, address in hosts:
            lines += ["", "[[hosts]]", f'name = "{host}"', f'address = "{address}"']
            if identity:
                hostname = (hostnames or {}).get(host, host)
                recorded = (fingerprints or {}).get(host, (host_fingerprint(address),))
                lines.append(f'hostname = "{hostname}"')
                rendered = ", ".join(f'"{value}"' for value in recorded)
                lines.append(f"ssh_fingerprints = [{rendered}]")
        return "\n".join(lines) + "\n"

    def write_profile(self, filename: str = "lab.toml", **fields: object) -> Path:
        path = self.profiles / filename
        path.write_text(self.profile_text(**fields), encoding="utf-8")  # type: ignore[arg-type]
        return path

    def environment(
        self, hosts: tuple[tuple[str, str], ...] = DEFAULT_HOSTS, **knobs: str
    ) -> dict[str, str]:
        """PATH-first fake lab environment, plus any fake command knobs.

        The fake lab's hosts answer `hostname` with their profile name by default,
        so a test that overrides one host's hostname does not accidentally change
        every other host's answer too.
        """

        aliases = json.dumps({address: name for name, address in hosts})
        return {
            "PATH": f"{LAB_BIN}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_LAB_HOST_ALIASES": aliases,
            **knobs,
        }
