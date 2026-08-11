# AGENTS.md

## Agent skills

Issues live in `tom19960222/ceph-incident-bundle`; use `gh` as documented in
`docs/agents/issue-tracker.md`. Canonical triage labels are defined in
`docs/agents/triage-labels.md`. Domain language is single-context: root `CONTEXT.md` plus
`docs/adr/`.

## Evidence collection safety

Before changing or running collector/lab code, read:

- `docs/read-only-safety.md`
- `docs/lab-validation-runbook.md`
- `docs/lab-bundle-contract.md`

Non-negotiable rules:

- Collection must not change persistent configuration, services, packages, mounts, Ceph desired
  state or Kubernetes objects/workloads. Writes stay in collector-owned workspaces and output.
- Treat every Node Evidence Archive as untrusted. Validate all members before extraction; reject
  absolute/traversal paths, links, devices, FIFOs, collisions and workspace escapes.
- Python 3.11+ is the only production implementation. `cephadm shell` and `kubectl exec` have no
  supported path and must not be reintroduced as fallbacks.
- Real-lab work uses an explicitly selected active TOML Lab Profile, never
  `CEPH-LAB-CONNECTION.md`, and fails closed on every identity mismatch.
- Profiles/reports may reference credential paths but never contain private keys, keyrings,
  passwords, tokens or credential payloads.
- `make validate` remains offline. Real-lab execution is a separate explicit opt-in.

A failed `validate-lab` retains its local evidence on purpose. Read the failure, then use
`make lab-clean` to preview and `make lab-clean CEPH_INCIDENT_LAB_CLEAN=1` to reclaim according to
the retention policy. Never wire cleanup into collect or the gate.

## Post-cutover qualification

Issue #21 PASS evidence is run `20260805T155047Z`, commit `155e057`. Its shell baseline bundle,
report and hashes remain local-only evidence. Issue #22 removed the shell runtime and dual-run
fixtures.

`make validate-lab` now requires four absolute paths:

```text
LAB_PROFILE=/absolute/path/to/lab.toml
LAB_BASELINE_REPORT=/absolute/path/to/20260805T155047Z/report.json
PRODUCTION_PYTHON=/absolute/path/to/cpython3.10
TOOLING_PYTHON=/absolute/path/to/python3.11
```

It validates that preserved PASS evidence before lab access, proves exact current lab identity,
runs one Python four-path full collect, verifies it, checks successful workstation cleanup,
compares it with the saved shell bundle, and proves stable state plus remote residue. A schema-v3
report additionally proves workstation/node runtime identity and one complete CPython 3.10 floor
witness. Only schema-v3 `status: pass` is Python 3.10 qualification proof; preserved schema-v2 PASS
reports keep their historical post-cutover meaning but do not prove 3.10 compatibility.

## Equivalence claims

The historical offline differential gate (#18) passed before cutover and its reviewed rules remain
in `docs/differential-normalizer.md`; its executable and shell fakes were intentionally removed.
Current `make validate` checks the Python suite only, including the scenario ledger:

- `docs/test-scenario-ledger.md` maps every frozen shell scenario to live Python coverage or an
  implementation-detail classification.
- `docs/test-scenario-audit.md` records clause-level review and fingerprints every mapping.
- The current mechanical count is 145 scenarios: 134 behavior-bearing/ported and 11 shell-only
  implementation details. The older #22 text saying 137/127/10 predates added scenarios and must
  not be used to drop later coverage.

The workstation-side equivalence was demonstrated by the historical differential gate. Node
evidence equivalence remains asserted by the N-series Python black-box tests, not demonstrated by
shell-versus-Python node-body comparison; ADR 0010 deliberately differs on manifest coverage.

The real-lab normalizer compares collector-authored structure and decisions across different live
moments, not evidence bodies: member set, manifest argv/exit, capture headers, JSON parseability,
skip classes, source/runner selection and complete coverage. See `docs/lab-bundle-contract.md` for
the exact limits. Runs #67 and #68 closed known gaps before shell removal.
