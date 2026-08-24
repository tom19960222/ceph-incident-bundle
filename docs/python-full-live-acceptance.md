# Full-environment live acceptance record

> **Historical evidence:** Do not read this document or treat its qualified snapshot as a current
> requirement unless investigating the historical qualification.

## Verdict

**PASS — evidence-only, operationally read-only acceptance.** The exact post-#103
production head completed the issue #104 full configured environment gate through
the installed production CLI. The run covered all seven inventory nodes, direct
Ceph collection, local Rook/Kubernetes collection, direct Prometheus collection,
atomic publication, untrusted bundle admission, and before/after state and cleanup
proof.

No production code, production test, architecture, Lab Profile, inventory,
credential, Ceph desired state, Kubernetes object, service, package, mount, or
runtime was changed. This qualification adds only this sanitized record.

## Immutable subject and authority

| Item | Recorded value |
| --- | --- |
| Qualified production commit | `6b84ddf155ef01954fe6a21bfc882446993b34e6` |
| Qualified production tree | `ca7a5c208c3c64a19538e07f49cf911eeeaa3831` |
| Passing run | `FULL-20260821T071547Z-07210100` |
| Boundary marker | `READ-ONLY RUN STARTED FULL-20260821T071547Z-07210100` |
| Fresh discovery authority | `DISCOVERY-20260820T190404Z-6481c053` |
| Discovery manifest SHA-256 | `b1892930d7ef2eb4a8118a77421cdde174fd26c1f18c7b383c7a9e8da8d10b6c` |
| Active profile canonical SHA-256 | `2b1bf4f89bc402643c518e667b3c45a0d2029ba92960b3034d89e1e724d2c70a` |
| Full inventory SHA-256 | `a2c93805cdbf9e1c72e518c3a55cca749988da2a52ce25262bb7d501aa0f571e` |
| Installed-product wheel SHA-256 | `fbad45b07c49e436a3f9a28df1ba3f7c340cf2368d9627f97f832127a53bacf0` |
| Installed CLI SHA-256 | `b2632dec2efb8dd9b5a2b004ac4c2ae7c9fa9d1075beb372fbf6ce90048a3e7e` |
| Settle diagnostic manifest SHA-256 | `aa116ba77b75c9515a15100746b9ad76780df3b3556852b74528cca8a8c703c5` |
| Numeric inspector contract SHA-256 | `e8e03842e2090406fa7e10dc4e4b065dc9bb4ec69f7fbd37c2d062ceb9fb7404` |

The original authorization explicitly opted into this real-lab read-only gate.
The selected active TOML Lab Profile was the only connection authority; the
legacy connection note was not read. Every offered host key, hostname, runtime,
Ceph FSID, Rook FSID, Kubernetes context and namespace, and Prometheus readiness
identity matched the pinned authority before collection. Credential payloads
were not persisted in the report or committed evidence.

## Production path and ordering

- The installed workstation command ran from a fresh CPython 3.10.19 virtual
  environment. All seven fixed remote `python3` runtimes resolved before and
  after collection to `/usr/local/bin/python3`, CPython 3.10.20, with exact
  structured identity matches.
- The full inventory launched exactly seven system OpenSSH processes in accepted
  inventory order. The configured first inventory node collected direct Ceph
  evidence in that same SSH session; no secondary Ceph SSH path existed.
- Local Kubernetes began only after all seven inventory SSH launches completed.
  It used the exact configured context and two namespaces, with five fixed
  control captures and 32 topology-derived current/previous log captures.
- Direct Prometheus GET collection followed Kubernetes. Atomic publication
  followed all sources. The collector used no local shell, `cephadm shell`,
  `kubectl exec`, port-forward, SCP, SFTP, remote Kubernetes path, alternate
  runtime, package installation, or mutating command.

The production invocation, with only owner-local paths elided, was:

```text
<installed-cli> collect \
  --inventory <owned-run-root>/full-inventory.ini \
  --since 24h \
  --output-dir <owned-run-root>/bundle-output
```

The installed CLI and Inventory Snapshot are identified by their hashes above.
The published bundle's actual run-relative location was
`bundle-output/ceph-incident-bundle-20260821T071951Z.tar.gz`.

## Bundle admission and configured coverage

The installed command exited zero and wrote exactly one standard-output result
line naming the delivered `partial` bundle. Standard error retained natural
attempt failures rather than suppressing them. The published bundle has SHA-256
`e6bf8cc8637e802f3d7afae58735b0388d4cd93a7012a8f224bc890b8a875e6e`,
size 397,807,118 bytes, and 4,072 unique members.

Untrusted archive inspection proved that every member was an ordinary file or
directory; paths were relative, traversal-free, portable-unique, collision-free,
and free of file-as-ancestor conflicts. The exact accepted Inventory Snapshot
was present. Complete configured coverage was:

- seven admitted Node Evidence Archives in inventory order;
- 24 fixed Ceph Probe Captures and eight crash-detail captures, with no Ceph
  capture failure;
- five Rook control captures and 32 log captures, with no log failure;
- three admitted Prometheus jobs, three metric-name discoveries, and 894 unique
  24-hour range pairs at the required 15-second step, with no range failure.

Rook log capture paths encode one six-digit numeric sequence across current and
previous captures. The inspector required sequences 1 through 32 to be unique
and contiguous, required each stdout/stderr/result triplet, and compared argv in
numeric sequence order with both the observed Pod/container topology and the 37
entry local kubectl process ledger. All 32 matched. Lexical member-path order is
not treated as execution order.

