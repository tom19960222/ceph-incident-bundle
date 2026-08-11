---
status: superseded by ADR-0013
---

# Node 不符合 Python 3.11 條件時 graceful skip

Python 3.11 或更新版本是 supported node 的執行條件，但 collect 不能假設所有實際環境永遠符合這項條件。找不到 `python3` 或版本過舊時，該 node 會成為 skipped node，失敗原因寫入 incident bundle，其他 node 繼續收集，整體沿用既有 partial exit code `2`；不另外增加 SSH preflight，而是在真正執行 node collector 的同一次連線內判斷，避免額外連線與檢查後狀態又改變的競態。

ADR 0013 只把 supported floor 由 3.11 下修為 3.10；本 ADR 的 graceful-skip、
單一 SSH connection 與 partial exit `2` 處置仍然有效。
