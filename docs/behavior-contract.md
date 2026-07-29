# 行為契約：ceph-incident-bundle 現有 shell 實作

> 本文件是 GitHub issue #6（wayfinder map #3）的產出。目的：作為 Python 3.11 重寫時「功能等價」的唯一依據。讀者應能只靠這份文件（不讀 shell code）寫出行為等價的實作。每個事實都附上來源檔案與行號（以本文件撰寫當下的 commit 為準，`git_commit` 見 bundle `environment.txt`）。
>
> 涵蓋檔案：`run/collect.sh`、`lib/common.sh`、`lib/bundle.sh`、`lib/collect-cluster-cephadm.sh`、`lib/collect-cluster-rook.sh`、`lib/collect-node.sh`、`lib/collect-var-log.sh`、`lib/collect-prometheus.sh`、`lib/verify-bundle.sh`、`README.md`。

---

## 1. 頂層執行流程（`main`，run/collect.sh:349-680）

依序執行，任何一步的行為都不可調換順序（後面各節有細節）：

1. 無參數 → 印 usage 到 stderr、exit 1（run/collect.sh:360-363）。
2. 解析 CLI flags（run/collect.sh:365-476）；未知 flag → usage 到 stderr、exit 1（run/collect.sh:471-474）。
3. 驗證輸入（run/collect.sh:478-498）：mode、var-log-max-bytes、kube-context 字元集、kube-mode、prom 相關、inventory 檔存在、ssh key 檔存在。任一失敗 → `die`（stderr `FATAL:` + exit 1，lib/common.sh:11-14）。
4. `export CEPH_INCIDENT_ALLOW_CEPHADM_SHELL`、`CEPH_INCIDENT_ALLOW_KUBECTL_EXEC`（run/collect.sh:481-482）、`CEPH_INCIDENT_TRUST_SSH_HOST_KEY`（run/collect.sh:499）。
5. 解析 inventory（run/collect.sh:501，§5），驗證 `SSH_USER` / namespaces / seed（run/collect.sh:503-517）。
6. 工作機沒有 `timeout`/`gtimeout` → 印 WARNING（外層 timeout 全部停用，只剩 SSH ConnectTimeout/ServerAlive；run/collect.sh:519-521）。
7. 建立 `out_dir`；`timestamp=$(date -u +%Y%m%dT%H%M%SZ)`；workdir 為 `out_dir/tmp.<timestamp>.<pid>`（run/collect.sh:523-527）。
8. 建立空的 runtime known_hosts 檔 `workdir/.runtime-known-hosts`，export `CEPH_INCIDENT_KNOWN_HOSTS_FILE` 指向它（run/collect.sh:528-529）。
9. 設 EXIT trap（`cleanup_workdir`）與 INT/TERM trap（`on_interrupt`）（run/collect.sh:535-536，§16）。
10. 寫初始 metadata：`README-FIRST.txt`、`environment.txt`、空 `manifest.jsonl`、空 `errors.log`（run/collect.sh:537；lib/bundle.sh:87-115）。
11. 解析 `HOSTS` 成 `HOST_ALIASES[]`/`HOST_TARGETS[]`（run/collect.sh:544-569，§5.3）。
12. `collect_clusters`（cluster ceph + rook 層，§7）；非 0 → `errors.log` 記 `cluster collection exited <rc>`、整體 rc=2（run/collect.sh:573-584）。
13. 有 `--prom-url` 時執行 `collect_prometheus`（§10）；非 0 → errors.log + rc=2（run/collect.sh:586-599）。
14. 逐台（依 inventory 順序、序列）收集每個 node（§11）；單台失敗 → errors.log 記 `node <alias> (<target>) collector exited <rc>`、rc=2，但**繼續下一台**（run/collect.sh:601-620）。
15. 測試 hook：`COLLECT_TEST_ABORT_AFTER_NODES` 非空 → `die "test abort after nodes"`（run/collect.sh:622-625）。
16. redact（預設開，§13）；失敗 → errors.log 記 `redaction failed; original collected artifact was preserved`、rc=2。`--no-redact` 時只寫空的 `redactions.log`（run/collect.sh:627-635）。
17. `enforce_node_log_caps`（redaction 後在工作機上再次驗每台 node 的 log 容量上限，§12.5）；失敗 → rc=2（run/collect.sh:636-638）。
18. 刪除 `workdir/.runtime-known-hosts`（run/collect.sh:639）。
19. 寫 `summary.txt`（帶目前 rc 當 final_status）與 `CONTENTS.md`（run/collect.sh:640-641；lib/bundle.sh:117-130、169-201）。
20. **打包前** verify workdir（stdout 丟棄、stderr 附加到 `errors.log`）；失敗 → `CLEANUP_KEEP=1`（保留 workdir）、errors.log 記 `bundle verification failed (rc=..); workdir kept, NOT packaged for sharing`、**重寫** summary.txt（final_status=1）、stderr 印 `VERIFY FAILED: workdir kept at <path> (not packaged) — review errors.log`、return 1（run/collect.sh:643-660）。
21. 打包：`COPYFILE_DISABLE=1 tar -czf out_dir/ceph-incident-<timestamp>.tar.gz -C workdir .`（run/collect.sh:662-664）。
22. **打包後**再 verify 一次 tar.gz；失敗 → 保留 workdir、**刪除 tar.gz**、stderr 印 `VERIFY FAILED on packaged bundle; removed it, workdir kept at <path>`、return 1（run/collect.sh:665-676）。
23. stdout 印唯一一行 `bundle: <path>`，return 累計 rc（0 或 2）（run/collect.sh:678-679）。

---

## 2. CLI flags 全表（run/collect.sh:365-476；usage 文字 19-72）

### 2.1 必填

| flag | 語意 | 驗證 |
|---|---|---|
| `--inventory PATH` | inventory 檔（§5） | 必須存在且為檔案，否則 `die "missing inventory: <path或<unset>>"`（run/collect.sh:497） |
| `--ssh-key PATH` | 對每台 node 使用的同一把 SSH 私鑰 | 必須存在，否則 `die "missing ssh key: ..."`（run/collect.sh:498） |

### 2.2 選填（值型）

| flag | 預設 | 語意 / 驗證 |
|---|---|---|
| `--seed USER@HOST` | 空（inventory `SEED_HOST` 補位） | 覆寫 cluster-ceph 來源 node。覆寫優先序：`--seed` > inventory `SEED_HOST`（後者會經 `ssh_target_for_host` 補 SSH_USER）> 自動探測（run/collect.sh:512-516）。非空時須通過 `is_safe_ssh_target`，否則 `die "invalid SSH seed target"`（run/collect.sh:517） |
| `--out DIR` | `<repo>/results`（run/collect.sh:350） | 輸出目錄（workdir 與最終 tar.gz 都在裡面） |
| `--mode auto\|cephadm\|rook` | `auto`（run/collect.sh:351） | 其他值 → `die "unsupported mode: ..."`（run/collect.sh:478） |
| `--kube-context CTX` | 空 | 傳給 rook 層 kubectl 的 `--context`。字元白名單 `A-Za-z0-9._@:/-`，含其他字元 → die（run/collect.sh:486-488） |
| `--kube-mode remote\|local` | `remote`（run/collect.sh:357） | rook 層 kubectl 執行位置（§7.3）。其他值 → die（run/collect.sh:489） |
| `--since DURATION` | `24h`（run/collect.sh:351） | log/journal 時間窗。**平常不驗證格式**；只有給了 `--prom-url` 時才要求符合 `N`/`Ns`/`Nm`/`Nh`/`Nd`/`Nw`（run/collect.sh:490-493；lib/collect-prometheus.sh:22-39）。含單引號的值會導致 node 收集失敗（§11.1 shell_quote） |
| `--prom-url URL` | 空（= 完全不碰 Prometheus） | Prometheus base URL（§10） |
| `--prom-job-regex RE` | `ceph\|node`（run/collect.sh:356） | scrape job 過濾（`grep -qiE`，大小寫不敏感） |
| `--prom-step SECONDS` | 空（自動：`max(15, ceil(window/10000))`，lib/collect-prometheus.sh:43-48） | 須符合 `^[1-9][0-9]*$`，否則 die（run/collect.sh:494） |
| `--prom-timeout SECONDS` | `600`（run/collect.sh:356） | Prometheus dump 整體時間預算。須為數字（run/collect.sh:495） |
| `--timeout SECONDS` | `20`（run/collect.sh:351） | 單一指令 / SSH connect timeout。**未做數字驗證** |
| `--node-timeout SECONDS` | `600`（run/collect.sh:351） | 單台 node 整輪收集的外層 timeout。未做數字驗證 |
| `--var-log-max-bytes N\|unlimited` | `10737418240`（10 GiB，run/collect.sh:352） | 每台 node 的 `/var/log` payload 上限。須為 `unlimited` 或純數字，否則 die（run/collect.sh:479-480） |

