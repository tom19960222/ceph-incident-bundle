# User Guide

## 開始前的準備

這份指南會帶你從下載原始碼開始，完成 Target Node（你要收集證據的 Ceph／Kubernetes 主機）、Ceph、Kubernetes 與 Prometheus 的完整證據收集。所有 command 都在 Collection Workstation（你用來發起收集的 Linux 工作機）執行，除非步驟明確說明它會透過 SSH 在 Target Node 執行。

先準備以下資訊與權限：

- Collection Workstation 已安裝 Git、CPython 3.10+、system OpenSSH 與 `kubectl`。
- 每台 Target Node 的 hostname 或 IP address。
- 可以用 SSH key 非互動登入每台 Target Node 的 `root` account。工具固定連線到 `root@ssh_address`。
- 每台 Target Node 的 `python3` 是 CPython 3.10+。
- 一台已列入 Inventory 的 Target Node 能直接執行 `ceph` command，作為 Ceph source。
- 可用的 Kubernetes context、external consumer namespace 與 Rook operator namespace。
- 可由 Collection Workstation 存取的 Prometheus base URL。

若缺少 Git、OpenSSH 或 `kubectl`，請依 Collection Workstation 的 Linux distribution 安裝對應 package，或請 workstation 管理員安裝；不要在事故期間猜測 package name 或更動受調查的 Target Node。

確認 Python 版本：

```bash
python3.10 --version
```

預期看到 `Python 3.10.x`。若 command 不存在，請先請系統管理員提供 CPython 3.10+；不要替換或修改系統 Python。

> **重要：**最後產生的 Incident Bundle 保存未經遮蔽的 Raw Evidence，可能包含 `/etc/ceph` keyring、URL credential 或其他敏感資料。工具不會 redaction 或 secret scanning，不能把 Bundle 當成已 sanitized 或可直接分享的檔案。

## 取得原始碼

選一個你有寫入權限的工作目錄，clone repository，然後進入專案：

```bash
git clone https://github.com/tom19960222/ceph-incident-bundle.git
cd ceph-incident-bundle
```

確認目前位置正確：

```bash
pwd
ls
```

`pwd` 的最後一段應是 `ceph-incident-bundle`，`ls` 應至少看到 `README.md`、`pyproject.toml`、`inventory/` 與 `src/`。

## 建立 virtual environment 並安裝

Virtual environment 是專案專用的 Python 環境，可以避免修改 system Python。建立 `.venv` 並安裝 CLI：

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ceph-incident-bundle --help
```

最後一個 command 應顯示兩個 subcommand：`generate-inventory` 與 `collect`。後續步驟都使用 `.venv/bin/ceph-incident-bundle`，因此不需要 activate virtual environment。

若安裝失敗：

1. 確認仍位於 repository root。
2. 重新執行 `python3.10 --version`。
3. 將完整 pip error 提供給負責維護 Collection Workstation 的人員。Production package 沒有 third-party runtime dependency，不應要求額外下載 collector dependency。

## 檢查完整收集所需的連線

先完成 preflight，可以在正式事故收集前發現 SSH、Ceph 或 Kubernetes access 問題。以下範例使用：

- Target Node：`monitor01.example.com`、`192.0.2.20`
- Ceph source：`monitor01.example.com`
- Kubernetes context：`production-read-only`
- Namespace：`rook-ceph-external`、`rook-ceph`
- Prometheus URL：`https://prometheus.example.com`

請把範例值換成你的實際環境。

### 1. 確認 SSH 與 remote Python

第一次連線時，先用 ordinary SSH 讓你有機會人工確認 host key：

```bash
ssh root@monitor01.example.com
```

確認 fingerprint 符合組織紀錄後才接受，登入成功便輸入 `exit` 返回 Collection Workstation。不要在無法確認 fingerprint 時接受陌生 host key。

接著模擬 collector 的 noninteractive connection：

```bash
ssh -T -o BatchMode=yes root@monitor01.example.com python3 --version
ssh -T -o BatchMode=yes root@192.0.2.20 python3 --version
```

