# Real-Lab Validation Runbook

## Current Status

Issue #21 produced auditable PASS evidence in run `20260805T155047Z` at commit
`155e057`: shell and Python each completed a four-path collect, both bundles verified,
their normalized contracts were equivalent, stable state was unchanged, and all seven nodes
were residue-clean. Issue #22 removes the shell runtime and changes `make validate-lab` into the
post-cutover proof:

- report SHA-256: `e681f7fe662c1e08ab55f6812d9b444dc0fdb5b36580706372fc264dc124c11d`;
- full commit: `155e057956974ae6b72ab53d76f71d96ab5f0e06`;
- shell bundle SHA-256: `00b51641829b4fc535ad0a0f41ca4be519b22ea69688725e9e1d97a972abc64f`.

1. validate the preserved #21 `report.json` and shell bundle/hash locally;
2. prove the active Lab Profile still resolves to the exact saved lab identity;
3. record the Python 3.11+ tooling runtime, exact workstation CPython 3.10.x
   production runtime and every inventory node's fixed `python3`; select a CPython 3.10.x witness;
4. run one Python four-path full collect through the selected production interpreter;
5. verify it and compare it with the saved shell baseline;
6. prove node runtimes unchanged, stable state unchanged, the witness complete, plus workstation
   and remote residue clean;
7. write schema-v3 `report.md` and `report.json`.

`lab-preflight` proves identity only. Only a schema-v3 `validate-lab` report with
`status: pass` proves CPython 3.10 qualification. Existing schema-v2 PASS reports retain their
historical post-cutover meaning and are deliberately not upgraded into Python 3.10 evidence.

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
  PRODUCTION_PYTHON=/absolute/path/to/cpython3.10 \
  TOOLING_PYTHON=/absolute/path/to/python3.11 \
  CEPH_INCIDENT_LAB_CONFIRM=1
```

All four paths must be absolute. Interpreters are pre-provisioned; the gate never installs,
switches or updates Python. `LAB_ARGS='--runs-dir <path> --collect-timeout <seconds>'` may
change only the local artifact root and stuck-run ceiling; it cannot change the fixed collect
vector.

## Gate Order

The first failing stage stops later work, except remote residue is always checked after any
collect starts.

1. **Code identity:** tracked files must match HEAD so the report names code that actually ran.
2. **Read-only command surface:** fixed argv contains no side-effecting opt-in; inherited
   `CEPH_INCIDENT_*` variables are stripped.
3. **Local runtime identity:** harness Python is 3.11+; the explicit production interpreter is
   exact CPython 3.10.x and becomes the only workstation collect/verify entrypoint.
4. **Baseline evidence:** report schema 1, `status: pass`, clean 40-character commit, matching
   profile hash, equivalent comparison, unchanged stable state, clean residue, exactly one
   verified/complete shell run, and a shell bundle whose current SHA-256 still matches.
5. **Strict identity preflight:** active profile, owner-only credentials, pinned SSH keys,
   required hostnames, Ceph/Rook FSIDs and Prometheus readiness.
6. **Baseline identity:** current Ceph, Rook, Prometheus and host identity must exactly equal the
   saved report before collection starts.
7. **Pre runtime proof:** after strict identity, fixed read-only SSH argv records every node's
   structured runtime. All nodes require supported CPython 3.10+, and at least one exact 3.10.x
   node is selected as the floor witness before collection.
8. **Pre snapshot and residue baseline:** capture stable state schema 1 and each node's existing
   collector workspace/helper listing.
9. **Python full collect:** fixed `--mode auto --kube-mode local --since 24h
   --no-trust-ssh-host-key --redact`, plus profile inventory/key/Prometheus inputs. One invocation
   must cover Ceph, Rook, Prometheus, every node and `/var/log`.
10. **Witness and full coverage:** the selected witness must have accepted Node Evidence plus
    `/var/log`; all other collector paths and nodes remain mandatory.
11. **Verify and workstation cleanup:** bundle must pass Python structural/content verification;
   the successful output directory may contain only the bundle plus `collect.log` and
   `verify.log`—no `tmp.*` owned workdir.
12. **Normalized comparison:** compare the preserved shell bundle with the new Python bundle
   under [`lab-bundle-contract.md`](lab-bundle-contract.md).
13. **Post proof:** every node's complete runtime identity must exactly match its pre facts,
    stable state must be unchanged and no node may gain an attributable workspace/helper process.

Once a collect has started, post-runtime and remote-residue probes run even if verify, coverage,
cleanup or comparison failed. A residue finding becomes the primary failure class while earlier
failed checks remain in the report. The gate never auto-deletes remote residue.

## Report Contract

Every attempt reserves `results/lab-validation/<run-id>/` before touching the lab and writes
owner-only `report.md` plus `report.json`; `LATEST` names that directory. Schema version 3 records:

- post-cutover commit/dirty state and active profile identity;
- preserved report path/hash, baseline commit/profile hash and shell bundle path/hash;
- baseline shell run and current Python run, verify result and five coverage fields;
- normalized comparison differences;
- stable-state schema/differences;
- workstation cleanup check and per-node remote residue;
- tooling and production runtime identity, every node's pre/post probe status and structured
  runtime, exact-match result and selected CPython 3.10 witness;
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
- Runtime failure: provision the named isolated workstation interpreter or fixed node `python3`
  before rerunning; qualification itself never changes runtimes.
- Collect/verify/coverage/workstation-cleanup/comparison failure: inspect the named local run;
  do not recreate the removed shell implementation.
- Stable-state difference: treat as a possible read-only regression and inspect the command ledger.
- Remote residue: review only the invocation-owned paths/processes; do not broad-delete by prefix.
- Handoff always names the report directory and `LATEST` state, not only a chat summary.
