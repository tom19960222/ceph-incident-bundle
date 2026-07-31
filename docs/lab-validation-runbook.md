# Real-Lab Validation Runbook

## Current Implementation Status

本文件定義 Python rewrite cutover 前的 real-lab validation 操作流程與 agent handoff 契約。

Issue #19 已實作 Lab Profile、status/discovery/activation workflow、strict identity preflight 與 Lab Validation Report foundation；issue #20 已實作 dual-run qualification harness。目前可用的介面：

- `make lab-status LAB_PROFILE=...` — 純本機狀態與唯一下一步。
- `make lab-profile-discover LAB_PROFILE=...` — 唯讀 discovery，只產生 candidate。
- `make lab-profile-activate LAB_PROFILE=... LAB_CANDIDATE=... CEPH_INCIDENT_LAB_ACTIVATE=1` — 顯式且留下稽核紀錄的 activation。
- `make lab-preflight LAB_PROFILE=... CEPH_INCIDENT_LAB_CONFIRM=1` — strict identity preflight，寫出一份 Lab Validation Report。
- `make validate-lab LAB_PROFILE=... CEPH_INCIDENT_LAB_CONFIRM=1` — 完整 real-lab gate。

`lab-preflight` 通過**只證明 lab identity，不是 qualification evidence**；它產生的 report 中 collector coverage、兩次 full collect、bundle comparison、stable-state diff 與 residue 一律是 `not-run`。只有 `validate-lab` 寫出 `status: pass` 的 report 才是 qualification evidence。

Harness 存在不等於 qualification 已經完成。**在真實 lab 執行 `make validate-lab` 並取得 `status: pass` 的 report 之前，不得宣告 Python candidate 已通過 real-lab gate**，也不得用手動拼接的一組長指令冒充正式 qualification。實際執行與 cutover 判定屬於 issue #21。

Shell reference 的 Node Evidence Archive receiver 已由 issue #23 完成 pre-extraction hardening；這只解除 archive receiver prerequisite，不取代本 gate 的 strict identity、full-collect、stable-state 與 residue 條件。

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
3. 先執行 `make lab-status LAB_PROFILE=/absolute/path/to/lab.toml`（加 `LAB_ARGS=--json` 可取得 machine-readable 版本）。
4. 只執行 status/report 提供的唯一 `next_action`。若它要求人工確認 candidate 或 identity 差異，停止並交給操作人員；不要自行信任新 identity。

`lab-status` 是純讀取本機狀態的入口，不連線 lab、不改寫 active profile、不執行任何外部指令。它顯示 profile state/hash、missing identity、credential path 是否可用、待審 candidate、最近一次 activation 與 report，以及唯一下一步；只顯示 credential **路徑**，不顯示內容。Exit code：`0` 表示可以繼續，`2` 表示被擋住且必須先做 `next_action`。

`lab-status` 的下一步依序判斷：credential path 不可用 → 待審 candidate → bootstrap profile → candidate profile → 最近一次 report 的結果 → 可執行 preflight。Candidate 排在 profile state 之前，避免已經產出 candidate 的流程被反覆送回 discovery。

可能的 `state`：`profile-missing`、`profile-invalid`、`profile-bootstrap`、`profile-candidate`、`credential-path-invalid`、`candidate-pending-review`、`last-attempt-failed`、`ready-for-preflight`、`preflight-passed`、`gate-passed`。最後三個 exit `0`，其餘 exit `2`。`preflight-passed` 的下一步是執行 `make validate-lab`；`gate-passed` 只有在 dual-run gate 寫出 `status: pass` 之後才會出現。

## Lab Replacement Workflow

Lab 隨時可能被刪除重建。新環境不能沿用舊 profile 的信任：