The consumer namespace observed no Pods, so its regular, init, ephemeral, and
qualifying previous counts were all zero. The operator namespace observed 16
regular containers and no init or ephemeral containers; every regular container
qualified for one previous capture, giving 16 current plus 16 previous captures.
This was the complete observed topology and stayed inside the confirmed model.

The `partial` outcome is truthful: 30 attempted Node Probe failures were retained
with their raw stderr/result records. They were the expected unavailable
container or time-synchronization surfaces on this topology. Collection
continued through all later Node, Ceph, Rook, Prometheus, publication, and
cleanup work; no Ceph, Rook log, or Prometheus range capture failed.

For a delivered `complete` or `partial` bundle, the production contract is exit
zero plus exactly one stdout line in the form `<delivered-path> (<outcome>)`;
attempt diagnostics remain on stderr. Publication has no minimum Raw Evidence
threshold: a metadata-only admitted partial bundle is still a truthful delivery.
For nondelivery, the contract is nonzero, empty stdout, diagnostic stderr ending
exactly `FAIL: no Incident Bundle delivered`. Nondelivery was not induced in this
live run. Issue #102's installed-process offline qualification carries that path,
including startup rejection, source failure, admission failure, publication
failure, cleanup residue, and metadata-only partial delivery.

## Read-only state and cleanup proof

Before and after the passing production invocation:

- all seven runtime identities matched exactly;
- all seven package inventories, persistent service configuration, systemd
  override trees and symlink targets, persistent mount intent, actual root
  mounts, Ceph configuration, machine identities, and filesystem tables were
  unchanged under the reviewed stable projection;
- Ceph monitor identity, CRUSH topology, pools, configuration, Rook CephCluster
  desired state, and configured Kubernetes Deployment, StatefulSet, and
  DaemonSet desired state were unchanged;
- Prometheus readiness status, body size, and body hash were unchanged;
- all seven remote residue comparisons were clean; and
- no workstation collector workspace, private bundle candidate, or temporary
  uncompressed inspection copy remained.

No cleanup failure occurred in the normal live run. Live fault injection was not
performed because qualification policy forbids deliberately leaving residue in
the lab. The passing #102 offline CPython 3.10 gate retains the required
workstation-cleanup-residue and remote-cleanup-failure evidence among its 193
passing tests.

## Retained external-drift failure and settle proof

The earlier `FULL-20260821T063100Z-7c16e9bf` run remains **FAIL** and was not
overwritten or reclassified. Its manifest SHA-256 is
`89cbc1326b4cf356056274016d823b500df446d9cfdd129e415ac923331ff54a`.
During its collection window, unattended operating-system upgrades changed the
package inventories of two monitor-role nodes and one OSD-role node:

- two nodes each recorded 15 version upgrades, with 13 `amd64` and two `all`
  packages; and
- one node recorded six version upgrades, with four `amd64` and two `all`
  packages.

The exact dpkg and semantic inventories prove upgrade-only transactions:
added=0, removed=0, status changes=0, and every resulting package status was
`ii`. The package service logs place all three transactions inside the failed
acceptance interval and identify them as unattended upgrades, not collector
commands. Runtime, all other Target Node stable fields, Ceph/Rook desired state,
remote residue, workstation residue, bundle admission, and configured coverage
were independently completed and remained valid, but package drift is sufficient
to fail that run.

The owner-only diagnostics remain local with manifests:

| Evidence | Manifest SHA-256 |
| --- | --- |
| First exact package diagnostic | `0f05165a3a624a2a2404d0630a669917dabd3d40c262261c6727f00036d024d7` |
| Cross-node package attribution | `1846a6452d25eac366ec2999afc93051d19ad2f5afb0e2f4410404ff38c6f843` |
| Seven-node settle diagnostic | `aa116ba77b75c9515a15100746b9ad76780df3b3556852b74528cca8a8c703c5` |
| Completed failed-run offline analysis | `871d2f26c7676eb60d21f3676199037f6d79c26a5902ec2d8cba2c2c852f1c1c` |

The settle diagnostic captured every node twice, 142.883 seconds apart. All
seven legacy and semantic package inventories matched exactly between captures,
and both captures observed zero apt/dpkg transaction processes. No timer or
package setting was stopped, disabled, or changed. Only after this proof and the
completed failed-run audit was a fresh run ID allowed to start.

## Local retention and related gates

The passing run's owner-only, gitignored raw evidence remains local under
`results/live-full/FULL-20260821T071547Z-07210100/`. Its 27-entry evidence
manifest SHA-256 is
`1588be5782c4bb47afaa837d78644fedb9055bf9a0f8c93c6d02b93ad94156bd`;
every entry was independently rehashed after completion. The retained FAIL,
diagnostic, settle, and analysis roots also remain local-only. Raw bundles,
complete inventories, internal addresses, host keys, credential paths, raw
Probe output, and command diagnostics were not committed or uploaded.

Issue #101's independent readability/architecture gate passed in PR #134 at
merge commit `a0f8aeb53eeb7494e682def222ba08226b0600d7`. Its final reviewer
configuration was three Claude Sonnet 5 medium reviews: whole-system,
Standards, and Spec. All three returned ACCEPT with zero findings after the final
fix removed two production-private-helper test dependencies without changing
production code. Issue #102's offline CPython 3.10 installed-product
qualification and issue #103's single-node live acceptance remain separate
prerequisites. No live finding required a production or test change, so this
issue is an evidence-only qualification record.
