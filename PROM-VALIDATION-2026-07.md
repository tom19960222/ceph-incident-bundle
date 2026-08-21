# `--prom-url` Prometheus metrics dump — 真機驗證證據（2026-07-10）

> **歷史紀錄，不能當成現行操作步驟。** 本頁記錄已由 #115 移除的 shell
> implementation 當時做過的驗證，內文中的 command、path、flag、layout 與 exit code
> 都不是現行產品介面，請勿執行。現行產品只有 installed Python
> `ceph-incident-bundle`；操作方式以 [`README.md`](README.md) 與
> [`docs/python-rewrite-spec.md`](docs/python-rewrite-spec.md) 為準。需要 rollback 時使用
> Git history、release tag 或先前發布的 artifact，不可從本頁重建舊 runtime。

> 背景：`--prom-url` 功能落地時 README 註明「尚未對真 Prometheus 驗證」。先前一次
> 口頭記錄的驗證沒有留下任何可稽核 artifacts（bundle 已隨被清掉的 worktree 消失），
> 因此 2026-07-10 重跑一次完整收集並把證據落在本檔。raw bundle 本體被 `.gitignore`
> 排除（只存本機），本檔是 committed 的驗證索引。

## 驗證環境

| 項目 | 值 |
|---|---|
| 日期 | 2026-07-10T17:40–18:06Z |
| 叢集 | cephadm ceph v19.2.3，3 mon（.166/.167/.164）+ 3 OSD host（.169/.171/.174，9 OSD），HEALTH_OK |
| Prometheus | v3.12.0（工作機本機，port 9790），使用真 active mgr `192.168.18.167:9283`（scrape_interval 5s）；dump 前確認 `up{job="ceph"}==1`、ceph_* metric 名 97 個 |
| 指令 | `run/collect.sh --inventory inventory/ceph-lab.example.env --ssh-key <repo>/.ssh/id_ed25519 --prom-url http://127.0.0.1:9790` |
| 收集範圍 | 完整 6-node collection（非只跑 prom 層）——同時複驗整條 pipeline |

## 結果

bundle：`results/ceph-incident-20260710T174049Z.tar.gz`（本機，gitignored）。
整體 exit code = **0**（verify → tar → verify 全過；stdout 僅 `bundle: <path>` 一行）。

### 逐項斷言

| # | 斷言 | 結果 | 證據 |
|---|---|---|---|
| 1 | bundle 內有 `cluster/prometheus/` 層 | ✅ | tar 清單 109 個 entry：`dump-info.txt`、`buildinfo.json`、`targets.json`、`ceph/index.txt`、`ceph/<metric>.json.gz` × 103 |
| 2 | 每個 job 的 metric 逐一 dump 且 gzip | ✅ | `jobs_seen=ceph`、`jobs_matched=ceph`、`metrics_ok=103`、`metrics_failed=0`（dump-info.txt） |
| 3 | budget 截斷未觸發（健康路徑） | ✅ | `truncated=0`（dump-info.txt）；截斷路徑由 `tests/test-prom-collector.sh` 的 fake curl 情境覆蓋 |
| 4 | step 自動計算 | ✅ | `since=24h` → `step_seconds=15`（`ceil(86400/10000)` → min 15s 規則，`lib/collect-prometheus.sh:43-48`） |
| 5 | dump 檔是可解析的真 time series | ✅ | 抽查 `ceph/ceph_health_status.json.gz`：gunzip → 合法 JSON，1 series、有樣本、值 `0`（吻合叢集當時 HEALTH_OK） |
| 6 | `environment.txt` 記錄 prom 欄位 | ✅ | `prom_url=http://127.0.0.1:9790`、`prom_jobs=ceph`；同檔 `ceph_source=ikaros@192.168.18.166`、`ceph_runner=sudo`（三階擇優真機選了 `sudo -n ceph`） |
| 7 | metrics json.gz 不做 redaction、其餘照常 | ✅ | `redactions.log` 對 `json.gz` 零項；`dump-info.txt`／`ceph/index.txt`／`targets.json`／`buildinfo.json` 4 個文字檔都有進 redaction 清單（各 0 line(s) redacted） |
| 8 | Prometheus 不可達 → `SKIPPED.txt` + 不炸整包 | ✅（歷史 bundle） | `results/ceph-incident-20260707T090438Z.tar.gz`（本機）的 `cluster/prometheus/SKIPPED.txt`：`SKIPPED: prometheus not reachable: http://prom.example:9090 (curl exit 6: ...)` |
| 9 | URL 帳密遮蔽（`user:***@`） | ➖ 本輪未演練 | 本輪 URL 無帳密；行為由 `lib/collect-prometheus.sh:51-60`（`prom_mask_url`）與 `tests/test-prom-collector.sh` 覆蓋 |

### 限制

- 本輪 Prometheus 只 scrape 了 `ceph` 一個 job（`job_regex` 預設 `ceph|node` 中的
  `node` job 在此 stack 不存在，`jobs_matched=ceph` 為正確行為）；多 job fan-out
  由測試覆蓋。
- 24h 窗內只有 stack 存活的 ~25 分鐘有樣本——dump 的是「窗內既有的所有樣本」，
  行為正確，但樣本密度不代表長期運行下的檔案大小。