### 2.3 選填（開關型）

| flag | 預設 | 語意 |
|---|---|---|
| `--skip-logs` | off | 傳給 node collector，跳過 `/var/log` 收集，僅寫 `logs/var-log/SKIPPED.txt`（lib/collect-node.sh:429-430） |
| `--keep-original-logs` | off | 合併成功的文字 log 額外在 `original/` 保留來源格式（§12.4） |
| `--allow-cephadm-shell` | off；**初始值來自環境變數 `CEPH_INCIDENT_ALLOW_CEPHADM_SHELL`**（run/collect.sh:353） | 允許 ceph runner fallback 到 `sudo -n cephadm shell -- ceph`（可能啟動/pull container；§8） |
| `--allow-kubectl-exec` | off；初始值來自 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC`（run/collect.sh:354） | 允許 rook toolbox `kubectl exec ... ceph status`（§9） |
| `--trust-ssh-host-key` | **on（預設）**（run/collect.sh:355） | SSH 加 `StrictHostKeyChecking=accept-new` 與雙 known_hosts（§6） |
| `--no-trust-ssh-host-key` | — | 與上互斥（後出現者生效；同一變數 `trust_ssh_host_key` 0/1）。回到 OpenSSH 原生 host key 檢查 |
| `--redact` | **on（預設）**（run/collect.sh:355） | 打包前遮蔽敏感行（§13） |
| `--no-redact` | — | 與上互斥（後出現者生效）。不遮蔽，仍寫出空 `redactions.log`（run/collect.sh:634） |
| `--quiet` | off | 實作為 `export CEPH_INCIDENT_QUIET=1`（run/collect.sh:459-461），關掉 stderr 進度輸出；`bundle:` 行與錯誤照舊 |
| `--keep-workdir` | off | EXIT 時保留暫存 workdir（除錯用；lib/bundle.sh:205-215） |
| `--help` / `-h` | — | 印 usage 到 **stdout**、exit 0（run/collect.sh:467-470） |

互斥關係整理：`--trust-ssh-host-key`/`--no-trust-ssh-host-key` 與 `--redact`/`--no-redact` 是同一變數的正反開關，重複給時**最後一個生效**；`--mode` 三值互斥；`--kube-mode` 二值互斥。`--seed` 與 inventory `SEED_HOST` 的優先序見上表。沒有其他 flag 組合檢查（例如 `--prom-step` 沒配 `--prom-url` 也不報錯，只是沒用到）。

---

## 3. Exit code 與 stdout/stderr 契約

### 3.1 Exit code（run/collect.sh:69-71 usage、README.md:217-221）

| code | 語意 |
|---|---|
| `0` | 收集完成，無已知失敗。叢集本身故障（OSD down 等）不算收集失敗 |
| `2` | partial：部分 command / node / 層失敗，但 bundle 已產生且驗證通過 |
| `1` | usage 錯誤、config/輸入錯誤（`die`）、或 **verify 失敗**（此時不產出 tar.gz、保留 workdir） |
| `130` | 被 INT/TERM 中斷（`on_interrupt`，lib/bundle.sh:220-225） |

觸發 rc=2 的完整清單：malformed/unsafe HOSTS entry（run/collect.sh:549-565）、cluster 層收集失敗或無來源（§7.4）、prometheus 層失敗（run/collect.sh:595-598）、任一 node 失敗（run/collect.sh:612-617）、redaction 失敗（run/collect.sh:629-632）、post-redaction 容量超限（run/collect.sh:636-638）。

### 3.2 stdout/stderr

- **stdout 只有最後一行 `bundle: <path>`**（成功時；run/collect.sh:678）。例外：`--help` 的 usage 印在 stdout。
- 所有 log（`[UTC時戳] 訊息`）、進度（`progress`）、`FATAL:`、`VERIFY FAILED:`、`kept workdir:`、中斷訊息都在 **stderr**（lib/common.sh:6-9、115-118）。
- `progress` 格式 `[%FT%TZ] msg`，被 `CEPH_INCIDENT_QUIET` 抑制；`log` 不受 quiet 影響（lib/common.sh:6-9 vs 115-118）。
- 進度訊息內容（重寫時建議保留語意，不必逐字）：`starting: mode=..., N hosts`、`probing N nodes for capabilities…`、`[i/N] probe <target>: <caps|none>`、`collecting ceph cluster from X via Y…`、`[k/24] ceph <cmd>`、`ceph crash info (recent)…`、`collecting rook from ...`、`rook: pods/events/resources/operator-log/toolbox…`、`collecting prometheus metrics from <masked-url>…`、`prometheus: job J — N metrics, step Ss…`、`[i/N] node <alias>…`、`[i/N] node <alias>: ok|SKIPPED (exit rc)`、`redacting…`、`verifying…`、`packaging…`。

---

## 4. 環境變數（CEPH_INCIDENT_*）

| 變數 | 預設 | 用途 / 出處 |
|---|---|---|
| `CEPH_INCIDENT_ALLOW_CEPHADM_SHELL` | `0` | `--allow-cephadm-shell` 的初始值（run/collect.sh:353）；main 會 export 回去（:481）；ceph runner gate 讀它（:127） |
| `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC` | `0` | 同上（run/collect.sh:354、482）；rook toolbox gate（lib/collect-cluster-rook.sh:198） |
| `CEPH_INCIDENT_TRUST_SSH_HOST_KEY` | `1` | main export（run/collect.sh:499）；`ssh_base_opts` 讀（lib/common.sh:42） |
| `CEPH_INCIDENT_KNOWN_HOSTS_FILE` | 無 | main 指向 workdir 暫存 known_hosts（run/collect.sh:528-529）；`ssh_base_opts` 讀（lib/common.sh:44-46） |
| `CEPH_INCIDENT_QUIET` | 未設 | `--quiet` export（run/collect.sh:460）；`progress` 讀（lib/common.sh:116） |
| `CEPH_INCIDENT_VAR_LOG_DIR` | `/var/log` | node collector 掃描根目錄覆寫（測試用；lib/collect-node.sh:434） |
| `CEPH_INCIDENT_VAR_LIB_CEPH_DIR` | `/var/lib/ceph` | cephadm config 收集根目錄覆寫（lib/collect-node.sh:229） |
| `CEPH_INCIDENT_TIMESYNCD_CONF` | `/etc/systemd/timesyncd.conf` | timesyncd config 路徑覆寫（lib/collect-node.sh:194） |
| `CEPH_INCIDENT_TIMESYNCD_CONF_D_DIR` | `/etc/systemd/timesyncd.conf.d` | 同上（lib/collect-node.sh:195） |
| `CEPH_INCIDENT_VAR_LOG_SCAN_MAX_BYTES` | `67108864`（64 MiB） | find 路徑清單 staging 上限（lib/collect-var-log.sh:314） |
| `CEPH_INCIDENT_VAR_LOG_MAX_ENTRIES` | `100000` | 掃描檔案數上限（lib/collect-var-log.sh:315） |
| `CEPH_INCIDENT_VAR_LOG_FREE_RESERVE_BYTES` | `1073741824`（1 GiB） | 遠端輸出目錄需保留的最小剩餘空間（lib/collect-var-log.sh:317） |
| `CEPH_INCIDENT_TEST_ALLOW_ATIME_READ` | `0` | 測試 escape hatch：允許用 `cat` 讀（略過 dd noatime；lib/collect-var-log.sh:41-44、lib/collect-node.sh:155-158、lib/collect-var-log.sh:60） |
| `COLLECT_TEST_ABORT_AFTER_NODES` | 未設 | 測試 hook：node 收完後強制 die（run/collect.sh:622-625） |
| `COMMAND_TIMEOUT` | `20` | `run_capture` 的單指令 timeout（呼叫端逐次設定；lib/common.sh:276） |
| `ERROR_LOG` | 無 | `run_capture`/probe 失敗時追加的 errors.log 路徑（lib/common.sh:300-304） |

注意：node 端相關變數（`CEPH_INCIDENT_VAR_LOG_*`、`TIMESYNCD` 等）是在**遠端 node 的環境**讀取，工作機不會把它們轉送過去。

---

## 5. Inventory 解析規則

### 5.1 格式（lib/bundle.sh:32-77）

宣告式格式，**不是被 source 的 shell**。逐行解析：

- 空行與 `#` 開頭（可有前導空白）行跳過（:43）。
- 接受的 scalar 賦值只有四個 key：`SSH_USER`、`SEED_HOST`、`ROOK_NAMESPACE`、`ROOK_OPERATOR_NAMESPACE`，格式 `KEY="value"`（雙引號必要，value 不可含雙引號），行尾可帶 `#註解`（regex 見 :34）。
- value 內含 `$(`、`${` 或反引號 → 整份 inventory 拒絕（:59-61）。
- `HOSTS=()`（單行空陣列）合法（:52-53）。
- `HOSTS=(` 單獨一行開始多行模式（:54-55）；其中每行必須是 `"<內容>"`（可帶行尾註解，:35、:47-48）；`)` 收尾（:45-46）。
- 其他任何行（含不認識的變數、指令、redirect）→ 解析失敗（:71-72）。
- 檔案結束時 HOSTS 括號未關 → 失敗（:76）。
- 解析失敗時 collect.sh `die "inventory must contain only supported quoted assignments and HOSTS entries"`（run/collect.sh:501）。

