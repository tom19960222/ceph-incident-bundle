# Shell 測試情境清單（Python unittest 移植依據）

> 對應 issue #7（research，map issue #3）。
> 目的：把 `tests/` 下 8 個 shell 測試檔的所有測試情境整理成 checklist，作為移植到
> Python unittest（Python 3.11、純標準庫、沿用「假指令塞 PATH」手法）的依據。

## 分類定義

- **【功能等價-必移植】**：斷言的是對外可觀察行為 —— exit code、bundle 目錄結構、
  artifact 內容、manifest 格式、SKIPPED / OVER-LIMIT 語意、安全防護（redaction、
  拒絕危險輸入）等。移植後的 Python 實作必須保留這些行為，測試必須重寫。
- **【實作細節-不移植】**：斷言的是 bash / shell 實作本身 —— shell-native helper、
  `set -e` 狀態、`--` 呼叫慣例、對 python3 的依賴檢查、grep 措辭等。移植到 Python
  後這些斷言失去意義（或由 Python 執行期天然保證），不需要移植。

註：所有「檔案中含有某字串」的斷言，移植時應視為**語意**斷言（例如 SKIPPED 檔要說明
原因、errors.log 要記到該次失敗），不必逐字複製 shell 版的英文措辭；下表凡是分類為
必移植者，皆以此原則理解。

## 總覽

| 檔案 | 情境數 | 必移植 | 不移植 |
|---|---|---|---|
| `tests/run-tests.sh` | 7 | 4 | 3 |
| `tests/test-common.sh` | 23 | 18 | 5 |
| `tests/test-cephadm-collector.sh` | 5 | 5 | 0 |
| `tests/test-node-collector.sh` | 13 | 13 | 0 |
| `tests/test-var-log-collector.sh` | 14 | 14 | 0 |
| `tests/test-rook-collector.sh` | 10 | 10 | 0 |
| `tests/test-prom-collector.sh` | 19 | 17 | 2 |
| `tests/test-verify-bundle.sh` | 11 | 11 | 0 |
| `tests/test-collect.sh` | 36 | 36 | 0 |
| **合計** | **138** | **128** | **10** |

> 原始盤點（issue #7）為 137／127／10；#15 之後補入 `P6a`（Prometheus 憑證邊界），
> 因此實際列數為 138／128／10。逐項覆蓋狀態見 `docs/test-scenario-ledger.md`，
> 該對照由 `tests/test_python_scenario_ledger.py` 機械檢查，總數不一致會失敗。

> 本清單的範圍是 collect／verify 這條 observable contract 的 shell 測試。
> `tests/run-tests.sh` 另外會跑 `tests/test-hosts-to-inventory.sh`（#47），它測的是
> 工作機端的輔助工具 `run/hosts-to-inventory.sh`——產生 inventory 的離線轉換器，
> 不參與 collect 的 observable contract，也不在 differential gate 的比較面內，
> 因此刻意不列入上表、ledger 與 audit。這是範圍，不是漏記。

---

## 1. `tests/run-tests.sh`（總入口）

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| R1 | 所有實作與測試檔存在於預期路徑 | 無 | 20–39 | 【實作細節-不移植】repo 佈局檢查，Python 化後由 import / 打包取代 |
| R2 | `run/collect.sh`、`lib/verify-bundle.sh` 與 shell compatibility fixture exporter 具可執行位 | 無 | 41–43 | 【實作細節-不移植】打包細節（入口點形式會改變） |
| R3 | `collect.sh` 無參數 → exit 1 且輸出含 `Usage:` | `run_and_capture`（捕捉 status+輸出） | 45–49 | 【功能等價-必移植】CLI 契約 |
| R4 | `verify-bundle.sh` 無參數 → exit 1 且輸出含 `Usage:` | 同上 | 51–55 | 【功能等價-必移植】 |
| R5 | `verify-bundle.sh` 指到不存在路徑 → 非 0，輸出說明失敗（`VERIFY FAIL:`/`Usage:`/`error` 擇一） | 同上 | 57–61 | 【功能等價-必移植】 |
| R6 | `collect.sh` 帶不存在的 inventory → 非 0，輸出說明（`missing inventory` 等） | 同上 | 63–67 | 【功能等價-必移植】 |
| R7 | 依序執行 8 個子測試檔並要求 exit 0 | 無 | 69–109 | 【實作細節-不移植】測試 harness 本身，由 `tests/run-python-tests.sh` 取代 |

