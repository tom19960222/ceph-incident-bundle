# Node manifest 是證據索引，其涵蓋範圍不受 observable equivalence 凍結

Python receiver 要求 Node Evidence Archive 內每一份 evidence 都有對應的 manifest entry，shell reference 只檢查反方向。這個較嚴的邊界予以保留，因此 shell 以直接複製產生、不寫 manifest 的 evidence，在 Python node collector 必須補上 entry。manifest 的語意因此從「已執行 command 的紀錄」擴充為「archive 內全部 evidence 的索引」。ADR 0006 將「manifest 語意」列為凍結契約，本決定把該項收窄為「每一筆 entry 的欄位意義與 command-policy 忠實度」，entry 的涵蓋範圍不再凍結。

## Consequences

- Archive 內除 `manifest.jsonl` 與 `errors.log` 外的每一份 evidence 都必須恰有一筆 entry；缺漏或重複都使整包 archive 被拒收。
- 複製類 evidence 的 `command` 記為 `collect-node copy <來源絕對路徑>`，不記實際讀取指令；後者隨 EUID 與 sudo 可用性改變，不是穩定契約。
- 未執行的指令留下的 SKIPPED marker（optional 工具不存在、privileged 讀取沒有 sudo、`cephadm` 不存在、複製來源不存在）同樣要有 entry：`command` 記本來要執行的 argv，`exit_code` 記 127（指令不存在）或 2（來源不存在／複製失敗）。marker 本身寫得完整，它所代表的證據卻不完整，所以 `exit_code` 一律非 0。timesyncd config 的 marker 同時代表 `conf` 與 `conf.d` 兩個來源，`command` 因此記 `collect-node copy <conf> <conf.d>`。
- `/var/lib/ceph` listing 的 `command` 記為 `collect-node list <目錄絕對路徑>`。理由同複製類（`find` 與 `sudo -n find` 兩種形狀），另加一個更硬的理由：真實 find expression 內含 `*keyring*`／`*private_key*` 這些 pattern，逐字記錄會讓 content safety 把整行 manifest 遮成 `[REDACTED]`，該 artifact 反而失去 index entry。artifact 內容本身不受影響——credential 在 `find` 走進去之前就被 prune 掉。代價是 manifest 不再顯示這條掃描是否 privileged，因此 command policy 改由 N9 的 argv ledger 斷言（`sudo -n find`）守住。
- 這兩類是本 ADR 第一條的機械後果，但 #18 的比對規則是逐類列舉，所以 `docs/python-rewrite-plan.md` 的 `## Contract Adjudications` 第 7 項另行補列，並標明由 #36 提出、等 #8 確認。
- `exit_code` 表示該筆 artifact 自身是否為完整證據（0 為完整），不表示所屬 collector 群組的整體結果。
- 來源存在但複製失敗時寫 SKIPPED artifact 與對應 entry，使 partial bundle 能指出遺失的是哪一份證據；shell 在這條路徑靜默失敗。
- #18 差異比對：shell 記錄的每一筆 entry，Python 必須有對應且 command 語意相同；Python 額外的 entry 與 SKIPPED artifact 僅限已列舉類別（複製類 evidence、`/var/log` 產出樹、未執行指令的 SKIPPED marker、`/var/lib/ceph` listing 的 collector verb）。manifest 不得整份排除於比對之外，否則 N9、N10 等 command-policy 斷言會失去對照組。
