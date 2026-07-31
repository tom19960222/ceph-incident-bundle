# Real-Lab Bundle Contract 與 Stable-State Schema

> 對應 issue #20（同 lab 的 shell／Python 四路 full collect gate）。
> 實作：`validation/lab_bundle.py`、`validation/lab_snapshot.py`、
> `validation/lab_contract.py`；執行入口：`make validate-lab`。

`make validate-lab` 在同一個 real lab 先後跑一次 shell reference full collect 與
一次 Python candidate full collect，然後比較兩份 bundle，並比較兩次 collect 前後
的 stable state。這份文件是那兩個比較「比什麼、刻意不比什麼」的唯一依據；擴大或
縮小任一份清單，需要與改變 collector 行為相同等級的 review。

## 為什麼 real-lab 比較不是 byte comparison

Offline equivalence gate（`make test-differential`，issue #18，見
[`differential-normalizer.md`](differential-normalizer.md)）已經證明：**同樣的
輸入**進去，兩個實作吐出同樣的 bytes。它做得到，是因為它的世界是假的而且是凍結
的。

Real lab 兩者都不是。兩次 qualification collect 相隔數分鐘打在活的 cluster 上，
`ceph -s` 的數字本來就會不同，journal 本來就會多幾行。在那裡要求 evidence bytes
完全相同不是嚴格，而是一道只能靠運氣通過的 gate。

所以 real-lab gate 比較的是「與什麼時候採集無關」的那一部分：

| 比較項目 | 為什麼它是 contract |
| --- | --- |
| member 路徑集合 | 兩個實作必須產出同一組 artifact，放在同一個位置 |
| `manifest.jsonl`（含每個 node 的 manifest） | collector、artifact、完整 command argv 與 exit code — CLI semantics、runner 選擇與 source 選擇都在這裡變成可觀測 |
| 每個 captured artifact 的 `# key: value` header | host、collector、timeout 與 truncation 標記 |
| artifact body 是否解析得出 JSON | 「是不是 JSON」是實作決定的：把 evidence 包裝、截斷或重新序列化的 candidate 會在這裡現形 |
| `environment.txt` 的選擇欄位 | `mode`、`seed`、`since`、`timeout`、`git_commit`、`ceph_source`、`ceph_runner`、`rook_source`、`prom_url`、`prom_jobs` |
| `summary.txt` | `cluster_status`、`node_ok`、`node_failed`、`final_status` — partial collection 在這裡變成可觀測 |
| SKIPPED／partial artifact 的分類 | 兩邊必須以同一個原因略過同一件事 |
| `errors.log` 的事件分類集合 | 兩邊記錄粒度不同，但事件必須相同 |
| 四條 collector path 的 coverage | Ceph、Rook、Prometheus、全部 inventory nodes、`/var/log` |

刻意**不**比較的：

- Captured artifact 的 body 本身，**包含它的 JSON key path**。兩個實作都不「轉換」
  evidence：它們執行一條指令並逐字記錄輸出。Manifest 已經釘住是哪條指令、exit code
  是多少；兩份 manifest 一致，就代表兩個 body 是同一個 cluster 對同一個問題在兩個
  時刻的回答。連 key path 都比會在不是 candidate 造成的事情上失敗——健康時
  `health.checks` 是 `{}`，一出現 slow op 就多一個 key；某個 counter 這次是 `0`
  下次是 `0.5`。會因為一次暫時性 HEALTH_WARN 就失敗的 gate，只會被學會「重跑到過為
  止」，那比一個少比但說得準的 gate 更糟。
- `/var/log` payload 與重壓縮後的 metric dump bytes（`nodes/*/logs/var-log/{merged,raw,original}/`、
  任何 `.gz`／`.xz`／`.bz2`／`.zst`）。這些只比對「存在與路徑」。
- 超過 4 MiB 的 artifact 內容；同樣只比對存在與路徑。

Evidence 處理本身的 byte-level 等價是 offline gate 的職責：那裡的輸入是凍結的，所以
它可以精確比對。

## Normalizer 允許忽略的差異（完整清單）

`validation/lab_bundle.py` 的 `_default_substitutions()`，每一條都是時鐘或亂數：

| 規則 | 理由 |
| --- | --- |
| `ceph-incident-<YYYYMMDDTHHMMSSZ>` → `ceph-incident-<stamp>` | bundle 檔名帶採集時刻 |
| ISO-8601 timestamp → `<timestamp>` | 採集時刻 |
| `.../ceph-incident-node[.-]<suffix>` → `<node-workspace>` | 遠端 workspace 的 mktemp 後綴／invocation id |
| `<dir>/.<name>.{plain,encoded}.<random>` → `<dir>/<name>` | redaction 暫存檔指向同一個 artifact |
| `.../tmp.<random>` → `<workdir>` | 工作機暫存目錄 |
| 32 位 hex → `<invocation>` | node invocation identifier |
| 各自的 `--out` 目錄與 run directory → `<bundle>`／`<run>` | 兩次 run 依定義寫在不同目錄 |