## 2. `tests/test-common.sh`（common.sh / bundle.sh helpers）

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| C1 | `json_escape` 正確跳脫 `"` 與 `\` | 無 | 19–23 | 【實作細節-不移植】Python 用 `json` 模組，helper 不存在 |
| C2 | `json_escape` 不呼叫 python3（shell-native） | 假 `python3`（exit 99）塞 PATH | 25–38 | 【實作細節-不移植】純 shell 約束 |
| C3 | `manifest_add` 寫出一行 JSONL，欄位 host/collector/artifact/command/exit_code/started/ended 齊全且型別正確（exit_code 為數字） | 以 python3 解析驗證 | 40–77 | 【功能等價-必移植】manifest 是 bundle 對外格式 |
| C4 | `manifest_add` 拒絕非數字 exit_code（非 0 退出並說明 exit_code 問題） | 子 shell 執行捕捉 rc | 79–109 | 【功能等價-必移植】manifest 完整性 |
| C5 | `redact_file`：Password/SECRET/token/keyring/private_key 行整行換成 `[REDACTED]`，安全行不動，redaction log 非空且提及檔名 | 建構固定內容檔 | 111–133 | 【功能等價-必移植】 |
| C6 | `redact_file`：`-----BEGIN ... PRIVATE KEY-----`、`private-key:`、`PRIVATE KEY` 變體皆被遮蔽 | 同上 | 135–152 | 【功能等價-必移植】 |
| C7 | `redact_file`：多行 PEM 本體（含 base64 行與 END 行）整段遮蔽，前後安全行保留 | 同上 | 154–174 | 【功能等價-必移植】 |
| C8 | `redact_file`：Ceph key 素材（`key = AQB...==`、`"auth_key": "AQB..."`）遮蔽；一般句子不過度遮蔽 | 同上 | 176–193 | 【功能等價-必移植】 |
| C9 | `redact_file` 保留原檔權限（640） | `chmod` + `stat` | 195–206 | 【功能等價-必移植】 |
| C10 | `redact_gz_file`：gzip 檔解壓-遮蔽-重壓，正常內容保留、秘密不外洩、mode 保留 | `gzip` 產生素材 | 208–228 | 【功能等價-必移植】 |
| C11 | `redact_compressed_file` 支援 xz / bz2 / zst 三種 codec | 對應壓縮工具產生素材 | 230–251 | 【功能等價-必移植】 |
| C12 | 重壓縮失敗時回非 0、原壓縮檔內容/mode 原封不動（不破壞原 artifact） | 假 `gzip`（`-dc` 轉呼叫真 gzip、壓縮時 exit 9；靠 `REAL_GZIP` env）塞 PATH | 253–285 | 【功能等價-必移植】 |
| C13 | `redact_bundle_text`：早期壓縮檔遮蔽失敗 → 整體回 2，但**繼續**遮蔽後面的檔案，且 `redactions.log` 留下 `NOT redacted` 警告 | 同 C12 的假 gzip | 287–313 | 【功能等價-必移植】 |
| C14 | `redact_bundle_text`：`merged/` 純文字要遮蔽；`raw/` 下 opaque tar.gz 位元不動（hash 相同、tar 仍可讀） | tar 產生 raw 素材 | 315–334 | 【功能等價-必移植】 |
| C15 | `enforce_node_log_caps`：遮蔽後超過 cap → 回 2、丟棄 merged payload、寫 `OVER-LIMIT.txt`（含 post-redaction 標記） | 手工佈局 bundle 目錄 + `PAYLOAD-BYTES.txt` | 336–352 | 【功能等價-必移植】 |
| C16 | `progress`：預設輸出訊息；`CEPH_INCIDENT_QUIET=1` 時完全靜默 | env 變數 | 354–360 | 【功能等價-必移植】 |
| C17 | `progress` 只寫 stderr、不污染 stdout | fd 重導 | 362–366 | 【功能等價-必移植】stdout 純淨性契約 |
| C18 | `run_capture` 成功路徑：artifact 首行 `# host: ...` 標頭、含指令輸出；manifest 一行、artifact 路徑與 exit_code=0 正確 | python3 驗證 manifest | 368–390 | 【功能等價-必移植】 |
| C19 | `run_capture` 失敗路徑：回傳指令的非 0 碼（7）、輸出仍寫入 artifact、`ERROR_LOG` 記 `exit=7`、manifest 記 exit_code=7 | `ERROR_LOG` env | 392–410 | 【功能等價-必移植】失敗仍留證據 |
| C20 | `run_capture` 缺 `--` 分隔符 → 致命錯誤並說明 | 子 shell | 412–433 | 【實作細節-不移植】shell 呼叫慣例；Python API 天然無此問題 |
| C21 | `run_capture` 以預設 20s timeout 包住指令，artifact 標頭記 `# timeout: 20s` | 假 `timeout`（記 argv 到 `TIMEOUT_LOG` 後透傳）塞 PATH | 435–454 | 【功能等價-必移植】timeout 語意（機制改用 `subprocess` timeout） |
| C22 | artifact 檔名以 `-` 開頭仍可正確建立 | 切換 cwd 執行 | 456–468 | 【實作細節-不移植】shell 重導/`--` 陷阱；Python `open()` 無此問題 |
| C23 | `run_capture` 不改變呼叫端 errexit 狀態 | `set +e` 前後驗證 | 470–483 | 【實作細節-不移植】純 bash 語意 |

## 3. `tests/test-cephadm-collector.sh`（collect-cluster-cephadm.sh）

全部透過 `tests/fixtures/bin/ssh` 假指令（見專章）。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| CA1 | Happy path：產出 json/text 全套 artifact（status、health-detail、osd-tree、orch-ps、crash-ls…）；crash info **上限 10 筆**；crash id 消毒（`crash/02`→`crash_02`）且與 `crash:02` 消毒後同名時加 `-2` 防碰撞；manifest 34 行；ssh log 含 `sudo -n cephadm shell -- ceph ...` 且帶 `ConnectTimeout=30`、`ServerAliveInterval=30` | `FAKE_SSH_LOG`；fixture 回 12 筆 crash id | 51–88 | 【功能等價-必移植】（manifest 行數=收集命令數，移植時同步為新常數） |
| CA2 | 單一指令失敗（osd perf）→ 整體回 2 但**繼續收集**後續 artifact；失敗輸出仍寫入 artifact；manifest 記 `exit_code:17` | `FAKE_SSH_FAIL_ON="osd perf"`（fixture 對匹配指令 exit 17） | 90–110 | 【功能等價-必移植】partial-failure 語意 |
| CA3 | `crash ls` 回非 JSON → 寫 `crash-info-skip.txt`（含 SKIPPED）、不建 `crash-info/` 目錄、不再對 crash id 發 ssh | `FAKE_SSH_CRASH_LS_BROKEN=1` | 112–128 | 【功能等價-必移植】 |
| CA4 | runner=`direct`：遠端跑純 `ceph ...`，**不得**出現 `cephadm shell` 或 `sudo` | `FAKE_SSH_LOG` 檢查 argv | 130–148 | 【功能等價-必移植】 |
| CA5 | runner=`sudo`：遠端跑 `sudo -n ceph ...`，不得出現 `cephadm shell` | 同上 | 150–164 | 【功能等價-必移植】 |

## 4. `tests/test-node-collector.sh`（collect-node.sh）

