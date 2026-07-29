# 完整移植 content safety，驗證後再獨立移除

Python rewrite 先忠實移植既有 redaction 與 secret-content verification，通過完整功能等價驗證後才以獨立變更移除。這會暫時投入時間翻譯已知即將刪除的功能，但能讓 Python cutover 維持單純的實作替換，任何行為差異都可判定為移植問題，而不是同時混入契約變更。

## Consequences

- Content safety 在公開入口模組內形成一個內部 seam，與 structural verification 分離；相關 options、執行 phase、結果與測試集中放置，使未來可整段刪除。
- 不新增第四個 Python 檔案，也不建立可擴充的 secret-policy framework；目標是忠實、集中且容易移除。
- Python cutover 驗證完成前，redaction、secret checks、既有 CLI flags 與 observable behaviour 全部保留。
- 後續移除 content safety 必須是獨立變更，不能混入 Python rewrite 的功能等價驗收。

## Removal Boundary

未來整段刪除 `--redact`／`--no-redact`、文字與壓縮檔 redaction、機密檔名與路徑檢查、private-key/Ceph-key/base64 內容掃描、`UNREDACTED-OPAQUE.txt`，以及其專屬 options、phase、結果與測試。Structural verification 仍保留 archive traversal 防護、必要 metadata/summary/manifest/evidence 結構、node evidence archive 完整性、payload cap、workdir 與最終 archive 的雙階段驗證，以及驗證失敗時保留 workdir且不產生可分享 archive 的行為。
