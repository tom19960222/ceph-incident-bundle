# Real-lab validation 使用可替換的 TOML Lab Profile

`make validate-lab` 透過明確指定的 TOML Lab Profile 取得 SSH user/key path、seed alias、host map、kubeconfig、Rook namespaces、Prometheus URL、預期 FSID 與 SSH fingerprints。Python 3.11 標準庫可用 `tomllib` 直接讀取，讓 production 與 validation tooling 都不需要第三方 YAML 套件，也避免實作不完整的自製 YAML parser。

## Consequences

- Repository 提供不含真實連線資料的 example profile；實際 Lab Profile 保持 local-only，不進 Git。
- Profile 只保存 endpoint、預期 identity 與 private key/kubeconfig 的檔案路徑，不保存 private key、Ceph keyring、密碼或 token 內容。
- `make validate-lab` 必須明確接收 `LAB_PROFILE=/absolute/path/to/lab.toml`，不能依賴寫死的 lab endpoints；validation harness 由 profile 的 host map 產生 shell 與 Python 共用的暫存 inventory，避免重複維護兩份 host 清單。
- `CEPH-LAB-CONNECTION.md` 是供人閱讀與維護的連線文件，validation harness 不解析 Markdown。
- 更換或重建 lab 時只替換 Lab Profile，不修改 validation harness。
- `make lab-profile-discover` 依 profile 中尚未信任的連線入口執行唯讀 discovery，取得 SSH fingerprints、Ceph/Rook FSID、hostnames 與 Prometheus readiness，並輸出獨立的 Lab Profile Candidate。
- Discovery 不得覆蓋 active profile，也不得讓 candidate 直接通過 `validate-lab`；操作人員或其委託的 agent 必須檢查差異後明確啟用 candidate。
