# Python Rewrite Implementation Plan

## Objective

以 Python 3.11 標準庫完整取代現有 shell implementation，同時維持 observable contract equivalence。Production runtime 只包含三個 Python 模組；shell reference 保留到 automated、differential 與 real-lab gates 全部通過。

## Contract Sources

- `docs/behavior-contract.md` 是現有 shell observable behaviour 的逐項基準。
- `docs/test-scenario-inventory.md` 是測試移植 ledger：共 137 個 shell scenarios，其中 127 個 behaviour-bearing scenarios 必須有 Python coverage；10 個 shell implementation details（R1、R2、R7、C1、C2、C20、C22、C23、P4、P8）不照搬。
- `CONTEXT.md` 與 `docs/adr/` 定義 domain language 與已鎖定的設計決策。
- `docs/read-only-safety.md` 定義 operationally read-only contract 與 proof obligations。
- `docs/lab-validation-runbook.md` 定義 agent-friendly、fail-closed 的 lab workflow。

任何實作與測試若和這些來源衝突，必須先回到 parent spec #8 澄清，不能自行放寬安全或等價條件。

## Locked Design

- 三個 production modules：公開入口、工作機 collectors、自足 node collector。
- 公開入口使用明確的 `collect` 與 `verify` subcommands，不保留 shell compatibility wrapper。
- Supported node 需要 Python 3.11+；條件不符時 graceful skip，其他 evidence 繼續收集，整體為 partial。
- Node collector payload 經 SSH stdin 傳送；node evidence archive 經 stdout 回傳；所有診斷走 stderr。
- Content safety 先忠實移植並通過功能等價驗證，再以獨立變更移除；structural verification 長期保留。
- 驗收要求 observable contract equivalence，不要求 tar/gzip/JSON 等非語意 serialization byte-identical。
- Production 與 validation tooling 都不使用第三方 Python packages。

詳細理由與 consequences 見 `docs/adr/0001-*.md` 至 `docs/adr/0009-*.md`。

## Delivery Phases

### 1. Design and contract baseline

- 納入 behavior-contract 與 test-scenario inventory research。
- 完成 `CONTEXT.md`、ADRs、agent instructions 與 lab runbook。
- 以 GitHub spec #8 與 implementation tickets #9–#22 追蹤進度。

### 2. Validation foundation

- 建立 Python test runner 與 bundle normalizer。
- 建立 shell/Python differential harness 骨架。
- 定義 TOML Lab Profile、profile candidate、status/discover workflow 與 Lab Validation Report schema。
- Production shell 保持不變。

### 3. Node collector

- 以 TDD 完成自足的 node collector。
- 移植 node evidence、`/var/log`、manifest、payload cap、exit codes 與 cleanup。
- 驗證 stdin payload、stdout archive 與 stderr diagnostics 協定。

### 4. Workstation collectors

- 以 TDD 移植 Ceph、Rook、Prometheus collectors。
- 集中 command execution、capture 與 manifest policies。
- Production shell 入口仍保持可用。

### 5. Public entrypoint and integration

- 實作 `collect`／`verify`、CLI、inventory、source selection、orchestration 與 bundle lifecycle。
- 忠實移植暫時保留的 content safety。
- 完成 Python suite、fake-environment differential suite 與整體 code review。

### 6. Real-lab validation and cutover

- 先完成 #23，讓 shell reference 在任何 extraction write 前驗證 Node Evidence Archive；未完成時不得把 shell run 當成 qualification evidence。
- 在同一 lab 連續執行 shell reference full collect 與 Python candidate full collect；每次 invocation 都同時收齊 Ceph、Rook、Prometheus 與全部 nodes（包含 `/var/log`）。
- 比較兩份已驗證 bundle 的 normalized observable contracts。
- 證明 stable state snapshot 前後一致，且遠端沒有 collector 暫存資源殘留。
- 所有 gates 通過後才刪除 shell、更新 README／Makefile，再重跑 final offline 與 real-lab gates。

## Validation Gates

### Offline gate

`make validate` 必須保持離線且可重複，包含：

- 既有 shell tests（cutover 前）。
- Python tests，涵蓋 127 個必移植情境。
- 約 8–12 個代表性黑箱 differential scenarios。
- Python 3.11 compatibility 與靜態檢查。

### Real-lab gate

`make validate-lab LAB_PROFILE=/absolute/path/to/lab.toml CEPH_INCIDENT_LAB_CONFIRM=1`：

- Identity preflight 必須 fail closed。
- Shell 與 Python 各執行一次 full collect。
- 四條 collector paths 必須完整，不能以 partial coverage 通過。
- 兩份 bundle 必須獨立通過 structural verification 與 cutover 階段仍存在的 content-safety checks。
- Lab Validation Report 必須記錄 code/profile identity、coverage、comparison、stable-state diff、cleanup proof、status 與 next action。

## Replaceable Lab Workflow

- Lab Profile 是 validation 的唯一連線來源；harness 由其 host map 產生暫存 inventory。
- Lab 重建後執行 `make lab-profile-discover`，只產生未信任的 candidate。
- Agent 或操作人員檢查 candidate 後明確啟用，再執行 strict preflight 與 full validation。
- Repository 不硬編碼 endpoints、credentials 或即時 cluster identity。

> Current status: `lab-status`、`lab-profile-discover` 與 `validate-lab` 是已鎖定但尚未實作的公開操作介面。#19 負責 profile/status/discovery，#20 負責 dual-run real-lab harness；#9 不提供暫時性的 ad-hoc 替代品。

## Out of Scope

- Python cutover 同時新增 collector 功能或改變 evidence 範圍。
- 改變 inventory 格式、bundle 結構或既有 CLI flag semantics。
- 在 Python rewrite 內同時移除 content safety。
- 以 byte-for-byte archive equality 取代 observable contract verification。
