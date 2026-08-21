# Operational Read-Only Safety Contract

## Purpose

`ceph-incident-bundle` 收集事故證據。它不是修復工具、診斷動作執行器或設定管理工具。所有 production collectors、validation tooling 與 real-lab canary 都必須符合本文件；遇到無法證明安全的路徑時，應停止該路徑並回報 partial 或 failure，而不是降低安全條件繼續。

這裡的 **operationally read-only** 意指收集行為不得改變受測環境的持久或期望狀態。它不宣稱執行觀測時遠端儲存裝置的每一個 metadata bit、服務 counter 或 audit log 都完全不變。

## Current Authority

Issues #85 and #115 define the Python-only product. Historical shell collectors, root-level CLI,
verifier/redactor commands and their lab modules are retired rollback history, not executable
fallbacks or current qualification authority. The safety boundary is now proved by public Python
black-box tests and the #102–#104 qualification records.

## Current Python Production Status

CPython 3.10+ 是唯一 production implementation 的正式支援範圍；公開入口只有已安裝的
`ceph-incident-bundle`，subcommands 僅 `generate-inventory` 與 `collect`。`make validate`
完全離線。Real-lab proof 必須使用明確選定的 active Lab Profile 與使用者明確 opt-in，
並由 agent-managed acceptance workflow 產生；它不是產品命令或 Make target。

## Safety Boundary

### Permitted effects

收集器只可以產生下列寫入或自然副作用：

1. 在工作機上，由本次 invocation 建立且明確擁有的 output、workdir、下載暫存檔、validation report 與 local-only `LATEST` 指標。
2. 在 node 上，由本次 invocation 建立、名稱不可與使用者資料碰撞、權限受限且可辨識所有權的暫存 workspace。它只能用來組裝 node evidence archive，並必須在 success、partial、failure、timeout 與 interrupt 路徑清除。
3. SSH、Ceph、Kubernetes、HTTP、system journal 或安全稽核系統因「被讀取」而自然新增的 access/audit records。
4. 服務因時間流逝或讀取請求自然改變的 counters、epochs、timestamps、cache state 與連線統計。
5. Atime 只能是無法避免的唯讀副作用；不得修改來源內容或 desired state，且
   component-wise no-follow、open 後 `fstat` 重查與 workspace containment 仍為強制規則。

這些例外不能被用來合理化任何 persistent configuration、desired state 或 workload mutation。

### Prohibited effects

收集器與 validation harness 不得：

- 修改、建立或刪除受測環境的 persistent configuration，包括 Ceph config、keyring、system configuration、Kubernetes objects 或應用設定。
- 啟動、停止、重啟、reload、enable 或 disable service、daemon、Pod、Deployment、Job、CronJob、operator 或其他 workload。
- 安裝、移除或更新 package、container image、kernel module、binary 或 runtime dependency。
- mount、unmount、remount filesystem，或修改 mount option、LVM、block device、network、firewall、sysctl、time setting 或 host identity。
- 改變 Ceph desired state 或資料，包括但不限於建立/刪除 pool、OSD、MON、MGR、filesystem、RBD、user，修改 CRUSH、auth、quota、flag、orchestrator spec、balancer 或 configuration。
- 對 Kubernetes 執行 create、apply、patch、replace、delete、edit、scale、rollout、cordon、drain、taint、label、annotate 或任何會建立 process/workload 的操作。
- 為了收集而建立 toolbox、debug Pod、ephemeral container、port-forward 或 tunnel，或改變 RBAC/ServiceAccount。
- 寫入來源 log、journal、Ceph data directory、Kubernetes volume 或任何不屬於本次 invocation 的路徑。
- 以清理為名刪除無法由本次 invocation ownership token 證明所屬的檔案或目錄。

外部指令必須以參數陣列呼叫，不能把 lab/profile 值插入 local shell expression。任何未列入測試與 review 的新外部指令都視為未證明安全。

## Unsupported Execution Paths

`cephadm shell` 可能啟動 container 或 pull image；`kubectl exec` 會在既有 Pod 內建立 process。因此：

