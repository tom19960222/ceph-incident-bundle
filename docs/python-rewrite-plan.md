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

## Current Python Candidate Boundary

PR #24／#10 建立 validation foundation 的第一條 vertical slice：公開 `verify` 能從 shell-produced workdir/archive 驗證必要 metadata、cluster/node evidence、archive integrity 與已納入的 traversal/link/special-member containment。Issue #11 再加入單一 inventory node 的公開 `collect` slice：工作機以一次 SSH process 將自足 `ceph_incident_node.py` 經 stdin 傳送，固定 bootstrap 在同一連線檢查 Python 3.11、以穩定 source name compile／exec，node 以 stdout 回傳 Node Evidence Archive 並只以 stderr 輸出 diagnostics。

#11 建立 shell contract 的七個 basic node commands；#12 在同一個自足 payload 內加入 `/var/log` 與 journal forensic evidence。Python candidate 現在保留 plain／rotated／gz／xz／bz2／zst discovery、family ordering、collision-safe merge tree、opaque/raw disposition、optional originals、sensitive-path exclusion、noatime/nofollow safe reads、metadata/payload caps、journal shared budget、INDEX／manifest 與 partial semantics。高 cardinality 輸出若會超過 receiver 的 16 MiB manifest cap，node 會先丟棄 `/var/log` payload tree、留下 `MANIFEST-LIMIT.txt` 與 INDEX，並回傳可接受的 partial archive，而不是讓整個 node evidence 被 receiver 拒絕。Fake filesystem 與 fake external-command tests 覆蓋 behavior inventory 的 V1–V14，以及 combined `/var/log`／journal cap、entry-count 與 manifest-size boundary；safe read 或第二階段 decode 失敗都不會退回不安全讀法或留下部分合併內容。

工作機先把 SSH stdout 寫進 invocation-owned candidate，完整驗證 gzip/tar/member payload、manifest 一對一 mapping、依 `/var/log` cap 加 archive overhead 計算的 payload cap、member type/name/collision/hierarchy 後才手工解出；missing/old Python 會成為 Skipped Node，valid archive 搭配 remote nonzero 會保留為 partial，corrupt/truncated/missing-manifest/unsafe archive 則在 extraction write 前原子拒絕。每次 node invocation identifier 會記在 `environment.txt`；success、valid-partial、fatal archive failure、timeout、SIGINT 與模擬 SSH 斷線的 SIGHUP cleanup 都有 fake-SSH offline proof。macOS 上的 tar 也明確停用 AppleDouble metadata，避免 archive 出現 collector 未宣告的 evidence members。

#13 再加入 direct Ceph CLI 這條 cluster evidence path：`--seed`（優先）或 inventory `SEED_HOST` 指定 ceph source 後，工作機以 `ssh <base opts> <seed> ceph <words…>` 逐條收集既有 20 個 JSON 與 4 個 text artifacts，並依 `crash-ls.json` 收集最多 10 筆 `crash info`（檔名消毒與 `-2` 防碰撞）。共用的 capture policy 會寫出 `# host/# collector/# started/# timeout` 檔頭、合流 stdout+stderr、以 rename 落地 artifact、追加 manifest 一行與 errors.log 一行；timeout 記 exit 124 並補 `# TRUNCATED`，missing command 記 127，exit 255/124/137 會寫 `ssh-debug/cluster-ceph-<target>.log`。任一 required capture 失敗使該層為 partial（2）並讓整體 exit 2，但所有指令照跑且失敗輸出仍保留為 evidence；crash list 無法解析只寫 `crash-info-skip.txt`，不算失敗。Workstation collector seam 已具備兩個 read-only runner 的 execution semantics：direct 跑 `ceph`，sudo 跑 `sudo -n ceph`；兩者都以 explicit argv 呼叫，且都不可到達 `cephadm shell`。公開 `collect` 在 #13 這條 slice 固定使用 direct，runner／source 的 selection、capability probe、mode 與 auto orchestration（含是否改用 sudo）仍屬 #16，也不在此新增公開 CLI flag。`sudo -n cephadm shell -- ceph` 不是受支援的 runner；`cephadm shell` 與 `kubectl exec` 維持 default-off 且不可由此路徑啟用。若工作機本身沒有 `ssh`，Ceph capture 記 127、該 node 成為 Skipped Node，整體仍產出可通過 verify 的 partial bundle（2），不會變成 fatal。

