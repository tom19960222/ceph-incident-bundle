# AGENTS.md

## Project

Issues live in `tom19960222/ceph-incident-bundle`; use `gh` as documented in
`docs/agents/issue-tracker.md`. Canonical triage labels are in
`docs/agents/triage-labels.md`. Domain language is single-context: root `CONTEXT.md` plus
`docs/adr/`.

## Product boundaries

- Production is CPython 3.10+ and the installed `ceph-incident-bundle` command, with exactly
  `generate-inventory` and `collect` as public subcommands.
- Collection must not change persistent configuration, services, packages, mounts, Ceph desired
  state, or Kubernetes objects and workloads.
- Treat remote command output, Node Evidence Archives, and Kubernetes and Prometheus responses as
  untrusted. Archive admission must reject traversal, links, special members, collisions, and
  workspace escapes before extraction.
- Do not restore `cephadm shell`, `kubectl exec`, a shell runtime, compatibility collectors, a
  verifier, or a redactor.
- Incident Bundles contain Raw Evidence that may include credentials. Never describe one as
  sanitized or safe to share.
- Real-lab work is a separate explicit opt-in. Only then read `docs/lab-validation-runbook.md` and
  `docs/lab-bundle-contract.md`; never put credential payloads in profiles, reports, or Git.

Read `docs/read-only-safety.md` before changing `src/ceph_incident_bundle/remote_collector.py`.
Collection modules below `src/ceph_incident_bundle/collect/` have narrower local instructions.

The Python rewrite, cutover audit, and qualification records are historical evidence. Do not read
or treat them as current requirements unless the task is specifically investigating that history.

## Lean change contract

Before implementation, state one observable outcome, a risk tier, no more than three acceptance
criteria, and explicit non-goals. Do not add frameworks, compatibility layers, unrelated
refactors, or hardening outside the current threat model.

- **Tier A — local data or documentation:** warning at 3 changed files, 100 production lines, or
  3 new tests.
- **Tier B — ordinary observable behavior:** warning at 5 changed files, 300 production lines, or
  8 new tests.
- **Tier C — archive, publication, signals, or cleanup:** set a task-specific budget first and
  handle no more than three current threats.

Crossing a warning requires stopping before implementation to narrow the change or obtain explicit
approval. Use at most one full review/fix cycle. Fix reproducible defects, data-loss risks, stated
acceptance failures, and security issues inside the current threat model. Defer other observations;
do not require zero findings.
