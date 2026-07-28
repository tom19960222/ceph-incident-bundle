# Real-Lab Validation Runbook

## Current Implementation Status

本文件定義 Python rewrite cutover 前的 real-lab validation 操作流程與 agent handoff 契約。

截至 issue #9，以下介面**尚未實作，現在不可假設可用**：

- `make lab-status`
- `make lab-profile-discover`
- `make validate-lab`

Lab Profile、status/discovery workflow 與 report foundation 由 issue #19 實作；同 lab 的 shell/Python full-collect automation 與完整 `validate-lab` gate 由 issue #20 實作。Issue #9 只建立契約與操作文件，不修改 production collectors 或 Makefile。

在 #19、#20 完成前，不得用手動拼接的一組長指令冒充正式 qualification，也不得宣告 Python candidate 已通過 real-lab gate。

## Non-Negotiable Safety Rules

執行前必須先讀 [Operational Read-Only Safety Contract](read-only-safety.md)。摘要如下：

- Collect 只能讀取 lab；不得改變 persistent config、service、package、mount、Ceph desired state 或 Kubernetes workload。
- `cephadm shell` 與 `kubectl exec` 即使在一般 CLI 中保留明確 opt-in，也不屬於 qualification 的允許路徑。不要設定相關 flag 或環境變數。
- Lab Profile 是 automation 唯一連線來源。永遠不要解析 `CEPH-LAB-CONNECTION.md`。
- Profile 只引用 secret 的檔案路徑；不得把 private key、keyring、password、token 或 kubeconfig credential payload 寫進 profile、report、log 或 ticket。
- Identity mismatch、coverage 不完整、bundle verify 失敗、stable-state diff 或 remote residue 都必須 fail closed。
- 每份狀態或 validation report 只能產生一個 `next_action`。

## Agent Entry Point

新 agent 不應依賴先前聊天記憶。接手時依序：

1. 讀取根目錄 `AGENTS.md`、本 runbook、read-only safety contract、`CONTEXT.md` 與相關 ADR。
2. 確認工作目錄、Git commit/dirty state 與預定的 Lab Profile 絕對路徑；不要把 local-only profile 加入 Git。
3. Issue #19 完成後，先執行 `make lab-status LAB_PROFILE=/absolute/path/to/lab.toml`。
4. 只執行 status/report 提供的唯一 `next_action`。若它要求人工確認 candidate 或 identity 差異，停止並交給操作人員；不要自行信任新 identity。

`lab-status` 必須是純讀取本機狀態的入口，不得連線 lab 或改寫 active profile。它應顯示 active/candidate 狀態、profile hash、必要檔案存在性、最近 report 與唯一下一步，但不得顯示 secret content。

## Lab Replacement Workflow

Lab 隨時可能被刪除重建。新環境不能沿用舊 profile 的信任：

1. 準備一份只含連線入口與 credential path 的 local-only bootstrap/candidate input；不要把聊天、Markdown connection note 當成 machine input。
2. Issue #19 完成後，以 `make lab-profile-discover LAB_PROFILE=/absolute/path/to/bootstrap.toml` 執行唯讀 discovery。
3. Discovery 必須輸出獨立的 **Lab Profile Candidate**，不得覆寫或啟用 active profile。它只能使用讀取型操作，並記錄 SSH fingerprints、Ceph/Rook FSID、hostnames/host map 與 Prometheus readiness。
4. 操作人員或被明確委託的 agent 比較 candidate 與預期拓撲、identity 和 credential paths。檢查報告不得包含 credential content。
5. 只有在差異被明確接受後，才能以 #19 定義的 activation 動作建立 active Lab Profile。Activation 必須是顯式動作，不能由 discovery 或 validation 自動發生。
6. 重新執行 `lab-status`。未啟用 candidate、缺少 expected identity、SSH fingerprint 或 FSID 不符時，下一步只能是修正/審核 profile，不能進入 collect。

禁止以 `StrictHostKeyChecking=no`、自動接受所有新 fingerprint、skip identity check 或直接修改 expected FSID 的方式讓 gate 通過。

## Qualification Workflow

Issue #20 完成後，正式入口為：

```text
make validate-lab LAB_PROFILE=/absolute/path/to/lab.toml CEPH_INCIDENT_LAB_CONFIRM=1
```

這個 target 必須保持明確 opt-in，不能被一般 `make validate`、日常 CI 或無確認的 agent 自動觸發。Validation harness 應完成下列狀態流程；操作人員不得跳過或重排 gate：

### 1. Local preflight

- 確認 explicit confirmation、乾淨可辨識的 Git commit、active profile 與 profile hash。
- 驗證 profile schema 與所有 credential paths 的存在性/權限，只記錄 path 是否有效，不讀出或記錄 secret content。
- 由 profile host map 產生一次性的 shared inventory，供 shell reference 與 Python candidate 共用；不得另外維護第二份手工 inventory。
- 確認 qualification 沒有啟用 cephadm-shell、kubectl-exec 或其他有額外副作用的 opt-in。

### 2. Strict lab identity preflight