線性腳本。fixture：大量 inline 假指令塞 PATH（`sudo`+`FAKE_SUDO_LOG`、`journalctl`+
`FAKE_JOURNALCTL_NO_CEPH`/`FAKE_JOURNALCTL_LARGE`、`timedatectl`/`systemctl`+
`FAKE_TIMESYNCD_MISSING`、`cephadm`、`podman`、`docker`(exit 1)、`timeout`(透傳)、
`dmesg`、`hostname`、`uname`、`uptime`、`free`、`df`、`lsblk`、`ip`，以及 optional 工具
`iostat`/`chronyc`/`pvs`/`vgs`/`lvs` 記 argv 到 `FAKE_OPTIONAL_LOG`；`ntpq` 故意移除）；
資料目錄用 `CEPH_INCIDENT_VAR_LOG_DIR`、`CEPH_INCIDENT_VAR_LIB_CEPH_DIR`、
`CEPH_INCIDENT_TIMESYNCD_CONF{,_D_DIR}`、`CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1` 注入。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| N1 | Happy path 全套執行 → exit 0（失敗時傾印 errors.log 供除錯） | 上述全套假 bin | 202–219 | 【功能等價-必移植】 |
| N2 | 產出固定 artifact 清單：system/、resources/、storage/、network/、kernel/、time/（timedatectl 三連發＋timesyncd status/journal）、systemd/failed-units、cephadm/cephadm-ls.json、logs/var-log/INDEX.tsv、journal-all-since.txt | 同上 | 221–240 | 【功能等價-必移植】bundle 結構契約 |
| N3 | 各 artifact 內容來自對應指令輸出（cephadm-ls 的 `"style":"cephadm"`、dmesg、timedatectl 各子命令、docker-ps、INDEX.tsv 含 ceph.log…） | 假 bin 的固定輸出 | 242–250 | 【功能等價-必移植】 |
| N4 | optional 指令不存在（ntpq）→ artifact 寫 `SKIPPED: command not found: ntpq` | 刻意不建 `ntpq` | 251 | 【功能等價-必移植】SKIPPED 語意 |
| N5 | timesyncd 設定檔與 conf.d 逐檔複製進 `time/systemd-timesyncd-config/` | env 指到假 conf | 252–253 | 【功能等價-必移植】 |
| N6 | log family 合併：`ceph.log.2.gz`＋`.1`＋現行檔合併為 `.merged`（壓縮內容也在內）；osd log family 也合併；超大檔整檔收集（不截斷） | 假 var-log 目錄 | 255–260 | 【功能等價-必移植】 |
| N7 | `var-lib-ceph-configs/` 複製 config、**排除 keyring**；listing 檔也不得出現 keyring | 假 var-lib 目錄 | 262–266 | 【功能等價-必移植】安全行為 |
| N8 | optional 工具收到正確 argv（`iostat -xz 1 3`、`pvs/vgs/lvs --noheadings --separator ' '`） | `FAKE_OPTIONAL_LOG` | 268–271 | 【功能等價-必移植】收集參數保真 |
| N9 | dmesg 經 `sudo -n` 執行 | `FAKE_SUDO_LOG` | 273 | 【功能等價-必移植】 |
| N10 | dmesg 與 ceph journal 用加重 timeout（120s，非 `--timeout 5`），artifact 標頭記 `# timeout: 120s` | artifact 標頭 | 275–278 | 【功能等價-必移植】 |
| N11 | journal 匯出＋/var/log 共用同一 byte cap；溢出 → exit 2、`logs/var-log/OVER-LIMIT.txt`、merged/raw/original payload 全不保留 | `FAKE_JOURNALCTL_LARGE=1`＋`--var-log-max-bytes 4096` | 280–301 | 【功能等價-必移植】 |
| N12 | 非 ceph 節點沒有 ceph journal（journalctl exit 1）→ 整體仍 exit 0，`journal-ceph.txt` 留 `no entries` | `FAKE_JOURNALCTL_NO_CEPH=1`＋`--skip-logs` | 303–318 | 【功能等價-必移植】 |
| N13 | timesyncd 全缺（timedatectl/systemctl/journalctl 皆失敗、conf 不存在）→ 整體仍 exit 0；錯誤輸出留在對應 artifact；config 目錄寫 `SKIPPED.txt`（config not found） | `FAKE_TIMESYNCD_MISSING=1`＋指向不存在 conf | 320–343 | 【功能等價-必移植】 |

## 5. `tests/test-var-log-collector.sh`（collect-var-log.sh）

以 `collect_var_logs <var_log> <out> <max_bytes> <keep_originals>` 直呼函式；
全程 `CEPH_INCIDENT_TEST_ALLOW_ATIME_READ=1`。輔助斷言 `assert_before`（檔內行序）。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| V1 | 數字輪替（`.2.gz`→`.1`→現行）合併為單一 `.merged`，順序舊→新；產 `INDEX.tsv`；預設**不**保留 `original/` | gzip 素材 | 27–44 | 【功能等價-必移植】 |
| V2 | gz/xz/bz2/zst 四種 codec＋日期式輪替（`-YYYYMMDD.<ext>`）依日期舊→新合併，最後接現行檔；子目錄映射到 `merged/tree/dirs/...` | 四種壓縮工具 | 46–65 | 【功能等價-必移植】 |
| V3 | opaque 檔（zip、tar.gz、binary wtmp、journal）byte-for-byte 保留到 `raw/`、不合併；記入 `UNREDACTED-OPAQUE.txt` 警告 | 二進位素材 | 67–87 | 【功能等價-必移植】 |
| V4 | `keep_originals=1` 才保留 `original/`（原始檔與壓縮檔皆逐位保留） | 參數切換 | 89–101 | 【功能等價-必移植】 |
| V5 | 總量超過 max_bytes → 回 2、`OVER-LIMIT.txt`、merged/raw/original 全不留 | `max_bytes=5` | 103–117 | 【功能等價-必移植】 |
| V6 | 壞壓縮檔：raw 保留原檔、其他 family 照常收集、`ERRORS.tsv` 記 `decode-failed`、整體回 2 | 假 gzip 內容 | 119–135 | 【功能等價-必移植】 |
| V7 | 不追 symlink；`*.pem`/`*.key`（含壓縮、輪替變體）等敏感路徑不讀不抄；記入 `SKIPPED-sensitive.txt` | symlink 指到外部 sentinel | 137–160 | 【功能等價-必移植】安全行為 |
| V8 | 來源檔內容 / mode / mtime 收集後完全不變 | hash + `stat` 前後比對 | 162–184 | 【功能等價-必移植】唯讀保證 |
| V9 | 缺 codec（PATH 無 zstd）→ 回 2、raw 保留、`ERRORS.tsv` 記 `missing-codec:zstd` | `PATH="/usr/bin:/bin"` 縮限 | 186–200 | 【功能等價-必移植】 |
| V10 | 頂層 family 輸出檔（`app.merged`）與同名目錄（`app/`、`app.merged/`）不互相碰撞，三者皆正確產出 | 刻意命名衝突 | 202–220 | 【功能等價-必移植】 |
| V11 | 零填充數字輪替（`.010`/`.09`/`.08`）以十進位排序（不可 octal 崩潰） | 檔名素材 | 222–238 | 【功能等價-必移植】 |
| V12 | 檔案後段才出現 NUL → 視為 binary，raw 保留、不合併為文字 | 1MiB 文字＋NUL 尾 | 240–252 | 【功能等價-必移植】 |
| V13 | 第二階段解壓失敗（第一次探測成功、正式解壓失敗）→ 回 2、壓縮原檔保留在 raw、部分解碼位元組**不得**洩入 merged | 假 gzip＋呼叫計數器（第 3 次起 exit 7），`REAL_GZIP`/`GZIP_COUNTER` env | 254–289 | 【功能等價-必移植】 |
| V14 | 掃描 metadata 本身有上限：超過 → 回 2、`SCAN-LIMIT.txt`、不留 payload | `CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES=5` | 291–305 | 【功能等價-必移植】 |

## 6. `tests/test-rook-collector.sh`（collect-cluster-rook.sh）