- Python collect 不提供任何啟用這兩條路徑的 flag、環境變數或 fallback。
- Real-lab qualification 不得以別名、wrapper 或其他間接方式執行同等動作。
- Qualification 必須使用直接、唯讀的 Ceph CLI、本機 `kubectl` read operations 與 Prometheus HTTP GET。缺少這些安全路徑時，結果是 fail closed，不是啟用 fallback。

## Filesystem and Workspace Rules

### Source reads

- 所有 selected-file source 必須以 component-wise descriptor traversal 與 no-follow 規則
  讀取；每個 parent、leaf 與 drop-in directory 都不能跟隨 symlink。
- Source 在 inspect 與 open 之間改變時，必須用 opened descriptor 的 `fstat` 重新確認
  regular-file type；`/var/log` 還必須以同一次 `fstat` 重新確認 `mtime >= cutoff`。
- Optional missing、symlink 與 special file 可以安全略過。Inspection、open、read 或 owned
  workspace write 失敗必須留下具體 diagnostic、使 node result nonzero，並繼續後續來源。
- 不解析 `/etc/resolv.conf` 或其他 source symlink；不能退回會跟隨 link 的一般讀法。
- 目錄列舉只在已安全開啟的 directory descriptor 下進行，不能跨出預定 root。

### Owned workspace containment

- 每次 invocation 的 local 與 remote workspace 都必須由程式建立，並帶有不可猜測的唯一名稱或 ownership token。
- 所有中間檔、合併輸出、archive 與 cleanup target 都必須先解析並確認位於該 workspace 內。
- 不接受使用 `/`、home、固定共享目錄、profile 提供的任意 cleanup root，或未驗證的環境變數作為遞迴清理範圍。
- Cleanup 只能移除本次 invocation 建立且仍在 owned workspace boundary 內的資源。無法證明 ownership 時，保留資源、標記 failure 並回報精確位置；不得擴大刪除範圍。
- 工作機上的失敗 workdir 依 observable contract 保留供調查；這是 collector-owned output，不是 lab mutation。

### Node archive acceptance before extraction

工作機必須先把 SSH stdout 存入 owned workspace 中的候選檔，再於**解壓前**完成驗證。至少必須拒絕：

- 無效或截斷的 gzip/tar stream。
- 絕對路徑、空名稱、含 `..` traversal 或正規化後離開預定 extraction root 的 member。
- symlink、hardlink、device、FIFO、socket 或其他非 regular-file/directory member。
- 重複或正規化後碰撞的 member name。
- 不符合固定 node evidence root/schema 的 archive。

只有整份 member table 與結構通過後，才能解壓到新建立的 owned extraction directory。Extraction 不得跟隨既有 symlink、覆寫 workspace 外檔案或信任 archive 內的 ownership/permission metadata。有效 archive 可以伴隨 remote nonzero exit status 並被保留為 partial evidence；不安全或不完整 archive 絕不能解壓。

## Remote Residue Contract

每個 node collector invocation 都必須留下可供 residue check 辨識、但不含 secret 的 invocation identifier。正常、partial、failure、timeout 與 interrupt 後，受測 nodes 上都不得殘留該 invocation 的 workspace、payload、archive 或 helper process。

Residue check 只能檢查本次 invocation 的 identifier 或安全的固定 collector prefix；不能掃描後刪除所有相似名稱。發現殘留時 real-lab qualification 必須失敗，報告位置，且不得自動執行超出 ownership boundary 的補救清理。

## Lab Identity and Secret Boundary

