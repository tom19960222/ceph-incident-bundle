# Real-Lab Validation Runbook

## Current Status

Issue #21 produced auditable PASS evidence in run `20260805T155047Z` at commit
`155e057`: shell and Python each completed a four-path collect, both bundles verified,
their normalized contracts were equivalent, stable state was unchanged, and all seven nodes
were residue-clean. Issue #22 removes the shell runtime and changes `make validate-lab` into the
post-cutover proof:

1. validate the preserved #21 `report.json` and shell bundle/hash locally;
2. prove the active Lab Profile still resolves to the exact saved lab identity;
3. run one Python four-path full collect;
4. verify it and compare it with the saved shell baseline;
5. prove stable state unchanged plus workstation and remote residue clean;
6. write schema-v2 `report.md` and `report.json`.

`lab-preflight` proves identity only. Only a post-cutover `validate-lab` report with
`status: pass` proves the shipped Python-only commit against the preserved baseline.

## Non-Negotiable Safety Rules

- Use only an explicitly selected, active TOML Lab Profile. Never parse
  `CEPH-LAB-CONNECTION.md`.
- Keep `cephadm shell` and `kubectl exec` unreachable. Python exposes neither opt-in.
- Fail closed on profile state, credential permission, SSH fingerprint, hostname, Ceph/Rook
  FSID, Prometheus endpoint or baseline identity mismatch.
- Profiles/reports may name credential paths but never persist credential contents.
- Never convert a partial collect, incomplete coverage, failed verify, changed stable field or
  residue finding into PASS by rerunning or weakening a comparison.
- A failed collect keeps its local evidence. Cleanup is a separate explicit operation after the
  failure has been read.

## Agent Entry Point

Start local-only:

```bash
make lab-status LAB_PROFILE=/absolute/path/to/lab.toml
```

Follow its single `next_action`. A fresh or rebuilt lab still follows
`lab-profile-discover` → human review → `lab-profile-activate` → `lab-preflight`.
Do not edit recorded fingerprints/FSIDs to make a mismatch pass.

The post-cutover full gate additionally needs the saved #21 report:

```bash
make validate-lab \
  LAB_PROFILE=/absolute/path/to/lab.toml \
  LAB_BASELINE_REPORT=/absolute/path/to/20260805T155047Z/report.json \
  CEPH_INCIDENT_LAB_CONFIRM=1
```

Both paths must be absolute. `LAB_ARGS='--runs-dir <path> --collect-timeout <seconds>'` may
change only the local artifact root and stuck-run ceiling; it cannot change the fixed collect
vector.

## Gate Order

The first failing stage stops later work, except remote residue is always checked after any
collect starts.

1. **Code identity:** tracked files must match HEAD so the report names code that actually ran.
2. **Read-only command surface:** fixed argv contains no side-effecting opt-in; inherited
   `CEPH_INCIDENT_*` variables are stripped.
3. **Baseline evidence:** report schema 1, `status: pass`, clean 40-character commit, matching
   profile hash, equivalent comparison, unchanged stable state, clean residue, exactly one
   verified/complete shell run, and a shell bundle whose current SHA-256 still matches.
4. **Strict identity preflight:** active profile, owner-only credentials, pinned SSH keys,
   required hostnames, Ceph/Rook FSIDs and Prometheus readiness.
5. **Baseline identity:** current Ceph, Rook, Prometheus and host identity must exactly equal the
   saved report before collection starts.
6. **Pre snapshot and residue baseline:** capture stable state schema 1 and each node's existing
   collector workspace/helper listing.
7. **Python full collect:** fixed `--mode auto --kube-mode local --since 24h
   --no-trust-ssh-host-key --redact`, plus profile inventory/key/Prometheus inputs. One invocation
   must cover Ceph, Rook, Prometheus, every node and `/var/log`.
8. **Verify and workstation cleanup:** bundle must pass Python structural/content verification;
   the successful output directory may contain only the bundle plus `collect.log` and
   `verify.log`—no `tmp.*` owned workdir.
9. **Normalized comparison:** compare the preserved shell bundle with the new Python bundle
   under [`lab-bundle-contract.md`](lab-bundle-contract.md).
10. **Post proof:** stable state must be unchanged and no node may gain an attributable
    workspace/helper process.

Once a collect has started, remote residue runs even if verify, coverage, cleanup or comparison
failed. A residue finding becomes the primary failure class while the earlier failed check remains
in the report. The gate never auto-deletes remote residue.

## Report Contract

Every attempt reserves `results/lab-validation/<run-id>/` before touching the lab and writes
owner-only `report.md` plus `report.json`; `LATEST` names that directory. Schema version 2 records:

- post-cutover commit/dirty state and active profile identity;
- preserved report path/hash, baseline commit/profile hash and shell bundle path/hash;
- baseline shell run and current Python run, verify result and five coverage fields;
- normalized comparison differences;
- stable-state schema/differences;
- workstation cleanup check and per-node remote residue;
- one exact status and one single-line `next_action`.

Collector stdout/stderr stays in `<run>/python/{collect,verify}.log`, not in the report. Report
writing scans both formats for credential markers and fails closed. Anything not reached remains
`not-run`.

## Artifact Cleanup

A failed gate deliberately keeps evidence. After reading it:

```bash
make lab-clean
make lab-clean CEPH_INCIDENT_LAB_CLEAN=1
```

The first command is preview-only. The confirmed command keeps the newest run plus every report
and command ledger by default; `LAB_ARGS='--keep N'` changes the count. `lab-clean` accepts no Lab
Profile, never follows a root symlink, and only acts inside the explicit run-artifact root. It is
never called by collect or validation.

## Failure and Handoff

- Baseline failure: do not touch the lab; restore/select the preserved #21 PASS report and bundle.
- Identity failure: review/discover/activate the profile; never accept-current.
- Collect/verify/coverage/workstation-cleanup/comparison failure: inspect the named local run;
  do not recreate the removed shell implementation.
- Stable-state difference: treat as a possible read-only regression and inspect the command ledger.
- Remote residue: review only the invocation-owned paths/processes; do not broad-delete by prefix.
- Handoff always names the report directory and `LATEST` state, not only a chat summary.
