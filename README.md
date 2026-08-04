# Ceph Incident Bundle

## 這是做什麼的

這套 script 是事故發生時的「先保留現場」工具。它會從一台工作機透過 SSH 到所有 Ceph node 收集系統狀態、time sync 狀態、Ceph 狀態，以及每台 node 的 `/var/log`，最後打包成一個 `.tar.gz`。

它不會修復 Ceph，也不會執行 restart、delete、repair、scrub 這類會改變 cluster 狀態的操作。

## 什麼時候執行

建議在以下情境先跑一次：

- `ceph health detail` 出現 `HEALTH_WARN` 或 `HEALTH_ERR`
- OSD down、PG stuck、I/O latency 異常、MON quorum 異常
- node CPU、RAM、disk、網路或 time sync 看起來異常，但還不確定是不是 Ceph 問題
- 準備請別人或 AI 協助判讀，需要保留當下證據

## SSH host key

工具的 SSH 使用 `BatchMode=yes`。預設 `accept-new` 會先讀既有
`~/.ssh/known_hosts`，但新的 host key 只寫進本次 collector workdir 的暫存檔，
不修改使用者的 `known_hosts`；已知 host 的 key 不一致仍會拒絕連線。

## 最短操作流程

在 repo root 執行：

```bash
bash run/collect.sh \
  --inventory inventory/ceph-lab.example.env \
  --ssh-key .ssh/id_ed25519 \
  --seed ikaros@192.168.18.166 \
  --mode cephadm \
  --since 24h
```

成功後會看到：

```text
bundle: results/ceph-incident-YYYYMMDDTHHMMSSZ.tar.gz
```

驗證 bundle：

```bash
bash lib/verify-bundle.sh <bundle.tar.gz>
```

## 如何填 inventory

Inventory 是 shell 檔案，格式如下：

```bash
SSH_USER="ikaros"
SEED_HOST="192.168.18.166"
ROOK_NAMESPACE="rook-ceph"
HOSTS=(
  "monitor01=192.168.18.166"
  "mon02=192.168.18.167"
  "osd01=192.168.18.169"
)
```

- `SSH_USER`：登入每台 node 的 Linux 帳號。
- `SEED_HOST`：**選填**。手動指定 cluster-level `ceph` command 要在哪台跑;不填則 `auto` 會自動挑第一台「ceph 連得上」的 node(有 `ceph` 或 `cephadm` 且 `ceph -s` 成功)。
- `ROOK_NAMESPACE`：Rook 的 namespace，未填時預設 `rook-ceph`。
- `HOSTS`：每個項目是 `alias=host`，alias 會成為 bundle 裡 `nodes/<alias>/` 的目錄名稱。external-ceph rook 拓樸可以把 **external ceph 主機與 k8s node 混在同一份** `HOSTS` 裡。

Inventory 是 declarative 格式，只接受上述 quoted scalar 與 `HOSTS` array；
不會再當成 shell script `source`，因此不能放 command substitution 或其他命令。

## 自動偵測（auto，預設）

預設 `--mode auto` 會逐台 node 經 ssh 偵測能力，再分層收集：

- node 上有 `ceph` 或 `cephadm` → 從**第一台連得上 cluster** 的 node 收 cluster-level ceph。預設只試直接 `ceph` → `sudo -n ceph`，兩者都是既有 CLI 的唯讀查詢。`cephadm shell` 可能啟動或 pull container，因此預設禁用；只有明確給 `--allow-cephadm-shell` 才作最後 fallback。「可用」= `ceph -s` 連得上，不是 binary 存在；選到哪個會記在進度與 `environment.txt` 的 `ceph_runner=`。
- rook 層的 `kubectl` 由 `--kube-mode` 決定（預設 `remote`）：
  - `remote`（預設）：從**第一台**有 kubectl 的 inventory node、用 ssh 在該 node 上跑 `kubectl`。
  - `local`：在**執行工具的跳板機本機**跑 `kubectl`（kubectl/kubeconfig 在跳板機、不在 node 上時用這個）。
  - 兩種都可配 `--kube-context`。
- 兩層都有來源就都收、各收一次;node 層一律每台都收。

Rook 層預設不執行 toolbox `kubectl exec`，因為它會在 Pod 內啟動 process；
需要這份額外證據時才明確加 `--allow-kubectl-exec`。

```bash
bash run/collect.sh \
  --inventory inventory/ceph-lab.example.env \
  --ssh-key .ssh/id_ed25519 \
  --since 24h
```

## external ceph + rook（一份 inventory）

把 external ceph 主機和有 `kubectl` 的 k8s node 列進同一份 `HOSTS`，`auto` 會：ceph 層從 ceph 主機收、rook 層在 k8s node 上跑 kubectl 收。指定 context：

