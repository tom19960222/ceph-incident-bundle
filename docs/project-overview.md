# Ceph Incident Bundle 專案介紹

## 專案解決的問題

Ceph 發生事故時，工程師需要在狀態繼續變化前保存主機、Ceph、Kubernetes 與 Prometheus 的原始資料。手動逐台登入和複製資料不但慢，也容易漏掉命令輸出、混淆收集時間，甚至在除錯時意外改變正在調查的系統。

`ceph-incident-bundle` 是一個 Python 3.10+ command-line interface（CLI）。操作人員在自己的 Collection Workstation（收集工作機）明確列出 Target Node（目標節點），工具便以 operationally read-only（不變更 persistent operational state）的方式嘗試收集證據，最後在工作機產生一個 `.tar.gz` Incident Bundle（事故證據包）。它保存當時能取得的資料，不診斷健康狀態、不修復叢集，也不保證所有來源都成功。

正式公開介面只有兩個 subcommand：

- `generate-inventory`：根據 hosts file 產生一份可供人工檢查的 Node Inventory 草稿。
- `collect`：驗證 Inventory，依明確範圍收集資料並發布 Incident Bundle。

## 收集哪些事故證據

一次完整收集可以包含四類 evidence source：

- **Target Node evidence**：主機身分、時間、作業系統、CPU、記憶體、process、storage、network、kernel、失敗的 systemd unit、container runtime、time synchronization、journal，以及時間範圍內的 `/var/log` regular file。
- **Ceph evidence**：在 Inventory 指定的一個 Target Node 上，以固定的 direct `ceph` command 收集 cluster status、health、OSD、PG、MON、MGR、orchestrator、configuration 與 crash 資料；每台節點也會嘗試保存 node-local Ceph configuration file。
- **Kubernetes evidence**：在 Collection Workstation 使用明確的 context 與 namespace 執行固定的 `kubectl get` 和 `kubectl logs`，保存 external consumer namespace 的 Rook object、event 與 Pod，以及 external consumer／operator namespace 的 Pod 與 container log。
- **Prometheus evidence**：由 Collection Workstation 使用 Python standard library 發送 HTTP GET，保存 build information、target、相關 job、metric name 與 range query response。

Command output 以 Probe Capture 保存。Probe 是收集器執行的一個固定 command；每個 Probe 都有獨立的 `stdout`、`stderr` 與 `result.json`。收集器保留原始輸出和執行結果，不把非零 exit code 解讀成叢集健康結論。

## 從 Node Inventory 到 Incident Bundle

Node Inventory 是一份 declarative INI file。`[nodes]` 明確決定 SSH scope，每個 `inventory_name` 對應一個 hostname 或 IP address；Ceph、Kubernetes 或 Prometheus 的觀測結果不會自動增加 Target Node。Ceph source、Kubernetes context 和 Prometheus URL 也都必須由操作人員明確設定。

執行 `collect` 後，工作機會先完整驗證 Inventory、evidence window 與 output directory。接著依序處理每個 Target Node，再處理選用的 Kubernetes 與 Prometheus 來源。Target Node 使用 system OpenSSH 連線到固定的 `root@ssh_address`；工具將 standalone Remote Node Collector 傳給遠端 `python3 -` 暫時執行，不安裝 agent，也不在遠端留下正式產品元件。

遠端 collector 將 Node Evidence Archive 經 SSH 傳回。這份 archive 是不可信 transport input；工作機會在解壓前完整拒絕 traversal path、link、special member、duplicate、portable name collision、缺少必要目錄和不完整 stream。只有通過 admission 的 evidence contribution 才會進入最後的 Incident Bundle。

所有來源嘗試完成後，工作機驗證已准入資料、建立 private archive candidate、嘗試清除本次 invocation 擁有的 workspace，再以不覆寫既有 destination 的方式發布最終檔案。

## 收集結果

成功交付 Bundle 時，command 的 standard output 會顯示檔案路徑與 outcome：

- `complete`：所有實際嘗試的來源、Probe 與已知 cleanup 都成功。
- `partial`：Bundle 已成功交付，但至少一個實際嘗試的來源、Probe 或 cleanup 失敗。成功取得的 evidence 仍會保留。

這兩種已交付結果都是 exit status 0。若 Inventory 在啟動前被拒絕、output directory 無法使用，或其他錯誤使 Bundle 沒有交付，standard output 會保持空白、exit status 非 0，standard error 最後會出現 `FAIL: no Incident Bundle delivered`。

因此，`partial` 不代表「沒有結果」，而是提醒調查人員先保留已交付的 Bundle，再查看 standard error 判斷缺少哪些來源。

## 安全界線與不支援的用途

收集行為是 operationally read-only：不變更 persistent configuration、service、package、mount、Ceph desired state、Kubernetes object 或 workload。它允許本次執行擁有的暫存 workspace，以及 SSH、API access log、audit record、atime 或 cache 等無法避免的觀測副作用；已知但無法清除的 residue 會被報告。

Incident Bundle 包含 Raw Evidence（未經內容處理的原始證據）。收集器不執行 redaction、secret scanning、sensitivity classification 或 semantic validation，也不會宣稱 Bundle 已 sanitized 或適合分享。`/etc/ceph` 可能包含 keyring，Prometheus URL 或其他回應也可能含 credential。

本專案刻意不提供 repair action、arbitrary user command、shell runtime、direct-on-node command、compatibility collector、verifier 或 redactor；也不會使用 `cephadm shell`、`kubectl exec`、debug Pod、ephemeral container 或其他 mutating fallback。它的工作是保存證據，而不是改變、驗證或修復被調查的系統。