線性腳本。fixture：inline 假 `kubectl`（`FAKE_KUBECTL_LOG` 記 argv；`FAKE_KUBECTL_MODE`
切換 present / missing-namespace / context-missing / connection-refused / with-toolbox /
op-lookup-fail；容忍前置 `--context CTX`）；inline 假 `ssh`（記 `FAKE_SSH_LOG`，把
`kubectl` 之後的參數轉呼叫本機假 kubectl）；`minimal_bin`（只有 dirname/mkdir 的 PATH）
模擬「機器上沒有 kubectl」。頂層 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC=1`。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| K1 | 顯式 rook 模式、無 kubectl → exit 2＋`cluster/rook/SKIPPED.txt`（kubectl command not found） | `PATH=minimal_bin` | 39–53 | 【功能等價-必移植】 |
| K2 | 同上但帶 `--allow-skip`（auto 模式 fallback）→ exit 0、同樣的 SKIPPED 檔 | 同上 | 56–70 | 【功能等價-必移植】skip 語意分流 |
| K3 | namespace 不存在 → exit 2；SKIPPED 檔含歸類原因（namespace not found: rook-ceph）**與** kubectl 原始錯誤輸出 | `FAKE_KUBECTL_MODE=missing-namespace` | 152–162 | 【功能等價-必移植】 |
| K4 | `--kube-context lab` 但 context 不存在 → exit 2；SKIPPED 含 `kubectl context not found: lab`＋原始錯誤 | `FAKE_KUBECTL_MODE=context-missing` | 164–180 | 【功能等價-必移植】 |
| K5 | API server 連不上 → exit 2；SKIPPED 含 cannot connect＋原始錯誤 | `FAKE_KUBECTL_MODE=connection-refused` | 182–191 | 【功能等價-必移植】 |
| K6 | 未設 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC` → toolbox 收集停用：寫 `toolbox-SKIPPED.txt`（exec disabled），且 kubectl log 中**不得**出現 `exec` | unset env＋`FAKE_KUBECTL_LOG` | 193–204 | 【功能等價-必移植】安全預設 |
| K7 | Happy path（含 toolbox）：pods-wide / events / rook-resources.yaml / operator.log / toolbox-status 各含對應輸出；有呼叫 namespace 偵測與 operator logs | `FAKE_KUBECTL_MODE=with-toolbox` | 206–217 | 【功能等價-必移植】 |
| K8 | external cluster：`--namespace rook-ceph-external --operator-namespace rook-ceph` → 資源來自 external ns、operator log 從 operator ns 收 | mode=present | 219–229 | 【功能等價-必移植】 |
| K9 | remote 模式（`--ssh-target --ssh-key --kube-context`）：kubectl 透過 ssh 在目標節點執行且帶 `--context lab`；artifact 照常產出 | 假 `ssh` 轉呼叫 kubectl；`FAKE_SSH_LOG` | 231–260 | 【功能等價-必移植】 |
| K10 | operator pod 查詢失敗不可讓收集中止（set -e 回歸）：exit 0＋`operator-SKIPPED.txt`（operator Pod not found） | `FAKE_KUBECTL_MODE=op-lookup-fail` | 262–271 | 【功能等價-必移植】 |

## 7. `tests/test-prom-collector.sh`（collect-prometheus.sh）

透過 `tests/fixtures/bin/curl` 假指令（見專章）；`run_prom` 於 subshell 中把 fakebin
塞 PATH 後直呼 `collect_prometheus`。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| P1 | duration 解析：`90/45s/30m/24h/7d/2w` 正確換算；拒絕 `yesterday`、`5x`、空字串、`0`、`000`；前導零視為十進位（`010h`→36000、`008`→8，不 octal 崩潰） | 純函式 | 42–56 | 【功能等價-必移植】CLI 輸入契約 |
| P2 | auto step：15s 下限；7d → ceil(604800/10000)=61 | 純函式 | 58–62 | 【功能等價-必移植】 |
| P3 | URL 遮蔽：`http://u:sekrit@h` → `u:***@h`；無憑證原樣 | 純函式 | 64–68 | 【功能等價-必移植】（記錄用 URL 不洩密） |
| P4 | 前置指令檢查：缺 python3 時失敗並點名 python3 | 縮限 PATH 只留 curl | 70–78 | 【實作細節-不移植】Python 化後 python3 即執行期 |
| P5 | Happy path：buildinfo.json、targets.json、dump-info.txt、各 job 目錄 index.txt＋`<metric>.json.gz`；不符 regex 的 job（grafana）不建目錄；24h → `step=15`；dump-info 記錄 window 起迄相差 86400s；manifest 4 行（buildinfo/targets/2 jobs）；environment.txt 記 `prom_url`、`prom_jobs` | fixture curl 預設回應；`FAKE_CURL_LOG` | 80–107 | 【功能等價-必移植】 |
| P6 | Prometheus 連不上 → 回 2、`SKIPPED.txt`（not reachable）、errors.log 記 skip | `FAKE_CURL_DOWN=1`（curl exit 7） | 109–116 | 【功能等價-必移植】 |
| P6a | curl 失敗診斷即使回顯含 basic-auth 的完整 URL，也不得把密碼寫入 bundle；SKIPPED 只留遮蔽 URL | `FAKE_CURL_DOWN=1`＋`FAKE_CURL_ECHO_URL_ON_ERROR=1`＋密碼含 `@` | 118–129 | 【功能等價-必移植】憑證邊界 |
| P7 | `--job-regex` 全不匹配 → 回 2；SKIPPED 說明 no scrape job matched 並列出看到的 job | 預設 jobs 回應 | 131–137 | 【功能等價-必移植】 |
| P8 | 缺 python3（進入收集後）→ 回 2、SKIPPED 點名 python3 | 縮限 PATH（留 mkdir/date/dirname/curl） | 139–157 | 【實作細節-不移植】同 P4 |
| P9 | 單一 metric query_range 失敗 → 回 2；其他 metric 照常 dump；失敗 metric **不留** .json.gz；index.txt 記 failed、errors.log 記錄 | `FAKE_CURL_FAIL_METRICS='ceph_osd_up'` | 159–168 | 【功能等價-必移植】 |
| P10 | `--budget 0` 觸發截斷 → 回 2；index.txt 記 TRUNCATED、dump-info 記 `truncated=1`、errors.log 記錄 | 參數 | 170–177 | 【功能等價-必移植】 |
| P11 | job 名含不安全字元（`"`）→ 回 2；errors.log 記 unsafe name；安全 job 照常收集 | `FAKE_CURL_JOBS_JSON` 覆寫 jobs 回應 | 179–186 | 【功能等價-必移植】路徑安全 |
| P12 | 7d window → `step=61` | `FAKE_CURL_LOG` | 188–193 | 【功能等價-必移植】 |
| P13 | redaction 排除 `cluster/prometheus/<job>/` 的 metric dump（gz 內容不動），但 dump-info.txt 仍要遮蔽；且排除規則有錨定——`nodes/.../cluster/prometheus/...` 相似路徑**仍要**遮蔽 | 手工佈局＋`redact_bundle_text` | 195–214 | 【功能等價-必移植】 |
| P14 | targets 抓取失敗 → 回 2；不留 targets.json；buildinfo 與 metric dump 照常；errors.log 記 targets fetch failed | `FAKE_CURL_FAIL_PATHS='/api/v1/targets'`（curl exit 22、先寫入 partial 再失敗） | 216–226 | 【功能等價-必移植】 |
| P15 | job 列表抓取失敗 → 回 2、SKIPPED 說 job listing failed | `FAKE_CURL_FAIL_PATHS='/api/v1/label/job/values'` | 228–234 | 【功能等價-必移植】 |
| P16 | metric 名稱列表失敗 → 回 2；index.txt 記 FAILED: metric listing、errors.log 記錄 | `FAKE_CURL_FAIL_PATHS='/api/v1/label/__name__/values'` | 236–244 | 【功能等價-必移植】 |
| P17 | `--url` 尾端斜線 → 請求 URL 不得出現 `//api` 雙斜線 | `FAKE_CURL_LOG` | 246–258 | 【功能等價-必移植】 |
| P18 | `--job-regex '-zzz'`（dash 開頭）→ 回 2 且不得把 regex 當成 grep 選項（stderr 無 `grep:`） | 捕捉 stderr | 260–265 | 【功能等價-必移植】行為部分（rc 語意）；`grep:` 措辭斷言本身屬 shell 細節，Python `grep -E` 保留此邊界 |

