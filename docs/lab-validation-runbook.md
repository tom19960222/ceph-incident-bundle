# Real-Lab Validation Runbook

> **Opt-in only:** Read this document only when executing or changing an explicitly authorized
> real-lab acceptance workflow. It is not a default requirement for product changes.

## Current Status

Issues #85 and #115 define a Python-only product. There is no product command or Make target for
live-lab execution. A real-lab run is an agent-managed acceptance action that requires explicit
user opt-in, an active TOML Lab Profile, pinned identities and an owner-only evidence root.

Historical reviewed qualification records are:

- `docs/python-offline-qualification.md` — #102, CPython 3.10 clean-source wheel/install gate.
- `docs/python-single-node-acceptance.md` — #103, one pinned Target Node.
- `docs/python-full-live-acceptance.md` — #104, seven-node Node/Ceph/Rook/Prometheus flow.

The raw reports, manifests and bundles referenced by those documents are local-only evidence. A
future run must create a new run ID and new evidence root; it never overwrites or reclassifies a
retained PASS or FAIL.

## Non-Negotiable Safety Rules

- Use only an explicitly selected, active TOML Lab Profile. Never parse
  `CEPH-LAB-CONNECTION.md`.
- Keep `cephadm shell` and `kubectl exec` unreachable. Python exposes neither opt-in nor fallback.
- Fail closed on profile state, credential permission, SSH fingerprint, hostname, Ceph/Rook FSID,
  Prometheus endpoint, frozen input or runtime identity mismatch.
- Profiles/reports may name credential paths but never persist credential contents.
- Never convert incomplete coverage, unsafe structure, unexplained stable-state drift or residue
  into PASS by rerunning or weakening a comparison. A product `partial` is acceptable only when
  every attempted failure is truthful and all required configured sources were attempted.
- A failed collect keeps its local evidence. Cleanup is a separate explicit operation after the
  failure has been read.

## Agent Entry Point

Start local-only. Read and hash the selected active profile, verify owner-only credential paths,
freeze commit/tree, inventory, wheel, installed console script, interpreter and harness. Do not
connect to the lab until every local prerequisite passes.

Before the first live command, emit exactly:

```text
READ-ONLY RUN STARTED <run-id>
```

The marker authorizes only the already approved run boundary. It does not authorize repair,
package installation, service changes, cleanup outside invocation ownership, a second target or a
rerun. A diagnostic or rerun needs its own unique marker and retained evidence root.

## Gate Order

The first failing stage stops later work, except remote residue is always checked after any
collect starts.

1. **Code and input identity:** tracked files match HEAD; record commit/tree and hashes for the
   profile, inventory, credential-path files, wheel, installed CLI and harness.
2. **Command surface:** inspect fixed argv and wrapper ledgers. No privilege escalation, alternate
   runtime, remote Kubernetes execution, container shell, local shell interpolation or fallback.
3. **Strict identity preflight:** active profile, owner-only credentials, pinned SSH host keys,
   hostnames, expected runtime, Ceph/Rook FSIDs, Kubernetes context/namespaces and Prometheus URL.
4. **Pre-state and residue:** capture stable target state, cluster desired state, runtime and
   invocation-specific residue before collection.
5. **Installed product:** run the frozen `ceph-incident-bundle collect` through the pre-provisioned
   CPython 3.10 interpreter. The acceptance harness never installs or switches runtimes.
6. **Collection order:** all inventory nodes in inventory order; direct Ceph only inside the
   selected node's same SSH session; then local Rook; then local Prometheus; publication last.
7. **Untrusted inspection:** inspect the published bundle under
   [`lab-bundle-contract.md`](lab-bundle-contract.md) without trusting or extracting members.
8. **Post-state and residue:** always capture runtime, stable state and residue after collection
   started, even when collection, inspection or publication failed.
9. **Verdict:** require exact identity, configured coverage, structural safety, truthful outcome,
   stable state and clean residue. Any unexplained mismatch is FAIL.

Once a collect has started, post-runtime and remote-residue probes run even if inspection,
coverage, cleanup or publication failed. A residue finding becomes the primary failure class while
earlier failures remain recorded. The gate never auto-deletes remote residue.

## Report Contract

Every attempt reserves a unique gitignored `results/<acceptance-kind>/<run-id>/` before touching the
lab. Directories are owner-only, ordinary files are owner-readable/writable only, and evidence
scripts retain owner execute only. Each report records frozen hashes, command ledgers, exact exit
and streams, bundle hash/size/member count, coverage, pre/post projections, residue, manifest hash,
one verdict and one next action. Anything not reached remains not observed.

Reports and qualification documents are sanitized: they may name credential paths but cannot
contain private keys, keyrings, passwords, tokens, kubeconfig credentials or raw secret-bearing
payloads.

## Artifact Cleanup

A failed gate deliberately keeps evidence. Cleanup is a separate, explicitly reviewed local
operation after the failure is understood. It must validate the exact root, ownership, manifest,
file types and destination first; it never follows symlinks, never touches the lab and never uses a
broad prefix. Product collection and acceptance do not invoke evidence cleanup.

## Failure and Handoff

- Frozen-input failure: do not touch the lab; correct or explicitly select inputs locally.
- Identity failure: review discovery/profile evidence; never accept-current or rewrite expected
  identity merely to pass.
- Runtime failure: stop. Qualification never changes runtimes.
- Collection/coverage/inspection/publication failure: inspect the named retained local run.
- Stable-state difference: keep FAIL, inspect the command ledger and distinguish product mutation
  from external drift. Do not stop timers or services to manufacture a stable interval. A later
  settle diagnostic or rerun needs a new marker and new evidence root.
- Remote residue: review only the invocation-owned paths/processes; do not broad-delete by prefix.
- Handoff always names the exact evidence directory and manifest SHA-256, not only a chat summary.
