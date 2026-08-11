# Real-Lab Bundle Contract 與 Stable-State Schema

> #20 先用同 lab 的 shell／Python 四路 full collect 建立此 contract；#22
> post-cutover gate 以保存的 #21 shell baseline bundle 對新的一次 Python full collect
> 套用同一份 normalizer。
> 實作：`validation/lab_bundle.py`、`validation/lab_snapshot.py`、
> `validation/lab_contract.py`；執行入口：`make validate-lab`。

`make validate-lab` 先驗證保存的 #21 PASS report、shell bundle hash 與 lab identity，
再於同一個已驗證 lab 跑一次 Python full collect，然後比較兩份 bundle，並比較本次
collect 前後的 stable state。這份文件是比較「比什麼、刻意不比什麼」的唯一依據；
擴大或縮小任一份清單，需要與改變 collector 行為相同等級的 review。

## 為什麼 real-lab 比較不是 byte comparison

Cutover 前的 offline equivalence gate（當時的 `make test-differential`，issue #18，見
[`differential-normalizer.md`](differential-normalizer.md)）曾證明：**同樣的輸入**
進去，兩個實作吐出同樣的 bytes。該 target 隨 shell implementation 一起退役；這份
reviewed normalizer 與保存的 PASS evidence 是它留下的 contract，不代表現行
`make validate` 還會執行 shell。

Real lab 兩者都不是。保存的 shell collect 與本次 Python collect 打在活的 cluster 上，
`ceph -s` 的數字本來就會不同，journal 本來就會多幾行。在那裡要求 evidence bytes
完全相同不是嚴格，而是一道只能靠運氣通過的 gate。

所以 real-lab gate 比較的是「與什麼時候採集無關」的那一部分：

| 比較項目 | 為什麼它是 contract |
| --- | --- |
| member 路徑集合 | 兩個實作必須產出同一組 artifact，放在同一個位置——唯一例外是 `/var/log` payload 樹內的檔案集合，見下方「刻意不比較」 |
| 頂層 `manifest.jsonl` | collector、artifact、完整 command argv 與 exit code — CLI semantics、runner 選擇與 source 選擇都在這裡變成可觀測 |
| 每個 node 的 `manifest.jsonl` | 同上，但只比對「兩邊都宣稱的那一面」，見下節 |
| 每個 captured artifact 的 `# key: value` header | host、collector、timeout 與 truncation 標記——`# timeout` 逐字比對，qualification 工作機因此必須提供 timeout binary（[ADR 0011](adr/0011-require-a-timeout-binary-on-the-qualification-workstation.md)） |
| artifact body 是否解析得出 JSON | 「是不是 JSON」是實作決定的：把 evidence 包裝、截斷或重新序列化的 candidate 會在這裡現形 |
| `environment.txt` 的選擇欄位 | `mode`、`seed`、`since`、`timeout`、`ceph_source`、`ceph_runner`、`rook_source`、`prom_url`、`prom_jobs` |
| `cluster/prometheus/dump-info.txt` 的決策欄位 | `since`、`step_seconds`、`job_regex`、`jobs_matched`、`truncated`；清單中的欄位缺失會明確記成 `None`，不會因文件變短而縮小比較範圍 |
| `summary.txt` | `cluster_status`、`node_ok`、`node_failed`、`final_status` — partial collection 在這裡變成可觀測 |
| SKIPPED／partial artifact 的分類 | 兩邊必須以同一個原因略過同一件事 |
| `errors.log` 的事件分類集合 | 兩邊記錄粒度不同，但事件必須相同 |
| 四條 collector path 的 coverage | Ceph、Rook、Prometheus、全部 inventory nodes、`/var/log` |

Coverage 的 skip 判定同時看檔名與內容。Collector 有時把 `SKIPPED: <reason>` 直接寫進
evidence 原本要佔的那個 artifact——`/var/log` 超過 per-node cap 時就是這樣改寫
`journal-all-since.txt`——所以只看檔名的判定會把一個完全沒收到 log 的 node 算成
covered。

刻意**不**比較的：

