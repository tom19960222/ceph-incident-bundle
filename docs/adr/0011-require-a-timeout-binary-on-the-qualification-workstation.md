# Qualification 工作站必須提供 GNU timeout

真 lab 第一次跑 `make validate-lab`（#52，run `20260802T170828Z`）時，bundle comparison 的 51 項差異有 29 項是同一件事：cluster capture 的 `# timeout` 標頭 shell 寫 `unavailable`、Python 寫 `20s`。這不是 candidate 的行為差異，是工作站的：shell reference 依 `docs/behavior-contract.md` §14 在找不到 `timeout`／`gtimeout` 時寫 `unavailable`，並且**實際上不對 cluster 指令施加任何 timeout**；Python candidate 用 `subprocess` 自帶的 timeout，不依賴外部 binary。那台 macOS 工作站沒裝 coreutils，於是 reference 在沒有 timeout 保護的狀態下跑完了整輪 collect。

兩個出路：讓 gate 豁免 `# timeout` 標頭，或要求工作站提供 timeout binary。裁定採後者（2026-08-05，#52）：

- 豁免會讓 timeout 語意從 gate 消失——candidate 若真的把 timeout 弄壞（寫錯值、根本沒生效），gate 再也看不見。
- `unavailable` 代表 reference 跑在低保護模式：一條卡死的 cluster 指令要等到整體 collect 上限（預設 4 小時）才會被打斷。qualification 本來就不該在這種模式下進行。
- 裝 coreutils 是一次性的工作站前置條件，成本遠低於在比對器裡開一條永久豁免。

## Consequences

- Qualification 工作站在執行 `make validate-lab` 前，必須讓 `timeout` 或 `gtimeout` 可被 `timeout_cmd()`（`lib/common.sh`）找到；macOS 上 `brew install coreutils` 即可。前置條件記載於 `docs/lab-validation-runbook.md` 的 Qualification Workflow 一節。
- `# timeout` 標頭維持逐字比對，不開豁免。工作站缺 binary 時 gate 仍會在 comparison 階段以 `unavailable != 20s` 失敗——這是預期行為，修復方式是補裝 binary，不是放寬比對。Harness 不另做提前檢查：標頭比對本身就是 enforcement，提前檢查只是把同一個失敗搬到更早的階段。
- `docs/behavior-contract.md` §14 的 `unavailable` fallback 行為不變：它仍是 shell 在一般操作環境的既定行為，本 ADR 只約束 qualification 工作站。
- #52 的第一類 A（29 項 timeout 標頭差異）以此裁定結案；連同其餘兩類半的修復（PR #55），下一次 `make validate-lab` 的 bundle comparison 預期殘餘差異為 0。
