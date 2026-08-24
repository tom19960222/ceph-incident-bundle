# Python V1 Real-Lab Bundle Contract 與 Stable-State Schema

> **Opt-in only:** Read this document only when executing or changing an explicitly authorized
> real-lab acceptance workflow. It is not a default requirement for product changes.

Issues #85 and #115 define the current Python-only contract. A live acceptance harness inspects
the installed collector's published bundle as untrusted input and compares pre/post stable state;
there is no product verifier, product manifest requirement, legacy differential runner or live-lab
Make target. Expanding or shrinking this contract needs the same review as a collector behavior
change.

## Current bundle surface

The final gzip tar has exactly one top-level bundle directory containing:

- `inventory.ini` — the validated input inventory snapshot;
- `collection.json` — exactly `collector_version`, `started_at`, `finished_at`, `since`, and
  `outcome`; timestamps are RFC 3339 UTC ending in `Z`, and outcome is `complete` or `partial`;
- `nodes/` — admitted Node Evidence Archives, one directory per inventory alias;
- `ceph/` — admitted same-session direct Ceph evidence when configured;
- `kubernetes/` — admitted local Rook evidence when configured;
- `prometheus/` — admitted local HTTP evidence when configured.

Only complete, atomically promoted source contributions may enter publication. The four source
directories are fixed schema boundaries; an empty directory is not admitted evidence. If every
evidence attempt fails but final construction succeeds, the metadata-only bundle is still a
truthful deliverable `partial` with exit zero. Capture bytes and result records remain raw;
acceptance reads only the bounded artifacts needed for structural and coverage decisions.

Each Probe Capture has exactly `stdout`, `stderr`, and `result.json`. The JSON has exactly six
fields: `argv`, `started_at`, `finished_at`, `outcome`, `exit_code`, and `error`. Outcome is
`exited`, `failed_to_start`, or `timed_out`; exit code is an integer only for `exited`, and error is
null or exactly `{ "kind": <string>, "message": <string> }`. Dynamic Kubernetes log and
Prometheus range paths use one-based numeric sequence names; width may grow beyond six digits
without collision.

## Current coverage contract

- **Nodes:** every inventory node is attempted in inventory order over exactly one SSH session;
  every accepted archive is mapped back to the expected alias and fixed node schema.
- **Ceph:** only the selected source node receives direct Ceph work in that same SSH session; all
  24 fixed probes run in order, followed by at most the first ten crash details after the complete
  crash response passes strict schema validation.
- **Kubernetes:** after all nodes, four consumer controls run through local `kubectl`; operator
  Pods JSON is the fifth control only when the operator namespace differs. Equal namespaces
  deduplicate control and log work. Regular, init, ephemeral and eligible previous-container logs
  then run in numeric sequence order.
- **Prometheus:** after Kubernetes, fixed control GETs discover jobs and metrics; exact ordered
  first-occurrence dedup and the configured filter determine query-range pairs. All pairs share one
  start/end window and unchanged step.

Configured source failure is truthful `partial`, not silent omission. Every attempted failure must
have a corresponding result/diagnostic, later independent work continues, and source-private
staging never enters the final bundle.

## Historical evidence boundary

Predecessor shell bundles and differential-normalizer notes are retained only in Git history and
prior releases. They are not reconstructed, executed or used to waive current Python coverage.
Current acceptance compares the frozen invocation against its own declared inventory, command
ledger, fixed schemas, configured-source coverage, pre/post stable-state projection and residue.
Live evidence bodies are time-dependent; acceptance validates their raw preservation and
structure, not equality with evidence captured at a different moment.

