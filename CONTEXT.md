# Ceph Incident Evidence

This context defines the language for preserving evidence during Ceph incident investigation without changing the operational state being investigated.

## Language

**Incident Bundle**:
A portable collection of raw evidence preserved by one incident-time collection. It is not a backup, a repair payload, or a claim that the evidence is complete or safe to share.
_Avoid_: Diagnostic dump, backup

**Raw Evidence**:
Evidence preserved without redaction, secret detection, semantic content validation, sensitivity classification, or content-dependent handling. Credentials may be present, but the collector does not treat the evidence differently because of them.
_Avoid_: Sanitized evidence, shareable evidence

**Admissible Artifact**:
A regular file or ordinary directory represented at one unique, safe relative path inside collector output. Link semantics, special filesystem objects, and paths that escape or collide within the output root are not admissible evidence.
_Avoid_: Arbitrary filesystem object

**Evidence Probe**:
A built-in read-only command invocation whose raw standard output, standard error, and exit status are preserved as evidence. One Probe is independent of every other Probe.
_Avoid_: Check, assertion, user command

**Probe Capture**:
The directory produced by one attempted Evidence Probe. It keeps byte-for-byte `stdout` and `stderr` files separate from collector-authored execution facts in `result.json`, including a nullable exit code and an explicit outcome such as exited, failed to start, or timed out.
_Avoid_: Combined transcript, annotated command output

**Best-effort Collection**:
A collection that attempts independent evidence sources and preserves whatever succeeds instead of requiring all requested evidence to succeed together.
_Avoid_: All-or-nothing collection

**Operationally Read-only Collection**:
A collection run that leaves persistent configuration, services, packages, mounts, Ceph desired state, and Kubernetes objects or workloads unchanged. Collector-owned ephemeral work and unavoidable observation side effects are allowed, but unreported residue is not.
_Avoid_: Zero-write collection, non-invasive collection

**Collection Workstation**:
The operator-controlled machine that starts collection, coordinates Target Nodes, and owns the resulting Incident Bundle.
_Avoid_: Collector node, control node

**Target Node**:
A remote server selected for node-local evidence collection and reached from the Collection Workstation over SSH.
_Avoid_: Local node, managed agent

**Node Baseline**:
The default point-in-time evidence about a Target Node's identity, clock, operating system, load, compute, processes, storage, network, kernel, failed services, and container runtime state.
_Avoid_: Health check, node validation

**Time Synchronization Evidence**:
Raw status and configuration evidence from the common time synchronization implementations that may exist on a Target Node. Collection attempts every supported implementation without choosing an active one first.
_Avoid_: Time validation, clock health result

**Log Evidence Window**:
One relative duration shared by filesystem-log and journal collection. Filesystem eligibility is an intentionally approximate decision based on file modification time, while journald applies the duration through its own time query.
_Avoid_: Exact incident boundary, per-log window

**Journal Evidence**:
One all-system human-readable journal capture for the Log Evidence Window, together with any raw journal regular files admitted by the ordinary `/var/log` rules.
_Avoid_: Per-service journal set, journal health report

**Ceph Cluster Evidence**:
Raw point-in-time output from the fixed direct `ceph` query set, collected once for a Ceph cluster. It describes cluster state without repairing or changing that state.
_Avoid_: cephadm evidence, Ceph health result

**Node-local Ceph Evidence**:
Raw Ceph configuration files and a metadata-only view of Ceph state paths on each Target Node. It excludes daemon databases, block data, and all `cephadm` command output.
_Avoid_: Ceph Cluster Evidence, Ceph data backup

**Rook Cluster Evidence**:
Raw Kubernetes object, event, and Pod/container log output collected from the Collection Workstation in the configured external-consumer and operator namespaces. It uses only read operations and never starts a process inside a workload.
_Avoid_: Kubernetes backup, toolbox evidence

**Prometheus Evidence**:
Optional raw Prometheus API evidence collected directly from the Collection Workstation for the shared Log Evidence Window. It is absent when no Prometheus endpoint is configured.
_Avoid_: Metrics analysis, monitoring health result

**Prometheus Capture**:
The directory produced by one attempted Prometheus HTTP GET. It keeps the byte-for-byte response body in `response` and collector-authored request facts in `result.json`; it is not a Probe Capture and has no artificial standard streams or process exit code.
_Avoid_: HTTP Probe, parsed metrics result

**Metrics Filter**:
An optional regular expression applied to discovered Prometheus metric names before range queries are scheduled. An empty filter admits every discovered metric name.
_Avoid_: Job selector, metrics validation

**Collection Scope**:
The exact set of named Target Nodes explicitly selected by the operator for one collection. Cluster observations never expand this set implicitly.
_Avoid_: Discovered fleet, inferred targets

**Inventory Name (`inventory_name`)**:
The stable operator-facing name on the left side of one `[nodes]` entry. It identifies that Target Node throughout collection and names its directory below `nodes/`; it is distinct from the address used as the SSH destination.
_Avoid_: SSH address, discovered node name

**Inventory Name Collision**:
Two Node Inventory entries whose `inventory_name` values have the same portable NFC-normalized and case-folded path key. A collision means the inventory requires operator editing and cannot define a Collection Scope.
_Avoid_: Duplicate host, automatically renamed node

**SSH Address (`ssh_address`)**:
The hostname, IPv4 address, or IPv6 address on the right side of one `[nodes]` entry. Together with the common SSH user, it identifies the OpenSSH destination but never controls the node's evidence directory name.
_Avoid_: Inventory Name, `user@host`, host-and-port string

**Node Inventory**:
A declarative file that defines one common SSH user, maps each `inventory_name` to a Target Node `ssh_address`, and may select one Target Node as the Ceph query source. The nodes present in this file are the Collection Scope.
_Avoid_: Shell inventory, discovered targets

**Inventory Snapshot**:
The byte-for-byte copy of the accepted Node Inventory stored in an Incident Bundle as collection context. It is neither normalized nor redacted and receives no content-dependent handling.
_Avoid_: Effective configuration, sanitized inventory

**Remote Node Collector**:
An ephemeral collector executed on a Target Node to preserve node-local incident evidence. It is transferred for one collection run and is not installed as a persistent agent.
_Avoid_: Agent, daemon, installed collector

**Node Evidence Archive**:
The transient gzip-compressed tar stream emitted by one Remote Node Collector to transport that Target Node's evidence to the Collection Workstation. It is untrusted transport input, not an Incident Bundle, and exists on disk only in a collector-owned workstation workspace while structural admission is decided.
_Avoid_: Incident Bundle, remote bundle, trusted archive

**Skipped Node**:
A Target Node for which no Node Evidence Archive was admitted because collection could not proceed or complete safely. The collection command prints the reason for the operator, while the Incident Bundle represents the requested node only through its Inventory Snapshot and contains no node directory or failure record.
_Avoid_: Ignored node, missing node

**Partial Collection**:
A collection that preserves a usable Incident Bundle while truthfully reporting that one or more requested evidence outcomes were skipped or failed.
_Avoid_: Complete collection
