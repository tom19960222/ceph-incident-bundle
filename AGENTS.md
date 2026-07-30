# AGENTS.md

## Agent skills

### Issue tracker

Issues live as GitHub issues in `tom19960222/ceph-incident-bundle`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Evidence collection safety

This repository builds an operationally read-only evidence collector. Before changing or running collection code, read:

- `docs/read-only-safety.md` for the safety contract and proof obligations.
- `docs/lab-validation-runbook.md` for the fail-closed lab workflow.

The non-negotiable rules are:

- Collection must not change persistent configuration, services, packages, mounts, Ceph desired state, or Kubernetes objects/workloads. Writes are limited to collector-owned workspaces and final bundle output; query-side audit/access logs and counters may change naturally.
- Treat every Node Evidence Archive as untrusted. Validate every member before extraction, and never allow absolute paths, traversal, links, devices, FIFOs, or writes outside the collector-owned workspace.
- Real-lab qualification must keep `cephadm shell` and `kubectl exec` disabled. Those opt-ins can create runtime side effects and cannot be used as read-only proof.
- Fail closed on any SSH fingerprint, Ceph/Rook identity, required-node, or endpoint mismatch. Never bypass an identity mismatch to make a gate pass.
- `CEPH-LAB-CONNECTION.md` is human-maintained context only. Real-lab validation automation must use an explicitly selected local TOML Lab Profile and must never parse that Markdown file. Ordinary inventory-driven collect retains its documented CLI contract and is not, by itself, qualification evidence.
- Profiles and reports may reference credential paths, but must never contain private keys, keyrings, passwords, tokens, or other credential contents.
- Ordinary `make validate` must remain offline. Real-lab execution always requires a separate explicit opt-in and a reviewed active Lab Profile.

The `lab-status`, `lab-profile-discover`, and `validate-lab` targets described by the runbook are planned interfaces owned by issues #19 and #20. Until those tickets land, do not invent ad-hoc replacements or claim that the automated real-lab gate exists.

## Equivalence claims

`make validate` includes the offline observable-contract equivalence gate (`make test-differential`): the shell reference and the Python candidate run the same scenarios in one shared fake world and their normalized contracts are compared. Before changing either implementation or the normalizer, read:

- `docs/differential-normalizer.md` — the only list of differences the gate may ignore. Widening it needs the same review as changing behaviour.
- `docs/test-scenario-ledger.md` — which shell scenario each Python test covers, and which are still blocked.

The gate compares the workstation side. The node collector's own evidence surface is only partly ported (#36), so do not claim the candidate is observable-equivalent, feature-complete or qualification-ready.
