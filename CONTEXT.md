# Ceph Incident Evidence Collection

這個 context 定義從 Ceph 或 Rook 環境收集唯讀事故證據，並組裝成可攜式 incident bundle 時使用的共同語言。

## Language

**Incident Bundle**:
一次收集作業產生的事故證據封存檔，包含 cluster evidence、node evidence、metadata 與 manifest。
_Avoid_: Report, package

**工作機（Workstation）**:
由操作人員控制、負責發起收集並組裝最終 incident bundle 的機器。
_Avoid_: Client, local host, control node

**Node**:
被收集 node evidence 的 Ceph 或 Rook 主機。
_Avoid_: Remote host, target server

**Supported Node**:
具備 Python 3.11 或更新版本，符合 node evidence 收集執行條件的 node。
_Avoid_: Compatible node, healthy node

**Skipped Node**:
因執行條件不符或收集失敗而沒有完整 node evidence，但其失敗原因仍被記錄在 incident bundle 中的 node。
_Avoid_: Failed host, unsupported server

**Cluster Evidence**:
描述 Ceph、Rook 或 Prometheus 共享狀態，且不歸屬於單一 node 的事故證據。
_Avoid_: Global evidence, cluster logs

**Node Evidence**:
歸屬於單一 node 的主機層級事故證據。
_Avoid_: Remote evidence, host dump

**Node Collector Payload**:
由工作機透過 SSH stdin 串流給 supported node 執行的自足 Python 原始碼。
_Avoid_: Deploy package, uploaded script, node bundle

**Node Evidence Archive**:
單一 node collector 透過 SSH stdout 回傳給工作機的壓縮 evidence 封存檔；它是 incident bundle 的輸入，不是最終 incident bundle。
_Avoid_: Node bundle, remote tarball

**Evidence Manifest**:
列出 Node Evidence Archive 或 incident bundle 內每一份 evidence 及其來源、狀態與時間的索引；archive 內每一份 evidence 都必須在索引中有一筆對應紀錄。
_Avoid_: Command log, 執行紀錄

**Collect**:
從指定環境取得 cluster evidence 與 node evidence，並產生一份經驗證的 incident bundle。
_Avoid_: Run, gather, dump

**Verify**:
檢查既有 incident bundle 是否符合 structural verification 契約；content safety 尚未移除前，也會執行其機密材料檢查。
_Avoid_: Check, validate, scan

**Structural Verification**:
確認 incident bundle 的封存格式、必要 metadata、manifest 與 evidence 結構完整且可讀。
_Avoid_: Content scan, secret check

**Content Safety**:
對 incident bundle 執行既有 redaction 與已知機密材料檢查的暫時性政策；它降低誤分享風險，但不是完整 DLP 保證。
_Avoid_: Structural verification, sanitization, DLP

**Real-Lab Canary**:
在 production-like Ceph 或 Rook 環境執行的唯讀 collect 驗證，除了檢查 incident bundle，也會比較收集前後的穩定狀態並確認 node 沒有殘留暫存資源。
_Avoid_: Smoke test, production test

**Stable State Snapshot**:
Real-lab canary 用來證明 collect 沒有改變系統狀態的一組穩定 identity 與 configuration 欄位；不包含會自然變動的 counters、epochs 或時間資料。
_Avoid_: Full state dump, health snapshot

**Full Collect**:
在同一個 lab、同一次 collect 中取得 Ceph、Rook、Prometheus 與所有 inventory nodes（包含 `/var/log`）的完整 evidence。
_Avoid_: Collector smoke test, split canaries, partial collect

**Lab Profile**:
描述 real-lab canary 連線入口、預期 cluster identity 與必要 collector coverage 的本機 TOML 設定；它只引用憑證路徑，不保存憑證內容。
_Avoid_: Connection document, inventory, lab credentials

**Lab Profile Candidate**:
由唯讀 discovery 產生、包含新 lab identity 但尚未經操作人員確認的 Lab Profile；它不能直接用於 real-lab canary。
_Avoid_: Active profile, trusted profile

**Lab Validation Report**:
一次 real-lab gate 的持久化結果，包含 code/profile identity、collector coverage、shell/Python full collect、bundle comparison、stable-state diff 與下一步；同時提供人讀與 machine-readable 格式。
_Avoid_: Test log, bundle report, chat summary

**Operationally Read-Only Collect**:
不刻意改變 persistent configuration、service state、package state、mount state、Ceph desired state 或 Kubernetes objects/workloads 的 collect。只允許 collector-owned workspace、最終 incident bundle 與可清理的 node 暫存輸出；查詢自然造成的 audit/access log、request counter 或 cache 變化不屬於 desired-state mutation，但必須在驗證報告中誠實區分。
_Avoid_: Zero-write collect, side-effect-free query, harmless collect

**Collector-Owned Workspace**:
由 collector 自己建立並驗證 ownership/containment、唯一允許建立、覆寫、rename 或刪除 evidence artifacts 的目錄。來自 inventory、Node Evidence Archive 或外部 command 的資料不能擴張這個寫入邊界。
_Avoid_: Temp path, output directory, arbitrary path

**Read-Only Proof**:
以 command-policy assertions、source filesystem invariants、Stable State Snapshot 與 remote residue checks 證明 collect 符合 Operationally Read-Only Collect 的驗收證據；不是只憑 command 名稱或操作者宣稱。
_Avoid_: Safety assumption, smoke-test result, no-error run
