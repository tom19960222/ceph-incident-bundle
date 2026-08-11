---
status: accepted
---

# Production runtime 支援 CPython 3.10，validation tooling 保持 3.11

Production runtime 的最低支援版本由 Python 3.11 下修為 CPython 3.10；
這個邊界包含 `ceph_incident_bundle.py` 的 `collect` 與 `verify`、工作機
collectors `ceph_incident_collectors.py`，以及透過 SSH stdin 傳送的自足
`ceph_incident_node.py`。Local-only `validation/` 與 real-lab harness 可繼續使用
Python 3.11+ 標準庫；兩者必須使用可獨立選擇、可追溯的 interpreter，
避免 tooling 的 3.11 PASS 被誤當成 production 的 3.10 proof。

## Compatibility boundary

- 正式相容性承諾與必要驗收證據以 CPython 為準。其他 Python
  implementation 若回報 `sys.version_info >= (3, 10)` 不會被 production bootstrap
  刻意擋下，但不能取代 CPython 3.10 proof，也不屬於正式支援範圍。
- Node 上的 interpreter command 固定為 `python3`。Collector 不搜尋
  `python3.10`、`python3.11` 或其他替代名稱，也不增加第二次 SSH
  preflight。
- 遠端 bootstrap 在同一次 SSH connection 中先檢查 Python 3.10 floor，
  通過後才從 stdin 讀取並 compile payload。找不到 `python3` 或版本
  低於 3.10 時，bootstrap 以明確訊息與 remote exit `75` 拒絕 payload；
  該 node 仍是 Skipped Node，其他 evidence 繼續收集，整體為 partial exit `2`。
- 這個變更不改變 CLI、bundle schema、Node Evidence Archive、single-SSH
  transport、archive-before-extraction、content safety、operationally read-only
  contract 或 cleanup semantics。

## Interpreter isolation

Offline 與 real-lab gates 必須分別接收 production 與 tooling interpreter 的明確
path：`PRODUCTION_PYTHON` 執行 production code，`TOOLING_PYTHON` 執行
validation/lab tooling。Gates 檢查並記錄 resolved path、
`sys.implementation.name` 與完整版本。
Repository 不下載、安裝、升級或切換 Python，不執行 `pip install`，不修改
global site-packages、系統 `python3`、shell default 或 `.python-version`。執行者
必須預先提供隔離的 CPython 3.10 與 Python 3.11+ interpreter；node 的
`python3` 也必須在 qualification 之前完成 provision。
直接在低於 3.10 的 workstation Python 上執行 production source 不需要
compatibility wrapper；清楚的 workstation fail-fast 責任屬於上述 gates。

## Acceptance

### Offline

`make validate` 保持完全離線，並且必須同時通過兩道可區分的 gate：

1. 真實 CPython 3.10.x 執行全部 production tests，包含 `collect`、
   `verify`、Ceph、Rook、Prometheus、content safety、archive rejection、
   interruption/cleanup 與 scenario ledger；remote black-box test 也必須真正以
   CPython 3.10 執行 node payload 並產生可接受的 Node Evidence Archive，
   不能用偽造 `sys.version_info` 取代。同一個黑箱邊界必須另外驗證
   Python 3.9 與 missing-`python3` 均會保留明確 diagnostics、Skipped Node、
   其他 node 繼續收集、整體 partial `2` 與既有 cleanup semantics。
2. Python 3.11+ 執行現有完整 suite，包含 validation/lab harness、
   Python-only layout checks、production regression tests 與 134 個 behavior-bearing
   mappings。拆分 runner 不得讓任何現行 safety、archive 或 cleanup test
   離開必要 gate。

任一 interpreter 缺少、版本／implementation 不符、任一 test 失敗或沒有
記錄 resolved runtime identity，都不能通過 offline acceptance。
Gate 還必須證明指定低於 3.10 的 `PRODUCTION_PYTHON` 會在任何 test 或連線前
以 production-specific 訊息 fail fast，低於 3.11 的 `TOOLING_PYTHON` 也會在
import `tomllib` 之前以 tooling-specific 訊息 fail fast。

### Real lab

`make validate-lab` 仍是 explicit opt-in，保留所有既有 baseline provenance、strict
identity、four-path Full Collect、verify、normalized comparison、stable-state、
workstation cleanup 與 remote residue 條件，並額外要求：

1. Lab harness 使用 Python 3.11+；實際執行 workstation `collect` 與 `verify`
   的 production interpreter 必須是 CPython 3.10.x。
2. Strict identity 通過後、collect 前，以固定且唯讀的 SSH argv 記錄每台
   inventory node 的 resolved `sys.executable`、`sys.implementation.name`，以及
   `sys.version_info` 的 `major`、`minor`、`micro`、`releaselevel`、`serial`；
   collect 後再查一次，兩次的 structured fields 必須一致。
3. Active Lab Profile 中至少一台 inventory node 的固定 `python3` 必須是
   CPython 3.10.x；找不到 witness 時必須在 collect 前 fail closed。該
   floor-witness node 必須參與同一次 Full Collect，成功完成
   node evidence 與 `/var/log`，回傳通過驗證的 archive，而且沒有 remote
   residue；partial 或 skipped 不能成為 proof。其他 nodes 可使用任何
   supported CPython 3.10+ runtime。
4. Schema-v3 Lab Validation Report 必須記錄 tooling/production interpreter identity、
   每台 node 的 pre/post runtime facts、probe exit status 與 floor-witness node。任一 probe
   失敗、版本前後變動、沒有 CPython 3.10.x witness 或 witness collect 不完整，
   都必須在 report 中 fail closed。

Runtime proof 屬於 lab report，不新增 Node Evidence Archive member，不改變與保存
shell baseline 之間的 bundle comparison surface。手動版本輸出、截圖或獨立
smoke test 不能取代 schema-v3 `status: pass`。Schema v1 與 v2 report 保留
當時的原意；舊 v2 PASS 仍是當時有效的 post-cutover proof，但不能證明
Python 3.10 compatibility。保存的 #21 schema-v1 baseline 仍可作為 schema-v3
qualification 的歷史比較來源。

## Consequences

ADR 0003 的 graceful-skip 決策保留，只有 3.11 floor 被本 ADR supersede；
ADR 0004 的 single-SSH transport 保持不變。#21、#22 與保存 baseline 在
Python 3.11 上產生的歷史事實不改寫，也不能追溯聲稱為 3.10 proof。

本 ADR 記錄已接受的目標與驗收邊界，不是實作已通過的證據。在 collector、
Makefile、tests 與 lab report schema 實作完成並通過上述 gates 前，README、
AGENTS、safety contract 與 runbook 必須繼續描述可執行的 Python 3.11 現況；
實作交付必須在同一變更中同步更新這些現行文件與所有版本錯誤訊息。
