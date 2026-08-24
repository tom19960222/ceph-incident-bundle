# Operational Read-Only Safety Contract

`ceph-incident-bundle` preserves incident evidence; it does not repair, configure, or certify the
systems it observes. When a source cannot be collected safely, that source fails or becomes
partial instead of using a mutating or structurally unsafe fallback.

## Trust boundary

The Collection Workstation OS, invoking user, inventory author, and configured workspace and
output parents are trusted. SSH results, remote output, Node Evidence Archives, and Kubernetes and
Prometheus responses are untrusted.

The collector defends against unsafe archive structure, command injection, output overwrite,
workspace escape, and loss of successfully admitted evidence. V1 does not defend against a
malicious local user replacing trusted directories during collection, absurdly large
human-authored integers, unavailable terminal streams after successful publication, or every
theoretical syscall race. See ADR-0015.

## Operationally read-only collection

Collector and validation code must not:

- change persistent configuration, services, packages, mounts, filesystems, block devices,
  networking, host identity, or time settings;
- change Ceph desired state or data;
- create, modify, delete, scale, enter, or otherwise change Kubernetes objects or workloads;
- install a runtime or use `cephadm shell`, `kubectl exec`, a toolbox, debug Pod, ephemeral
  container, port-forward, or mutating fallback;
- write to evidence sources or delete anything not owned by the current invocation.

Collector-owned local and remote workspaces, output files, and unavoidable observation effects
such as access logs, audit records, atime, caches, and counters are permitted. Known cleanup
residue is reported rather than hidden or removed with a broader cleanup.

External programs use explicit argument arrays, never shell interpolation. Ceph collection uses
fixed direct `ceph` commands, Kubernetes collection uses fixed local read commands, and Prometheus
collection uses HTTP GET.

## Source and archive safety

Selected remote files are read without following links. Directory traversal stays beneath the
selected root, opened leaves are checked as regular files, and failures are reported while later
independent evidence continues.

A Node Evidence Archive is stored in an invocation-owned private workspace and validated in full
before extraction. Reject invalid or truncated streams, absolute or traversing paths, empty names,
links, devices, FIFOs, sockets, other special members, duplicate or normalized-colliding names,
ancestor conflicts, and members outside the fixed node evidence schema. Extraction must not trust
archive ownership or permissions or overwrite paths outside its new destination.

Cleanup is limited to resources created by the invocation. Publication must not replace an
existing final output. A valid archive may accompany a nonzero remote status and still contribute
partial evidence; an unsafe or incomplete archive contributes nothing.

## Raw evidence

Evidence is preserved without redaction, secret scanning, semantic validation, sensitivity
classification, or a safe-to-share claim. Profiles and reports may name credential paths but must
never contain credential payloads.

Real-lab identity, stable-state, residue, and evidence-retention gates are not production defaults.
They apply only to an explicitly authorized acceptance run under
`docs/lab-validation-runbook.md` and `docs/lab-bundle-contract.md`.