- 連線並比對 profile 中所有 SSH host fingerprints；不能在此階段自動更新信任資料。
- 比對 Ceph FSID、Rook/external cluster FSID、必要 hostnames/host map 與 Prometheus endpoint identity/readiness。
- 確認所有 inventory nodes 都是 supported/expected targets，且 direct Ceph CLI、本機 kubectl read operations、Prometheus HTTP GET 與 node SSH 安全路徑可用。
- 任一缺值或 mismatch 立即停止，不執行 collect。報告只給一個修復 identity/profile 的 `next_action`。

### 3. Pre-collection stable state snapshot

在第一次 collect 前取得受控欄位的 snapshot。應包含足以偵測 persistent/desired-state mutation 的 stable identity 與 configuration，例如 cluster FSID、host membership、service/orchestrator specs 的穩定部分、pool/CRUSH/config identity，以及 Kubernetes workload/object specs 的穩定摘要。

Snapshot schema 必須明確排除會自然變動的 counter、epoch、timestamp、uptime、health history、request statistics、audit/access records 與非決定性排列。不得用整份未正規化的 status dump 做相等比較。

### 4. Shell reference full collect

- 使用 active profile 產生的 shared inventory 與 qualification 固定參數，執行一次 shell reference collect。
- 單一 invocation 必須同時收齊 Ceph、Rook、Prometheus、全部 inventory nodes 與 `/var/log`。
- 保存 exit status、stdout bundle path、stderr/command ledger、coverage 與 invocation identifier。
- Bundle 必須獨立通過 verify。Partial、缺少任一路徑、使用 default-off execution path 或 verify failure 都立即使 qualification 失敗。

### 5. Python candidate full collect

- 使用與 shell reference 相同的 active profile identity、shared inventory、collector coverage 與語意等價參數，接著執行一次 Python candidate collect。
- 同樣要求單一 invocation 收齊四條路徑並獨立通過 verify；不得重用 shell bundle 的 artifacts 補足 Python bundle。
- 保存與 shell run 相同種類的 evidence，以供 normalized comparison。

### 6. Safe archive handling and comparison

- 所有 node archive 在解壓前依 read-only safety contract 驗證 traversal、link/special member、collision、完整性、manifest 與 payload cap。
- 比較兩份 bundle 的 normalized observable contract：CLI/exit semantics、collector coverage、artifact/path 與內容語意、manifest、SKIPPED/partial、runner/source 選擇和 cleanup 結果。
- 不要求 tar member order、gzip header、mtime、JSON whitespace/key order、隨機 temp path 或 stderr 措辭 byte-identical。

### 7. Post-collection proof

- 第二次 collect 完成後再次取得相同 schema 的 stable state snapshot，並與 pre-collection snapshot 比較。
- 以兩次 invocation identifier 對每個 inventory node 執行 remote residue check；不得為通過檢查而刪除 ownership 無法證明的資源。
- Stable-state diff 必須為空，且兩次 invocation 均不得有 remote workspace、payload、archive 或 helper process 殘留。

只有 shell bundle、Python bundle、full coverage、normalized comparison、stable-state comparison 與 residue check 全部通過，qualification 才能標記 pass。

## Lab Validation Report

每次嘗試，不論 pass 或 fail，都應在 local-only validation output 中留下同一 run directory 內的 `report.md` 與 `report.json`，並由 local-only `LATEST` 指向該目錄。兩種格式必須表達相同結果，至少包含：

- validation timestamp、Git commit、dirty-state indicator、profile path 的安全顯示、profile hash。
- 已驗證的 SSH fingerprints、Ceph/Rook identity 與 host map 摘要，不含 secret。
- shell 與 Python invocation 的 exit/result、bundle path/hash、verify result 與四條 collector coverage。
- normalized bundle comparison 結果。
- stable-state snapshot schema/version 與 diff 結果。
- 每個 inventory node 的 remote residue result。
- `status`：`pass` 或具體 failure class。
- `next_action`：**恰好一個**具體動作。Pass 時也只能有一個下一步，例如進入指定 cutover ticket；不能放空陣列或建議清單。

Report 不得包含 private key、keyring、password、token、Authorization header、kubeconfig credential payload、完整環境變數 dump 或 command stdin。若錯誤輸出可能帶 secret，應在寫入 report 前遮蔽，只保留定位問題所需的 bounded diagnostics。

## Failure and Handoff Rules

- Identity/profile failure：不開始 collect；唯一 `next_action` 指向 profile review/discovery/activation 中的一步。
- Read-only command-surface failure：立即停止後續 collection；保存安全的 command ledger，唯一 `next_action` 指向修正 collector 或 allowlist，不能要求重跑並略過檢查。
- Bundle/coverage/comparison failure：shell implementation 保留，唯一 `next_action` 指向對應 artifact 或 differential failure 的調查。
- Stable-state diff：視為可能的 read-only regression，停止 cutover；唯一 `next_action` 是 review 被改變的 stable field 與對應 command ledger。
- Remote residue：停止 cutover，不做廣泛自動清理；唯一 `next_action` 是依 invocation ownership 資訊進行人工審核。
- Agent handoff 必須提供 report directory 與 `LATEST` 狀態，不要只貼聊天摘要。接手 agent 從 `lab-status` 與唯一 `next_action` 繼續。

Shell reference 必須保留到正式 qualification 通過且最終 cutover ticket 明確允許移除；本 runbook 本身不授權刪除 shell 或放寬任何 safety gate。
