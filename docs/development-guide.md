# 開發說明

## 本機開發需求與取得原始碼

開發環境使用 Linux 或另一個具備 POSIX file protection 的平台。Production runtime 的最低版本是 CPython 3.10；可以使用較新的 CPython 執行日常 unit test，但 release validation 必須明確選用 CPython 3.10.x。

需要的本機工具如下：

- Git。
- CPython 3.10+，包含 `venv` 與 `pip`。
- `make`，用於執行 repository 提供的 test target。
- 能離線建置 wheel 的 `setuptools>=61`。Production package 沒有 third-party runtime dependency。

取得原始碼：

```bash
git clone https://github.com/tom19960222/ceph-incident-bundle.git
cd ceph-incident-bundle
```

正式 product code 位於 `src/ceph_incident_bundle/`。`docs/python-*` 是 Python rewrite、cutover 和 qualification 的歷史紀錄，除非工作項目明確調查那段歷史，否則不要把它們當成目前需求。現行 domain language 以 [`CONTEXT.md`](../CONTEXT.md) 為準；collection safety boundary 以 [`read-only-safety.md`](read-only-safety.md)、相關 ADR 與 source code 為準。

## 建立 Python 開發環境

在 repository root 建立獨立 virtual environment，不要修改 system Python：

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/ceph-incident-bundle --help
```

Help output 應只列出 `generate-inventory` 與 `collect`。Editable install 適合本機反覆修改；production-style 安裝則使用：

```bash
.venv/bin/python -m pip install .
```

Unit test 本身透過 `PYTHONPATH` 直接載入 `src`，不要求先安裝 package。若目前環境無法執行 editable build，仍可先跑快速測試：

```bash
make test PYTHON=.venv/bin/python
```

## Repository 目錄與測試配置

主要路徑如下：

| 路徑 | 用途 |
| --- | --- |
| `src/ceph_incident_bundle/` | CLI、Inventory 與 Remote Node Collector |
| `src/ceph_incident_bundle/collect/` | workstation-side collection、archive admission 與 publication |
| `tests/python/` | component、CLI 與 installed artifact test |
| `inventory/example.ini` | 完整 Inventory 格式範例 |
| `validation/run_offline.py` | clean source、wheel、isolated install 與完整 test orchestration |
| `CONTEXT.md` | canonical domain terminology |
| `docs/adr/` | 現行架構決策與邊界理由 |
| `results/` | 可供本機 collection output 使用的既有目錄 |

Test suite 使用 standard-library `unittest`。`make test` 執行快速 component set：bundle、collect、inventory、Kubernetes、node archive、Prometheus 與 remote collector。它不取代 installed CLI validation。

單獨執行一個 test module：

```bash
PYTHONPATH=src:tests/python PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest -v test_inventory
```

執行 repository 中所有可在本機直接執行的 Python test。`test_cli.py` 需要 installed command path；未設定 `CEPH_INCIDENT_BUNDLE_COMMAND` 時整組 skip，canonical `make validate` 會在 installed environment 提供這個值：

```bash
PYTHONPATH=src:tests/python PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests/python -v
```

## 日常修改、執行與測試流程

每次修改建議依下列順序進行：

1. 先確認 observable behavior、風險層級、最多三項 acceptance criteria 與 non-goal。例如：「invalid Inventory 不建立 workspace」是 acceptance criterion；「不改寫其他 CLI error」是 non-goal。
2. 從 [`architecture.md`](architecture.md) 的功能修改位置對照找到負責模組與對應 test。
3. 若修改 `remote_collector.py`，先閱讀 [`read-only-safety.md`](read-only-safety.md)。若修改 `collect/` 下的 archive admission 或 publication，也要閱讀該目錄的 `AGENTS.md` 與相關安全決策。
4. 先執行最接近修改點的 test，確認 baseline。
5. 在最窄的責任模組完成修改，不加入 compatibility layer、unrelated refactor 或 threat model 之外的 hardening。
6. 重跑 focused test，再執行 `make test`。
7. 準備 merge 或 release 前，使用 CPython 3.10.x 執行 `make validate`。

安裝 editable package 後，可以直接檢查 CLI 行為：

```bash
.venv/bin/ceph-incident-bundle --help
.venv/bin/ceph-incident-bundle generate-inventory --help
.venv/bin/ceph-incident-bundle collect --help
```

不要用 real Ceph、Kubernetes 或 Prometheus 環境代替 unit test。Real-lab validation 是另一個需要明確 opt-in 的流程；一般開發工作不應讀取 credential payload，也不應把 lab identity 或 credential 寫入 Git。

## 完整驗證與 CPython 3.10 相容性

Canonical validation command 是：

```bash
make validate PYTHON=/absolute/path/to/cpython3.10
```

`validation/run_offline.py` 會先拒絕非 CPython 3.10.x interpreter，再完成以下步驟：

1. 複製一份排除 `.git`、cache、virtual environment、result 與本機 artifact 的 clean source。
2. 使用 `--no-index --no-build-isolation --no-deps` 建置 wheel，避免下載 dependency。
3. 建立隔離 virtual environment 並安裝剛產生的 wheel。
4. 確認 wheel 安裝後產生 `ceph-incident-bundle` console command。
5. 以該 installed command 為對象，在 installed artifact 環境執行完整 Python test suite；其中包含 public CLI surface 與 wheel metadata 驗證。

Production code 不得使用排除 CPython 3.10 的 syntax 或 standard-library API。`pyproject.toml` 的 `Requires-Python` 必須維持 `>=3.10`，wheel metadata 不應出現 runtime dependency，installed help 只能暴露兩個公開 subcommand。

## 常見開發問題與修改前檢查事項

- **`make validate` 立即拒絕 interpreter**：確認 `PYTHON` 指向 CPython 3.10.x 的絕對路徑，不是 3.11+、PyPy 或 virtual environment 中的其他版本。
- **Offline wheel build 缺少 build backend**：先在預先準備的 validation interpreter 確認 `setuptools>=61` 可用；canonical validation 不會從 network 安裝套件。
- **修改 Inventory 後 CLI test 失敗**：同步檢查 `inventory.py` 的 parsing/default/validation、`generate_inventory.py` 的 draft behavior、`inventory/example.ini` 與 `test_inventory.py`／`test_cli.py`。
- **修改 Probe catalog**：catalog 是 product behavior。同步檢查 capture schema、read-only argument vector、Python 3.10 compatibility 與 `test_remote_collector.py` 的獨立 expected catalog。
- **修改 archive 或 output layout**：先確認這是否是公開承諾。Internal filename、metadata representation 與 member ordering 預設不是 fixed interface，但 structural safety、required evidence roots、no-overwrite publication 和已准入 evidence preservation 不能被破壞。
- **新增 dependency 或 command**：Production package 目前只有 standard library。新增 dependency、shell interpolation、fallback collector 或 arbitrary command surface 都超出既有產品邊界，不能當成順手改善。
- **test 留下本機檔案**：測試與 validation 應使用 temporary directory；不要提交 `.venv`、`inventory.ini`、Bundle、SSH material 或 `results/` 中的收集內容。