每台都應輸出 `Python 3.10.x` 或更新的 CPython。若出現 password prompt、`Permission denied`、host key error 或較舊版本，先修正 OpenSSH config／key／known hosts 或請管理員準備 remote runtime。Collector 不會安裝 Python，也不會改用其他 interpreter。

SSH key、port、jump host 和 known hosts 都交給 system OpenSSH config 管理。例如需要 jump host 時，先在 `~/.ssh/config` 完成並測試設定；Inventory 只放 hostname 或 IP，不放 `user@host`、port 或 shell option。

### 2. 確認 Ceph source

```bash
ssh -T -o BatchMode=yes root@monitor01.example.com ceph status
```

Command 必須能直接找到 `ceph` executable 並存取正確 cluster。Collector 不會使用 `sudo`、`cephadm shell` 或自動換到另一台節點。

### 3. 確認 Kubernetes context 與 namespace

```bash
kubectl config get-contexts
kubectl --context=production-read-only \
  --namespace=rook-ceph-external get pods
kubectl --context=production-read-only \
  --namespace=rook-ceph get pods
```

先在 context list 找到完全相同的名稱，再執行兩個 read command。不要只依賴目前的 ambient context；collector 一定使用 Inventory 指定的 context。

### 4. 確認 Prometheus URL

確認 URL 是 Collection Workstation 能直接存取的 Prometheus base URL，例如 `https://prometheus.example.com`，不要包含 query string 或 fragment。先使用 Python standard library 呼叫 collector 也會使用的 build-information endpoint：

```bash
PROMETHEUS_URL=https://prometheus.example.com
.venv/bin/python -c \
  'import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1].rstrip("/") + "/api/v1/status/buildinfo", timeout=10).status)' \
  "$PROMETHEUS_URL"
```

成功時應輸出 HTTP status `200`。若出現 DNS、connection、TLS、authentication 或 HTTP error，先請 Prometheus／workstation 管理員修正存取方式。Collector 只有一個 `url` 欄位，沒有另外的 username、password 或 environment-variable authentication 設定；請使用組織已提供、可直接存取的完整 base URL。若該 URL 本身包含 credential，Inventory Snapshot 會原樣進入 Bundle，工具不會遮蔽。

## 產生並確認 `inventory.ini`

請依實際環境二選一，不要連續執行兩個選項。

**選項 A：`/etc/hosts` 已包含所有 Target Node。**使用 generator 產生草稿：

```bash
.venv/bin/ceph-incident-bundle generate-inventory
```

成功後目前目錄會出現 `inventory.ini`。

**選項 B：`/etc/hosts` 沒有完整 Target Node。**不要先執行選項 A，直接複製完整格式範例：

```bash
cp inventory/example.ini inventory.ini
```

用你熟悉的文字編輯器開啟，例如：

```bash
nano inventory.ini
```

完整收集的內容應類似：

```ini
[common]
probe_timeout = 30m
ssh_connect_timeout = 15s

[nodes]
monitor01 = monitor01.example.com
osd01 = 192.0.2.20

[ceph]
source = monitor01

[kubernetes]
context = production-read-only
consumer_namespace = rook-ceph-external
operator_namespace = rook-ceph

[prometheus]
url = https://prometheus.example.com
metrics_filter_regex =
query_step = 15s
request_timeout = 5m
```

逐段確認：

- `[common]`：Probe 是 collector 執行的一個固定 command，每個 Probe 都分開保存 `stdout`、`stderr` 與 `result.json`。`probe_timeout` 是每個 Probe 的最長等待時間；`ssh_connect_timeout` 是 SSH 建立連線的等待時間。
- `[nodes]`：等號左邊是穩定、可攜且唯一的 `inventory_name`；右邊是 SSH hostname 或 IP。至少要有一台。
- `[ceph]`：`source` 必須等於 `[nodes]` 左邊的某個名稱，不是 hostname。
- `[kubernetes]`：`context` 必須明確填寫；兩個 namespace 要與 preflight 使用的值一致。
- `[prometheus]`：`url` 是 base URL。空的 `metrics_filter_regex` 表示保留所有從相關 job 發現的 metric；`query_step` 控制 range query 間隔。