1. 準備一份只含連線入口與 credential path 的 local-only bootstrap profile（`state = "bootstrap"`）；從 `validation/lab-bootstrap.example.toml` 複製，不要把聊天、Markdown connection note 當成 machine input。
2. 執行 `make lab-profile-discover LAB_PROFILE=/absolute/path/to/bootstrap.toml`。
3. Discovery 輸出獨立的 **Lab Profile Candidate**（預設 `<name>.candidate.toml`），不覆寫也不啟用 active profile。它只使用讀取型操作（`ssh-keyscan`、`hostname`、`ceph fsid`、本機 `kubectl get`、Prometheus readiness GET），並記錄 SSH fingerprints、Ceph/Rook FSID、hostnames/host map 與 Prometheus readiness。任一 identity 讀不到就完全不寫 candidate，只回報缺哪一項。
4. 操作人員或被明確委託的 agent 比較 candidate 與預期拓撲、identity 和 credential paths。Discovery 會列出與輸入 profile 的每一項差異；檢查報告不含 credential content。
5. 只有在差異被明確接受後，才執行 `make lab-profile-activate LAB_PROFILE=/absolute/path/to/lab.toml LAB_CANDIDATE=/absolute/path/to/lab.candidate.toml CEPH_INCIDENT_LAB_ACTIVATE=1`。Activation 不會由 discovery 或 validation 自動發生；覆蓋既有 active profile 需要再加 `LAB_ARGS=--replace-active`。每次 activation 會在 active profile 旁的 `lab-activation.log.jsonl` 追加一筆只含 identity 的稽核紀錄。
6. 重新執行 `lab-status`。未啟用 candidate、缺少 expected identity、SSH fingerprint 或 FSID 不符時，下一步只能是修正/審核 profile，不能進入 collect。

禁止以 `StrictHostKeyChecking=no`、自動接受所有新 fingerprint、skip identity check 或直接修改 expected FSID 的方式讓 gate 通過。Discovery 與 preflight 的每一條 SSH 連線都用 collector-owned known_hosts 搭配 `StrictHostKeyChecking=yes`，不讀也不寫操作人員自己的 `known_hosts`。

## Lab Profile

Schema 與可提交的 placeholder 範例見 `validation/lab-profile.example.toml`（active）與 `validation/lab-bootstrap.example.toml`（bootstrap）。要點：

- `state` 只有 `bootstrap`、`candidate`、`active` 三種。只有 `bootstrap` 可以缺 identity；`candidate` 與 `active` 必須帶齊 Ceph/Rook FSID 與每台 host 的 hostname 與 SSH fingerprints，否則 loader 直接拒收。
- Profile 必須同時描述四條 collector path 的入口（`[ceph] seed`、`[rook] kubeconfig_path`/`namespace`/`operator_namespace`、`[prometheus] url`、`[[hosts]]` host map），因為 qualification 要求四條路徑都完整。
- 未知的 table 或 key 一律拒收，避免打錯字的 identity 欄位靜默失效。Profile 內出現任何 credential material（PEM header、kubeconfig credential 欄位等）也直接拒收。
- `profile hash` 是 canonical content hash：註解與排版不影響它，identity 改變才會改變它。
- 實際 profile 一律 local-only。Repository 只提供無秘密的 example，`.gitignore` 預設忽略 TOML。

## Identity Preflight

```text
make lab-preflight LAB_PROFILE=/absolute/path/to/lab.toml CEPH_INCIDENT_LAB_CONFIRM=1
```

依序檢查 profile state（必須 `active`）、credential paths（存在、是一般檔案、只有 owner 可讀，不讀內容）、SSH fingerprints、必要 hosts、Ceph FSID、Rook FSID、Prometheus readiness；第一個失敗的 stage 就停止，並輸出唯一的 `next_action`。Fingerprint 規則是「host 提供的每一把 key 都必須已經在 profile 裡」；profile 記錄了但 host 這次沒提供的 key 不算 mismatch。

每次執行都會寫一份 Lab Validation Report。**通過只代表 identity 正確，不是 qualification**；report 的 `status` 是 `preflight-pass` 而不是 `pass`，唯一的 `next_action` 是執行 `make validate-lab`。

## Qualification Workflow

正式入口為：

```text
make validate-lab LAB_PROFILE=/absolute/path/to/lab.toml CEPH_INCIDENT_LAB_CONFIRM=1
```

這個 target 保持明確 opt-in：它需要絕對 Lab Profile 路徑與 `CEPH_INCIDENT_LAB_CONFIRM=1`，不會被一般 `make validate`、日常 CI 或無確認的 agent 自動觸發。`LAB_ARGS='--collect-timeout <seconds>'` 可調整單次 full collect 的上限（預設 4 小時），`LAB_ARGS=--json` 取得 machine-readable 輸出。

