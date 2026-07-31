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
- `docs/lab-bundle-contract.md` for what the real-lab gate compares and what its stable-state snapshot keeps.

The non-negotiable rules are:

- Collection must not change persistent configuration, services, packages, mounts, Ceph desired state, or Kubernetes objects/workloads. Writes are limited to collector-owned workspaces and final bundle output; query-side audit/access logs and counters may change naturally.
- Treat every Node Evidence Archive as untrusted. Validate every member before extraction, and never allow absolute paths, traversal, links, devices, FIFOs, or writes outside the collector-owned workspace.
- Real-lab qualification must keep `cephadm shell` and `kubectl exec` disabled. Those opt-ins can create runtime side effects and cannot be used as read-only proof.
- Fail closed on any SSH fingerprint, Ceph/Rook identity, required-node, or endpoint mismatch. Never bypass an identity mismatch to make a gate pass.
- `CEPH-LAB-CONNECTION.md` is human-maintained context only. Real-lab validation automation must use an explicitly selected local TOML Lab Profile and must never parse that Markdown file. Ordinary inventory-driven collect retains its documented CLI contract and is not, by itself, qualification evidence.
- Profiles and reports may reference credential paths, but must never contain private keys, keyrings, passwords, tokens, or other credential contents.
- Ordinary `make validate` must remain offline. Real-lab execution always requires a separate explicit opt-in and a reviewed active Lab Profile.

`make lab-status`, `make lab-profile-discover`, `make lab-profile-activate` and `make lab-preflight` are implemented (issue #19), as is `make validate-lab` — the dual-run full-collect gate (issue #20). Start from `make lab-status LAB_PROFILE=/absolute/path/to/lab.toml` and follow its single `next_action`. A passing `lab-preflight` proves lab identity only; only a `validate-lab` report with `status: pass` is qualification evidence. The harness existing is not the same as qualification having happened: running it in a real lab and deciding cutover is issue #21, so until such a report exists, do not claim the candidate passed the real-lab gate and do not assemble an ad-hoc substitute for it.

## Equivalence claims

`make validate` includes the offline observable-contract equivalence gate (`make test-differential`): the shell reference and the Python candidate run the same scenarios in one shared fake world and their normalized contracts are compared. Before changing either implementation or the normalizer, read:

- `docs/differential-normalizer.md` — the only list of differences the gate may ignore. Widening it needs the same review as changing behaviour.
- `docs/test-scenario-ledger.md` — which shell scenario each Python test covers, plus the gate declaration and its exact scope.
- `docs/test-scenario-audit.md` — the by-hand reading of whether those tests assert every clause of the row they are pointed at: the audit method, the per-row findings, and the fingerprints that force a re-audit whenever the inventory row or the ledger's coverage cell changes. It records a reading; it does not enforce one.

The gate was declared passed on 2026-07-30 for the workstation-side contract (#18). That declaration is not qualification: it says the two implementations agree offline, nothing about a real lab.

The differential gate compares the workstation side: both implementations receive the same canned Node Evidence Archive, so it says nothing about the node collector's own evidence surface. That surface is fully ported (#36), but its equivalence is *asserted* by the ledger's N-series black-box tests (`tests/test_python_collect_node.py`) — hand-written against the shell contract — not *demonstrated* by a shell-versus-Python comparison; ADR 0010 also makes the node manifest deliberately diverge. Say it that way when the distinction matters.

`make validate-lab` (#20) is the real-lab counterpart, and it compares a different thing on purpose: two collects of a *live* cluster observe different moments, so it compares the bundles' member set, manifest (command argv and exit code), capture headers, whether each artifact still parses as JSON, skip classes, source/runner selection and four-path coverage — not evidence bodies or `/var/log` bytes. `docs/lab-bundle-contract.md` is the reviewed statement of that scope. The gate has not been run against a real lab, so do not claim the candidate is qualification-ready.
