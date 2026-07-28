# Operational Read-Only Safety Contract

## Purpose

`ceph-incident-bundle` 收集事故證據。它不是修復工具、診斷動作執行器或設定管理工具。所有 production collectors、validation tooling 與 real-lab canary 都必須符合本文件；遇到無法證明安全的路徑時，應停止該路徑並回報 partial 或 failure，而不是降低安全條件繼續。

這裡的 **operationally read-only** 意指收集行為不得改變受測環境的持久或期望狀態。它不宣稱執行觀測時遠端儲存裝置的每一個 metadata bit、服務 counter 或 audit log 都完全不變。

## Current Shell Reference Status

現有 shell implementation 是 observable behaviour 的 reference，不是安全性已被完整證明的 oracle。Python cutover 不得照搬下列已知缺口：

- 工作機目前會在完整檢查 node archive member table 前呼叫外部 tar 解壓。Python node transport（#11）必須在任何 extraction write 前拒絕 traversal、absolute path、link、special member、collision、oversize、truncated stream 與缺少 manifest；#17 的 structural verification 也必須保留同一安全邊界。
- Shell remote cleanup 依賴 trap，對一般 success/failure/timeout/interrupt 是 best effort，但無法在 process/host 被強制終止時保證執行。#11 必須測試可恢復的所有終止路徑，#20/#21 必須以 invocation identifier 和 residue check 提供實機證據。
- 現行 `/var/log` 測試會啟用測試用的普通讀取 escape hatch，source immutability assertion 尚未涵蓋 atime、nofollow 與安全讀取失敗時 fail closed。#12 必須補齊 production read path，#18 必須把這些 invariants 納入 offline gate。
- Shell 保留 `cephadm shell` 與 `kubectl exec` 的明確 opt-in compatibility paths。它們是 default-off，而且不得出現在 #20/#21 的 operationally read-only qualification。

在上述 tickets 完成前，不得宣稱現有 shell 已滿足本文件的完整 proof obligations。受控 real lab 中使用 shell reference 時，仍必須先確認 nodes 身份可信、關閉兩個 opt-ins、限制 workstation output boundary，並在執行後檢查 stable state 與 remote residue。

## Safety Boundary

### Permitted effects

收集器只可以產生下列寫入或自然副作用：

1. 在工作機上，由本次 invocation 建立且明確擁有的 output、workdir、下載暫存檔、validation report 與 local-only `LATEST` 指標。
2. 在 node 上，由本次 invocation 建立、名稱不可與使用者資料碰撞、權限受限且可辨識所有權的暫存 workspace。它只能用來組裝 node evidence archive，並必須在 success、partial、failure、timeout 與 interrupt 路徑清除。
3. SSH、sudo、Ceph、Kubernetes、HTTP、system journal 或安全稽核系統因「被讀取」而自然新增的 access/audit records。
4. 服務因時間流逝或讀取請求自然改變的 counters、epochs、timestamps、cache state 與連線統計。
5. 在無法完全避免時，目錄列舉造成的 filesystem atime 變化；這不放寬一般檔案內容的 no-atime/no-follow 讀取規則。

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

## Default-Off Execution Paths

`cephadm shell` 可能啟動 container 或 pull image；`kubectl exec` 會在既有 Pod 內建立 process。因此：

- `--allow-cephadm-shell` 與 `--allow-kubectl-exec` 必須維持 default-off。
- 一般 collect 即使保留這兩個明確 opt-in 的既有契約，也不能自行啟用它們。
- Real-lab qualification **不得**設定這兩個 flag 或對應環境變數，也不得以其他方式執行同等動作。
- Qualification 必須使用直接、唯讀的 Ceph CLI、本機 `kubectl` read operations 與 Prometheus HTTP GET。缺少這些安全路徑時，結果是 fail closed，不是啟用 fallback。

## Filesystem and Workspace Rules

### Source reads

- `/var/log` 與其他可能影響 atime 或遭遇 symlink race 的一般檔案，必須以支援 `noatime` 與 `nofollow` 的讀法取得；目前契約為 GNU `dd iflag=noatime,nofollow`，必要時使用非互動 `sudo -n`。
- 若平台不支援安全讀法、權限不足、路徑在檢查後變成 symlink，或任何條件無法證明，該檔案必須記為 read-failed/partial；不得退回 `cat`、一般 `open` 或會跟隨 symlink 的讀法。
- 目錄列舉可以讀 metadata，但不能追蹤跨出預定 root 的 symlink。

### Owned workspace containment

