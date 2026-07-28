# 刪除 shell 前必須通過 automated、differential 與 real-lab gates

Python cutover 不能只靠假資料測試。刪除 shell 前，既有 shell suite、新 Python suite、shell/Python 黑箱 differential harness 與 real-lab canary 必須全部通過；differential harness 比較正規化後的 observable contracts，real-lab canary 則使用真實 Ceph/Rook 主機與外部指令驗證整條收集路徑。

## Consequences

- Shell 實作保留到所有 gates 通過，作為 differential reference。
- Real-lab canary 必須在 collect 前後取得 stable state snapshot，確認 identity/configuration 未改變，且 node 沒有殘留 collector 暫存目錄。
- 自然變動的 Ceph counters、epochs 與時間資料不得用來判定 read-only regression。
- 每份 real-lab incident bundle 都必須通過 structural verification；cutover 階段也必須通過仍存在的 content-safety checks。
- Lab 連線、憑證與即時拓撲視為外部輸入，執行前必須重新確認，不能沿用舊紀錄直接假設仍有效。
- Real-lab gate 必須在同一個 lab 以單一 full collect invocation 同時涵蓋 Ceph、Rook、Prometheus 與全部 inventory nodes（包含 `/var/log`）；不能用四次彼此獨立的 smoke tests 拼成通過紀錄。
- Full collect 必須產生完整 evidence，不得以缺少任一 collector 路徑的 partial bundle 宣告通過；Python 產物必須符合既有 shell reference 的 observable contract。
- Lab 應使用 read-only 的直接 Ceph CLI、本機 kubectl 與 Prometheus HTTP 路徑，不為了通過 gate 開啟 `cephadm shell` 或 `kubectl exec` 等可能產生額外執行副作用的 opt-in。
- 實機等價驗證包含兩次連續執行：先以 shell reference 跑一次 full collect，再以 Python candidate 使用相同 inventory、參數與 lab 跑一次 full collect。每次 invocation 都必須自行收齊四條 collector 路徑，不能把 shell 與 Python 的結果混合成一份通過紀錄。
- Stable state snapshot 涵蓋兩次 full collect 的整個前後區間；兩份 bundle 都必須獨立通過 verify，之後才比較正規化後的 observable contracts。
- 一般 `make validate` 必須保持離線且可重複，只執行 shell、Python、假環境 differential 與靜態檢查；real-lab gate 由獨立的 `make validate-lab` 執行。
- `make validate-lab` 是明確 opt-in，必須要求 `CEPH_INCIDENT_LAB_CONFIRM=1` 或同等確認，不能由一般 CI 或日常驗證意外觸發。
- Lab endpoints、inventory、SSH key path、kubeconfig、Prometheus URL 與預期 cluster identity 都是可替換的輸入，不能硬編碼在 validation harness；更換 lab 時應只需更換連線設定，不修改驗證邏輯。