預設值（未出現在檔案時）：`SSH_USER=''`、`SEED_HOST=''`、`ROOK_NAMESPACE='rook-ceph'`、`ROOK_OPERATOR_NAMESPACE='rook-ceph'`（lib/bundle.sh:36-40）。注意 collect.sh 再套一層 `${VAR:-rook-ceph}`（run/collect.sh:505-506），所以空字串也會變回預設。

### 5.2 值驗證（run/collect.sh:503-517）

- `SSH_USER` 非空時：不可 `-` 開頭、須符合 `^[A-Za-z0-9._%+-]+$`，否則 die（:507-509）。
- `ROOK_NAMESPACE`/`ROOK_OPERATOR_NAMESPACE`：`^[A-Za-z0-9][A-Za-z0-9.-]*$`（lib/bundle.sh:24-27），否則 die。
- `SEED_HOST` 經 `ssh_target_for_host`：host 已含 `@` 或 SSH_USER 空 → 原樣；否則 `SSH_USER@host`（lib/bundle.sh:9-16）。
- 最終 seed 須通過 `is_safe_ssh_target`：非空、不可 `-` 開頭、符合 `^([A-Za-z0-9._%+-]+@)?(\[IPv6\]|[A-Za-z0-9._:-]+)$`（lib/bundle.sh:18-22）。

### 5.3 HOSTS entry 解析（run/collect.sh:544-569）

- `HOSTS` 為空陣列 → `die "inventory HOSTS is empty"`（:545-547）。
- 每個 entry 必須是 `alias=host`（含 `=`、兩側皆非空），否則記 errors.log `skipped malformed HOSTS entry: <entry>`、rc=2、**繼續**（:549-552）。
- alias 須符合 `^[A-Za-z0-9][A-Za-z0-9._-]*$` 且不是 `.`/`..`，否則記 `skipped unsafe host alias: ...`、rc=2、繼續（:554-558）。
- host 部分經 `ssh_target_for_host` 補 SSH_USER，再過 `is_safe_ssh_target`，不安全 → 記 `skipped unsafe SSH target for alias ...`、rc=2、繼續（:560-565）。
- 通過者依序進 `HOST_ALIASES[]`/`HOST_TARGETS[]`（順序 = inventory 順序 = 探測順序 = node 收集順序）。

---

## 6. SSH 選項與 host key 行為

### 6.1 基本選項向量（`ssh_base_opts`，lib/common.sh:28-48）

所有 SSH（探測、cluster、node、rook remote、debug）共用同一組：

```
-i <ssh_key>
-o BatchMode=yes
-o IdentitiesOnly=yes
-o IdentityAgent=none
-o LogLevel=ERROR
-o ConnectTimeout=<timeout>
-o ServerAliveInterval=<timeout>
-o ServerAliveCountMax=1
```

`<timeout>` 是 `--timeout` 的值（預設 20）。`LogLevel=ERROR` 是刻意的：讓 "Permanently added ..." 之類訊息不進 artifact（:30-33 註解）。

### 6.2 host key（lib/common.sh:42-47；README.md:18-22）

當 `CEPH_INCIDENT_TRUST_SSH_HOST_KEY=1`（預設）額外加：

```
-o StrictHostKeyChecking=accept-new
-o "UserKnownHostsFile=<workdir>/.runtime-known-hosts ${HOME}/.ssh/known_hosts"
```

語意：新 host key 自動接受但**只寫進 workdir 暫存檔**（UserKnownHostsFile 的第一個檔案），不改使用者的 `~/.ssh/known_hosts`；既有 known_hosts 仍會參考，已知 host 的 key 不一致仍拒絕連線。該暫存檔在打包前刪除（run/collect.sh:639），且中斷/結束時隨 workdir 清掉。`--no-trust-ssh-host-key` 時完全不加這兩個選項（回 OpenSSH 預設）。

### 6.3 timeout 包裝

外層以 `timeout`（或 macOS `gtimeout`）包住整個 ssh 指令（`timeout_cmd`，lib/common.sh:122-128）；兩者皆無時**不包**、只印一次警告（run/collect.sh:519-521）。node 收集用 `--node-timeout` 包（run/collect.sh:291-297），其餘用 `--timeout`。

### 6.4 SSH debug log（lib/common.sh:60-96）

以下情況會對同一 target **再跑一次** `ssh -vvv -o LogLevel=DEBUG3 <target> true`，輸出寫進 `workdir/ssh-debug/<safe_label>-<safe_target>.log`（safe 化：非 `A-Za-z0-9._-` 轉 `_`，`..` 轉 `__`；lib/common.sh:50-58）：

- capability probe ssh 失敗（任意非 0；run/collect.sh:91-96，label `capability-probe`）
- ceph runner probe 得到 exit 255（run/collect.sh:116-118，label `cluster-ceph-<method>`）
- cluster ceph 指令 exit 255/124/137（lib/collect-cluster-cephadm.sh:39-41，label `cluster-ceph`）
- node 收集 exit 255/124/137（run/collect.sh:313-315，label `node-<alias>`）

log 檔含 header（target/label/started/command）與 footer（ended/exit_code）。此檔會被打進 bundle（在 workdir 根下的 `ssh-debug/`）。

---

## 7. mode 與來源偵測（`collect_clusters`，run/collect.sh:142-262）

### 7.1 mode → 想收的層（:165-170）

| mode | want_ceph | want_rook |
|---|---|---|
| `cephadm` | 1 | 0 |
| `rook` | 0 | 1 |
| `auto` | 1 | 1 |

### 7.2 capability probe（`detect_node_caps`，run/collect.sh:74-98）

單次 ssh 在遠端執行（single-quoted，遠端展開）：

```sh
caps=""; command -v cephadm && caps="$caps cephadm"; command -v ceph && caps="$caps ceph"; command -v kubectl && caps="$caps kubectl"; printf "%s\n" "$caps"
```

ssh 失敗（非 0）→ 該 node 的 caps 視為空，但**必須**在 errors.log 記 `<UTC> capability probe failed for <target> (ssh exit <rc>) — node not considered as a cluster source` 並寫 ssh-debug（:91-96）——「探測不到」不等於「沒有能力」。

探測時機（:174-208）：

- `--seed`（或 SEED_HOST）存在且 want_ceph → ceph_source 直接釘為 seed，只對 seed 做 runner 選擇（:174-177）。**注意：seed 釘住後即使 runner 選不出來也不會 fallback 掃其他 node**（:184 條件 `-z ceph_source` 已為 false）。
- rook 只在 `kube_mode=remote` 時探測 node（local 不需要；:179-181）。
- 仍有未知來源時才逐台（inventory 順序、序列）探測；每台印進度 `[i/N] probe <target>: <caps|none>`；全部來源都找到就提早 break（:184-207）。
- ceph 來源 = 第一台 caps 含 `ceph` 或 `cephadm` **且** runner 實際連得上 cluster 的 node（:192-198）。
- rook 來源 = 第一台 caps 含 `kubectl` 的 node（**只看指令存在**，不驗 k8s 健康；:200-201）。

### 7.3 各層執行（:211-235）

- ceph 層：`ceph_source` 與 `ceph_runner` 都非空才收（§8 runner）；失敗 → rc=2，但 `ceph_done=1`（有嘗試）。
- rook 層：`kube_mode=local` 時 rook_source 記為字串 `local`、kubectl 在工作機本機跑；remote 時傳 `--ssh-target <rook_source> --ssh-key`。`--kube-context` 有值才傳。**mode=auto 時額外傳 `--allow-skip`**（:230）。
- `rook_done` 的定義是 `workdir/cluster/rook/pods-wide.txt` 存在（真的收到證據），**不是** collector return 0（allow-skip 只寫 SKIPPED 也回 0；:232-234）。

### 7.4 缺來源處理（:240-251）

用 `write_skip_artifact_once`（不覆蓋 collector 已寫的更具體原因；lib/common.sh:106-110）：

