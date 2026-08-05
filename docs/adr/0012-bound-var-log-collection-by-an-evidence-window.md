# 以 Evidence Window 界定 /var/log 的收集範圍

`--since` 一直只餵給 `journalctl`。`/var/log` 的收集沒有任何時間參數——var-log collector 的介面是 `collect_var_logs ROOT OUT MAX_BYTES KEEP_ORIGINALS`，唯一的界線是位元組上限。結果是整棵 `/var/log` 被收走，輪替的 `.gz` 解壓合併，binary systemd journal 原樣複製。

#21 第一次在真 lab 完整執行時（run `20260804T233733Z`，七台 node，`--since 24h`）量到的規模：22.46 GiB，其中 15.32 GiB 是 522 個 binary journal 檔，7.12 GiB 是 merge candidate。單看 mon-02，syslog 家族 3,030 MiB 裡只有 134.8 MiB 是當前檔，其餘是最遠回溯到一個月前的輪替；binary journal 從檔名解碼出的時間範圍橫跨 2026-05-17 到 08-04，將近三個月，而同一份資料在 24 小時窗口內的文字萃取 `journal-all-since.txt` 只有 43 MiB。

也就是說：操作人員給了 `--since 24h`，卻拿到數週到數月的證據。這同時傷害傳輸、磁碟、redaction 與 bundle 體積，而且沒有任何一份產物說明多收了什麼。

裁定（2026-08-05，#59 的後續）：**`--since` 的語意提升為 Evidence Window，每條 collector path 以自己的資料所能達到的精度遵守它。** 能逐筆過濾的取窗口內的記錄；只能整檔取捨的取窗口內的檔案，外加跨越窗口起點的那一個最新檔。

選擇機制時否決了三個替代方案：

- **逐行時間戳過濾**要解析任意 log 格式的時間戳（syslog RFC3164／RFC5424、Ceph 自有格式、pod log 的 RFC3339，還有不帶時間戳的行），解析失敗時的取捨無法定義，而且會破壞 merged 檔逐位元組保留原內容的性質。
- **輪替數上限**跨 node 語意不一致：輪替節奏由各家 `logrotate` 與 `SystemMaxFileSize` 決定，「保留兩個輪替」在不同機器上代表完全不同的時間跨度，與事故窗口沒有對應關係。
- **調小既有的位元組上限**在真 lab 從未觸發（七台全部遠低於 10 GiB 預設），而且用體積裁切會在不同 node 上砍掉不同時間範圍的證據，事後無法解釋。

**跨界規則是正確性所必需，不是最佳化。** 輪替檔的 mtime 是輪替發生的時刻，不是內容的時間範圍；當前檔的內容從上次輪替起算。若輪替剛好發生在 collect 前不久，窗口內的證據會整批落在最新的那個輪替檔裡。少了跨界規則，事故證據會在最不該消失的時候消失。代價是實際涵蓋一定比 `--since` 寬——mon-02 用 `--since 24h` 實際保留五天以上的 syslog——這是檔案層級取捨的本質，必須被記錄而不是被消除。

**Binary journal 套用同一條規則，不整批丟棄。** `journal-all-since.txt` 已經是窗口內的文字萃取，丟掉 binary 可以省下 bundle 的 68%。但 `journalctl` 的結構化查詢（依 unit、priority、boot、`_PID`）只能對 binary 格式跑，文字萃取檔答不出「這台在該次 boot 期間所有 priority ≤ 3 的記錄」這類問題。那是真實的鑑識能力，值得保留——但只需要保留窗口內的。

**Evidence Window 只接受 `N[smhdw]` 文法。** 檔案選取需要 epoch 來與 mtime 比較，而 `yesterday`、`2026-08-01 10:00` 這類 `journalctl` 吃得下的自由格式要轉 epoch 得靠 GNU `date -d`；BSD `date` 語法不同，這是 ADR 0011 同一類的工作機差異問題。用 `python3` 解析可以繞過，但那會讓 shell reference 與 Python candidate 共用同一個日期解析器，解析錯誤在兩側同時發生，differential gate 就看不見選檔錯誤——gate 的獨立性被掏空。改為只接受既有的嚴格 duration 文法：epoch 是 collect 起始時刻減去 duration 的純算術，兩側可各自獨立實作而必然同意。這對 Full Collect 零成本，因為那條路徑本來就因 `--prom-url` 而受同一文法限制（`docs/behavior-contract.md` §旗標表）。

## Consequences

- `--since` 的語意由「journalctl 的參數」提升為 **Evidence Window**，定義記在 `CONTEXT.md`。`Full Collect` 的定義同時改寫為「四條 collector path 全部成功走完、沒有一條 partial／missing／skipped」，明確與「evidence 收到底」脫鉤——後者從來就不是實情，位元組上限與 journal 的 `--since` 一直都在裁切。#21 的 acceptance criteria 不受影響。
- `INDEX.tsv` 增加來源 mtime 欄位與 `outside-window` disposition；被窗口排除的來源仍然列在 index 裡。這是本次唯一的 observable contract 變更，shell reference 與 Python candidate 必須同步實作，differential gate 與 behavior contract 的 var-log 章節一併更新。依 ADR 0010 的立場，index 是 evidence index——存在於 node 上但沒被收的檔案本身就是關於這次 collect 的證據，剔除它等於讓 index 說謊；判讀者必須能分辨「這台沒有更早的 log」與「有，但被窗口排除了」。
- 來源 mtime 本來就已經在收集（var-log collector 用它做「收集期間來源是否被改動」的 read-only safety 檢查），只是從未輸出。本次是把既有的值寫出來，不是新增量測。
- 不帶 `--prom-url` 的部分收集若使用非 `N[smhdw]` 的 `--since`，將 fail-closed 並提示正確格式。這是 breaking change，影響面限於部分收集；README 的所有範例都是 `24h`，不受影響。刻意不採 fail-open：讓「給了 `--since` 卻仍收到整棵 `/var/log`」變成只有警告的靜默狀態，等於讓這個缺陷悄悄復發。
- `mtime` 讀不到時（collector 既有的 `unknown` fallback）該來源仍然收取，並在 disposition／detail 標示。比照 redaction 短讀的既有原則：安靜地少收證據比多收更糟。
- 窗口過濾排在位元組上限估算之前；否則上限會對著即將被丟棄的資料做判斷。
- 預設 `--since` 維持 `24h`。跨界規則讓 `/var/log` 的實際涵蓋已達數日，而預設值該多寬應由真實事故經驗驅動，不由這次變更順手決定；需要更多歷史時 `--since 7d` 在本次變更後才真正對 `/var/log` 生效。
- 預期 bundle 體積大幅下降，但確切比例不預測：binary journal 的節省取決於各 node 的 journal 輪替設定，而在補記 raw 檔來源 mtime 之前，那項資料並不存在於任何既有 bundle 中。實測值在變更完成後於真 lab 量取並寫回 ticket。