```bash
SSH_USER="ikaros"
HOSTS=(
  "mon01=10.0.0.1"     # external ceph（有 cephadm）
  "osd01=10.0.0.2"     # external ceph
  "k8s1=10.0.0.9"      # k8s node（有 kubectl）
)
```

```bash
bash run/collect.sh \
  --inventory inventory/external.env \
  --ssh-key ~/.ssh/id_ed25519 \
  --kube-context my-cluster \
  --since 24h
```

## 只收單層（覆寫）

- `--mode cephadm`（可配 `--seed USER@HOST`）：只收 ceph 層。
- `--mode rook`：只收 rook 層（在第一台有 kubectl 的 node 上跑）。

## Prometheus metrics dump（選用）

給 `--prom-url` 時，會在收 cluster 證據後，從該 Prometheus 把「執行當下往回
`--since`」窗內、job 名稱符合 `ceph|node`（`--prom-job-regex` 可覆寫）的每個
metric 各打一次 `query_range`，原始 JSON 逐一 gzip 存進
`cluster/prometheus/<job>/<metric>.json.gz`。不給 `--prom-url` 則完全不碰
Prometheus。

```bash
bash run/collect.sh \
  --inventory inventory/ceph-lab.example.env \
  --ssh-key .ssh/id_ed25519 --mode cephadm --since 24h \
  --prom-url http://192.168.18.166:9095
```

- 前置：工作機要有 `curl` 與 `python3`，且 URL 從工作機直接可達（不走 ssh
  tunnel）。缺任一 → `cluster/prometheus/SKIPPED.txt` + exit 2。
- step 預設 `max(15, ceil(window/10000))` 秒（避開 Prometheus 每 series 11,000
  點上限；`--prom-step` 可覆寫）。整段 dump 的時間預算 `--prom-timeout`（預設
  600s），超時會截斷並在 `dump-info.txt`／`index.txt` 標 `TRUNCATED`。
- exit code 語意不變：dump 失敗／截斷 → exit 2（partial），bundle 照樣產出。
- 安全界線：`<job>/<metric>.json.gz` 是數值 time series，**不做** redaction
  （單行大 JSON 逐行 redact 極慢，且 regex 誤中會讓整檔變 `[REDACTED]`）；
  `dump-info.txt`、`index.txt`、`buildinfo.json`、`targets.json` 照常 redact。
  URL 內嵌的 `user:pass@` 寫進任何 artifact 前會遮蔽為 `user:***@`。
- 已對真 Prometheus 驗證（2026-07-10，真 cephadm lab + 本機 Prometheus v3.12.0，
  103 metrics 全數 dump 成功）：逐項斷言見 `PROM-VALIDATION-2026-07.md`。

## auto 的限制（已知）

- **來源挑「第一台」**：cluster-ceph 取第一台**ceph 連得上**的 node(會實際試 `ceph -s`,連不上就換下一個候選);cluster-rook(remote)取第一台**有 `kubectl` 指令**的 node(只看指令存在,不檢查 k8s 健康、不 fallback 到第二台)。若想釘住一台已知健康的 mon,用 `--seed USER@HOST`。
- **探測是逐台序列 ssh**:某層的能力完全不存在時(例如純 cephadm 叢集仍會為了 rook 掃完每台),或 node 沒回應時,探測會逐台等到 `ConnectTimeout`。大型 inventory 建議直接用 `--mode cephadm --seed ...` 跳過探測。探測 ssh 失敗的 node 會記進 `errors.log`(`capability probe failed for ...`),不會被當成「沒有該能力」而靜默忽略。

## `/var/log` 收集、合併與容量