- `mode=cephadm` 且 ceph 未收 → `cluster/ceph/SKIPPED.txt`：`no cephadm-capable node found (or --seed unreachable)`，rc=2。
- `mode=rook` 且 rook 未收 → `cluster/rook/SKIPPED.txt`：`no kubectl-capable node found`，rc=2。
- `mode=auto`：各層缺的寫 `no cephadm-capable node in inventory (auto)` / `no kubectl-capable node in inventory (auto)`；**兩層都沒收到才 rc=2**，收到一層即 0。

### 7.5 observability（:255-259）

無論成敗，append 到 `environment.txt`：`ceph_source=`、`ceph_runner=`、`rook_source=`（沒有時值為 `<none>`）。

---

## 8. ceph runner 選擇（run/collect.sh:102-137；lib/collect-cluster-cephadm.sh:14-20）

runner token → 遠端指令前綴（`ceph_runner_argv`）：

| token | 前綴 |
|---|---|
| `direct` | `ceph` |
| `sudo` | `sudo -n ceph` |
| 其他（`cephadm`） | `sudo -n cephadm shell -- ceph` |

`ceph_runner_for`（:124-137）：依序試 `direct` → `sudo`；只有 `CEPH_INCIDENT_ALLOW_CEPHADM_SHELL=1`（即 `--allow-cephadm-shell`）才把 `cephadm` 加進嘗試清單（:127）。每個 method 用 `ceph_runner_probe` 驗證（:102-120）：ssh 到 target 跑 `<前綴> --connect-timeout 5 -s`；「可用」的定義是 `ceph -s` **成功連上 cluster**，不是 binary 存在。probe exit 255 時寫 ssh-debug。

**契約陷阱**：`ceph_runner_for` 永遠 exit 0；「找不到 runner」用**空 stdout** 表示，呼叫端必須檢查輸出是否為空（:134-136 註解）。

---

## 9. cluster-ceph collector（lib/collect-cluster-cephadm.sh:130-209）

`collect_cluster_cephadm workdir manifest seed ssh_key since timeout runner`。`since` 參數**刻意不使用**（cluster 指令是 point-in-time snapshot；:136-137）。runner 預設值 `cephadm`（:131，實務上 orchestrator 一定會傳）。

每條指令 = `ssh <base_opts> <seed> <runner前綴> <指令字詞...>`，經 `run_capture`（§14）收進 artifact；exit 255/124/137 追加 ssh-debug（:39-41）。任何一條失敗 → 最後 return 2，但**全部照跑**。

### 9.1 JSON artifacts（`cluster/ceph/json/`，:142-163，指令都加 `--format json-pretty`）

| 檔名 | ceph 指令 |
|---|---|
| status.json | `status` |
| health-detail.json | `health detail` |
| versions.json | `versions` |
| df-detail.json | `df detail` |
| osd-tree.json | `osd tree` |
| osd-df.json | `osd df` |
| osd-dump.json | `osd dump` |
| osd-perf.json | `osd perf` |
| osd-blocked-by.json | `osd blocked-by` |
| pg-stat.json | `pg stat` |
| pg-dump.json | `pg dump` |
| pg-dump-stuck.json | `pg dump_stuck` |
| mon-dump.json | `mon dump` |
| quorum-status.json | `quorum_status` |
| mgr-dump.json | `mgr dump` |
| orch-host-ls.json | `orch host ls` |
| orch-ps.json | `orch ps` |
| orch-device-ls-wide.json | `orch device ls --wide` |
| config-dump.json | `config dump` |
| crash-ls.json | `crash ls` |

### 9.2 純文字 artifacts（`cluster/ceph/text/`，:165-170）

`status.txt`（`status`）、`health-detail.txt`（`health detail`）、`osd-tree.txt`（`osd tree`）、`orch-ps.txt`（`orch ps`）。

進度顯示 `[k/24] ceph <cmd>`（20 json + 4 text）。

### 9.3 recent crashes（:103-128、49-101）

1. 從 `json/crash-ls.json`（去掉 `#` header 行後）用 regex 抽 `"crash_id":"..."`，**只取前 10 個**（:58-63）。
2. 抽不到且內容不是已知空清單形（`[]`、`{}`、`{"crashes":[]}`、`{"items":[]}`、`{"entries":[]}`、`{"crash_ls":[]}` 去空白比對）→ 寫 `cluster/ceph/text/crash-info-skip.txt`：`SKIPPED: unable to parse crash list JSON for recent crash inspection`，**return 0（不算失敗）**（:110-113）。
3. 每個 crash_id 跑 `crash info <id>`，存 `cluster/ceph/json/crash-info/<safe_id>.json`；safe_id 是非 `A-Za-z0-9._-` 轉 `_`、`..` 轉 `__`、空則 `crash`；同名衝突加 `-2`、`-3`…（:80-101）。單筆失敗 → return 2。

---

## 10. cluster-rook collector（lib/collect-cluster-rook.sh）

`collect_cluster_rook --out DIR --manifest PATH [--namespace NS(rook-ceph)] [--operator-namespace NS] [--since 24h] [--timeout 20] [--allow-skip] [--ssh-target T --ssh-key K] [--kube-context CTX]`（:78-133）。`--out`/`--manifest` 缺 → usage+return 1。operator-namespace 未給時 = namespace（:141）。

kubectl 前綴（:146-153）：有 `--ssh-target` → `ssh <base_opts> <target> kubectl`（**remote：kubectl 在該 node 上跑**）；否則本機 `kubectl`。有 kube-context → 前綴加 `--context <ctx>`。

前置檢查（失敗都寫 `cluster/rook/SKIPPED.txt`；`--allow-skip` 時 return 0，否則 return 2）：

1. **只在 local 模式**檢查本機有無 kubectl（remote 已在探測時確認過；:159-162）。SKIPPED 理由：`kubectl command not found`。
2. namespace probe：`kubectl get namespace <ns>`（2>&1 捕捉）。失敗時用啟發式把 stderr 分類成人話理由（:53-76，nocasematch）：kubectl not found on target / context not found / 無法連 API（connection refused、i/o timeout、deadline、no route、tls handshake timeout…）/ `rook namespace not found: <ns>` / 授權失敗（forbidden、unauthorized、permission denied）/ 通用 `kubectl namespace probe failed for ...`；理由後面附壓平的原始錯誤（換行變 ` | `）。

收集項目（都經 `run_capture`，host 欄固定 `rook`，collector 欄 `collect-cluster-rook`；任一失敗 → return 2 但照跑完）：

| artifact | 指令 |
|---|---|
| `cluster/rook/pods-wide.txt` | `kubectl get pods -n <ns> -o wide` |
| `cluster/rook/events.txt` | `kubectl get events -n <ns> --sort-by=.lastTimestamp` |
| `cluster/rook/rook-resources.yaml` | `kubectl get cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io -n <ns> -o yaml` |
| `cluster/rook/operator.log` | `kubectl logs -n <operator_ns> <pod> --since=<since>`；pod = operator namespace 中 label `app=rook-ceph-operator` 的第一個 pod（`-o name` 取 head -1；:37-44）。找不到 → `operator-SKIPPED.txt`：`rook operator Pod not found in namespace: <ns>` |
| `cluster/rook/toolbox-status.txt` | 只有 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC=1` 才跑 `kubectl exec -n <ns> <toolbox-pod> -- ceph status`；預設寫 `toolbox-SKIPPED.txt`：`kubectl exec disabled by default for operational read-only collection`；開了但找不到 toolbox pod（label `app=rook-ceph-tools`）→ `toolbox-SKIPPED.txt`：`rook toolbox Pod not found`（:197-208） |

此檔可獨立執行（`BASH_SOURCE == $0` guard，:214-216）；collect-node.sh、collect-prometheus.sh 同樣有此 guard。

---

## 11. node 收集

### 11.1 工作機側 pipeline（`collect_remote_node`，run/collect.sh:264-340）

對每台 node：

1. `shell_quote` alias/since/timeout/var_log_max_bytes（單引號包裹；值含 `'` → 直接 return 1 = 該 node 失敗；lib/bundle.sh:81-85）。
2. 把 `lib/common.sh`、`lib/collect-node.sh`、`lib/collect-var-log.sh` 以 `tar | gzip` 從 `$COLLECT_ROOT` 串流進 ssh stdin（:306-310）。macOS bsdtar 需 `--no-xattrs` + `COPYFILE_DISABLE=1` 避免 xattr header 噪音（:300-304）。
3. 遠端單行 script（:280-287）語意：
   - `mktemp -d ${TMPDIR:-/tmp}/ceph-incident-node.XXXXXXXX`；失敗 → stderr `SKIPPED: remote tmp not writable`、**exit 75**。
   - trap EXIT/INT/TERM 刪除該暫存目錄（路徑 pattern 驗證後才 rm）。
   - `gzip -dc | tar -xf -` 解開收到的 lib 檔。
   - 跑 `bash $tmp/lib/collect-node.sh --out $tmp/out --host-alias A --since S --timeout T --var-log-max-bytes N [--skip-logs] [--keep-original-logs]`，記住 rc。
   - 把 `$out` 打包 `tar -cf - -C $out . | gzip -c` 回 stdout（pipe 失敗 → **exit 74**）；`$out` 不存在則造一個只含 `SKIPPED.txt`（`remote collect-node did not create output`）的目錄再打包。
   - `exit $rc`（node collector 的 rc 穿透回 ssh exit code）。