## 8. `tests/test-verify-bundle.sh`（verify-bundle.sh）

fixture：`make_valid_bundle_dir`（manifest.jsonl＋summary.txt＋README-FIRST.txt＋
cluster/ceph＋nodes/<host>/system）與 `make_bundle_archive`（tar.gz 化）；每個失敗
情境各做 dir 版與 archive 版。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| B1 | 合法 bundle（目錄與 tar.gz 兩形態）→ exit 0，stdout 恰為 `VERIFY PASS: <target>` | valid 佈局 | 43–52, 166–167 | 【功能等價-必移植】（輸出格式為對外契約，被上層 grep） |
| B2 | bundle 內含 symlink → 驗證失敗且訊息提及 symlink | `ln -s /etc/passwd` | 72–75, 168 | 【功能等價-必移植】 |
| B3 | 缺 manifest.jsonl（dir 與 archive）→ 失敗且點名 manifest.jsonl | 刪檔 | 77–82, 169–170 | 【功能等價-必移植】 |
| B4 | 檔名為 `keyring`（dir/archive）→ 失敗且點名 | 佈局 | 84–94, 171–172 | 【功能等價-必移植】 |
| B5 | 內含 `.ssh/` 目錄（dir/archive）→ 失敗且點名 | 佈局 | 96–106, 173–174 | 【功能等價-必移植】 |
| B6 | 檔名 `id_ed25519`（dir/archive）→ 失敗且點名 | 佈局 | 108–118, 175–176 | 【功能等價-必移植】 |
| B7 | 檔名 `private_key`（dir/archive）→ 失敗且點名 | 佈局 | 120–130, 177–178 | 【功能等價-必移植】 |
| B8 | 檔名 `*.pem`（dir/archive）→ 失敗且點名 | 佈局 | 132–143, 179–180 | 【功能等價-必移植】 |
| B9 | 允許副檔名內夾帶未遮蔽 PEM 本體（`-----BEGIN OPENSSH PRIVATE KEY-----`）→ 內容掃描失敗且點名 PRIVATE KEY | leak.txt 佈局 | 145–161, 181–182 | 【功能等價-必移植】 |
| B10 | 非法 tar.gz → 失敗且說 invalid archive | 純文字假檔 | 163–164, 183 | 【功能等價-必移植】 |
| B11 | 多餘參數 → 非 0＋Usage | 直接呼叫 | 185–189 | 【功能等價-必移植】 |

## 9. `tests/test-collect.sh`（run/collect.sh 端到端編排）

線性腳本。`tests/fixtures/shell-collect-environment.sh` 建立共用的能力感知假
`ssh`、`kubectl`、`timeout` 與外部宣告式 inventory；同一 helper 也供
`tests/export-shell-collect-fixture.sh` 產生 Python compatibility fixture。完整 shell
情境仍在頂層明確啟用兩條 compatibility paths；compatibility fixture 則明確關閉它們。

