# Node manifest 是證據索引，其涵蓋範圍不受 observable equivalence 凍結

Python receiver 要求 Node Evidence Archive 內每一份 evidence 都有對應的 manifest entry，shell reference 只檢查反方向。這個較嚴的邊界予以保留，因此 shell 以直接複製產生、不寫 manifest 的 evidence，在 Python node collector 必須補上 entry。manifest 的語意因此從「已執行 command 的紀錄」擴充為「archive 內全部 evidence 的索引」。ADR 0006 將「manifest 語意」列為凍結契約，本決定把該項收窄為「每一筆 entry 的欄位意義與 command-policy 忠實度」，entry 的涵蓋範圍不再凍結。

## Consequences

- Archive 內除 `manifest.jsonl` 與 `errors.log` 外的每一份 evidence 都必須恰有一筆 entry；缺漏或重複都使整包 archive 被拒收。
- 複製類 evidence 的 `command` 記為 `collect-node copy <來源絕對路徑>`，不記實際讀取指令；後者隨 EUID 與 sudo 可用性改變，不是穩定契約。
- `exit_code` 表示該筆 artifact 自身是否為完整證據（0 為完整），不表示所屬 collector 群組的整體結果。
- 來源存在但複製失敗時寫 SKIPPED artifact 與對應 entry，使 partial bundle 能指出遺失的是哪一份證據；shell 在這條路徑靜默失敗。
- #18 差異比對：shell 記錄的每一筆 entry，Python 必須有對應且 command 語意相同；Python 額外的 entry 與 SKIPPED artifact 僅限已列舉類別（複製類 evidence、`/var/log` 產出樹）。manifest 不得整份排除於比對之外，否則 N9、N10 等 command-policy 斷言會失去對照組。