4. 整條 ssh 用 `timeout <node_timeout>` 包（不是 `--timeout`！:291-297）。stdout 存 `workdir/.node-<alias>.tar.gz`。
5. 結果判定：
   - rc 255/124/137 → 寫 ssh-debug（label `node-<alias>`）。
   - rc 124/137（timeout 砍）→ `nodes/<alias>/SKIPPED.txt`：`node collection timed out after <s>s (exit <rc>) from <target>`，刪 tar，return 2。
   - 非空候選檔交給 `accept_node_archive`。它先確認 candidate／destination 都在本次 owned workspace，再把 candidate 複製到 workspace 內無 pathname 的 private snapshot；驗證與 extraction 都只讀同一 snapshot，candidate 替換或原地修改不能改變已驗收的 bytes。
   - Receiver 完整消耗 gzip stream，驗證整張 tar member table、兩個 tar end-of-archive blocks 與其後 zero padding、所有 regular-file payload、name normalization／collision／hierarchy，且只接受 POSIX regular file 與 directory。Root `manifest.jsonl` 必須是 bounded UTF-8 JSONL；每列須符合 `host/collector/artifact/command/exit_code/started/ended` schema、node identity，且 artifact 必須位於 remote `out/` 並對應 archive 內的 regular file。Payload cap 為既有 per-node `/var/log` cap 加 1 GiB；`unlimited` 仍保留 1 TiB 的 archive parser safety ceiling。
   - 驗證全部完成後才建立 `nodes/<alias>/`；逐檔以 exclusive、nofollow 的 regular-file write 解出，目錄／檔案權限由 collector 建立，不沿用 archive ownership、mode、link 或 special member metadata。Extraction failure 只清理本次新建的 node root。
   - 缺 `manifest.jsonl` → 保留既有 incomplete SKIPPED 語意；空、損壞、不安全或超限 archive → 建立只有 `SKIPPED: no usable node archive returned ...` 的 node directory。Rejected archive 的 member 不會寫入 extraction root 或其外部。
   - 刪除候選 tar；return remote rc（node collector 自己的 2 仍會保留已驗收 evidence，只計入 node_failed）。

### 11.2 node collector（`collect_node_main`，lib/collect-node.sh:273-493）

參數：`--out --host-alias --since(24h) --timeout(20) --skip-logs --keep-original-logs --var-log-max-bytes(10 GiB|unlimited)`。out/host-alias 必填；var-log-max-bytes 格式錯 → return 1。

輔助行為：

- `journal_since_arg`：`N[smhdw]` 格式 → 前面加 `-`（`24h` → `-24h`）；否則原樣傳給 `journalctl --since`（:124-131）。
- `heavy_timeout = max(--timeout, 120)`，用於 dmesg / journal 類重指令（:335-338）。
- `node_run_capture`（:26-36）：`run_capture` 包裝，失敗 return 2。
- `node_run_optional`（:38-48）：指令不存在 → artifact 位置寫 `SKIPPED: command not found: <cmd>`、return 0；**指令存在但失敗也 return 0**（`|| return 0`，:47）——optional 指令失敗只留在 manifest/errors.log，不影響 node 成敗。
- `node_run_privileged`（:50-65）：root 直接跑；非 root 有 sudo → `sudo -n <cmd>`；無 sudo → artifact 寫 `SKIPPED: sudo command not found for privileged read: <cmd>`、return 0。
- `node_copy_file`（:150-178）：以 `dd if=<src> iflag=noatime status=none` 讀（root/owner 直接、否則 `sudo -n dd`；測試 env 允許 cat）；寫到 `<dest>.tmp.$$` 再 mv。無法 no-atime read → 失敗（**沒有** cat fallback）。

收集清單（依序）：

**basic（`node_run_capture`，失敗算 node partial）**（:340-360）
| artifact | 指令 |
|---|---|
| `system/hostname.txt` | `hostname` |
| `system/uname.txt` | `uname -a` |
| `system/uptime.txt` | `uptime` |
| `resources/free.txt` | `free -h` |
| `storage/df.txt` | `df -hT` |
| `network/ip-addr.txt` | `ip addr show` |
| `systemd/failed-units.txt` | `systemctl --failed --no-pager --plain` |

**privileged（失敗算 partial，另註明者除外）**
| artifact | 指令 | 備註 |
|---|---|---|
| `storage/lsblk.txt` | `lsblk -a -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` | :362 |
| `kernel/dmesg.txt` | `dmesg -T` | heavy timeout；:365 |
| `time/systemd-timesyncd-journal.txt` | `journalctl --since <since> -u systemd-timesyncd --no-pager` | `\|\| true`（永不算失敗）；:396 |
| `cephadm/cephadm-ls.json` | `cephadm ls --format json-pretty` | 只在 cephadm 指令存在時跑且 `\|\| true`；不存在 → SKIPPED `command not found: cephadm`（:416-420） |
| `cephadm/var-lib-ceph-listing.txt` | `find /var/lib/ceph -maxdepth 3`（prune `*keyring*`/`*private_key*`/`*/.ssh/*`，輸出 `type path` 行） | 目錄不存在 → SKIPPED、return 0；:227-257 |

**optional（`node_run_optional`，失敗不算 partial）**（:368-414）
| artifact | 指令 |
|---|---|
| `systemd/journal-ceph.txt` | `sudo -n journalctl --since <since> -u 'ceph*' --no-pager`（heavy timeout；**存在性檢查檢查的是 `sudo`**，見 §17） |
| `resources/iostat.txt` | `iostat -xz 1 3` |
| `time/chronyc-tracking.txt` | `chronyc tracking` |
| `time/chronyc-sources.txt` | `chronyc sources -v` |
| `time/ntpq-peers.txt` | `ntpq -pn` |
| `time/timedatectl-status.txt` | `timedatectl status` |
| `time/timedatectl-show-timesync.txt` | `timedatectl show-timesync --all` |
| `time/timedatectl-timesync-status.txt` | `timedatectl timesync-status` |
| `time/systemd-timesyncd-status.txt` | `systemctl status systemd-timesyncd --no-pager --plain` |
| `storage/pvs.txt` | `pvs --noheadings --separator ' '` |
| `storage/vgs.txt` | `vgs --noheadings --separator ' '` |
| `storage/lvs.txt` | `lvs --noheadings --separator ' '` |
| `containers/podman-ps.txt` | `podman ps -a` |
| `containers/docker-ps.txt` | `docker ps -a` |

**檔案複製**
- `system/{os-release,hosts,resolv.conf}` ← `/etc/` 對應檔（存在且可讀才複製；複製失敗算 partial；:180-190）。
- `time/systemd-timesyncd-config/timesyncd.conf` 與 `timesyncd.conf.d/*.conf`（maxdepth 1）；一個都沒複製到 → 該目錄下 `SKIPPED.txt`（:192-225）。
- `cephadm/var-lib-ceph-configs/<相對路徑>` ← `/var/lib/ceph` maxdepth 4 內的 `ceph.conf`/`*.conf`/`config`/`*.config`（同樣 prune keyring/private_key/.ssh；:259-268）。

**/var/log 與 journal**（:429-487）
- `--skip-logs` → 只寫 `logs/var-log/SKIPPED.txt`：`log collection disabled by --skip-logs`。
- 否則跑 `collect_var_logs <root> <out>/logs/var-log <max_bytes> <keep_originals>`（§12）；非 0 算 partial。
- journal 全文（`journalctl --since <since> --no-pager` → `logs/var-log/journal-all-since.txt`）：
  - `unlimited`：直接 privileged 收（heavy timeout）。
  - 已有 `OVER-LIMIT.txt` → journal 改寫 SKIPPED（`not collected because /var/log payload exceeded the per-node cap`）、partial。
  - 有 `PAYLOAD-BYTES.txt` → journal 上限 = `max_bytes - payload`（下限 0），經 `node_run_privileged_bounded`（:67-122）：輸出過 `head -c limit+1`，超限 → 刪暫存、manifest 記 **exit_code 75**、return 3 → 呼叫端刪除 merged/raw/original 與 PAYLOAD-BYTES，寫 `OVER-LIMIT.txt`（`status=not-collected-journal-exceeded-remaining-cap`）與 journal SKIPPED（`combined /var/log and journal text exceeded the per-node cap`）、partial。成功則 payload 累加 journal 大小再驗一次總上限，超限同樣全刪（`status=not-collected-combined-payload-exceeded-cap`）；未超限 → 更新 `PAYLOAD-BYTES.txt`。
  - var_log 失敗且無 accounting → journal SKIPPED（`/var/log payload accounting was unavailable`）、partial。