Harness 依序完成下列狀態流程，第一個失敗的階段就停止；沒有 skip flag、沒有 accept-current、也沒有重跑到過為止的路徑：

### 1. Local preflight

- 確認 explicit confirmation、active profile 與 profile hash，並記錄執行時的 Git commit 與 dirty state（report 的 `code` 欄位；dirty 會顯示成 `<commit>-dirty`，讓一份 qualification evidence 能被追溯到實際跑的程式）。
- 驗證 profile schema 與所有 credential paths 的存在性/權限，只記錄 path 是否有效，不讀出或記錄 secret content。
- 由 profile host map 產生一次性的 shared inventory，供 shell reference 與 Python candidate 共用；不得另外維護第二份手工 inventory。
- 確認 qualification 沒有啟用 cephadm-shell、kubectl-exec 或其他有額外副作用的 opt-in。

### 2. Strict lab identity preflight

- 連線並比對 profile 中所有 SSH host fingerprints；不能在此階段自動更新信任資料。
- 比對 Ceph FSID、Rook/external cluster FSID、必要 hostnames/host map 與 Prometheus endpoint identity/readiness。
- 確認所有 inventory nodes 都是 supported/expected targets，且 direct Ceph CLI、本機 kubectl read operations、Prometheus HTTP GET 與 node SSH 安全路徑可用。
- 任一缺值或 mismatch 立即停止，不執行 collect。報告只給一個修復 identity/profile 的 `next_action`。

### 3. Pre-collection stable state snapshot

在第一次 collect 前取得受控欄位的 snapshot。它包含足以偵測 persistent/desired-state mutation 的 stable identity 與 configuration：cluster FSID 與 monitor 位置、CRUSH 拓撲與權重、pool identity/redundancy、`ceph config` 的持久設定，以及 Rook CephCluster `spec` 與 Kubernetes workload 的 desired state。

Snapshot schema 以 whitelist 明確排除會自然變動的 counter、epoch、timestamp、uptime、health history、request statistics、audit/access records 與非決定性排列；不使用整份未正規化的 status dump 做相等比較。逐欄位的定義與理由見 [`lab-bundle-contract.md`](lab-bundle-contract.md)。任一來源讀不到就 fail closed——殘缺的 snapshot 會和另一份殘缺的 snapshot 比對成功。

### 4. Shell reference full collect

- 使用 active profile 產生的 shared inventory 與 qualification 固定參數（`--mode auto --kube-mode local --since 24h --no-trust-ssh-host-key --redact`，加上 profile 的 `--prom-url`），執行一次 shell reference collect。這組參數是常數而非操作人員輸入，`--allow-cephadm-shell` 與 `--allow-kubectl-exec` 因此不可能被帶進來。
- Host key 信任來自 active profile：harness 用一個只含 profile 已信任 key 的 collector-owned `HOME/.ssh/known_hosts`，搭配 `--no-trust-ssh-host-key`，所以 collector 的 accept-new 模式不會被使用，操作人員自己的 `known_hosts` 也不會被讀寫。
- 單一 invocation 必須同時收齊 Ceph、Rook、Prometheus、全部 inventory nodes 與 `/var/log`。
- 保存 exit status、stdout bundle path、stderr/command ledger、coverage 與 invocation identifier。
- Bundle 必須獨立通過 verify。Partial、缺少任一路徑、使用 default-off execution path 或 verify failure 都立即使 qualification 失敗。

### 5. Python candidate full collect

- 使用與 shell reference 相同的 active profile identity、shared inventory、collector coverage 與語意等價參數，接著執行一次 Python candidate collect。
- 同樣要求單一 invocation 收齊四條路徑並獨立通過 verify；不得重用 shell bundle 的 artifacts 補足 Python bundle。
- 保存與 shell run 相同種類的 evidence，以供 normalized comparison。

### 6. Safe archive handling and comparison