另外，`environment.txt` 只比對上表列出的選擇欄位。`created_utc` 是時鐘；
`node_target_*`、`node_invocation_id_*`、`rook_namespace`、
`rook_operator_namespace`、`kube_context` 是 rewrite 宣告過的 candidate-only 可觀
測性（#11、#14），已記錄在 `docs/differential-normalizer.md`。清單以外的任何欄位
差異都會讓 gate 失敗。

## Bundle 讀取是不信任的

Bundle 是本次 collect 產生的，但「我們做的」不是信任理由。`read_bundle()` 逐一
檢查 member，遇到 link、device/FIFO 等特殊 member、absolute path 或 traversal 就
直接 fail closed，而且**從不解壓到磁碟**；只有參與比較的 artifact 會被讀進記憶
體，並有單檔上限。

## Stable-State Snapshot Schema（version 1）

兩次 collect 之前取一次、之後取一次，兩者必須完全相同。Snapshot 只能包含
**stable identity 與 desired configuration**；whitelist 就是重點——列舉保留欄位，
新版本新增的易變欄位不會突然讓 gate 失敗，而真正的 desired-state 變動也無法躲在
沒人列舉的欄位裡。

| 欄位 | 來源（唯讀） | 保留 | 排除 |
| --- | --- | --- | --- |
| `ceph_monitors` | `ceph mon dump --format json` | `fsid`、每個 mon 的 `name`／`rank`／`public_addr` | `epoch`、`modified`、`created`、`election_epoch`、`quorum`、feature bitmap |
| `ceph_crush_topology` | `ceph osd tree --format json` | `id`、`name`、`type`、`device_class`、`crush_weight`、`children` | `status`、`reweight`、`exists`、`primary_affinity` |
| `ceph_pools` | `ceph osd pool ls detail --format json` | `pool_id`、`pool_name`、`type`、`size`、`min_size`、`pg_num`、`pg_num_target`、`crush_rule`、`erasure_code_profile`、application 名稱 | `last_change`、`last_force_op_resend*`、所有使用量統計 |
| `ceph_config` | `ceph config dump --format json` | `section`、`name`、`value` | `level`、`can_update_at_runtime`、`mask` |
| `rook_cephclusters` | `kubectl -n <ns> get cephclusters.ceph.rook.io -o json` | `metadata.name`／`namespace`、整份 `spec` | `status`、`resourceVersion`、`generation` |
| `k8s_{deployments,statefulsets,daemonsets}_<ns>` | `kubectl -n <ns> get <resource> -o json` | `kind`、`name`、`namespace`、`spec.replicas`、container images | `status`、`resourceVersion`、`generation`、annotation、`creationTimestamp` |

其他規則：

- Ceph 只用 direct read-only CLI（`ceph`，必要時 `sudo -n ceph`）。`cephadm shell`
  永遠不是 fallback，因為它可能啟動 container。
- Kubernetes 只用本機 `kubectl get`；沒有 `exec`，沒有寫入 verb。
- Profile 的 `operator_namespace` 與 `namespace` 不同時，才會多讀一組 workload。
- Ceph 與 Kubernetes 回傳的集合本身無序，所以每個 projection 以自身 canonical
  form 排序：同一組物件換個順序不是變動。
- 任何一個來源讀不到就 fail closed。讀不到不等於沒有變動；一份殘缺的 snapshot 會
  和另一份殘缺的 snapshot 比對成功，那正是這個 gate 不能容許的事。

## Remote Residue

第一次 collect 前、第二次 collect 後，各對每個 inventory node 取一次
`${TMPDIR:-/tmp}/ceph-incident-node.*`、`ceph-incident-node-*` 的列表與 helper
process 列表。**只有期間新出現的**才算本次 run 的殘留；run 之前就存在的會被如實
報告為 pre-existing，但不歸咎於本次 run。

Probe 只讀：不刪除 workspace，不對 process 送 signal。能「清乾淨讓檢查通過」的
residue check 就不是 residue check；runbook 要求殘留必須送到人手上。

Probe script 的兩個細節是刻意的：開頭的 sentinel 註解讓 offline 測試的 fake
`ssh` 認得它；兩個 process marker 在 script 內以變數拼接，因為 script 自己的文字
會出現在 node 的 `ps` 輸出裡，直接寫出 marker 會讓每次 probe 都把自己回報成殘留。