任何 partial → collect_node_main return 2（穿透為 ssh exit 2 → 工作機記 node failed，但 artifacts 保留）。

---

## 12. /var/log collector（`collect_var_logs ROOT OUT MAX_BYTES KEEP_ORIGINALS`，lib/collect-var-log.sh:300-672）

原則：只讀不寫 ROOT；讀取一律 `dd iflag=noatime,nofollow`（必要時 `sudo -n`；無法 no-atime read → 該檔 read-failed，**不退回一般 cat**；:35-56）。回傳 0=完整、2=partial 或 not-collected、1=參數錯誤。

### 12.1 前置與掃描

1. ROOT 不是目錄 → `OUT/SKIPPED.txt`、return 0（:328-331）。
2. 寫 `INDEX.tsv` header：`source family codec stored_bytes decoded_bytes disposition detail`（tab 分隔；:334）。
3. 剩餘空間檢查（`df -Pk OUT`）：可用 < `reserve(1GiB) + scan_limit(64MiB)` → `INSUFFICIENT-SPACE.txt`（available/reserve/scan_staging/status=not-collected）、return 2（:340-350）。
4. `find ROOT -type f -print0`（root/sudo；:58-67），輸出經 `head -c scan_limit+1` staging；超過 → `SCAN-LIMIT.txt`（`status=not-collected-metadata-limit`）、return 2；find 自身失敗 → partial + `ERRORS.tsv` 記 scan 錯誤（:352-370）。
5. 逐檔處理超過 `entry_limit`（100000）→ 刪除所有輸出、`SCAN-LIMIT.txt`（max_entries）、return 2（:372-377、475-481）。

### 12.2 逐檔分類（:372-472），每檔一行寫進 INDEX.tsv

依序判定（先中先贏）：

1. **sensitive path**（大小寫不敏感；含 `keyring`、`.ssh`、`id_ed25519`、`private_key`，或副檔名 `.pem`/`.key`/`.crt`/`.pfx`/`.p12`（含 `.pem.*` 等變體））→ 記進 `SKIPPED-sensitive.txt`，INDEX disposition `skipped-sensitive`，完全不讀內容（:92-106、382-387）。
2. 之前已有檔案觸發 over-limit → disposition `not-inspected`（:389-392）。
3. **opaque archive**（`.zip`、`.tar`、`.tgz`、`.tbz`、`.tbz2`、`.txz`、`.tzst`、`.tar.{gz,xz,bz2,zst}`）→ raw 保留候選，記 `UNREDACTED-OPAQUE.txt`，disposition `raw`（detail `not auto-merged`）（:108-121、395-404）。
4. codec 由副檔名判定（`.gz`/`.xz`/`.bz2`/`.zst`/其他=plain；:123-135）；壓縮 codec 對應工具（gzip/xz/bzip2/zstd）不存在 → raw 保留、`ERRORS.tsv` 記 `missing-codec:<tool>`、disposition `raw-partial`、partial（:406-417）。
5. 量測解壓後大小（有上限時經 `head -c max+1` bounded；:255-275）。超上限 → 設全域 over_limit、disposition `over-limit`；量測失敗 → raw 保留、`decode-failed`、partial。
6. **文字判定**：整條 stream 解壓後檢查是否含 NUL byte（用 FIFO 同時算 total 與去 NUL bytes，兩者相等即文字；任何管線環節失敗算非文字；:179-208）。非文字 → raw 保留、記 UNREDACTED-OPAQUE、disposition `raw`（`binary or unknown`）。
7. 文字 → merge candidate，disposition `merge-candidate`（detail `oldest-to-newest`）。預估輸出 += 解壓大小 + 256（header 預留）；keep_originals 時再 += 原始大小。

### 12.3 family 與排序（:137-177）

去掉壓縮副檔名後的檔名 stem 依 pattern 分 family 與排序鍵（同目錄同 family 合併）：

- `stem.N`（數字輪替）→ family=stem；N ≤ 9 位數 → key = `"1" + %09d(999999999-N)`（N 越大 key 越小 = 越舊越前）；超長 N → key = `0-oversize-N`。
- `stem-YYYYMMDD` 或 `stem-YYYY-MM-DD` → family=stem，key = `"0"+日期數字`（日期舊的在前）。
- 其他（active 檔）→ key = `900000000000`（排最後 = 最新）。

排序：`LC_ALL=C sort -k1,1n(gid) -k2,2(key 字串) -k3,3n(掃描序)`（:541）。

### 12.4 輸出

- 預估總量 > max 或任何檔 over-limit → 刪 merged/original/raw、寫 `OVER-LIMIT.txt`（estimated_output_bytes/max_bytes/status=not-collected）、return 2（:483-489）。
- 第二次剩餘空間檢查（估計輸出 + reserve）→ `INSUFFICIENT-SPACE.txt`、return 2（:491-501）。
- raw 檔逐一 bounded copy 到 `OUT/raw/<相對路徑>`（共享剩餘 budget；超硬上限 → hard_cap_hit；讀失敗 → `read-failed` 記 ERRORS.tsv；:503-520）。
- 合併：每個 family 寫到 collision-free tree `OUT/merged/tree[/dirs/<dir>...]/files/<family>.merged`（目錄層放 `dirs/`、檔案層放 `files/`，避免 `foo.1` 與 `foo.merged/bar` 相撞；:210-219、551-554）。每個來源段落前有 header 行：`===== source=<rel> mtime_epoch=<m> stored_bytes=<s> codec=<c> =====`，段落後補一個換行（:556-605）。所有寫入計入 budget；超過 → hard cap。
- 合併中單一來源讀失敗 → 以 truncate 回滾該段落、記 `merge-read-failed`、改存 raw、partial（:583-604）。
- 合併後重新 stat 來源，大小或 mtime 改變：plain active 檔（key=900000000000）→ 只記 `WARNINGS.tsv` `active-log-changed-during-collection`；其他 → `changed-during-collection` 記 ERRORS.tsv、partial（:607-617）。
- keep_originals=1 → 來源 bounded copy 到 `OUT/original/<rel>`（:618-634）。
- hard cap 命中 → 刪 merged/raw/original、`OVER-LIMIT.txt`（`status=discarded-after-streaming-hard-cap`）、return 2（:641-647）。
- 最終實測 merged+raw+original 總 bytes；仍超限 → 全刪、`OVER-LIMIT.txt`（`status=discarded-after-hard-cap`）、return 2；否則寫進 `PAYLOAD-BYTES.txt`（:649-663）。
- 空的 `ERRORS.tsv`/`WARNINGS.tsv`/`UNREDACTED-OPAQUE.txt`/`SKIPPED-sensitive.txt` 刪除（:666-669）。partial → return 2。

### 12.5 工作機側 post-redaction cap（`enforce_node_log_caps`，lib/bundle.sh:255-288）

redaction 之後（redaction 可能改變大小）對每台 node 重算 `merged/ raw/ original/ + journal-all-since.txt` 總量；超過 `--var-log-max-bytes`（unlimited 直接跳過）→ 刪三個目錄、journal-all-since.txt 改寫成 SKIPPED（`not collected because post-redaction node log payload exceeded the per-node cap`）、刪 PAYLOAD-BYTES、寫 `OVER-LIMIT.txt`（actual_payload_bytes/max_bytes/status=not-collected-post-redaction-cap）、errors.log 記一筆、rc=2。未超且有 PAYLOAD-BYTES → 更新為實際值。

---

## 13. Redaction（lib/common.sh:154-249；lib/bundle.sh:228-253）

### 13.1 範圍（`redact_bundle_text`）

對 `workdir/cluster` 與 `workdir/nodes` 下、檔名符合 `*.txt|*.log|*.log.*|*.merged|*.yaml|*.json|*.jsonl|*.conf|config|*.gz|*.xz|*.bz2|*.zst` 的檔案就地遮蔽。**排除**：`cluster/prometheus/*/*.json.gz`（per-metric 數值 dump，故意不 redact）與 `nodes/*/logs/var-log/raw/*`（opaque 原檔；:248-251）。頂層檔（summary、environment、manifest、errors.log、ssh-debug/）**不在**遮蔽範圍。

### 13.2 規則（`redact_file`，行為單位 = 整行換成 `[REDACTED]`，nocasematch）

