from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.lab_fixture import DEFAULT_HOSTS, LAB_BIN, FakeLab, host_fingerprint
from validation.lab_baseline import BaselineRejected, load_cutover_baseline
from validation.lab_profile import load_profile


class CutoverBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.profile_path = self.lab.write_profile()
        self.profile = load_profile(self.profile_path)

    def test_accepts_the_preserved_pass_report_and_verified_shell_bundle(self) -> None:
        report_path, bundle_path = write_baseline(
            self.root, self.lab, self.profile_path
        )

        baseline = load_cutover_baseline(report_path, profile=self.profile)

        self.assertEqual(baseline.code_commit, "1" * 40)
        self.assertEqual(baseline.report_path, report_path)
        self.assertEqual(baseline.shell_bundle_path, bundle_path)
        self.assertEqual(baseline.shell_bundle_hash, sha256(bundle_path))
        self.assertEqual(baseline.identity["ceph_fsid"], self.profile.ceph_fsid)

    def test_rejects_a_report_that_did_not_pass(self) -> None:
        report_path, _ = write_baseline(self.root, self.lab, self.profile_path)
        document = json.loads(report_path.read_text(encoding="utf-8"))
        document["status"] = "bundle-comparison-failed"
        report_path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(BaselineRejected, "status is not pass"):
            load_cutover_baseline(report_path, profile=self.profile)

    def test_rejects_a_baseline_for_a_different_active_profile(self) -> None:
        report_path, _ = write_baseline(self.root, self.lab, self.profile_path)
        other_profile_path = self.lab.write_profile(name="other-lab")

        with self.assertRaisesRegex(BaselineRejected, "profile hash does not match"):
            load_cutover_baseline(
                report_path, profile=load_profile(other_profile_path)
            )

    def test_rejects_a_shell_bundle_whose_bytes_changed(self) -> None:
        report_path, bundle_path = write_baseline(
            self.root, self.lab, self.profile_path
        )
        bundle_path.write_bytes(bundle_path.read_bytes() + b"changed")

        with self.assertRaisesRegex(BaselineRejected, "hash no longer matches"):
            load_cutover_baseline(report_path, profile=self.profile)

def write_baseline(root: Path, lab: FakeLab, profile_path: Path) -> tuple[Path, Path]:
    profile = load_profile(profile_path)
    baseline_dir = root / "baseline"
    output = baseline_dir / "shell"
    output.mkdir(parents=True)
    inventory = baseline_dir / "inventory.env"
    inventory.write_text(
        "SSH_USER=operator\n"
        "SEED_HOST=10.0.0.11\n"
        "ROOK_NAMESPACE=rook-ceph\n"
        "ROOK_OPERATOR_NAMESPACE=rook-ceph\n"
        "HOSTS=(\n"
        + "".join(f'  "{name}={address}"\n' for name, address in DEFAULT_HOSTS)
        + ")\n",
        encoding="utf-8",
    )
    home = baseline_dir / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "known_hosts").write_text("pinned fake key\n", encoding="utf-8")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CEPH_INCIDENT_")
    }
    environment.update(
        HOME=str(home),
        KUBECONFIG=str(lab.kubeconfig),
        LC_ALL="C",
        TZ="UTC",
    )
    subprocess.run(
        [
            sys.executable,
            str(LAB_BIN / "fake-collect"),
            "--implementation",
            "shell",
            "--inventory",
            str(inventory),
            "--ssh-key",
            str(lab.ssh_key),
            "--out",
            str(output),
            "--prom-url",
            profile.prometheus_url,
            "--mode",
            "auto",
            "--kube-mode",
            "local",
            "--since",
            "24h",
            "--no-trust-ssh-host-key",
            "--redact",
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    bundle_path = next(output.glob("ceph-incident-*.tar.gz"))
    identity = {
        "ceph_fsid": profile.ceph_fsid,
        "rook_fsid": profile.rook_fsid,
        "prometheus": {"url": profile.prometheus_url, "ready": True},
        "hosts": [
            {
                "name": name,
                "address": address,
                "hostname": name,
                "ssh_fingerprints_verified": [host_fingerprint(address)],
            }
            for name, address in DEFAULT_HOSTS
        ],
    }
    report = {
            "schema_version": 1,
            "timestamp": "2026-08-05T16:44:40Z",
            "code": {"commit": "1" * 40, "dirty": False},
            "profile": {
                "path": str(profile_path),
                "hash": profile.profile_hash,
                "state": "active",
                "name": profile.name,
            },
            "lab_identity": identity,
            "preflight": [],
            "runs": [
                {
                    "implementation": "shell",
                    "exit_code": 0,
                    "invocation_id": bundle_path.name,
                    "bundle_path": str(bundle_path),
                    "bundle_hash": sha256(bundle_path),
                    "verify_result": "pass",
                    "coverage": {
                        "ceph": "collected",
                        "rook": "collected",
                        "prometheus": "collected",
                        "nodes": "collected",
                        "var_log": "collected",
                    },
                }
            ],
            "comparison": {"result": "equivalent", "differences": []},
            "stable_state": {
                "snapshot_schema_version": 1,
                "result": "unchanged",
                "differences": [],
            },
            "residue": [
                {"host": name, "result": "clean", "detail": "no residue"}
                for name, _ in DEFAULT_HOSTS
            ],
            "status": "pass",
            "next_action": "Proceed to issue #22",
        }
    report_path = baseline_dir / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path, bundle_path


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
