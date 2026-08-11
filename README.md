# Ceph Incident Bundle

`ceph-incident-bundle` 是 operationally read-only 的事故證據收集器。它從工作機經
SSH 收集 Ceph node、Ceph、Rook、Prometheus 與 `/var/log` 證據，產生可獨立驗證的
`.tar.gz` bundle；它不會修復叢集、變更設定、重啟服務或建立 Kubernetes workload。

Python 3.11 以上是唯一 production runtime。Shell reference、相容 wrapper 與
`/etc/hosts` 轉 inventory 的 shell helper 已在完成 offline 與 real-lab qualification
後移除。

## 最短操作流程

在 repo root 執行：

```bash
python3.11 ceph_incident_bundle.py collect \
  --inventory inventory/ceph-lab.example.env \
  --ssh-key .ssh/id_ed25519 \
  --seed ikaros@192.168.18.166 \
  --mode cephadm \
  --since 24h
```

成功後 stdout 只有 bundle 路徑：

```text
bundle: results/ceph-incident-YYYYMMDDTHHMMSSZ.tar.gz
```

驗證 bundle：

```bash
python3.11 ceph_incident_bundle.py verify \
  results/ceph-incident-YYYYMMDDTHHMMSSZ.tar.gz
```

## Inventory

Inventory 是宣告式資料，不會被當成 shell 執行：

```bash
SSH_USER="ikaros"
SEED_HOST="192.168.18.166"
ROOK_NAMESPACE="rook-ceph"
ROOK_OPERATOR_NAMESPACE="rook-ceph"
HOSTS=(
  "monitor01=192.168.18.166"
  "mon02=192.168.18.167"
  "osd01=192.168.18.169"
)
```

- `SSH_USER`：登入每台 node 的 Linux 帳號。
- `SEED_HOST`：選填；指定 cluster-level Ceph query 的 seed。
- `ROOK_NAMESPACE`／`ROOK_OPERATOR_NAMESPACE`：Rook namespaces。
- `HOSTS`：`alias=host`；alias 會成為 bundle 的 `nodes/<alias>/`。

只接受 quoted scalar 與 `HOSTS` array。Command substitution、額外 shell statement、
不安全 alias/target 都會在 SSH 或 output write 前被拒絕。原本的
`run/hosts-to-inventory.sh` 是 shell-only convenience surface，不屬於 collect contract；
cutover 選擇移除它而不增加第三個 production CLI。Inventory 語言本身沒有改變。

## 收集模式

`--mode auto`（預設）會逐台以 read-only probe 找可用來源：

- Ceph：直接 `ceph`，必要時 `sudo -n ceph`。
- Rook：`--kube-mode remote` 透過 SSH 使用 node 上的 `kubectl`，或
  `--kube-mode local` 使用工作機的 `kubectl`；可配 `--kube-context`。
- Node：每個 inventory node 都會收集。
- Prometheus：只有給 `--prom-url` 才使用 HTTP GET 收集。

Python implementation 沒有 `cephadm shell` 或 `kubectl exec` opt-in；這兩條會建立
額外 runtime process 的路徑已在 cutover 前撤掉，不能作為 fallback。

常用範例：

```bash
python3.11 ceph_incident_bundle.py collect \
  --inventory inventory/external.env \
  --ssh-key ~/.ssh/id_ed25519 \
  --mode auto \
  --kube-mode local \
  --kube-context my-cluster \
  --prom-url http://192.168.18.166:9095 \
  --since 24h
```

完整 collect 介面：

```text
ceph_incident_bundle.py collect --inventory PATH --ssh-key PATH
  [--seed USER@HOST] [--out DIR] [--mode auto|cephadm|rook]
  [--since N[smhdw]] [--timeout SECONDS] [--node-timeout SECONDS]
  [--skip-logs] [--keep-original-logs]
  [--var-log-max-bytes BYTES|unlimited]
  [--trust-ssh-host-key|--no-trust-ssh-host-key]
  [--kube-mode local|remote] [--kube-context CTX]
  [--prom-url URL] [--prom-job-regex RE] [--prom-step SECONDS]
  [--prom-timeout SECONDS] [--redact|--no-redact]
  [--quiet] [--keep-workdir]
```

## Evidence Window 與 `/var/log`