| # | 情境描述 | fixture 手法 | 行號 | 分類 |
|---|---|---|---|---|
| O1 | `--help` → exit 0；usage 文件化所有主要旗標（--kube-context、--no-trust-ssh-host-key、--no-redact、--prom-url、--keep-original-logs、--var-log-max-bytes、--allow-cephadm-shell、--allow-kubectl-exec） | `run_and_capture` | 59-71 | 【功能等價-必移植】CLI 契約（措辭不必逐字，旗標集合要在） |
| O2 | inventory 不存在 → exit 1 | 同上 | 73-75 | 【功能等價-必移植】 |
| O3 | inventory 是宣告式資料、**不得**被當 shell 執行：含 `$(touch ...)` 的 inventory → exit 1 且 marker 檔未被建立 | 惡意 inventory 檔 | 83-93 | 【功能等價-必移植】安全 |
| O4 | host alias 含 `../` → exit 1，且未在輸出根外建立檔案 | 惡意 alias | 95-103 | 【功能等價-必移植】 |
| O5 | SSH target 形如 `--ProxyCommand=...` → 失敗且**未曾**呼叫 ssh | `FAKE_SSH_LOG` 保持空 | 105-113 | 【功能等價-必移植】argv 注入防護 |
| O6 | auto 模式雙層收集 happy path：cluster/ceph 來自 ceph 節點、cluster/rook 來自 kube 節點、每節點 nodes/<alias>/…；node 端 config 被 `[REDACTED]`；kubectl 帶 `--context lab` 且跑在 kube 節點；預設 `StrictHostKeyChecking=accept-new`；node wrapper 用 `--node-timeout 90`；`--keep-original-logs`/`--var-log-max-bytes 123456` 轉發到遠端；environment.txt 記 `ceph_source`/`rook_source`；CONTENTS.md 逐 artifact 列來源指令；無 `--prom-url` 時 bundle 不含 prometheus 層；成功且未指定 `--keep-workdir` 時不殘留 `tmp.*` | `FAKE_CEPH_TARGETS`/`FAKE_KUBE_TARGETS`＋`FAKE_SSH_LOG`/`FAKE_TIMEOUT_LOG`＋tar 檢查 | 118-151 | 【功能等價-必移植】核心編排契約 |
| O7 | `--no-trust-ssh-host-key`：不再帶 accept-new，redaction 預設仍開（開關互相獨立） | `FAKE_SSH_LOG` | 153-163 | 【功能等價-必移植】 |
| O8 | `--no-redact`：秘密原文保留於 bundle；host key trust 預設仍開 | tar 內容 | 165-174 | 【功能等價-必移植】 |
| O9 | 顯式 `--trust-ssh-host-key --redact` 等同預設行為 | 同上 | 176-185 | 【功能等價-必移植】 |
| O10 | auto、無任何 capable 節點 → exit 2；`cluster/ceph/SKIPPED.txt`＋`cluster/rook/SKIPPED.txt` 都在；nodes 層照常收集 | `FAKE_CEPH_TARGETS="" FAKE_KUBE_TARGETS=""` | 187-203 | 【功能等價-必移植】 |
| O11 | 顯式 `--mode cephadm --seed`：只收 ceph 層，全程**不得**碰 kubectl | `FAKE_SSH_LOG` | 205-215 | 【功能等價-必移植】 |
| O12 | 顯式 seed 但 direct/sudo runner 都不通、且 cephadm-shell fallback 未授權（unset `CEPH_INCIDENT_ALLOW_CEPHADM_SHELL`）→ exit 2＋SKIPPED，**不得**動用 cephadm shell | `FAKE_CEPH_DIRECT_OK="" FAKE_CEPH_SUDO_OK=""` | 217-236 | 【功能等價-必移植】安全預設 |
| O13 | 兩台 cephadm 節點：cluster ceph 只從**第一台**收，不重複 | `FAKE_SSH_LOG` | 238-258 | 【功能等價-必移植】 |
| O14 | node 回傳 tar 缺 manifest → 該節點 SKIPPED、整體 exit 2 | `FAKE_SSH_NO_MANIFEST_ALIAS=kubenode` | 270-276 | 【功能等價-必移植】 |
| O15 | node 回傳非 tar → SKIPPED、exit 2 | `FAKE_SSH_BAD_TAR_ALIAS=kubenode` | 278-284 | 【功能等價-必移植】 |
| O16 | 單一 host 收集失敗（remote exit 2）→ 整體 exit 2、bundle 含 errors.log | `FAKE_SSH_FAIL_ALIAS=kubenode` | 286-292 | 【功能等價-必移植】 |
| O17 | 中途 abort → trap 清掉 workdir，`--out` 下不留 `tmp.*` | `COLLECT_TEST_ABORT_AFTER_NODES=1`（實作內建測試鉤子） | 294-304 | 【功能等價-必移植】 |
| O18 | verify 失敗（node 夾帶 `.pem`）→ exit 1、**不產** bundle、workdir 保留一份供調查 | `FAKE_SSH_PEM_ALIAS=kubenode` | 306-318 | 【功能等價-必移植】 |
| O19 | auto、只有 kube 節點且 namespace 不存在、無 ceph → exit 2（不得綠色 exit 0）；rook SKIPPED 的**具體原因**（namespace not found）不得被泛用 auto skip 覆寫 | `FAKE_KUBE_NS_MISSING=1` | 320-339 | 【功能等價-必移植】 |
| O20 | 能力探測 ssh 失敗的節點 → errors.log 記 `capability probe failed for <target>`；bundle 留該 target 的 `ssh-debug/*.log`（verbose 重試輸出） | `FAKE_PROBE_FAIL_TARGETS`；假 ssh 對 `-vvv` 輸出 debug1/debug3 後 exit 255 | 341-352 | 【功能等價-必移植】 |
| O21 | node 收集 ssh 傳輸失敗 → exit 2＋該 target 的 ssh-debug log 入 bundle | `FAKE_SSH_CONNECT_FAIL_TARGETS="10.0.0.9"` | 354-364 | 【功能等價-必移植】 |
| O22 | cluster ceph ssh 傳輸失敗 → exit 2＋ssh-debug log（內容含 `label: cluster-ceph`） | `FAKE_SSH_CONNECT_FAIL_TARGETS="10.0.0.1"` | 366-376 | 【功能等價-必移植】 |
| O23 | `HOSTS=()` 空清單 → exit 1＋明確訊息（HOSTS is empty） | 空 inventory | 378-385 | 【功能等價-必移植】 |
| O24 | `--kube-context` 含 shell metacharacter（`bad;ctx`）→ exit 1＋說明；合法 EKS ARN 式 context（含 `@ : /`）要通過驗證（隨後才因 inventory 失敗） | 直接呼叫 | 387-397 | 【功能等價-必移植】 |
| O25 | 偏好 direct runner：`ceph -s` 可直連時用純 `ceph`，不用 cephadm shell；environment.txt 記 `ceph_runner=direct` | `FAKE_CEPH_BIN_TARGETS`＋`FAKE_CEPH_DIRECT_OK` | 399-417 | 【功能等價-必移植】 |
| O26 | direct/sudo 都不通、cephadm 通 → fallback 用 `sudo -n cephadm shell`；environment.txt 記 `ceph_runner=cephadm` | `FAKE_CEPH_DIRECT_OK="" FAKE_CEPH_SUDO_OK=""` | 419-435 | 【功能等價-必移植】 |
| O27 | `--kube-mode local`：rook 層用本機 kubectl（不經 ssh）；environment.txt 記 `rook_source=local` | `FAKE_SSH_LOG` 無 kubectl | 437-447 | 【功能等價-必移植】 |
| O28 | `--kube-mode bogus` → exit 1＋說明 | 直接呼叫 | 449-454 | 【功能等價-必移植】 |
| O29 | `--prom-url`＋不可解析 `--since` → 前置檢查 exit 1＋說明 | 直接呼叫 | 456-461 | 【功能等價-必移植】 |
| O30 | 非數字 `--prom-timeout` → exit 1 | 直接呼叫 | 463-465 | 【功能等價-必移植】 |
| O31 | `--prom-step 0` → exit 1 | 直接呼叫 | 467-469 | 【功能等價-必移植】 |
| O32 | `--prom-url` 端到端：prometheus dump 落在 bundle 的 `cluster/prometheus/`（dump-info、buildinfo、各 job gz）；不匹配 job 不 dump；environment.txt 記 prom_url；24h → step=15 | fixture curl＋`FAKE_CURL_LOG` | 471-490 | 【功能等價-必移植】 |
| O33 | progress 預設開：stderr 顯示節點/探測/收集進度；stdout 只有 `bundle:` 行、且 `bundle:` 不得出現在 stderr | stdout/stderr 分流捕捉 | 492-502 | 【功能等價-必移植】 |
| O34 | `--quiet`：stdout 仍印 `bundle:`，stderr 進度全部靜默 | 同上 | 504-511 | 【功能等價-必移植】 |
| O35 | 中斷處理（Ctrl-C 契約）：`on_interrupt` → exit 130、announce interrupted、移除 workdir | source lib 後直呼 handler（`CLEANUP_WORKDIR`/`CLEANUP_KEEP`） | 513-535 | 【功能等價-必移植】行為契約；移植時改為對 Python 版 SIGINT handler / cleanup 函式做單元測試 |
| O36 | `--keep-workdir` 時中斷處理保留 workdir（`CLEANUP_KEEP=1`） | 同上 | 536-550 | 【功能等價-必移植】 |

