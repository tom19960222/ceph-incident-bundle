# Python cutover 要求 observable contract equivalence

Python rewrite 的驗收標準是所有可觀察契約等價，而不是 incident bundle byte-for-byte 相同。CLI flags 與預設值、exit code、stdout、外部指令與參數、來源選擇、bundle 結構與 artifacts、manifest 語意、SKIPPED/partial/interrupt/workdir 生命週期，以及 cutover 階段仍存在的 content safety 結果都必須維持；stderr 措辭、JSON 空白與 key 排列、tar member 排列、gzip header、mtime、暫存路徑及內部實作可以不同。這個邊界避免測試被非語意 serialization 細節綁住，同時保留操作人員與自動化真正依賴的行為。
