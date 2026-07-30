# Differential Normalizer 合約

> 對應 issue #18（offline observable-contract equivalence gate）。
> 實作：`tests/differential/normalize.py`；情境：`tests/differential/scenarios.py`；
> 執行入口：`make test-differential`（已含在 `make validate`）。

Differential gate 讓 shell reference 與 Python candidate 在**同一個 fake world**
執行同一條 Collect 指令，再比較兩份 normalized observable contract。這份文件是
normalizer「被允許忽略什麼」的唯一依據：清單以外的任何差異都會讓 gate 失敗。

## 共享的執行環境（不是兩套 fixture）

`tests/differential/environment.py` 為每次 run 建出同一個世界，兩個實作拿到完全
相同的：

- fake executables：`tests/differential/fakes/{ssh,kubectl,timeout}` 與共用的
  `tests/fixtures/bin/curl`。fake `ssh` 同時服務兩種 remote command shape
  （shell 串流 `lib/collect-node.sh`、Python 串流 `ceph_incident_node.py`），
  其餘遠端操作（capability probe、Ceph runner probe、Ceph read command、remote
  kubectl）在兩邊逐字相同。
- inventory、SSH key path、`HOME`、`LC_ALL`、`TZ`。
- scenario knobs（capability map、失敗旋鈕、node archive case、sleep）。
- payload limits 與 CLI options：`--timeout`、`--node-timeout` 一律明示，
  避免兩個入口的 default 差異混進比較。
- Node Evidence Archive：由 `tests/differential/fakes/node_archive.py` 產生
  **同一份 bytes**，所以 node 端差異不會污染 workstation contract 的比較。

fake `ssh` 與 fake `kubectl` 都是白名單 adapter：未列入的 remote command、
`cephadm shell`、`kubectl exec`、mutating verb 或多出來的 token 一律 exit 99。

## 被比較的 observable contract

- `exit_code`、`stdout`（`bundle:` 行）、是否產出 archive、是否保留 workdir。
- bundle member 集合（檔案與目錄，排序後比較）。
- 每個 artifact 的內容：capture header（`# host` / `# collector` / `# timeout`）、
  body、JSON 內容、壓縮內容解壓後的 bytes、opaque raw evidence 的 sha256。
- `manifest.jsonl` 與每個 node 的 manifest：host、collector、bundle-relative
  artifact、command argv、exit_code，逐行逐欄比較。
- `summary.txt`、`environment.txt`、`CONTENTS.md`、`README-FIRST.txt`。
- `redactions.log` 的每檔遮蔽結果。
- `errors.log` 的失敗事件集合。
- external command policy：ssh 與 kubectl 的完整 argv ledger、node 請求的
  參數（alias、since、timeout、payload cap、skip-logs、keep-original-logs）。

## 被批准忽略的差異（清單以外都會失敗）

| # | 差異 | 處理方式 | 理由 |
|---|---|---|---|
| 1 | timestamps（ISO8601、epoch、`created_utc`、`started`/`ended`、mtimes） | 值換成 `<timestamp>` / `<epoch>` | 兩次 run 不可能同時；ADR 0006 只要求 observable contract 等價 |
| 2 | random temporary paths（workdir `tmp.*`、node 端 `/tmp/ceph-incident-node.*`、redaction scratch `.<name>.plain.XXXXXX`、invocation id、archive 檔名時間戳） | 換成 `<workdir>` / `<node-tmp>` / 原 artifact 名 / `<identifier>` | 由 mktemp 與 uuid 決定，不是行為 |
| 3 | JSON formatting 與 key order | 解析後比較資料結構 | ADR 0006 明文不要求 serialization byte-identical |
| 4 | shell quoting 風格（`printf %q` 對 `shlex.join`） | manifest / CONTENTS.md / ssh-debug 的 command 一律 `shlex.split` 成 argv 後比較 | 同一條 argv 的兩種書寫；argv 本身仍逐字比較 |
| 5 | tar member order | member 集合排序後比較 | AC 明列 |
| 6 | gzip metadata 與壓縮率 | 比較解壓後內容；`PAYLOAD-BYTES.txt` 只在該 node 的 payload 內含壓縮成員時換成 `<compressor-dependent-bytes>`（其餘情況逐字比較 byte 數） | 重壓縮大小由 compressor 決定；payload 內容仍逐一比較 |
| 7 | 檔案系統走訪順序（`redactions.log`） | 以「檔案 → 遮蔽結果」mapping 比較 | 每一筆與其結果都仍被比較，只有行序被忽略 |
| 8 | `errors.log` 的記錄粒度與交錯 | 以「分類後事件集合」比較 | 同一個失敗兩邊可能寫在不同時點；每個事件類別仍必須雙邊都有 |
| 9 | 人可讀失敗措辭（`SKIPPED*.txt`、`errors.log`、stderr lifecycle） | 只在**認得的措辭**上收斂成已記錄的語意 class；認不出來的字串以 `literal:` 逐字比較 | 沿用 `docs/test-scenario-inventory.md` §11.3「對齊語意、不逐字綁英文措辭」 |
| 10 | progress prose（stderr） | 只比較 lifecycle 事件（interrupted / verify-failed / workdir-kept） | progress 是人機介面；stdout 純淨性與 `--quiet` 由 Python suite 覆蓋 |
| 11 | candidate-only `environment.txt` 欄位（`node_target_*`、`node_invocation_id_*`、`rook_namespace`、`rook_operator_namespace`、`kube_context`） | 從比較中移除，另由 `test_candidate_environment_additions_stay_documented` 檢查只出現已記錄的 key | rewrite plan 明文要求 Python 記錄 node invocation identifier 等額外可觀察性 |

**不被忽略**：artifacts 的存在與內容、exit code 與 status、manifest、command
policy（含 default-off 邊界）、bundle lifecycle（workdir 保留 / archive 產出）、
payload cap 判定、redaction 決策。第 9 項的 fallback 是設計核心：normalizer 只
壓平「已審閱過等價」的措辭，新出現的訊息會以字面比較失敗，不會被靜靜吞掉。

## 這個 gate 覆蓋不到的地方

fake `ssh` 站在 SSH 邊界上，因此 differential run 比較的是**工作機端**的完整
契約；node collector 自己的 evidence surface 不在其中（兩邊的 node payload 由
定義就不同）。node 端的等價性由 `docs/test-scenario-ledger.md` 的 N 系列情境
負責，目前仍有未移植項目，見該文件的 blocked 清單。