- `environment.txt` 的 `git_commit`。Post-cutover bundle 必然來自比保存 baseline 更新的 commit；要求字串相同會讓 cutover proof 永遠失敗。這不是丟掉 code provenance：固定的 #21 authority 同時驗證 baseline report SHA-256、完整 commit 與 shell bundle SHA-256，schema-v3 report 的 `code.commit` 另外記錄且要求本次 checkout clean，兩端都各自被鎖住。
- Captured artifact 的 body 本身，**包含它的 JSON key path**。兩個實作都不「轉換」
  evidence：它們執行一條指令並逐字記錄輸出。Manifest 已經釘住是哪條指令、exit code
  是多少；兩份 manifest 一致，就代表兩個 body 是同一個 cluster 對同一個問題在兩個
  時刻的回答。連 key path 都比會在不是 candidate 造成的事情上失敗——健康時
  `health.checks` 是 `{}`，一出現 slow op 就多一個 key；某個 counter 這次是 `0`
  下次是 `0.5`。會因為一次暫時性 HEALTH_WARN 就失敗的 gate，只會被學會「重跑到過為
  止」，那比一個少比但說得準的 gate 更糟。
- 重壓縮後的 metric dump bytes（任何 `.gz`／`.xz`／`.bz2`／`.zst`）。這些只比對
  「存在與路徑」。
- 超過 4 MiB 的 artifact 內容；同樣只比對存在與路徑。
- `cluster/prometheus/dump-info.txt` 清單外的欄位。`metrics_ok`／
  `metrics_failed` 是兩次 collect 之間會改變的活體 metric 集合；
  `window_*_epoch`／`window_*_utc` 是採集時鐘，實際套用的窗口寬度另由 manifest argv
  normalizer 保留；`jobs_seen` 是 Prometheus 當時的 target 狀態。`url` 已由
  `environment.txt` 的 `prom_url` 比對，不在這裡重複建立第二份 contract。
- `/var/log` payload 樹（`nodes/*/logs/var-log/{merged,raw,original}/`）**連存在與
  路徑都不比**。這棵樹的檔案集合是活體機器的作為，不是實作的：兩次 collect 跨過
  UTC 日界，`sysstat/sa03` 就只在第二次存在；journald 也會在兩次之間輪替、改名它
  的 archived journal（#52 實測，3 + 4 項）。要求「存在與路徑」相同，等於要求兩個
  時刻的 `/var/log` 長得一樣，那與比對 evidence bytes 是同一種錯。
  放寬的界線就是這三棵子樹，一步不多：直接放在 `logs/var-log/` 下的成員——
  `journal-all-since.txt` capture（`sudo -n` 與 `--since` 窗口在它的 argv 上）與
  `INDEX.tsv`——照常逐筆比對；每個 node 的 `/var/log` path 仍受 per-bundle 四路
  coverage 檢查，整棵樹沒收依然會失敗；cluster artifact 與其他 node evidence 的
  member 路徑照常完全比對。

Evidence 處理本身的 byte-level 等價是 offline gate 的職責：那裡的輸入是凍結的，所以
它可以精確比對。

### Collector-authored 文字檔盤點

`cluster/prometheus/<job>/index.txt` 已逐欄檢查，但不選取 body 欄位。它的 metric
名稱與逐筆 `ok`／`failed`／`skipped` 是活體 Prometheus 在該次 collect 回答的集合與
結果；`TRUNCATED` 則已由 `dump-info.txt` 的 `truncated` 決策欄位表達。Gate 仍比較
`index.txt` 的 member 路徑，以及 manifest 中產生它的 argv 與 exit code，所以路徑、
窗口、step 或成功／partial 語意不會因此消失。

其餘同類文件也沒有未承接的穩定決策：`CONTENTS.md` 是 manifests 與 member set 的人讀
投影；`README-FIRST.txt` 是固定說明；`errors.log` 已化約成事件分類集合；
`nodes/*/logs/var-log/INDEX.tsv` 與相鄰的 size／warning／error metadata 描述當時活體
`/var/log` 的來源檔、bytes 與讀取結果。後一組保留 member／manifest／skip／coverage
比較，但不比較會隨 log rotation 和兩次讀取時刻改變的 body。

## Node manifest：只比對兩邊都宣稱的那一面（ADR 0010）