- Lab Profile 是 automation 唯一的連線來源。它只可保存 endpoint、host map、expected identity/fingerprint，以及 SSH private key、kubeconfig 等 credential **檔案路徑**。
- Profile、report、log、bundle comparison 與 `next_action` 不得複製 private key、keyring、password、token、kubeconfig credential payload 或其他 secret content。
- `CEPH-LAB-CONNECTION.md` 只供人閱讀；production code、test、discovery、status 與 validation harness 永遠不得解析它。
- 執行任何 real-lab qualification collect 前，必須比對 active profile 的 SSH host fingerprints、Ceph/Rook FSID、必要 hostname/host map 與其他定義的 stable identity。缺值、連線目標不一致、fingerprint/FSID mismatch 或 candidate 尚未明確啟用時，一律 fail closed；禁止用 accept-current、skip-check 或自動改寫 active profile 繞過。一般 inventory-driven collect 仍保留既有 CLI/host-key contract，但不能被當成通過 strict lab identity gate 的證據。
- Strict identity 是 acceptance harness 的先決條件，不是產品 fallback。Discovery、preflight
  與 acceptance 的每條 SSH 連線都使用 pinned collector-owned `known_hosts` 與
  `StrictHostKeyChecking=yes`，不讀寫操作人員的 `known_hosts`，也沒有 accept-new 路徑。
  Preflight 通過本身不構成 qualification evidence；coverage、stable-state、publication 與
  residue gates 仍必須全部完成。

## Proof Obligations

### Offline proof

Offline validation 必須可重複且不連接 lab，並至少證明：

- `make validate PYTHON=<absolute-cpython-3.10>` 從 clean source 建 wheel、安裝到 fresh
  environment，並透過 installed console entry point 跑完整 Python suite。
- fake adapters 記錄每個 external command 與 argv；測試對禁止的 mutating verbs、default-off fallback 與未預期 command fail closed。
- 每個 collector 只寫入 owned workspace；path traversal、symlink/hardlink、archive special
  files、member collision 與 truncated stream 在 extraction 前被拒絕。
- success、partial、failure、timeout 與 interrupt 都有 remote/local cleanup 或預期的 failure-workdir retention 測試。
- `/var/log` 無法使用 descriptor/no-follow 規則安全讀取時為 partial，不執行不安全 fallback。
- 134 個歷史 behavior-bearing rows 全部已有 disposition：仍屬 #85 required behavior 的部分
  由 public Python tests 覆蓋，#85 已取代或排除的部分記錄 obsolete rationale；另有 11 個
  shell-only rows 明確分類為退役 implementation detail。

Offline proof 是必要條件，但不能替代 real-lab proof。

### Real-lab proof

Real-lab qualification 必須凍結 commit/tree、wheel、installed CLI、inventory、active
profile、credential-path hashes 與 harness。Strict identity 通過後，只執行標記過的一次
installed Python collect。Full acceptance 必須依序收齊全部 inventory nodes、selected
same-session direct Ceph、本機 Rook、本機 Prometheus，再原子 publication。Qualification
不得安裝、切換或修改 workstation/node runtime。

通過條件全部為必要條件：

1. 使用 pinned identity、direct read-only Ceph CLI、本機唯讀 Kubernetes commands 與
   Prometheus HTTP GET；不得使用禁止路徑或 fallback。
2. Bundle structural inspection、inventory snapshot、全部 configured source coverage、result
   schemas、raw captures 與 publication contract 全部通過。Truthful partial 可以通過，但
   每個 attempted failure 必須可由 stderr/result 對應。
3. 前後 runtime、stable identity、persistent configuration、service/package/mount intent、
   Ceph/Rook desired state 與 Prometheus readiness 相同；自然 counters、timestamps、session
   identity 與 access records按已 review 的 projection排除。
4. 所有 inventory nodes 與 workstation 都通過本次 invocation 的 residue check；private
   candidate、workspace 與 inspection copy均已清除或依 failure policy保留並明確回報。
5. Raw evidence 以 owner-only modes 留在 gitignored `results/`，manifest逐檔 hash驗證；
   qualification document只記 sanitized hashes與摘要。

Current evidence records are `docs/python-offline-qualification.md`,
`docs/python-single-node-acceptance.md`, and `docs/python-full-live-acceptance.md`.

任一條件失敗即 fail closed。Lab Validation Report 必須只提供一個具體、可執行且不放寬安全條件的 `next_action`；不得提供多個互相競爭的建議，也不得建議略過 identity、coverage、verification、state diff 或 residue gate。