---

## 10. 假指令行為介面專章（供 Python 測試沿用）

Python 移植時建議原樣沿用這兩個 fixture 腳本（塞 PATH 手法不變，`unittest` 的
setUp 準備 env dict 傳給 `subprocess`），或以同介面改寫。

### 10.1 `tests/fixtures/bin/ssh`（假 ceph 叢集 ssh）

檔案：`tests/fixtures/bin/ssh`（1–188 行）。以「整串 argv 拼成字串後做 substring
match」分派，模擬透過 ssh 在遠端跑 `ceph ...` 的回應。

環境變數介面：

| 變數 | 行為 |
|---|---|
| `FAKE_SSH_LOG` | 每次呼叫把完整 argv（`$*`）append 一行到此檔（行 4, 8–10）。測試靠它斷言「發了哪些遠端指令、帶哪些 ssh 選項」。 |
| `FAKE_SSH_FAIL_ON` | 若非空且指令字串含此 substring，印 `simulated failure for <needle>` 到 stderr 並 **exit 17**（行 22–28）。用來模擬單一 ceph 子命令失敗。 |
| `FAKE_SSH_CRASH_LS_BROKEN=1` | `ceph crash ls --format json-pretty` 回非 JSON（`{not-json`）、exit 0（行 123–128）。模擬 crash 列表解析失敗。 |

指令分派（依 substring，`--format json-pretty` 變體優先於純文字變體）：

- `ceph status`、`health detail`、`versions`、`df detail`、`osd tree/df/dump/perf/blocked-by`、
  `pg stat/dump/dump_stuck`、`mon dump`、`quorum_status`、`mgr dump`、
  `orch host ls / ps / device ls`、`config dump`：各回一份固定 JSON 或文字（行 31–122）。
- `ceph crash ls`：回 **12 筆** crash id，其中刻意含 `crash/02` 與 `crash:02`
  （消毒後同名，測防碰撞）——供「上限 10 筆＋檔名消毒」情境（行 123–143）。
- `ceph crash info <id>`：對 crash-01…crash-10（含 `crash/02`、`crash:02`）各回對應
  JSON；**crash-11、crash-12 沒有分支**——若收集器未守住 10 筆上限就會落到 fallback。
- 其他任何指令：stderr 印 `unexpected ssh command:` 並 **exit 99**（行 184–187）——
  這是「白名單式 fixture」關鍵設計：收集器多發任何未預期指令都會直接爆測試。

### 10.2 `tests/fixtures/bin/curl`（假 Prometheus HTTP）

檔案：`tests/fixtures/bin/curl`（1–147 行）。模擬 `prom_curl` 產生的 argv 形狀：
`curl -q -fsS -G --connect-timeout T --max-time T -o OUT URL [--data-urlencode P]...`；
自行解析出 `-o` 輸出檔、URL 與 `--data-urlencode` 參數（行 25–53）。

環境變數介面：

| 變數 | 行為 |
|---|---|
| `FAKE_CURL_LOG`（必填） | 每次呼叫 append 人類可讀 argv 一行（行 19）。shell 測試靠它斷言 step 參數、URL 無雙斜線等。 |
| `FAKE_CURL_ARGV_LOG` | 選用的 NUL-delimited lossless argv ledger（行 20–23）；Python 黑箱測試靠它精確保留含空白參數的 argument boundary。 |
| `FAKE_CURL_DOWN=1` | 所有請求模擬連線失敗：stderr `curl: (7) ...`、**exit 7**（行 55–62）。 |
| `FAKE_CURL_FAIL_PATHS` | 空白分隔的 URL substring 清單；匹配的請求先把 `partial` 寫進 `-o` 輸出檔（仿真 curl 先 truncate 再失敗），stderr `curl: (22) ... 500`、**exit 22**（行 64–73）。用來測「失敗請求不得留下殘檔」。 |
| `FAKE_CURL_ECHO_URL_ON_ERROR` | 搭配 `FAKE_CURL_DOWN=1`，讓 fake curl 在 stderr 回顯完整 request URL；用來證明外部工具即使回顯含 basic-auth 的 URL，collector 也會在寫入 bundle 前遮蔽密碼。 |
| `FAKE_CURL_FAIL_METRICS` | 空白分隔 metric 名清單；只讓對應 metric 的 `query_range` 回 500 / exit 22（行 129–137）。 |
| `FAKE_CURL_JOBS_JSON` | 覆寫 `/api/v1/label/job/values` 回應本體（行 110–116）。用來注入不安全 job 名。 |
| `FAKE_CURL_TIMEOUT_PATHS` | 空白分隔的 URL substring 清單；匹配的請求模擬 curl 自己觸發 `--max-time`：輸出檔清空、stderr `curl: (28) Operation timed out`、**exit 28**。Python Prometheus slice（#15）用它覆蓋 timeout 情境。 |
| `FAKE_CURL_MALFORMED_PATHS` | 空白分隔的 URL substring 清單；匹配的請求以 **exit 0** 回一段非 Prometheus JSON 的本體（如 proxy error page）。用來覆蓋 malformed response 情境。 |
| `FAKE_CURL_NAMES_JSON` | 覆寫 `/api/v1/label/__name__/values` 回應本體（行 117–121）。用來注入不安全、非字串或含 `:` 的 metric 名。 |

端點回應（寫進 `-o` 指定檔）：

- `/api/v1/status/buildinfo`：固定 success＋version。
- `/api/v1/targets`：固定 activeTargets。
- `/api/v1/label/job/values`：預設 `["ceph","node-exporter","grafana"]`。
- `/api/v1/label/__name__/values`：依 `match[]` 參數中的 `job="..."` 回
  ceph→`[ceph_health_status, ceph_osd_up]`、node-exporter→`[node_load1]`、其他→空。
- `/api/v1/query_range`：從 `query` 參數摳出 `__name__="..."`，回一筆 matrix，
  時間戳用 `start` 參數（行 129–142）。
- 其他 URL：**exit 99**（白名單設計，同 ssh fixture）。