不要在 Inventory 中加入未知 section、拼錯的 key、SSH username、port 或 command。產生草稿時若看到 `ACTION REQUIRED` 或 `Inventory Name collision`，先修改重複名稱再收集。

## 執行完整證據收集

先建立一個專用 output directory；`collect` 要求它已存在，而且不會自動覆寫相同 final path：

```bash
mkdir -p results
```

執行完整收集，以下使用最近 24 小時作為共同 Log Evidence Window：

```bash
.venv/bin/ceph-incident-bundle collect \
  --inventory "$(pwd)/inventory.ini" \
  --since 24h \
  --output-dir "$(pwd)/results"
```

`--since` 接受正整數加單位：`m`（分鐘）、`h`（小時）、`d`（天）或 `w`（週）。它同時影響 Target Node 的 `/var/log`／journal、Kubernetes container log 與 Prometheus range query。Filesystem log 使用 modification time 做近似篩選，不代表精確事故邊界。

收集可能花一段時間，取決於 Target Node 數量、Probe timeout、log size、Kubernetes container 數量與 Prometheus metric 數量。執行期間：

- 不要同時修改 `inventory.ini`；Bundle 會保存啟動時接受的 snapshot。
- 不要刪除 `results/`。
- Terminal 出現某個 Probe failure 時，讓 command 繼續；其他 evidence source 仍會嘗試。
- 若必須中止，按一次 Ctrl-C，等待 collector 完成可做的 cleanup 並回報結果。

## 確認收集結果

成功交付時，最後一行 standard output 類似：

```text
/absolute/path/results/ceph-incident-bundle-20260825T153000Z.tar.gz (complete)
```

或：

```text
/absolute/path/results/ceph-incident-bundle-20260825T153000Z.tar.gz (partial)
```

確認檔案存在：

```bash
ls -lh results/ceph-incident-bundle-*.tar.gz
```

把成功訊息中那一個完整 Bundle path 複製到變數；不要使用可能同時匹配多個舊 Bundle 的 glob：

```bash
BUNDLE_PATH=/absolute/path/results/ceph-incident-bundle-20260825T153000Z.tar.gz
```

列出 archive 結構而不解壓：

```bash
tar -tzf "$BUNDLE_PATH" | less
```

預期看到 timestamp root、`inventory.ini`、`collection.json`、`nodes/`、`ceph/`，以及完整設定時的 `kubernetes/`、`prometheus/`。Archive 內的 `inventory.ini` 是收集開始時的 Inventory Snapshot，也就是 accepted `inventory.ini` 的 byte-for-byte 副本。某個 Target Node 若被 skipped，不會有對應的 `nodes/<inventory_name>/`；請對照這份 snapshot 與 terminal diagnostics。

若要確認一項實際 evidence，以下以 `status-text` Probe（對應 direct `ceph status`）為例。不確定有哪些 Probe 時，先用上方 `tar -tzf` command 查看實際清單。將 Bundle 解壓到只有目前使用者能存取的 temporary directory：

```bash
REVIEW_DIR="$(mktemp -d)"
tar -xzf "$BUNDLE_PATH" -C "$REVIEW_DIR"
find "$REVIEW_DIR" -path '*/ceph/probes/status-text/stdout' -exec less {} \;
```

最後一個 command 會開啟 direct `ceph status` Probe 保存的原始 stdout；按 `q` 離開 `less`。若沒有找到檔案，檢查此次收集是否設定並成功准入 Ceph source。`REVIEW_DIR` 內同樣是未經遮蔽的 Raw Evidence，請依組織政策保存，不要把它視為一般 temporary data。

## 理解 complete、partial 與失敗

把收集想成同時向多個來源索取文件：某一個來源沒有回覆，不代表已收到的文件要丟掉。因此 Bundle 可以是 partial，但仍然是有用且已交付的結果。

