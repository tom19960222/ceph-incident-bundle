# Qualification 工作機必須提供 timeout binary

真 lab 第一次跑 `make validate-lab`（#52，run `20260802T170828Z`）時，bundle comparison 的 51 項差異有 29 項是同一件事：cluster capture 的 `# timeout` 標頭 shell 寫 `unavailable`、Python 寫 `20s`。（那一輪 shell 的 crash-info 迴圈還吃著 #52 的 stdin bug，只收到部分 crash artifact；PR #55 修復後，同樣缺 binary 的情境會是 36 項。）這不是 candidate 的行為差異，是工作機的：shell reference 依 `docs/behavior-contract.md` §14 在找不到 `timeout`／`gtimeout` 時寫 `unavailable`，並停用外層單指令 timeout——cluster 指令只剩 SSH 連線層的 `ConnectTimeout`／`ServerAliveInterval` 把關，一條連線正常但卡住不回的指令，在 qualification 下要等 harness 的 collect 上限（預設 4 小時）兜底，一般 ops 直接跑 collect 時連這層都沒有。Python candidate 用 `subprocess` 自帶的 timeout，不依賴外部 binary。那台 macOS 工作機沒裝 coreutils，於是 reference 在這個低保護模式下跑完了整輪 collect。

兩個出路：讓 gate 豁免 `# timeout` 標頭，或要求工作機提供 timeout binary。裁定採後者（2026-08-05，#52）：

- 豁免會讓 timeout 語意從 gate 消失——candidate 若真的把 timeout 弄壞（寫錯值、根本沒生效），gate 再也看不見。
- `unavailable` 代表 reference 跑在低保護模式。qualification 本來就不該在這種模式下進行。
- 裝 coreutils 是一次性的工作機前置條件，成本遠低於在比對器裡開一條永久豁免。

## Consequences

- Qualification 工作機在執行 `make validate-lab` 前，必須讓 `timeout` 或 `gtimeout` 可被 `timeout_cmd()`（`lib/common.sh`）找到；macOS 上 `brew install coreutils` 即可。前置條件記載於 `docs/lab-validation-runbook.md` 的 Qualification Workflow 一節。
- `# timeout` 標頭維持逐字比對，不開豁免。工作機缺 binary 時 gate 仍會在 comparison 階段以 `unavailable != 20s` 失敗——這是預期行為，修復方式是補裝 binary，不是放寬比對。Harness 不另做提前檢查：標頭比對本身就是 enforcement，而 collect-shell 階段的輸出本來就帶著 reference 自己印的 WARNING（`run/collect.sh`），早期訊號已經存在。
- `docs/behavior-contract.md` §14 的 `unavailable` fallback 行為不變：它仍是 shell 在一般操作環境的既定行為，本 ADR 只約束 qualification 工作機。
- #52 的第一類 A 以此裁定結案。連同其餘兩類半的修復（PR #55），四類已知差異全數結清，下一次 comparison 預期不再有 timeout 標頭差異；活體 cluster 在兩次 run 之間的新事件（例如新增的 crash id 改變 `crash-info-<id>.json` 的檔名集合）不在本 ADR 的預測範圍內。