命中條件（:172-188）：

1. PEM private key 區塊：`-----BEGIN ... PRIVATE KEY-----` 起至 `-----END ... PRIVATE KEY-----` 止的**整段**。
2. 行含 `password`、`secret`、`token`、`keyring`、`private[ _-]key`（大小寫不敏感）。
3. 行匹配 `(^|非英數)key\s*[:=]`（如 ceph `key = AQ...`）。
4. 行含 base64 樣式 `[A-Za-z0-9+/]{38,}={1,2}`。

每檔在 `redactions.log` 記 `path: N line(s) redacted`；保留原檔 permission mode（:198-201）。

### 13.3 壓縮檔（`redact_compressed_file`）

`.gz/.xz/.bz2/.zst` 解壓 → redact → 重壓回原檔。**解壓失敗 → 檔案原樣保留、redactions.log 記 `... decompress failed, left as-is (NOT redACTED)`、return 0（不算錯誤）**；重壓失敗 → 原檔保留、記 log、return 1（→ 整體 redaction rc=2）（:221-244）。

---

## 14. `run_capture` 與 artifact/manifest 格式（lib/common.sh:140-152、251-307）

每個被捕捉的指令：

- artifact 檔頭三～四行註解：`# host: <h>`、`# collector: <c>`、`# started: <UTC>`、`# timeout: <COMMAND_TIMEOUT>s`（無 timeout binary 時 `# timeout: unavailable`）。之後是指令 stdout+stderr 合流。
- 指令被 `timeout $COMMAND_TIMEOUT`（預設 20）包住；exit 124/137 時檔尾補 `# TRUNCATED: command timed out after <s>s (exit <rc>)`（:292-294）。
- 先寫入同目錄 mktemp 暫存檔再 `mv`（不會留半寫檔）。
- `manifest.jsonl` 追加一行 JSON：`{"host":…,"collector":…,"artifact":<絕對路徑>,"command":<%q quoted 字串>,"exit_code":N,"started":…,"ended":…}`（自製 escape：`\ " \n \r \t`；:130-152）。exit_code 必須是數字，否則 die。
- 非 0 且設了 `ERROR_LOG` → 追加 `<ended> host=<h> collector=<c> artifact=<a> exit=<rc> command=<cmd>`（:300-304）。
- cluster 層寫 workdir 根的 `manifest.jsonl`/`errors.log`；node 層寫 node 自己 out 目錄下的（打包後成為 `nodes/<alias>/manifest.jsonl`、`nodes/<alias>/errors.log`）。

`CONTENTS.md`（lib/bundle.sh:169-201）由 manifest 生成：頂層檔案說明 + cluster 表格 + 每個 node 一段表格（`| exit | file | command |`）；node 的 artifact 路徑把遠端 `/tmp/…/out/` 前綴轉成 `nodes/<alias>/`（:152-158）；缺 node manifest 時寫 `Not collected — see nodes/<alias>/SKIPPED.txt`；有 var-log INDEX 時補一行指引。

---

## 15. Prometheus collector（lib/collect-prometheus.sh:123-327）

前置：URL 去尾端 `/`；`--since` 須可解析為秒（`N[smhdw]?`，0 拒絕）否則 return 1；工作機須有 `curl` 與 `python3`，缺 → `cluster/prometheus/SKIPPED.txt`（`<cmd> not found on this workstation`）+ errors.log、return 2（:143-157）。時間窗 = `[now-window, now]`（epoch）；step 未給 → `max(15, ceil(window/10000))`；預算 deadline = `SECONDS + budget`（:160-163）。

URL 中 `user:pass@` 在寫入任何 artifact/錯誤訊息前遮蔽為 `user:***@`（:51-60）。curl 統一 `curl -fsS -G --connect-timeout T --max-time T -o FILE`，額外參數走 `--data-urlencode`（:82-92）。原始 JSON 直接由 curl 寫檔（**不經 run_capture**，避免 header 汙染 JSON），manifest 由本 collector 自行 `manifest_add`（host=`prometheus`，command 記 `GET <masked-url>/...`）。

流程：