Historical acceptance anchors are `docs/python-offline-qualification.md` (#102),
`docs/python-single-node-acceptance.md` (#103), and `docs/python-full-live-acceptance.md` (#104).
Their raw manifests and bundles remain owner-only local evidence; this document does not replace
their exact hashes or verdicts.

## Current bundle inspection is untrusted

Bundle 是本次 collect 產生的，但「我們做的」不是信任理由。Acceptance inspection 逐一
檢查 member，遇到 link、device/FIFO/socket 等特殊 member、absolute/traversal/empty
path、duplicate、normalized collision 或 ancestor conflict 就直接 fail closed。Inspection
不信任 archive mode/ownership，不覆寫既有路徑；只有參與驗證的 artifact 會在既有單檔
上限內讀取。

## Stable-State Snapshot Schema（version 1）

本次 live Python collect 之前取一次、之後取一次，兩者必須完全相同。Snapshot 只能包含
**stable identity 與 desired configuration**；whitelist 就是重點——列舉保留欄位，
新版本新增的易變欄位不會突然讓 gate 失敗，而真正的 desired-state 變動也無法躲在
沒人列舉的欄位裡。

| 欄位 | 來源（唯讀） | 保留 | 排除 |
| --- | --- | --- | --- |
| `node_runtime` | pinned SSH runtime probe | hostname、OS、architecture、fixed Python path／implementation／version、required service activity | process/session identifiers、timestamps |
| `node_packages` | semantic package inventory | name、version、architecture、status | command timing與輸出順序 |
| `node_services` | persistent unit files與 `/etc/systemd/system` no-follow tree | non-transient name/state/preset、file bytes、symlink target；transient count/preset set另記 | transient unit identity、session scope number |
| `node_mounts` | `/etc/fstab`、`findmnt --fstab`、fixed root mount | persistent intent與 root mount完整欄位 | container/service live mount tree |
| `node_configuration` | fixed machine-id、Ceph/configuration paths | regular-file bytes與安全 symlink-target tree | optional absent sources |
| `ceph_monitors` | `ceph mon dump --format json` | `fsid`、每個 mon 的 `name`／`rank`／`public_addr` | `epoch`、`modified`、`created`、`election_epoch`、`quorum`、feature bitmap |
| `ceph_crush_topology` | `ceph osd tree --format json` | `id`、`name`、`type`、`device_class`、`crush_weight`、`children` | `status`、`reweight`、`exists`、`primary_affinity` |
| `ceph_pools` | `ceph osd pool ls detail --format json` | `pool_id`、`pool_name`、`type`、`size`、`min_size`、`pg_num`、`pg_num_target`、`crush_rule`、`erasure_code_profile`、application 名稱 | `last_change`、`last_force_op_resend*`、所有使用量統計 |
| `ceph_config` | `ceph config dump --format json` | `section`、`name`、`value` | `level`、`can_update_at_runtime`、`mask` |
| `rook_cephclusters` | `kubectl -n <ns> get cephclusters.ceph.rook.io -o json` | `metadata.name`／`namespace`、整份 `spec` | `status`、`resourceVersion`、`generation` |
| `k8s_{deployments,statefulsets,daemonsets}_<ns>` | `kubectl -n <ns> get <resource> -o json` | `kind`、`name`、`namespace`、`spec.replicas`、container images | `status`、`resourceVersion`、`generation`、annotation、`creationTimestamp` |
| `prometheus_readiness` | fixed HTTP readiness GET | endpoint identity與ready classification | request timestamp、runtime counters |

其他規則：

- Ceph 只用 direct read-only `ceph` CLI。`cephadm shell` 永遠不是 fallback，因為它可能
  啟動 container。
- Kubernetes 只用本機 `kubectl get`；沒有 `exec`，沒有寫入 verb。
- Profile 的 `operator_namespace` 與 `namespace` 不同時，才會多讀一組 workload。
- Ceph 與 Kubernetes 回傳的集合本身無序，所以每個 projection 以自身 canonical
  form 排序：同一組物件換個順序不是變動。
- 任何一個來源讀不到就 fail closed。讀不到不等於沒有變動；一份殘缺的 snapshot 會
  和另一份殘缺的 snapshot 比對成功，那正是這個 gate 不能容許的事。
- Package inventory若因外部 unattended transaction在 run window改變，該 run仍是
  FAIL。只讀 attribution必須列出 added/removed/version/status/architecture與時間；只有
  全部 hosts 在合理間隔的兩次 semantic inventory完全一致且 transaction process為零，
  才能用全新 run ID再做 acceptance。不得停止或修改 timer/service製造穩定窗口。

## Remote Residue

本次 live Python collect 前、後，各對每個 inventory node 取一次
`${TMPDIR:-/tmp}/ceph-incident-node.*`、`ceph-incident-node-*` 的列表與 helper
process 列表。**只有期間新出現的**才算本次 run 的殘留；run 之前就存在的會被如實
報告為 pre-existing，但不歸咎於本次 run。

Probe 只讀：不刪除 workspace，不對 process 送 signal。能「清乾淨讓檢查通過」的
residue check 就不是 residue check；runbook 要求殘留必須送到人手上。

Probe script 的兩個細節是刻意的：開頭的 sentinel 註解讓 offline 測試的 fake
`ssh` 認得它；兩個 process marker 在 script 內以變數拼接，因為 script 自己的文字
會出現在 node 的 `ps` 輸出裡，直接寫出 marker 會讓每次 probe 都把自己回報成殘留。
