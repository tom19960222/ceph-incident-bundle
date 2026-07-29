# 將 real-lab validation 做成 agent 可接手的狀態流程

Real-lab validation 不依賴個別 agent 的聊天記憶。Repository 以根目錄 `AGENTS.md` 保存不可違反的安全規則，以單一 runbook 描述狀態流程，以固定 Make targets 提供 status/discover/validate 入口，並讓每次執行留下人讀的 Markdown 與 machine-readable JSON Lab Validation Report。任何新 agent 都應能先讀 instructions、執行 status，再依唯一的 next action 接手。

## Consequences

- `AGENTS.md` 必須指向 lab runbook，並固定 read-only、identity preflight、禁止略過 mismatch、禁止啟用有額外副作用的 collector opt-ins，以及不得記錄憑證等規則。
- `make lab-status` 只讀取現況並回報下一步；`make lab-profile-discover` 只產生 candidate；`make validate-lab` 執行完整 gate。Agent 不需要自行組合底層長指令。
- 每次 validation 留下 `report.md` 與 `report.json`，包含 Git commit、profile hash、lab identity、四條 collector coverage、兩次 full collect、bundle comparison、stable-state diff、遠端殘留檢查、status 與 `next_action`。
- Local-only 的 `LATEST` 指標指向最近一次 report；報告與指標不得包含 private key、keyring、密碼、token 或其他 secret material。
- Identity mismatch、缺少 collector coverage、bundle 不完整或 read-only proof 失敗都必須 fail closed，並提供單一可執行的 next action，不能要求 agent 自行猜測或繞過 guard。
