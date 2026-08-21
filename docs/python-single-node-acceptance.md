# Live single-Target-Node acceptance record

## Verdict

**PASS — evidence-only, operationally read-only acceptance.** The exact post-#102
production head ran one reviewed Target Node through the installed production
collection path. No production code, test seam, architecture, Lab Profile,
inventory authority, credential, or lab state was changed.

This is the issue #103 single-node gate. It is not the full-environment gate and
does not qualify Ceph, Rook, or Prometheus collection coverage for issue #104.

## Immutable subject and authority

| Item | Recorded value |
| --- | --- |
| Qualified production commit | `1c1e5bc65d5f4736319d910d733d3705e0d854c5` |
| Passing run | `READONLY-20260821T060526Z-d75617ab` |
| Boundary marker | `READ-ONLY RUN STARTED READONLY-20260821T060526Z-d75617ab` |
| Fresh discovery authority | `DISCOVERY-20260820T190404Z-6481c053` |
| Discovery manifest SHA-256 | `b1892930d7ef2eb4a8118a77421cdde174fd26c1f18c7b383c7a9e8da8d10b6c` |
| Active profile canonical SHA-256 | `2b1bf4f89bc402643c518e667b3c45a0d2029ba92960b3034d89e1e724d2c70a` |
| Single-node inventory SHA-256 | `e43c5d13a4b924a91e71e62bc2de5ab22e48f4c7bd4ac616485271524c6ce79d` |
| Installed-product wheel SHA-256 | `adfdd2b0a3ddf28104baf2c1d161887f2ec8c333277e905fe8366711d612ee47` |

The original manager authorization explicitly allowed this read-only live gate.
The active TOML Lab Profile was the only machine-readable connection authority.
The selected Target Node's offered host keys, hostname, and inventory identity
matched that profile before collection. Credential files were owner-only and only
their paths were passed to the transport. The acceptance harness never read or
persisted the SSH private-key or kubeconfig credential payload. The Incident
Bundle contains unredacted Raw Evidence and may contain node-local credentials,
so it remains local-only and was never committed or uploaded.

## Runtime and one-SSH proof

- The installed workstation command ran on pre-provisioned CPython 3.10.19.
- The fixed remote `python3` resolved before and after collection to
  `/usr/local/bin/python3`, CPython 3.10.20, with an exact structured identity
  match. No runtime was installed, repaired, switched, or searched by alternate
  name.
- The production command started exactly one system OpenSSH process. Its product
  argv was the fixed noninteractive protocol: `ssh -T -o BatchMode=yes -o
  ConnectTimeout=15 root@<pinned-target> python3 - --since-seconds 3600
  --probe-timeout-seconds 1800`.
- The checked-in standalone Remote Node Collector source was the SSH standard
  input. Standard output carried the Node Evidence Archive and standard error
  carried diagnostics. There was no secondary SSH, SCP, SFTP, local shell,
  `sudo`, `cephadm`, remote Kubernetes, `kubectl exec`, port-forward, alternate
  interpreter, or package-install path.

## Delivery, archive admission, and truthful partial result

The installed command exited zero and produced exactly one standard-output line
containing the delivered path and `partial` outcome. Standard error retained the
four natural Target Node failures: missing `ntpq`, two nonzero `timedatectl`
timesync queries, and absent `systemd-timesyncd` service status. The Remote Node
Collector still completed later probes, file collection, archive streaming,
workstation admission, and final publication.

The delivered bundle:

- has SHA-256 `b1162d0b28ee4a5a618945d9facd89be0ce578c7d2ca90a582c16766e335f46e`;
- contains 152 unique ordinary file/directory members and no link, special,
  absolute, traversing, ambiguous, duplicate, or portable-colliding path;
- contains the exact accepted Inventory Snapshot;
- contains all 26 expected Node Probe Captures with separate raw stdout, raw
  stderr, and exact-schema `result.json`;
- captures the pinned hostname and reports the same `partial` outcome in
  `collection.json` as on standard output.

This demonstrates that an evidence failure does not discard later admissible Raw
Evidence and that a delivered Partial Collection exits zero. A separate local
invalid-inventory invocation opened no live connection, returned nonzero, wrote
no false standard-output result, and ended standard error with exactly `FAIL: no
Incident Bundle delivered`.

## Read-only state and cleanup proof

Before and after the live production invocation:

- the Target Node runtime, package inventory, persistent service configuration,
  systemd override files and symlink targets, persistent mount intent, actual
  root mount, Ceph configuration, machine identity, and filesystem table were
  unchanged;
- the reviewed Ceph and Kubernetes stable desired-state schema was unchanged;
- no new remote collector workspace or helper process existed;
- no workstation collector workspace, incomplete private candidate, or other
  known collector-owned residue remained.

The unit-file comparison ignores only the identity of rows whose exact
`systemctl list-unit-files` state is `transient`; it still compares their count
and preset set. Every non-transient row remains exact after order normalization,
and the independent `/etc/systemd/system` tree comparison retains every file and
symlink target. This narrow projection is backed by read-only diagnostic run
`DISCOVERY-UNITFILES-20260821T060209Z-2d78e32f716d`: its only semantic change was
one SSH session scope replacing another, both state `transient`, preset `-`,
while the exact persistent systemd tree remained equal. The diagnostic manifest
SHA-256 is `4a6d5b538bec87031b4eaca8810c81b320deeddba5cc454113b55e9e23655b76`.

No cleanup failure occurred in the normal live run. First-phase policy forbids
live fault injection, so no failure was induced on the Target Node or
workstation. The passing #102 offline CPython 3.10 gate retains the required
workstation-cleanup-residue and remote-cleanup-failure tests, including truthful
partial delivery and known-location reporting.

## Fail-closed attempts and local retention

Two earlier run IDs remain FAIL rather than being overwritten or reclassified:

| Run | Verdict | Reason | Manifest SHA-256 |
| --- | --- | --- | --- |
| `READONLY-20260821T054803Z-fd116858` | FAIL | The first acceptance projection compared the complete live mount tree and raw unit-file byte order as stable state. | `d4f0ed34c3ad6643351a9b7683f007e091b442a52d84cce8ee398a3226828adb` |
| `READONLY-20260821T055648Z-584d75b8` | FAIL | Mount intent was corrected, but complete sorted unit-file rows still included the observation SSH session's transient scope identity. | `b74e80a53a0d2c9c971f76b7680ea9665eb3d8271d9bc7e795ac3f9866bd1854` |

Both failed runs independently showed an exit-zero Partial Collection, exact
runtime match, unchanged Ceph/Kubernetes desired state, and clean remote and
workstation residue. They are not PASS evidence; they preserve the audit trail
that led to the exact transient-session classification.

The passing run's owner-only, gitignored raw evidence remains local under
`results/live-single-node/READONLY-20260821T060526Z-d75617ab/`. Its evidence
manifest SHA-256 is
`c1368ee5300b292c08dae4fe3eec13b7a4ebf1e52979c59fa163a2074822eae5`.
The two FAIL roots and the diagnostic root are also retained local-only. Raw
bundles, raw Probe output, complete inventories, internal endpoints, public host
keys, and command diagnostics are not committed or uploaded.

## Related gates

Issue #101's independent readability/architecture gate is already closed at
this production head. Issue #102 records the exact CPython 3.10 installed-product
offline qualification: 193 tests passed, including archive adversaries,
ordinary cleanup failures, partial delivery, nondelivery, one-SSH transport, and
the post-cutover repository surface.

No live finding changed production code or architecture, so no regression code
change or repeated product review was triggered. This issue still requires its
fresh independent Standards and Spec review over this evidence-only record before
publication.