[ADR 0010](adr/0010-manifest-as-evidence-index.md) 已經裁定 node manifest **刻意
diverge**：Python 的 node manifest 是「archive 內全部 evidence 的索引」，shell
reference 只記錄它實際執行過的指令。逐筆比對等於用 gate 推翻已經裁定的 ADR，數字也
說得很清楚——真 lab 上同一台 node 是 26 筆對 248 筆（#52）。

所以 node manifest 比對前先移除 ADR 0010 列舉的那幾類 entry。這幾類只有 Python 會
產生，但判定規則兩邊照跑——`contract_of` 逐份 bundle 化約，不知道自己拿到的是哪一
邊，這樣才不會出現「reference 專用的寬鬆路徑」：

| 移除的 entry | 怎麼認出來 |
| --- | --- |
| 複製類 evidence | `command` 是 index verb `collect-node copy …`，**且** artifact 在 bundle 裡沒有 capture 檔頭 |
| `/var/log` 產出樹 | `command` 是 index verb `collect-var-log /var/log`，**且** artifact 沒有 capture 檔頭 |
| SKIPPED／「證據不在」marker | artifact 在 bundle 裡的內容或檔名是 skip marker，**且** `exit_code` 是 ADR 0010 列的 127（指令不存在）或 2（證據不存在／複製失敗） |

`exit_code` 是那一列的一部分而不是修飾：reference 自己也會寫一種 marker——
`/var/log` 加 journal 超過 per-node cap 時的 `journal-all-since.txt`——而且**有**記
manifest entry，exit code 75。只認 artifact 就會把它一起吃掉，degradation 就從比對
裡消失了。ADR 0010 沒有列 75，所以它留在比對裡。同理 `--skip-logs` 的 marker（exit
0）也不在列舉內，仍逐筆比對；那一項的裁定還掛在 `docs/python-rewrite-plan.md`。

Index verb 的意思是「這份 evidence 沒有任何指令為它執行過」。這句話 manifest 自己
說了不算：artifact 只要帶著 `# host: ` capture 檔頭，就代表真的跑過指令，該筆 entry
留在比對裡。否則任何一筆 entry 只要換上 verb 就能從 gate 消失。

上表**三列都要拿 entry 去問 bundle**，所以 entry 記的 artifact 路徑必須先解析成
bundle 裡的 member 路徑：node manifest 記的是 evidence 在 node 上的絕對路徑
`<workspace>/out/<relative>`（shell reference 以 `--out "$tmp/out"` 呼叫 node
collector，Python candidate 自己算 `workspace / "out"`——旗標不同，`out/` 這段相同），
打包後同一份 evidence 是 `nodes/<alias>/<relative>`，所以 workspace 與 `out/` 兩段都
要脫掉。少脫一段問到的就是一個不存在的 member，而 bundle 對不存在的 member 只會回答
「不是 skip marker」「沒有 capture 檔頭」——剛好是讓三列**全部永遠不成立**的答案。
#52 的真 lab 上就是這樣讓 13 筆 entry 溜過整條化約，而離線測試全綠，因為 fixture 寫
的 artifact 沒有真實收集器會加的那段 `out/`。

**`logs/var-log/` 不是整包排除。** 那棵樹裡的 `journal-all-since.txt` 是兩邊都執行
並記錄的 capture，`sudo -n` 與 `--since` 時間窗都在它的 argv 上，所以它照常逐筆比
對；被移除的是那棵樹其餘由 `collect-var-log` 索引起來的產出。

`/var/lib/ceph` listing 是另一種情況：這台有 listing 時兩邊都會記，但記法不同。
Python 記 ADR 0010 的穩定 verb `collect-node list <dir>`；shell 記真正的 `find`
argv，而那串 expression 裡有 `*keyring*`，會被 content safety 整行遮成
`[REDACTED]`。同一件事的兩種記法，因此各自收斂成「這台有沒有記到 listing」一個事實
——ADR 0010 早已把這筆的 command policy 交給 N9 的 argv ledger 斷言。收斂是**認
artifact 而不是認 verb**，所以 #44 把 content safety 移掉、shell 的 `find` entry 不
再被遮之後，這條規則不會反過來讓 gate 誤判。

