# Ceph Incident Bundle

`ceph-incident-bundle` 從明確列在 Node Inventory 的主機收集事故當下的 Raw Evidence，
並在工作機產生一個 `.tar.gz` Incident Bundle。正式產品只有 Python 3.10+ 的
`ceph-incident-bundle` console command；公開 subcommand 只有
`generate-inventory` 與 `collect`。

收集器不修復叢集，也不修改 persistent configuration、service、package、mount、
Ceph desired state 或 Kubernetes object/workload。它會建立並清除本次 invocation
擁有的暫存 workspace；SSH、API 與檔案讀取仍可能自然留下 audit/access record。

## 安裝

用操作人員自己管理的 virtual environment 安裝，不要改系統 Python：

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ceph-incident-bundle --help
```

Production package 只使用 Python 3.10 standard library。安裝後不會提供第二個
collector、verifier、direct-on-node command 或相容入口。

## 最短流程

先產生草稿：

```bash
ceph-incident-bundle generate-inventory
```

這會讀 `/etc/hosts` 並寫出 `inventory.ini`。開始收集前一定要人工確認 scope、SSH
address、Ceph source、Kubernetes context 與 Prometheus URL。Repository 內的
[`inventory/example.ini`](inventory/example.ini) 是完整格式範例。

確認後執行：

```bash
ceph-incident-bundle collect
```

常用 override：

```bash
ceph-incident-bundle collect \
  --inventory /absolute/path/to/inventory.ini \
  --since 24h \
  --output-dir /absolute/path/to/results
```

SSH key、port、jump host、known hosts 等連線設定交給 system OpenSSH config 管理。
Collector 使用 `BatchMode=yes`、不開 pseudo-terminal，也不會自動接受新 host key。

## Inventory

Inventory 是 INI，不是可執行 shell。`[common]` 與非空的 `[nodes]` 必填；每個 node 都以
固定的 `root@ssh_address` 連線。`[ceph] source` 必須指向一個已列出的 inventory name。
Kubernetes 沒有明確 `context` 就完全不收；Prometheus 沒有明確 `url` 也完全不連線。

`inventory_name` 會直接成為 bundle 裡 `nodes/<inventory_name>/` 的目錄名稱；
`ssh_address` 只負責連線。Ceph、Kubernetes 與 Prometheus observation 不會偷偷擴張
SSH target 清單。

## 收集結果

成功交付時 stdout 只有一行 bundle path 與 `(complete)` 或 `(partial)`。兩種已交付
結果都是 exit 0；`partial` 表示至少一個實際嘗試的 evidence source、cleanup 或 Probe
失敗，但已完成的 evidence 仍被保留。Startup rejection 或工作機錯誤導致沒有 bundle
時，stdout 為空、exit 非 0，stderr 最後一行是：

```text
FAIL: no Incident Bundle delivered
```

Ctrl-C 不交付 bundle，完成可做的 cleanup 後 exit 130。

Bundle 根目錄固定包含：

```text
inventory.ini
collection.json
nodes/
ceph/
kubernetes/
prometheus/
```

每個 Probe Capture 分開保存 `stdout`、`stderr` 與 `result.json`。Prometheus Capture
保存原始 `response` 與 `result.json`。Node Evidence Archive 在任何 extraction 前都會
完整檢查 member；absolute/traversal path、link、special object、collision 或不完整結構
會讓整份 node archive fail closed。

## Raw Evidence 與安全界線

Collector 不遮蔽、不掃描 secret、不做內容驗證，也不宣稱 bundle 適合分享。
`/etc/ceph` 的一般檔案包含 keyring 在內都可能進入 bundle，Prometheus URL 的 credentials
也不會被特別處理。對外提供前由操作人員自行依組織政策保管與審查。

Remote Node Collector 只使用固定的 `python3 -`；缺少 CPython 3.10+ 時該 node 被略過，
不安裝 runtime、不切換 interpreter，也不回退到其他 collector。Ceph 只跑 direct
`ceph`；Kubernetes 只在工作機跑固定的 `kubectl get` 與 `kubectl logs`；不會啟動
container 或進入 workload。

## 常見問題

- Inventory 被拒絕：一次修完 stderr 列出的 unknown key、duplicate、duration、regex、
  unresolved reference 或 portable name collision；收集尚未開始。
- Node 被略過：先檢查 system OpenSSH 設定、`root@ssh_address`、host key 與該 node 的
  fixed `python3` 是否為 CPython 3.10+。
- 結果是 `partial`：先保留 bundle 與 stderr，再針對被點名的 source 處理；不要把
  `partial` 誤當成沒有交付。
- Output 已存在：換 output directory 或等待下一個 UTC second；collector 不覆寫既有
  destination。

## 開發驗證與 rollback

日常修改先用目前的 Python 直接跑 component test suite；installed CLI 與 wheel surface
留給 `make validate`：

```bash
make test
```

Pre-merge 或 release validation 會從 clean source build wheel、安裝到隔離環境，再從
installed console command 跑完整 Python suite。請明確選預先準備好的 CPython 3.10.x：

```bash
make validate PYTHON=/absolute/path/to/cpython3.10
```

驗證不連 real lab、不下載 dependency，也不改全域 Python。Real-lab acceptance 是另一個
明確授權流程。

需要退回舊版本時，使用 Git history、release tag 或先前發布的 artifact；repository
不保留舊 runtime 或相容 wrapper。