- `--timeout`（預設 20s）是**單一指令 / SSH 連線**的逾時。
- `--node-timeout`（預設 600s）是**單一 node 整輪收集**的逾時。兩者分開：慢或大的 node 不會被單指令逾時誤殺。
- 每台 node 會遞迴掃描 `/var/log` 的 regular files，不跟隨 symlink，也不讀 socket/device/FIFO。
- 同目錄、同 family 的 active/rotated log 會依最舊到最新合併；支援 `.gz`、`.xz`、`.bz2`、`.zst`。ZIP、tar、binary、journal raw file 不合併，原樣放在 `raw/` 並列入 `UNREDACTED-OPAQUE.txt`。合併檔使用 collision-free tree：每層來源目錄放在 `merged/tree/dirs/<name>/`，該層的 family 放在 `files/<family>.merged`。
- `/var/log` file history 不受 `--since` 限制；`journalctl` 的可讀文字輸出仍遵守 `--since`。
- 預設每台 log payload 上限 10 GiB。用 `--var-log-max-bytes BYTES|unlimited` 調整；payload 包含 merged/raw/original 與 `journal-all-since.txt`，redaction 後會再驗一次。預估或實際超限、遠端暫存空間不足時只留下 index/原因並回傳 exit 2，不產生看似完整的半套 log。另以 64 MiB scan-path staging 與 100,000 entries 上限約束 metadata/記憶體；超過時留下 `SCAN-LIMIT.txt`。
- 成功合併後預設不重複保存文字來源；`--keep-original-logs` 才會在 `original/` 保留來源格式。`--skip-logs` 可完全跳過 `/var/log` file collection。
- 被逾時砍掉（exit 124/137）的指令輸出會在 artifact 末尾標 `# TRUNCATED`，讓判讀者知道內容被截斷。
- **工作機若沒有 `timeout` / `gtimeout`**（如預設 macOS），會在開頭印警告；此時外層逾時停用，只靠 SSH `ConnectTimeout` / `ServerAlive` 把關。要完整把關可 `brew install coreutils`（提供 `gtimeout`），或在 Linux ops 機執行。Qualification 工作機必須先補齊，否則 real-lab gate 會在 bundle comparison 以 `# timeout` 標頭差異失敗（見 `docs/adr/0011-require-a-timeout-binary-on-the-qualification-workstation.md`）。

## 進度顯示

執行時會把進度印到 **stderr**（探測每台 node、cluster ceph 的逐條指令 `[k/24]`、每台 node 收集、redact/verify/packaging）。**stdout 只會有最後一行 `bundle: <path>`**，方便 script 直接抓。

要安靜（cron / 腳本）加 `--quiet`：不印進度,但 `bundle:` 與錯誤訊息照舊。

```bash
# 看得到進度（預設）
bash .../run/collect.sh --inventory inv.env --ssh-key key --since 24h
# 安靜，只取 bundle 路徑
BUNDLE=$(bash .../run/collect.sh --inventory inv.env --ssh-key key --since 24h --quiet | sed 's/^bundle: //')
```

## SSH host key 與 redaction 開關

預設行為：

- SSH 連線會加上 `StrictHostKeyChecking=accept-new`，第一次連到新 host 時自動接受 host key；如果 host key 之後變更，OpenSSH 仍會阻擋。
- bundle 打包前會執行 redaction，遮蔽明顯敏感內容。

需要改變預設時：

```bash
# 不自動接受新的 SSH host key，回到 OpenSSH 預設檢查行為
--no-trust-ssh-host-key

# 保留原始內容，不做 redaction
--no-redact
```

也可以明確寫出預設值：

```bash
--trust-ssh-host-key --redact
```

## bundle 內有什麼

主要檔案：

- `README-FIRST.txt`：打開 bundle 後先看的入口。
- `CONTENTS.md`：**人類可讀的目錄**——每個檔案是什麼,以及(對每個收集到的 artifact)**產生它的完整指令 + exit code**。分 cluster 一段、每台 node 一段,內容直接從 manifest 產生,永遠與實際收到的一致。想知道「某個檔是哪條指令跑出來的」看這份最快。
- `summary.txt`：本次收集摘要與成功/失敗數。
- `environment.txt`：收集時間、mode、seed、git commit,以及選到的 `ceph_source`/`ceph_runner`/`rook_source`。
- `manifest.jsonl`：每個 artifact 的 command、exit code、時間(machine-readable;`CONTENTS.md` 就是它的可讀版)。
- `errors.log`：非零 exit code、SSH 失敗、部分失敗。
- `redactions.log`：每個檔遮蔽了幾行。
- `cluster/`：cephadm(直接 `ceph` 或 `cephadm shell`)或 Rook cluster-level 狀態。
- `cluster/prometheus/` — 選用的 metrics dump（有給 `--prom-url` 才存在）
- `nodes/<alias>/`：每台 node 的系統、資源、disk、kernel、systemd、time sync、`logs/var-log/{merged/tree,raw,original}` 與 cephadm 狀態。

time sync 會同時保留常見工具的狀態：`timedatectl` / `systemd-timesyncd`、`chronyc`、`ntpq`。如果 node 使用 `systemd-timesyncd`，bundle 會收 `timedatectl status`、`timedatectl show-timesync --all`、`timedatectl timesync-status`、`systemctl status systemd-timesyncd`、`journalctl -u systemd-timesyncd`，以及 `/etc/systemd/timesyncd.conf` 與 `/etc/systemd/timesyncd.conf.d/*.conf`。

## exit code 怎麼看