#14 再加入 Rook 這條 cluster evidence path，且只走既有的 kubectl operational paths。`--kube-mode local` 讓 kubectl 在工作機執行，`--kube-mode remote` 讓 kubectl 以既有 SSH option vector 在 inventory node 上執行；兩者都以 explicit argv 呼叫 read-only verbs。Kubeconfig 仍由執行 kubectl 的那一端的環境決定（local 為工作機、remote 為 node），collector 不注入 `--kubeconfig`；`--kube-context` 沿用 shell 的 `A-Za-z0-9._@:/-` 白名單，並以 `--context CTX` 前綴傳給每一次呼叫。Remote runner 的 `--since` 只接受 repo 既有的正數 `N`／`Ns`／`Nm`／`Nh`／`Nd`／`Nw` duration grammar，避免 OpenSSH 把 remote kubectl argv 重組交給遠端 shell 時讓 metacharacter 逸出；不選 remote Rook 時維持既有 node input 語意。Namespace 來自 inventory 的 `ROOK_NAMESPACE` 與 `ROOK_OPERATOR_NAMESPACE`，兩者未給或為空時各自預設 `rook-ceph`；external cluster 的 resource 與 operator log 因此可分屬不同 namespace。收集 `pods-wide.txt`、`events.txt`、`rook-resources.yaml` 三個 required artifacts 與 optional 的 `operator.log`；operator Pod 查不到只寫 `operator-SKIPPED.txt`，不使該層 partial。Manifest 以 `host=rook`、`collector=collect-cluster-rook` 記錄每次 capture，並沿用共用 capture policy 的檔頭、合流輸出、timeout 124 與 `# TRUNCATED` 語意。

無法收集任何 Rook evidence 時一律 fail closed 成 partial（2）並在 `cluster/rook/SKIPPED.txt` 寫出歸類原因與原始 kubectl 錯誤：local 缺 kubectl、remote 缺 kubectl、context 不存在、API 連不上、namespace 不存在、授權失敗與 probe timeout 都有各自的離線黑箱案例。`kubectl exec` 在 Python candidate 完全沒有實作路徑，也沒有對應 opt-in flag；collector 保留 shell 的 read-only toolbox Pod lookup command ledger，之後永遠寫出 `toolbox-SKIPPED.txt`，不會執行 toolbox 命令。Fake kubectl 與 fake SSH 都是白名單 adapter：只回應 collector 被允許發出的完整 argv，任何 mutating verb、`exec`、`--kubeconfig`、附加 token 或未 pin 的 bootstrap source 一律 exit 99，而且 failure 旋鈕不能放寬白名單。

Rook 只在明確指定 `--kube-mode` 時收集，這和 #13 的 Ceph 層只在有 `--seed` 時收集一致：capability probe、auto mode 與跨層 source selection 仍屬 #16，本 slice 不提供替代品。#14 也不新增 `--allow-kubectl-exec`、`--kubeconfig` 或任何 lab gate。

這仍是有明確限制的 Python candidate，不是 feature-complete Collect／Verify 契約，也不是 real-lab qualification evidence。

- Direct Ceph 與 Rook slice 都尚未含 redaction；#17 移植 content safety 前，真實 lab 的 `config dump` 或 `rook-resources.yaml` 等輸出可能仍帶 key 材料而被 verifier 的 content check 攔下。
- #17 會移植 cutover 階段仍保留的 secret path/content checks，並補齊長期 Structural Verification payload cap、雙階段驗證與相關黑箱案例。
- #23 已修正 shell Node Evidence Archive 的 pre-extraction acceptance boundary；shell reference 仍須通過 #19／#20 的其餘 qualification gates。
- #17 的 malicious final Incident Bundle 黑箱案例負責收斂 shell/Python Verify 對 link、special member、member collision 等接受差異；#23 只處理 shell 收到 Node Evidence Archive 後、解壓前的窄幅安全邊界。
- #17 未完成前，PR #24 的 candidate 只能作為離線 validation foundation，不能宣稱 feature-complete、observable-equivalent 或 qualification-ready；#23 的 shell boundary 完成不改變這項限制。

本階段的 Python 3.11 baseline 由 Makefile 的 offline gate 在任何測試前 fail fast；#11 已完成單一 node 的 runtime negotiation 與 graceful-skip seam，#12 已完成 `/var/log` forensic evidence，#13 與 #14 已分別完成 direct Ceph 與 Rook 這兩條 cluster evidence path；Prometheus、多 node 與 multi-source orchestration 仍依後續 collector tickets 實作。

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