1. `GET /api/v1/status/buildinfo` → `buildinfo.json`；失敗（兼作連通性探測）→ 刪檔、SKIPPED（`prometheus not reachable: <masked> (curl exit N: detail)`）、return 2（:171-180）。
2. `GET /api/v1/targets` → `targets.json`；失敗只記 errors.log + failed=1（照樣繼續）（:182-191）。
3. `GET /api/v1/label/job/values` 列 scrape jobs；失敗或 JSON 非 `status=success` → SKIPPED（`prometheus job listing failed ...`）、return 2。job 名過 `grep -qiE <job_regex>` 過濾；名字含 `"` 或 `\` → 記 `prometheus job skipped (unsafe name)`、failed=1、跳過；沒半個 match → SKIPPED（`no scrape job matched regex '<re>' (jobs seen: ...)`）、return 2（:196-228）。
4. 每個 matched job（目錄名 = safe 化 job 名）：
   - `GET /api/v1/label/__name__/values match[]={job="<job>"} start end` 列 metrics；失敗 → job 的 `index.txt` 記 `FAILED: metric listing for job <job>`、manifest exit 2、跳下一個 job（:245-257）。
   - 每個 metric：超過預算 → `index.txt` 記 `TRUNCATED: budget <s>s exceeded`、整個 dump 停止（:265-272）；metric 名不符 `^[a-zA-Z_:][a-zA-Z0-9_:]*$` → `skipped <m> unsafe-name`（:273-279）；否則 `GET /api/v1/query_range query={__name__="<m>",job="<j>"} start end step` 存 `<jobdir>/<metric（: 換成 __）>.json`，成功判定 = curl 0 且檔案前 512 bytes 含 `"status":"success"`；成功 → `gzip -f` 成 `.json.gz`、index 記 `ok <m> <file>.gz`；失敗 → 刪檔、index 記 `failed <m> -`（:281-296）。
   - 每個 job 的 index.txt 也 manifest_add 一筆（帶 job_rc）。
5. 寫 `dump-info.txt`（url/since/window start・end epoch+UTC/step_seconds/job_regex/jobs_seen/jobs_matched/metrics_ok/metrics_failed/truncated；:304-318），並 append `prom_url=`、`prom_jobs=` 到 `environment.txt`（:320-323）。
6. 有任何 failed → return 2，否則 0。

---

## 16. 中斷、清理、workdir 生命週期

- EXIT trap `cleanup_workdir`（lib/bundle.sh:205-215）：`CLEANUP_KEEP=1`（`--keep-workdir` 或 verify 失敗）→ stderr 印 `kept workdir: <path>` 並保留；否則 `rm -rf` workdir。回傳原 exit code。
- INT/TERM trap `on_interrupt`（lib/bundle.sh:220-225）：解除所有 trap、stderr 印 `interrupted — stopping and cleaning up…`、跑 cleanup、**exit 130**。這是必要行為：沒有它 bash 會在 Ctrl-C 後繼續跑下一台 node。
- 遠端 node 暫存目錄由遠端自己的 trap 清理（run/collect.sh:280）。

---

## 17. verify-bundle（lib/verify-bundle.sh）

用法：`verify-bundle.sh <bundle-dir|bundle.tar.gz>`，參數數量不是 1 → usage(stderr)+exit 1。失敗訊息格式 `VERIFY FAIL: <原因>`（stderr）；成功 stdout 印 `VERIFY PASS: <path>`。

檢查（目錄樹，:96-103）：

1. **成員檢查**（`find -print0` 防換行走私）：不可有 symlink；路徑不可含 `keyring`、`.ssh`、`id_ed25519`、`private_key`，或以 `.pem/.key/.crt/.pfx/.p12` 結尾（:20-37）。
2. **內容檢查**：任何檔案殘留 `-----BEGIN ... PRIVATE KEY-----` 或 `^\s*key\s*=\s*[A-Za-z0-9+/]{20,}={0,2}` → fail（:57-66）。
3. **必要頂層檔**：`manifest.jsonl`、`summary.txt`、`README-FIRST.txt`（:68-78）。
4. **必要 artifacts**：`cluster/` 與 `nodes/` 下各至少一個檔案（SKIPPED.txt 也算；:80-94）。

tar.gz 額外：副檔名須 `.tar.gz`；`tar -tzf` 需成功；archive member 不可為絕對路徑或含 `..`；不可含 symlink/hardlink member（`tar -tvzf` 首字元 `l`/`h`）；再解到暫存目錄跑目錄樹檢查（:105-139）。

---

## 18. Bundle 完整目錄結構

```
ceph-incident-<UTC timestamp>.tar.gz
├── README-FIRST.txt              # 固定內容（lib/bundle.sh:92-102）
├── CONTENTS.md                   # 由 manifest 生成的人讀目錄（§14）
├── summary.txt                   # created_utc/mode/seed/cluster_status/node_ok/node_failed/final_status
├── environment.txt               # created_utc/mode/seed/since/timeout/git_commit
│                                 #  + ceph_source/ceph_runner/rook_source（§7.5）
│                                 #  + prom_url/prom_jobs（有 prom 時，§15）
├── manifest.jsonl                # cluster/prometheus 層每指令一行 JSON（§14）
├── errors.log                    # 非零 exit、probe 失敗、層級失敗（可為空）
├── redactions.log                # 每檔遮蔽行數（--no-redact 時為空檔）
├── ssh-debug/                    # 僅在 SSH 失敗時存在：<label>-<target>.log（§6.4）
├── cluster/
│   ├── ceph/                     # 或 SKIPPED.txt（§7.4）
│   │   ├── json/  (§9.1 的 20 檔 + crash-info/<id>.json ×≤10)
│   │   └── text/  (§9.2 的 4 檔 + crash-info-skip.txt 視情況)
│   ├── rook/                     # 或 SKIPPED.txt
│   │   ├── pods-wide.txt / events.txt / rook-resources.yaml
│   │   ├── operator.log 或 operator-SKIPPED.txt
│   │   └── toolbox-status.txt 或 toolbox-SKIPPED.txt
│   └── prometheus/               # 僅 --prom-url 時；或 SKIPPED.txt
│       ├── buildinfo.json / targets.json / dump-info.txt
│       └── <safe_job>/ index.txt + <metric>.json.gz…
└── nodes/<alias>/                # 每台一個；失敗時只有 SKIPPED.txt
    ├── manifest.jsonl            # 該 node 的指令紀錄（artifact 路徑是遠端暫存路徑）
    ├── errors.log                # 該 node 非零指令（可能不存在）
    ├── system/ {hostname,uname,uptime}.txt + os-release/hosts/resolv.conf
    ├── resources/ free.txt iostat.txt
    ├── storage/ df.txt lsblk.txt pvs.txt vgs.txt lvs.txt
    ├── network/ ip-addr.txt
    ├── kernel/ dmesg.txt
    ├── systemd/ failed-units.txt journal-ceph.txt
    ├── time/ chronyc-*.txt ntpq-peers.txt timedatectl-*.txt
    │        systemd-timesyncd-{status,journal}.txt
    │        systemd-timesyncd-config/{timesyncd.conf, timesyncd.conf.d/*.conf | SKIPPED.txt}
    ├── containers/ podman-ps.txt docker-ps.txt
    ├── cephadm/ cephadm-ls.json var-lib-ceph-listing.txt var-lib-ceph-configs/…
    └── logs/var-log/
        ├── INDEX.tsv             # 永遠存在（除非 --skip-logs / root 缺失）
        ├── PAYLOAD-BYTES.txt     # 成功時
        ├── journal-all-since.txt # 或內容為 SKIPPED 行
        ├── merged/tree/[dirs/<d>/…]files/<family>.merged
        ├── raw/<rel>             # opaque/binary/decode-failed 原檔
        ├── original/<rel>        # 僅 --keep-original-logs
        ├── ERRORS.tsv WARNINGS.tsv UNREDACTED-OPAQUE.txt SKIPPED-sensitive.txt  # 非空才留
        └── OVER-LIMIT.txt / SCAN-LIMIT.txt / INSUFFICIENT-SPACE.txt / SKIPPED.txt  # 各失敗態
```

任何「該收而沒收」的位置以 `SKIPPED: <原因>` 單行檔標示（lib/common.sh:100-110），且較具體的原因永不被通用原因覆蓋（`write_skip_artifact_once`）。

---

## 19. 重寫時容易漏掉的隱藏行為（checklist）

1. **`ceph_runner_for` 以空 stdout 表示失敗**，exit code 永遠 0（run/collect.sh:134-136）。
2. **auto 模式下給了打不通的 `--seed` 不會 fallback**：seed 釘住 ceph_source 後探測迴圈不再找其他 ceph 候選（run/collect.sh:174-177、184）。
3. **`rook_done` 看 `pods-wide.txt` 是否存在**，不是 collector 回傳值（run/collect.sh:232-234）。
4. **`node_run_optional` 吞掉失敗**（`|| return 0`，lib/collect-node.sh:47）：optional 指令失敗不影響 node exit code，只留 manifest/errors 紀錄。
5. **`systemd/journal-ceph.txt` 的存在性檢查檢查的是 `sudo`**（`node_run_optional … sudo -n journalctl …`，command_name=`sudo`；lib/collect-node.sh:368）——node 是 root 且沒裝 sudo 時會被 SKIP，而不是直接跑 journalctl。
6. **exit 75 有兩個不同語意**：遠端 mktemp 失敗（run/collect.sh:280，ssh exit 75）與 journal 超過剩餘 cap（manifest 內 exit_code 75；lib/collect-node.sh:105）。另有遠端 tar/gzip 串流失敗 = exit 74（run/collect.sh:287）。
7. **node tar 解開後若無 `manifest.jsonl` 視為截斷失敗**（run/collect.sh:324-330）。
8. **known_hosts 是「暫存檔 + 使用者檔」兩個路徑塞進同一個 UserKnownHostsFile 選項**，暫存檔於打包前刪除（lib/common.sh:44-46；run/collect.sh:528、639）。
9. **SSH 失敗會觸發第二次 `ssh -vvv` 連線**寫 ssh-debug（會多一次遠端連線副作用；§6.4）。
10. **redaction 排除清單**：prometheus per-metric `.json.gz` 與 var-log `raw/`；壓縮檔解壓失敗「原樣保留且不算錯」，重壓失敗才算錯（§13）。
11. **verify 跑兩次**（workdir 一次、tar.gz 一次）；失敗 → 保留 workdir、刪 tar、summary.txt 以 final_status=1 **重寫**、exit 1（run/collect.sh:643-676）。
12. **`--var-log-max-bytes` 在 redaction 後於工作機再驗一次**，可能把已收到的 node log 整批刪掉（§12.5）。
13. **`--since` 只有配 `--prom-url` 才驗格式**；journalctl 收到的是 `N[smhdw]` 加 `-` 前綴的變形；`/var/log` 檔案完全不受 `--since` 影響；cluster ceph 指令忽略 `since`（§2.2、§9、§11.2）。
14. **dmesg/journal 的 timeout 下限 120s**（heavy_timeout；lib/collect-node.sh:335-338）。
15. **crash info 只取前 10 筆**，crash JSON 解析失敗只寫 skip 檔不算失敗（§9.3）。
16. **工作機沒有 timeout/gtimeout 時所有外層逾時靜默停用**（只印一次警告；run/collect.sh:519-521）。
17. **`--quiet` 是靠環境變數傳遞**（`CEPH_INCIDENT_QUIET=1` export 給所有子層；run/collect.sh:460）。
18. **`--allow-cephadm-shell`/`--allow-kubectl-exec` 的預設值來自環境變數**，flag 與 env 雙向（run/collect.sh:353-354、481-482）。
19. **malformed HOSTS entry 不中止**（記 errors.log + rc=2 繼續）；但 `HOSTS` 全空是 die（§5.3）。
20. **含單引號的 alias/since/timeout 值會讓該 node 收集直接失敗**（shell_quote；lib/bundle.sh:81-85）。
21. **active log 在收集期間變動只是 warning，rotated 檔變動是 partial error**（lib/collect-var-log.sh:607-617）。
22. **`write_skip_artifact_once` 語意**：collector 寫過的具體 SKIPPED 原因不可被 orchestrator 的通用原因覆蓋（lib/common.sh:106-110）。
23. **stdout 純度**：除 `--help` 與最後的 `bundle:` 行外，stdout 不得有任何輸出；所有 artifact 檔頭有 `# host/# collector/# started/# timeout` 註解行、stdout+stderr 合流。
24. **INT/TERM 必須立刻停止整輪收集並 exit 130**（不是只清理然後繼續下一台；lib/bundle.sh:220-225）。
25. **macOS 相容細節**：bsdtar 需 `--no-xattrs` + `COPYFILE_DISABLE=1`；`stat -c`/`stat -f`、`date -r`/`date -d` 雙形式 fallback 散佈各處（run/collect.sh:300-308；lib/collect-var-log.sh:21-33；lib/collect-prometheus.sh:76-78）。
26. **prometheus 原始 JSON 不經 run_capture**（避免 header 汙染），manifest 條目由 collector 手寫，host 欄固定 `prometheus`（§15）。
27. **cluster/rook/prometheus 的 SKIPPED 也算 verify 的「cluster/ 至少一檔」**——verify 只要求 cluster 與 nodes 下各有任一檔案（lib/verify-bundle.sh:80-94）。