| Terminal 結果 | Exit status | 要做什麼 |
| --- | --- | --- |
| 顯示 bundle path 與 `(complete)` | `0` | 保存 Bundle；所有實際嘗試與 cleanup 都成功 |
| 顯示 bundle path 與 `(partial)` | `0` | 先保存 Bundle，再閱讀前面的 stderr，記錄缺少的 source／Probe |
| 最後顯示 `FAIL: no Incident Bundle delivered` | 非 `0` | 沒有交付 Bundle；依 stderr 修正 startup 或 publication 問題後重新執行 |
| Ctrl-C 後 exit `130` | `130` | Publication 前中止且沒有 Bundle；確認 stderr 是否報告 residue |

`partial` 不是 command failure，也不是「Bundle 不存在」。相反地，只要 stdout 提供 final path，就代表該路徑已發布。即使 stdout 因 terminal 問題無法寫出，stderr 也會嘗試說明已交付位置。

## 常見問題排除

### Inventory 被拒絕

Collector 會在連線前列出所有能找到的問題。一次修正 stderr 指出的 duplicate section/key、unknown section/key、invalid duration、invalid regex、invalid URL、unresolved Ceph source 或 Inventory Name collision，再重新執行。

### `Inventory output already exists`

`generate-inventory` 預設不覆寫。要保留舊檔時換 output：

```bash
.venv/bin/ceph-incident-bundle generate-inventory \
  --output inventory-new.ini
```

只有在確定可以取代原檔時才使用 `--force`。

### SSH connection 或 host key 失敗

重新執行 preflight 的 `ssh -T -o BatchMode=yes` command。修正 `~/.ssh/config`、private key permission、jump host、DNS 與 known hosts。Collector 不會顯示 password prompt，也不會自動接受新 host key。

### Target Node 顯示 unsupported Python

Remote command 固定是 `python3 -`，且要求 CPython 3.10+。請系統管理員準備正確的 `python3`；collector 不會安裝 runtime、不會搜尋 `python3.11` 等替代名稱，也不會 fallback 到 shell collector。

### Ceph Probe 失敗

確認 `[ceph] source` 指向正確 `inventory_name`，並重跑 preflight 的 remote `ceph status`。Collector 只在該 source 使用 direct `ceph` command，不會自動改用其他節點。

### Kubernetes Probe 失敗

確認 context 拼字、兩個 namespace、kubeconfig 與 read permission。所有 Kubernetes command 都在 Collection Workstation 執行；collector 不會在 Target Node 尋找 `kubectl`，也不會進入 Pod。

### Prometheus request 失敗或結果很多

確認 base URL 可由 Collection Workstation 存取、TLS／authentication 正確且 URL 不含 query／fragment。需要縮小 metric 時，在 Inventory 設定 `metrics_filter_regex`；這個 regex 套用在已發現的 metric name，不是 Prometheus job selector。

### 結果是 partial

先保留已交付 Bundle。往上查看 stderr 中以 Target Node、Kubernetes 或 Prometheus 開頭的 problem。Missing command、nonzero Probe、timeout、unsafe archive、HTTP error 或 cleanup residue 都可能造成 partial；修正來源後可以進行另一次收集，但不要覆寫原 Bundle。

### Output path 已存在

Collector 不覆寫既有 destination。因 filename 使用 UTC second，請等待下一秒重跑或指定另一個已存在的 output directory；不要刪除仍在調查中的舊 Bundle。

## Bundle 內容與敏感資料

Incident Bundle 保存 Inventory Snapshot、collection metadata，以及各個成功准入 source 的 Raw Evidence。Probe 的原始 `stdout`／`stderr` 與 collector-authored `result.json` 分開保存；Prometheus 則保存原始 `response` 與 request `result.json`。

Raw Evidence 可能包含 credential、hostname、IP address、configuration、log、process argument、keyring 或 access URL。工具不會檢查或移除這些內容，也不會證明資料完整、正確或安全分享。請把 Bundle 視為敏感事故資料，遵循組織既有的存取控制與保存政策。
