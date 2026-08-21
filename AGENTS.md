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
- CPython 3.10+ is the production support floor. `cephadm shell` and `kubectl exec` have no
  supported path and must not be reintroduced as fallbacks.
- Real-lab work uses an explicitly selected active TOML Lab Profile, never
  `CEPH-LAB-CONNECTION.md`, and fails closed on every identity mismatch.
- Profiles/reports may reference credential paths but never contain private keys, keyrings,
  passwords, tokens or credential payloads.
- `make validate` remains offline. Real-lab execution is a separate explicit opt-in.

A failed real-lab run retains its owner-only local evidence on purpose. Read and classify the
failure before considering a new run. Never wire broad cleanup into collect or an acceptance gate;
only invocation-owned resources may be reclaimed.

## Current Python-only product

Issues #85 and #115 define the current contract. The only production entry point is the installed
`ceph-incident-bundle` console script, with exactly two public subcommands:
`generate-inventory` and `collect`. Do not restore root-level collector scripts, a verifier or
redactor command, shell compatibility, a second runtime, or a second qualification suite.

`make validate` is the offline gate. It builds from a clean source copy, installs the wheel into a
fresh environment, and exercises the installed console script and Python qualification suite. It
must not connect to a lab. Real-lab execution is an agent-managed, separately authorized workflow;
it is not a Make target or a product command.

Current qualification records are:

- `docs/python-offline-qualification.md` — #102 offline CPython 3.10 installed-wheel proof.
- `docs/python-single-node-acceptance.md` — #103 pinned single-node read-only acceptance.
- `docs/python-full-live-acceptance.md` — #104 pinned seven-node full read-only acceptance.

Raw profiles, reports, manifests and bundles remain local-only under ignored `results/` paths with
owner-only permissions. Documents may record hashes and sanitized summaries, never credential
payloads.

## Cutover coverage

Current `make validate` checks the Python suite, including
`docs/python-cutover-coverage.md`, the authoritative non-runnable deletion audit. Its mechanical
count is 145 scenarios: all 134 behavior-bearing rows are either covered by current public Python
tests or explicitly #85-obsolete, and 11 rows are shell-only implementation details. Older counts
must not be used to drop later coverage.

The historical shell implementation and its differential tooling are rollback history only, not a
current executable contract. Current equivalence and acceptance claims come from public Python
black-box tests plus the #102–#104 records. See `docs/lab-bundle-contract.md` for the exact live
bundle, coverage, stable-state and residue obligations.
