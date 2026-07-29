# 使用明確的 collect 與 verify subcommands

Python 公開入口使用對稱的 `collect` 與 `verify` subcommands：`python3 ceph_incident_bundle.py collect ...` 產生 incident bundle，`python3 ceph_incident_bundle.py verify <bundle>` 驗證既有 bundle。這會讓 collect 指令比舊 shell 入口多一個單字，但避免「沒有 subcommand 就暗中代表 collect」的特殊解析規則，讓公開介面、文件與測試保持明確；既有 flags、exit code 與 stdout 的 `bundle: <path>` 契約不變，也不保留 shell compatibility wrapper。