- 每次 invocation 的 local 與 remote workspace 都必須由程式建立，並帶有不可猜測的唯一名稱或 ownership token。
- 所有中間檔、合併輸出、archive 與 cleanup target 都必須先解析並確認位於該 workspace 內。
- 不接受使用 `/`、home、固定共享目錄、profile 提供的任意 cleanup root，或未驗證的環境變數作為遞迴清理範圍。
- Cleanup 只能移除本次 invocation 建立且仍在 owned workspace boundary 內的資源。無法證明 ownership 時，保留資源、標記 failure 並回報精確位置；不得擴大刪除範圍。
- 工作機上的失敗 workdir 依 observable contract 保留供調查；這是 collector-owned output，不是 lab mutation。

### Node archive acceptance before extraction

工作機必須先把 SSH stdout 存入 owned workspace 中的候選檔，再於**解壓前**完成驗證。至少必須拒絕：

- 無效、截斷或超過 payload cap 的 gzip/tar stream。
- 絕對路徑、空名稱、含 `..` traversal 或正規化後離開預定 extraction root 的 member。
- symlink、hardlink、device、FIFO、socket 或其他非 regular-file/directory member。
- 重複或正規化後碰撞的 member name。
- 缺少必要 manifest，或 archive root/manifest 不符合 node evidence contract。

只有整份 member table 與結構通過後，才能解壓到新建立的 owned extraction directory。Extraction 不得跟隨既有 symlink、覆寫 workspace 外檔案或信任 archive 內的 ownership/permission metadata。有效 archive 可以伴隨 remote nonzero exit status 並被保留為 partial evidence；不安全或不完整 archive 絕不能解壓。

## Remote Residue Contract

每個 node collector invocation 都必須留下可供 residue check 辨識、但不含 secret 的 invocation identifier。正常、partial、failure、timeout 與 interrupt 後，受測 nodes 上都不得殘留該 invocation 的 workspace、payload、archive 或 helper process。

Residue check 只能檢查本次 invocation 的 identifier 或安全的固定 collector prefix；不能掃描後刪除所有相似名稱。發現殘留時 real-lab qualification 必須失敗，報告位置，且不得自動執行超出 ownership boundary 的補救清理。

## Lab Identity and Secret Boundary

- Lab Profile 是 automation 唯一的連線來源。它只可保存 endpoint、host map、expected identity/fingerprint，以及 SSH private key、kubeconfig 等 credential **檔案路徑**。
- Profile、report、log、bundle comparison 與 `next_action` 不得複製 private key、keyring、password、token、kubeconfig credential payload 或其他 secret content。
- `CEPH-LAB-CONNECTION.md` 只供人閱讀；production code、test、discovery、status 與 validation harness 永遠不得解析它。
- 執行任何 collect 前，必須比對 active profile 的 SSH host fingerprints、Ceph/Rook FSID、必要 hostname/host map 與其他定義的 stable identity。缺值、連線目標不一致、fingerprint/FSID mismatch 或 candidate 尚未明確啟用時，一律 fail closed；禁止用 accept-current、skip-check 或自動改寫 active profile 繞過。

## Proof Obligations

### Offline proof

Offline validation 必須可重複且不連接 lab，並至少證明：

- fake adapters 記錄每個 external command 與 argv；測試對禁止的 mutating verbs、default-off fallback 與未預期 command fail closed。
- 每個 collector 只寫入 owned workspace；path traversal、symlink/hardlink、archive special files、member collision、oversize 與 truncated stream 在 extraction 前被拒絕。
- success、partial、failure、timeout 與 interrupt 都有 remote/local cleanup 或預期的 failure-workdir retention 測試。
- `/var/log` 無法使用 noatime/nofollow 時為 partial，不執行不安全 fallback。
- shell reference 與 Python candidate 對 read-only command surface、artifacts、manifest、exit semantics 與 cleanup 結果進行 normalized differential comparison。

Offline proof 是必要條件，但不能替代 real-lab proof。

### Real-lab proof

Real-lab qualification 必須在 strict identity preflight 通過後，於同一 lab 依序完成 shell reference 與 Python candidate 的 full collect；每次單一 invocation 都必須收齊 Ceph、Rook、Prometheus 與全部 inventory nodes（含 `/var/log`）。驗證區間由第一次 collect 前的 stable state snapshot 開始，到第二次 collect 與 residue check 後的 snapshot 結束。

通過條件全部為必要條件：

1. 兩次 invocation 都未使用 `cephadm shell`、`kubectl exec` 或其他禁止動作。
2. 兩份 incident bundle 都獨立通過 verify，且四條 collector coverage 完整，不能用 partial coverage 通過。
3. 正規化後的 observable contracts 等價。
4. 前後 stable identity/configuration 相同。比較應排除自然變動的 counters、epochs、timestamps、health history 與 audit/access records。
5. 所有 inventory nodes 都通過本次 invocation 的 remote residue check。

任一條件失敗即 fail closed。Lab Validation Report 必須只提供一個具體、可執行且不放寬安全條件的 `next_action`；不得提供多個互相競爭的建議，也不得建議略過 identity、coverage、verification、state diff 或 residue gate。