Python Prometheus 黑箱案例另外把 `tests/fixtures/python-prometheus/bin/grep` 放在
`PATH` 最前面。這個 adapter 只接受完整 argv `grep -qiE -- PATTERN`，將每次呼叫
寫進 `FAKE_GREP_LOG`，再轉交系統 grep 執行；任何額外 option 或不同 argv 形狀都
會 exit 99。這同時證明 collector 沒有擴張外部命令 surface，且 job filter 保留
shell `grep -E` 的 POSIX ERE 語意。

### 10.3 Collect 共用 fixture 與各測試檔的 inline 假指令

Collect 的共用環境與其他測試檔的 inline fakes 都沿用同一手法：heredoc 產生假 bin、
`chmod +x`，再放到 PATH 最前。移植時必須保留下列介面：

- **`tests/fixtures/shell-collect-environment.sh` 的能力感知假 `ssh`**（行 47–139）：
  `test-collect.sh` 與 shell→Python compatibility exporter 共用的核心 fixture。依遠端指令分派：
  - `-vvv`：輸出 `debug1:`/`debug3:` 假 verbose log 後 exit 255（ssh-debug log 情境）。
  - `FAKE_SSH_CONNECT_FAIL_TARGETS`（substring 清單）：模擬 TCP 連線拒絕、exit 255。
  - `--connect-timeout 5 -s`（runner 連通性探測）：依 `FAKE_CEPH_DIRECT_OK` /
    `FAKE_CEPH_SUDO_OK` / `FAKE_CEPHADM_OK`（預設沿用 `FAKE_CEPH_TARGETS`）決定成敗。
  - `command -v cephadm`（能力探測）：`FAKE_PROBE_FAIL_TARGETS` → exit 255；否則依
    `FAKE_CEPH_TARGETS` / `FAKE_CEPH_BIN_TARGETS` / `FAKE_KUBE_TARGETS` 輸出
    `cephadm` / `ceph` / `kubectl` 能力字。
  - `cephadm shell -- ceph` 與 ` ceph `：exec 委派給 `$FIXTURE_SSH`（10.1 的共用 fixture）。
  - `collect-node.sh`：就地捏造 Node Evidence Archive 回傳 stdout；旋鈕：
    `FAKE_SSH_BAD_TAR_ALIAS`（回非 tar）、`FAKE_SSH_NO_MANIFEST_ALIAS`（tar 缺 manifest）、
    `FAKE_SSH_FAIL_ALIAS`（tar 完整但 exit 2）、`FAKE_SSH_PEM_ALIAS`（夾帶 .pem 觸發
    verify 失敗）、`FAKE_SSH_SLEEP`。`FAKE_SSH_NODE_ARCHIVE_CASE`／
    `FAKE_SSH_NODE_ARCHIVE_CASES` 會委派 `make-node-archive.py`，供公開 Collect 黑箱測試產生
    absolute／empty／traversal／link／special-member／collision／oversize／gzip-truncated／
    tar-truncated／missing-or-invalid-manifest archive；測試並確認 rejected member 未進入 node root、workspace 外 marker
    未被覆寫、valid archive 搭配 remote nonzero 仍保留 evidence。
  - `kubectl ...`：轉呼叫同一 helper 產生的本機假 kubectl。
- **同一共用 helper 的假 `kubectl`**（行 13–38）：`FAKE_KUBE_NS_MISSING=1`
  模擬 namespace 不存在；`FAKE_KUBE_TOOLS_POD=1` 讓 tools Pod 可被發現，用來證明
  default-off compatibility fixture 不會偷偷執行 `kubectl exec`；容忍前置 `--context CTX`。
- **同一共用 helper 的假 `timeout`**（行 40–45）：記第一個參數（秒數）到
  `FAKE_TIMEOUT_LOG` 後透傳執行。`test-common.sh` 另有同介面的 inline fake，寫入
  `TIMEOUT_LOG`。
- 共用假 `kubectl` 與 `ssh` 對未列入介面的命令一律輸出 `unexpected ...` 並 exit 99；
  這個白名單 fallback 是 fixture 偵測新增 command surface 的主要防回歸機制。
- **`test-rook-collector.sh` 假 `kubectl`**（行 74–148）：`FAKE_KUBECTL_LOG` 記 argv；
  `FAKE_KUBECTL_MODE` ∈ present / missing-namespace / context-missing /
  connection-refused / with-toolbox / op-lookup-fail；假 `ssh`（行 233–245）把
  `kubectl` 之後的參數轉給本機假 kubectl。
- **`test-node-collector.sh` 假系統工具全家桶**（行 40–196）：見第 4 節開頭清單；
  旋鈕 `FAKE_SUDO_LOG`、`FAKE_OPTIONAL_LOG`、`FAKE_JOURNALCTL_NO_CEPH`、
  `FAKE_JOURNALCTL_LARGE`、`FAKE_TIMESYNCD_MISSING`。
- **假 `gzip`**（test-common 行 264–273、297–304；test-var-log 行 263–276）：靠
  `REAL_GZIP` env 讓 `-dc` 走真 gzip、壓縮方向失敗；var-log 版另用 `GZIP_COUNTER`
  檔做「第 N 次呼叫才失敗」。
- **假 `python3`**（test-common 行 28–33、test-cephadm 行 16–21）：exit 99，證明
  受測程式不依賴 python3——移植後此類斷言反轉為無意義，不移植。

### 10.4 受測程式吃的測試注入環境變數（非假指令，但移植需保留同等 seam）

`CEPH_INCIDENT_VAR_LOG_DIR`、`CEPH_INCIDENT_VAR_LIB_CEPH_DIR`、
`CEPH_INCIDENT_TIMESYNCD_CONF`、`CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR`、
`CEPH_INCIDENT_TEST_ALLOW_ATIME_READ`、`CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES`、
`CEPH_INCIDENT_QUIET`、`CEPH_INCIDENT_ALLOW_CEPHADM_SHELL`、
`CEPH_INCIDENT_ALLOW_KUBECTL_EXEC`、`COLLECT_TEST_ABORT_AFTER_NODES`、`ERROR_LOG`。
Python 版可改為建構參數或設定物件，但測試層需要等價注入點。

---

## 11. 移植注意事項（摘要）

1. **exit code 三值語意是全域契約**：0＝完整成功、1＝致命（用法錯誤 / verify 失敗）、
   2＝partial（有 SKIPPED / 單項失敗但 bundle 仍產出）。幾乎每個情境都在斷言這件事。
2. **白名單式 fixture（unexpected → exit 99）務必保留**：它讓「多發了一個指令」立即
   失敗，是這套測試抓回歸的主力。
3. 內容斷言移植時對齊**語意**（SKIPPED 檔要講原因、errors.log 要點名 target），
   不要逐字綁 shell 版英文措辭。
4. 10 個【實作細節-不移植】情境：R1、R2、R7、C1、C2、C20、C22、C23、P4、P8。