- `--timeout`（預設 20s）界定單一 command／SSH；`--node-timeout`（預設 600s）
  界定單一 node 的整輪收集。
- `--since` 對 `/var/log` 必須是 `N`、`Ns`、`Nm`、`Nh`、`Nd` 或 `Nw`。
- 會收窗口內檔案，加上每個 rotated family 跨過窗口起點的最新一份；排除項仍寫入
  `INDEX.tsv`，避免把「存在但在窗口外」誤讀成「不存在」。
- Regular files 以 no-atime/no-follow 路徑讀取；無法證明安全時記 partial，不退回一般
  `open`／`cat`。
- `.gz`、`.xz`、`.bz2`、`.zst` 可合併；opaque/binary evidence 留在 `raw/`。
- 預設每台 log payload 上限 10 GiB；超限時保留 bounded index/原因並 exit 2，不產生
  看似完整的半套 evidence。

## Output 與 exit code

主要成員：

- `README-FIRST.txt`、`CONTENTS.md`、`summary.txt`、`environment.txt`
- `manifest.jsonl`、`errors.log`、`redactions.log`
- `cluster/{ceph,rook,prometheus}/`
- `nodes/<alias>/`

Exit code：

- `0`：收集完成，沒有已知 collector failure。
- `2`：partial；bundle 仍產生，先讀 `errors.log` 與 `summary.txt`。
- `1`：usage/input/verification failure。驗證失敗時保留 owned workdir 供調查。

進度寫 stderr；stdout 保持只有 `bundle:`。`--quiet` 可停用正常進度訊息。

## 安全界線

- 只允許 collector-owned local/remote workspace 與 final bundle writes。
- 不修改 persistent configuration、service、package、mount、Ceph desired state 或
  Kubernetes object/workload。
- Node Evidence Archive 在 extraction 前完整驗證 gzip/tar EOF、payload cap、member
  name/type、collision、manifest mapping；拒絕 traversal、absolute path、links、device、
  FIFO、socket 與任何 workspace 外 write。
- SSH host key 預設 accept-new 只寫本次 workdir；已知 key mismatch 仍拒絕。
- Redaction 預設開啟，但不是完整 DLP；分享前仍需人工檢查內部 IP、hostname、帳號與
  其他敏感資訊。

完整契約見 [`docs/read-only-safety.md`](docs/read-only-safety.md) 與
[`docs/behavior-contract.md`](docs/behavior-contract.md)。

## 驗證

離線 gate 要用絕對路徑明確提供彼此獨立的 production 與 tooling interpreter；production
gate 要求 exact CPython 3.10.x，tooling gate 為 Python 3.11+。入口會先
解析並列出兩者的 executable、
implementation 與 structured version，再依序執行 production test gate 和既有完整
suite。Repository 不會下載或安裝 runtime／package，也不會修改 system Python、global
site-packages、shell 或 version-manager default：

```bash
make validate \
  PRODUCTION_PYTHON=/absolute/path/to/production-python \
  TOOLING_PYTHON=/absolute/path/to/tooling-python \
  TEST_JOBS=8
```

post-cutover real-lab gate 不再執行 shell。它先驗證保存的 #21 PASS report 與 shell
baseline bundle/hash，再對同一 active Lab Profile 執行一次 Python 四路 full collect，
比較 normalized contract，並檢查 stable state、workstation cleanup 與 remote residue：

```bash
make validate-lab \
  LAB_PROFILE=/absolute/path/to/lab.toml \
  LAB_BASELINE_REPORT=/absolute/path/to/20260805T155047Z/report.json \
  PRODUCTION_PYTHON=/absolute/path/to/cpython3.10 \
  TOOLING_PYTHON=/absolute/path/to/python3.11 \
  CEPH_INCIDENT_LAB_CONFIRM=1
```

Harness 由 tooling interpreter 執行；workstation `collect`／`verify` 只由 production
interpreter 執行。只有 schema-v3 final `report.md`／`report.json` 的 `status: pass`，連同
每台 node 的 pre/post runtime facts 與 CPython 3.10 witness，才是 3.10 qualification
proof。既有 schema-v2 PASS 保留原本 post-cutover proof 的意義，但不能被當成 3.10
proof。詳細流程見 [`docs/lab-validation-runbook.md`](docs/lab-validation-runbook.md)。