**順序是規則的一部分**：上表三列先判，判掉的不會走到這條收斂。`/var/lib/ceph` 不存
在的 node 上兩邊都寫同一份 SKIPPED marker，只有 candidate 會把那份 marker 也編進索
引；那筆 entry 是第三列的 marker index，就在那裡被移除。先判收斂的話，candidate 會
因為「有一筆 entry 指到 listing artifact」而宣稱這台記到了 listing，reference 宣稱沒
有——兩邊寫出一模一樣的證據，卻被 gate 講成有分歧。#52 的 k8s node 就是這樣被誤報
的。反過來，真的只有一邊記到 listing 仍然是差異，照報。

被遮蔽的那一行另外算：**只有**該 node 的 listing 真的在 bundle 裡（且不是 skip）、
且這份 manifest 自己沒有任何一筆 entry 收斂成 listing 時，才會去吃被遮蔽的行，而且
**只吃掉一行**。第二行 `[REDACTED]` 是這裡沒有解釋的遮蔽，會原樣留在比對裡讓
gate 失敗。已知代價：一行 `[REDACTED]` 本身不帶任何資訊，所以在 listing artifact 存
在的前提下，「被遮蔽的 listing entry」與「少索引了 listing 又剛好有一行別的被遮蔽」
這兩種情況分不出來。

沒有落入上表任何一類的 entry 一律留在比對裡。放寬只到 node manifest 為止：頂層
`manifest.jsonl` 仍然逐筆比對，cluster artifact 的 exit code、skip 分類與
source／runner 選擇也都不受影響。

## Normalizer 允許忽略的差異（完整清單）

`validation/lab_bundle.py` 的 `_default_substitutions()`，加上 `_argv()` 對 argv 逐
筆做的 query-window 改寫。每一條都是時鐘或亂數：

**與離線 gate 的差異是刻意的。** `docs/differential-normalizer.md` 規則 1a 把任何被
記錄的請求裡 `start=`／`end=`／`time=` 的值整個換成 `<epoch>`，窗口寬度一起丟；下表
那一條保留寬度，比 1a **更嚴**。兩份文件並排讀不要誤以為其中一邊是 bug——理由記在該
列的「理由」欄。

| 規則 | 理由 |
| --- | --- |
| `ceph-incident-<YYYYMMDDTHHMMSSZ>` → `ceph-incident-<stamp>` | bundle 檔名帶採集時刻 |
| ISO-8601 timestamp → `<timestamp>` | 採集時刻 |
| `.../ceph-incident-node[.-]<suffix>` → `<node-workspace>` | 遠端 workspace 的 mktemp 後綴／invocation id |
| `<dir>/.<name>.{plain,encoded}.<random>` → `<dir>/<name>` | redaction 暫存檔指向同一個 artifact |
| `.../tmp.<random>[.<pid>]` → `<workdir>` | 工作機暫存目錄。兩邊命名法不同——reference 是 `tmp.<stamp>.$$`，candidate 是 `mkdtemp` 的後綴（字母表含 `_`）——這條規則必須兩種都吃完整，只吃掉一半會留下 `<workdir>.61493` 這種尾巴（#52） |
| `start=<epoch>`／`end=<epoch>` → `start=<epoch-Ns>`／`end=<epoch>`，**僅限 artifact 落在 `cluster/prometheus/` 的 entry** | Prometheus query window 的兩端是採集起始時刻算出來的 epoch，兩次 run 必然不同。**窗口寬度 `N` 保留**：`dump-info.txt` 的 selected `since` 記錄 collector 宣告的 Evidence Window；manifest argv 的 `end - start` 則獨立證明它實際套用的秒數。兩者都要保留，才看得見「宣告 24h 卻查 12h」。已知代價是窗口的絕對錨點不再可觀測：寬度對、但把 `end` 算在錯誤時間基準上的 candidate 會靜默通過 |
| 32 位 hex → `<invocation>` | node invocation identifier |
| 各自的 `--out` 目錄與 run directory → `<bundle>`／`<run>` | 保存的 shell bundle 與本次 Python run 依定義位於不同目錄 |

