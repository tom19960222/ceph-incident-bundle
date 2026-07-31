"""A fake lab for the Lab Profile and qualification workflow tests.

Nothing here connects to a real lab.  `tests/fixtures/lab/bin` holds whitelist
fakes for `ssh-keyscan`, `ssh`, `kubectl` and `curl`; putting that directory
first on PATH is what makes discovery, preflight and status testable offline.
The same directory holds the stand-in collect and verify entrypoints the
qualification harness is exercised against — see `fake_entrypoints`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from validation.lab_qualify import CollectEntrypoint


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


def fake_entrypoints() -> tuple[CollectEntrypoint, ...]:
    """The stand-in reference and candidate the harness tests drive.

    Both are the same script told which implementation it is playing, so a test
    that makes one bundle diverge is changing exactly one thing.
    """

    collect = str(LAB_BIN / "fake-collect")
    verify = str(LAB_BIN / "fake-verify")
    return tuple(
        CollectEntrypoint(
            implementation,
            (sys.executable, collect, "--implementation", implementation),
            (sys.executable, verify, "--implementation", implementation),
        )
        for implementation in ("shell", "python")
    )


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
        # The nodes' collector leftovers, as the fake `ssh` reports them.  A run
        # that leaks appends to this file, so residue appears *during* the run
        # exactly as it would in a lab.
        self.residue_ledger = self.root / "residue.json"

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
        operator_namespace: str = "rook-ceph",
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
            f'operator_namespace = "{operator_namespace}"',
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
            "FAKE_LAB_RESIDUE_FILE": str(self.residue_ledger),
            **knobs,
        }

    def checkout(self, *, clean: bool = True) -> Path:
        """A throwaway git checkout for the gate's code-identity stage.

        The gate refuses to produce qualification evidence from a checkout whose
        tracked files were modified, so the tests need a checkout they control:
        driving that stage from this repository's own working tree would make
        every test depend on whether the developer had saved a file.
        """

        root = self.root / "checkout"
        if not root.exists():
            root.mkdir()
            (root / "collector.py").write_text("# stand-in collector\n", encoding="utf-8")
            for arguments in (
                ("init", "-q"),
                ("config", "user.email", "lab@example.invalid"),
                ("config", "user.name", "Lab Fixture"),
                ("add", "collector.py"),
                ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"),
            ):
                subprocess.run(
                    ["git", "-C", str(root), *arguments], check=True, capture_output=True
                )
        if not clean:
            (root / "collector.py").write_text("# edited in place\n", encoding="utf-8")
        return root

    def leave_residue(self, address: str, entry: str) -> None:
        """Plant one leftover entry the fake `ssh` will report for `address`."""

        try:
            state = json.loads(self.residue_ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault(address, []).append(entry)
        self.residue_ledger.write_text(json.dumps(state), encoding="utf-8")