- 所有 node archive 在解壓前依 read-only safety contract 驗證 traversal、link/special member、collision、完整性、manifest 與 payload cap；harness 讀取兩份 bundle 時同樣逐一檢查 member，遇到 link、special member、absolute path 或 traversal 直接 fail closed，且從不解壓到磁碟。
- 比較兩份 bundle 的 normalized observable contract：CLI/exit semantics、collector coverage、artifact/path 與內容語意、manifest、SKIPPED/partial、runner/source 選擇和 cleanup 結果。
- 不要求 tar member order、gzip header、mtime、JSON whitespace/key order、隨機 temp path 或 stderr 措辭 byte-identical。兩次 collect 相隔數分鐘打在活的 cluster 上，所以 captured artifact 的 body（counter、log 內容、sample，連 JSON key path 一起）也不比對——manifest 已經釘住是哪條指令、exit 是多少，body 是 cluster 的回答而不是 collector 的產物；byte-level 等價由 offline gate（#18）負責。逐項清單與理由見 [`lab-bundle-contract.md`](lab-bundle-contract.md)。

### 7. Post-collection proof

- 第二次 collect 完成後再次取得相同 schema 的 stable state snapshot，並與 pre-collection snapshot 比較。
- 對每個 inventory node 執行 remote residue check：只有在兩次 collect 期間新出現的 workspace 或 helper process 才歸咎於本次 run，run 之前就存在的會如實報告為 pre-existing。Probe 只讀，不刪除任何 ownership 無法證明的資源，也不對 process 送 signal。
- Stable-state diff 必須為空，且兩次 invocation 均不得有 remote workspace、payload、archive 或 helper process 殘留。

只有 shell bundle、Python bundle、full coverage、normalized comparison、stable-state comparison 與 residue check 全部通過，qualification 才會標記 pass。

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

實作狀態：schema 由 issue #19 固定，writer 會在寫入前檢查兩件事並 fail closed——`next_action` 恰好一個非空單行字串，且兩種格式都不含 credential marker。Run directory 預設在 `results/lab-validation/<run-id>/`（`LAB_ARGS='--runs-dir <path>'` 可覆寫），`LATEST` 是同層記錄 run directory 名稱的檔案。

`validate-lab` 會在任何動作之前先取得 run directory，因為兩份 bundle 與兩份 command ledger（`<run>/shell/`、`<run>/python/`，各含 `collect.log` 與 `verify.log`）就放在 report 旁邊。Collector 的 stdout/stderr 只寫進那些 local-only、owner-only 的 ledger，不進 report：report 在寫入前會被掃描 credential marker，而 collector 的 stderr 正是最可能夾帶意外診斷的地方。Gate 停在哪一階段，該階段之後的欄位就保持 `not-run`，所以 report 說得出「停在哪」而不只是「停了」。

`lab-preflight` 的每一次嘗試都會留下 report，包含連 profile 都讀不進來的情況；那種 report 的 `profile.hash`／`state` 為 `null`，`preflight` 只有一筆失敗的 `profile-load`，`status` 是具體的 profile failure class。`lab-status` 只會沿用 report 記錄的 `next_action`，且只在它確實是單行非空字串時沿用；否則改用本機推導的下一步。

## Failure and Handoff Rules

- Identity/profile failure：不開始 collect；唯一 `next_action` 指向 profile review/discovery/activation 中的一步。
- Read-only command-surface failure：立即停止後續 collection；保存安全的 command ledger，唯一 `next_action` 指向修正 collector 或 allowlist，不能要求重跑並略過檢查。
- Bundle/coverage/comparison failure：shell implementation 保留，唯一 `next_action` 指向對應 artifact 或 differential failure 的調查。
- Stable-state diff：視為可能的 read-only regression，停止 cutover；唯一 `next_action` 是 review 被改變的 stable field 與對應 command ledger。
- Remote residue：停止 cutover，不做廣泛自動清理；唯一 `next_action` 是依 invocation ownership 資訊進行人工審核。
- Agent handoff 必須提供 report directory 與 `LATEST` 狀態，不要只貼聊天摘要。接手 agent 從 `lab-status` 與唯一 `next_action` 繼續。

Shell reference 必須保留到正式 qualification 通過且最終 cutover ticket 明確允許移除；本 runbook 本身不授權刪除 shell 或放寬任何 safety gate。