另外，`environment.txt` 只比對上表列出的選擇欄位。`created_utc` 是時鐘；
`node_target_*`、`node_invocation_id_*`、`rook_namespace`、
`rook_operator_namespace`、`kube_context` 是 rewrite 宣告過的 candidate-only 可觀
測性（#11、#14），已記錄在 `docs/differential-normalizer.md`。清單以外的任何欄位
差異都會讓 gate 失敗。

## Bundle 讀取是不信任的

Bundle 是本次 collect 產生的，但「我們做的」不是信任理由。`read_bundle()` 逐一
檢查 member，遇到 link、device/FIFO 等特殊 member、absolute path 或 traversal 就
直接 fail closed，而且**從不解壓到磁碟**；只有參與比較的 artifact 會被讀進記憶
體，並有單檔上限。

## Stable-State Snapshot Schema（version 1）

本次 live Python collect 之前取一次、之後取一次，兩者必須完全相同。Snapshot 只能包含
**stable identity 與 desired configuration**；whitelist 就是重點——列舉保留欄位，
新版本新增的易變欄位不會突然讓 gate 失敗，而真正的 desired-state 變動也無法躲在
沒人列舉的欄位裡。

| 欄位 | 來源（唯讀） | 保留 | 排除 |
| --- | --- | --- | --- |
| `ceph_monitors` | `ceph mon dump --format json` | `fsid`、每個 mon 的 `name`／`rank`／`public_addr` | `epoch`、`modified`、`created`、`election_epoch`、`quorum`、feature bitmap |
| `ceph_crush_topology` | `ceph osd tree --format json` | `id`、`name`、`type`、`device_class`、`crush_weight`、`children` | `status`、`reweight`、`exists`、`primary_affinity` |
| `ceph_pools` | `ceph osd pool ls detail --format json` | `pool_id`、`pool_name`、`type`、`size`、`min_size`、`pg_num`、`pg_num_target`、`crush_rule`、`erasure_code_profile`、application 名稱 | `last_change`、`last_force_op_resend*`、所有使用量統計 |
| `ceph_config` | `ceph config dump --format json` | `section`、`name`、`value` | `level`、`can_update_at_runtime`、`mask` |
| `rook_cephclusters` | `kubectl -n <ns> get cephclusters.ceph.rook.io -o json` | `metadata.name`／`namespace`、整份 `spec` | `status`、`resourceVersion`、`generation` |
| `k8s_{deployments,statefulsets,daemonsets}_<ns>` | `kubectl -n <ns> get <resource> -o json` | `kind`、`name`、`namespace`、`spec.replicas`、container images | `status`、`resourceVersion`、`generation`、annotation、`creationTimestamp` |

其他規則：

- Ceph 只用 direct read-only CLI（`ceph`，必要時 `sudo -n ceph`）。`cephadm shell`
  永遠不是 fallback，因為它可能啟動 container。
- Kubernetes 只用本機 `kubectl get`；沒有 `exec`，沒有寫入 verb。
- Profile 的 `operator_namespace` 與 `namespace` 不同時，才會多讀一組 workload。
- Ceph 與 Kubernetes 回傳的集合本身無序，所以每個 projection 以自身 canonical
  form 排序：同一組物件換個順序不是變動。
- 任何一個來源讀不到就 fail closed。讀不到不等於沒有變動；一份殘缺的 snapshot 會
  和另一份殘缺的 snapshot 比對成功，那正是這個 gate 不能容許的事。

## Remote Residue

本次 live Python collect 前、後，各對每個 inventory node 取一次
`${TMPDIR:-/tmp}/ceph-incident-node.*`、`ceph-incident-node-*` 的列表與 helper
process 列表。**只有期間新出現的**才算本次 run 的殘留；run 之前就存在的會被如實
報告為 pre-existing，但不歸咎於本次 run。

Probe 只讀：不刪除 workspace，不對 process 送 signal。能「清乾淨讓檢查通過」的
residue check 就不是 residue check；runbook 要求殘留必須送到人手上。

Probe script 的兩個細節是刻意的：開頭的 sentinel 註解讓 offline 測試的 fake
`ssh` 認得它；兩個 process marker 在 script 內以變數拼接，因為 script 自己的文字
會出現在 node 的 `ps` 輸出裡，直接寫出 marker 會讓每次 probe 都把自己回報成殘留。