- `0`：收集完成，沒有已知失敗。（注意：OSD/MON down 這類**叢集故障本身**會被收進 bundle，不算收集失敗，仍是 `0`。）
- `2`：有部分 command 或部分 node 失敗，但 bundle 已產生。先看 `errors.log` 和 `summary.txt`。
- `1`：使用方式或必要輸入錯誤（inventory / SSH key 不存在），或 **bundle 驗證失敗**。驗證失敗時不會打包可分享的 `.tar.gz`，而是**保留 workdir**（印出路徑）讓你檢查——已收集的證據不會因驗證失敗被刪掉。

## 常見失敗與處理

- `missing inventory`：確認 `--inventory` 路徑存在。
- `missing ssh key`：確認 `--ssh-key` 路徑存在，且本機可讀。
- `node <alias> collector exited 255` / `Host key verification failed`：SSH 連線、帳號、key、**known_hosts**(見上方「前置需求」)或 sudo 權限問題。新跳板機最常見的是 known_hosts 還沒有該 node 的 host key。
- `VERIFY FAIL`：bundle 結構不完整，或包含 `keyring`、`.ssh`、`id_ed25519`、`private_key`、`*.pem`/`*.key`/`*.crt` 這類路徑，或檔案內容殘留未遮蔽的 private key / `key = <base64>` 金鑰材料。此時 workdir 會被保留、不打包，先看印出的路徑與 `errors.log`。
- exit code `2`：先不要重跑覆蓋判讀脈絡，先保留 `.tar.gz`，再看 `errors.log` 決定是否針對失敗 node 補跑。

## 安全界線

- 這套工具以 operationally read-only 收集為原則：不修改 persistent config、service、package、mount、Ceph cluster 或 Kubernetes workload。它只在 collector 自己的遠端暫存目錄組裝輸出，結束後清除；SSH/sudo/audit log 的自然增長是觀測行為不可避免的副作用。
- `/var/log` 來源以 GNU `dd iflag=noatime,nofollow`（必要時 `sudo -n`）讀取；不支援 no-atime/no-follow read 時回傳 partial，不退回會更新 atime 或跟隨 race-time symlink 的一般讀法。目錄列舉仍由 `find` 完成，在少見的 `strictatime` filesystem 上可能更新目錄 atime；因此保證是「不改 operational/config state」，不是宣稱遠端磁碟每個 metadata bit 都不變。
- 不會自動安裝缺少的解壓工具、不會修改來源權限，也不會在來源目錄原地解壓。
- 遮蔽（redaction）預設開啟，涵蓋：含 `password`/`secret`/`token`/`keyring`/`private key` 的文字行、Ceph 金鑰材料（`key = AQB..==` 與 base64 區塊）、整段多行 PEM private key block；並會把 `*.gz` 解壓後遮蔽再壓回。但這**不是完整 DLP**。若使用 `--no-redact`，bundle 會保留原始內容。
- `verify-bundle.sh` 會以**檔名**（keyring/.ssh/id_ed25519/private_key/*.pem/*.key/*.crt）與**內容**（殘留的 PRIVATE KEY block / `key = <base64>`）兩道把關，但仍不能保證內容完全沒有敏感資料。
- 分享 bundle 前仍應自行檢查是否包含內部 IP、hostname、路徑、帳號名稱或其他敏感資料。

## Lab 驗證（multi-fault）

2026-06-30 在真 cephadm v19.2.3 叢集（3 mon + 9 OSD、pool `.mgr` size 3）跑過多故障矩陣，破壞性情境皆先 `ok-to-stop` / 確認 quorum 後注入並立即回退，最後 HEALTH_OK：

| 情境 | 注入 | bundle | exit |
|---|---|---|---|
| 健康基準 | 無 | VERIFY PASS、6/6 node、312 行遮蔽 | 0 |
| OSD down | 停 osd.0 | 收到 `OSD_DOWN`（text+json）| 0 |
| MON 少一台 | 停 mon-02（quorum 在）| 收到 `MON_DOWN`（out of quorum）| 0 |
| node 不可達 | inventory 加假 host | 該 node `SKIPPED.txt`、其餘照收、errors.log 有記 | 2 |
| seed 不可達 | `--seed` 指死 host | cluster collector 失敗、6 node 仍收 | 2 |

這些結果是從母 repo 移植時保留的真機驗證紀錄；Prometheus dump 的可稽核細節見
`PROM-VALIDATION-2026-07.md`。重新執行前，請依你的叢集 topology 與 inventory
重新確認結果，不要把這些日期化數字當成目前叢集狀態。

- 已知 optional/read-only 非零紀錄：各 node 的 LVM 查詢（`pvs` / `vgs` / `lvs`）、`docker ps -a`、node-level `sudo cephadm ls --format json-pretty` 可能回非零；artifact 與 node 內部 `errors.log` 會保留原始輸出，整體 bundle 仍驗證通過。
