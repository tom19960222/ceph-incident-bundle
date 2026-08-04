# 移植情境 Ledger（每個 shell 測試情境的 Python 覆蓋對照）

> 對應 issue #18（offline observable-contract equivalence gate）。
> 來源清單：`docs/test-scenario-inventory.md`；本檔逐項指出每個情境現在由哪些
> 通過的 Python test 覆蓋。`tests/test_python_scenario_ledger.py` 會機械檢查
> 這份對照：ID 必須與 inventory 完全一致，`ported` 的每個 test 都必須存在，
> `not-ported` 必須正好是 inventory 分類為【實作細節-不移植】的那十項。
>
> 機械檢查證明的是「指向的測試存在且通過」，不是「該測試斷言了該列的每個子句」。
> 後者是人讀出來的：稽核方法、逐列結論與重跑條件記在
> `docs/test-scenario-audit.md`（issue #42）。該檔的每一列都釘住一個指紋，蓋住
> inventory 對應列與**本檔這一列的覆蓋欄**；任一邊改動都會讓該列的稽核紀錄失效，
> 並讓 `make validate` 失敗，直到有人重讀那一列。
>
> 覆蓋欄以「**對應關係**：…」開頭的段落，說明的是 shell 的某個概念在 Python 沒有
> 一對一等價物時，兩邊是怎麼對上的（旗標改名、開關撤除、或 fixture 用了不同的
> 退出碼）。

## 總覽

| 狀態 | 數量 | 意義 |
|---|---|---|
| ported | 128 | 已有通過的 Python test 覆蓋該情境語意 |
| blocked | 0 | Python 實作尚未移植該行為，測試無從撰寫 |
| not-ported | 10 | inventory 分類為 shell 實作細節，移植後失去意義 |
| **合計** | **138** | inventory 全部情境 |

> Inventory 撰寫時的總數是 137／127／10；之後 #15 補入 `P6a`（Prometheus
> 憑證邊界）而未更新總覽，因此實際列數為 138／128／10。本 ledger 以實際列數為準。

`differential` 欄位標出該情境同時被哪個 offline differential scenario
（`tests/differential/scenarios.py`）在 shell／Python 雙跑中端到端比較。


## 1. `tests/run-tests.sh`（總入口）

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| R1 | 所有實作與測試檔存在於預期路徑 | not-ported | repo 佈局檢查；Python 化後由 import／打包取代 | — |
| R2 | `run/collect.sh`、`lib/verify-bundle.sh` 與 she… | not-ported | 入口點可執行位屬打包細節 | — |
| R3 | `collect.sh` 無參數 → exit 1 且輸出含 `Usage:` | ported | `test_python_collect_cli.CollectCliContractTests.test_collect_without_required_options_is_a_fatal_usage_failure` | — |
| R4 | `verify-bundle.sh` 無參數 → exit 1 且輸出含 `Usage:` | ported | `test_python_verify.VerifyCliTests.test_wrong_invocation_prints_usage_to_stderr` | — |
| R5 | `verify-bundle.sh` 指到不存在路徑 → 非 0，輸出說明失敗（`VERI… | ported | `test_python_verify.VerifyCliTests.test_invalid_command_and_targets_fail_closed` | — |
| R6 | `collect.sh` 帶不存在的 inventory → 非 0，輸出說明（`miss… | ported | `test_python_collect_cli.CollectCliContractTests.test_a_missing_inventory_names_the_file_and_writes_nothing` | — |
| R7 | 依序執行 8 個子測試檔並要求 exit 0 | not-ported | 測試 harness 本身，由 `tests/run-python-tests.sh` 取代 | — |

## 2. `tests/test-common.sh`（common.sh / bundle.sh helpers）

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| C1 | `json_escape` 正確跳脫 `"` 與 `\` | not-ported | `json_escape` 不存在；Python 用 `json` 模組 | — |
| C2 | `json_escape` 不呼叫 python3（shell-native） | not-ported | 「不呼叫 python3」是純 shell 約束 | — |
| C3 | `manifest_add` 寫出一行 JSONL，欄位 host/collector/a… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture` | — |
| C4 | `manifest_add` 拒絕非數字 exit_code（非 0 退出並說明 exit… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction`（`nonnumeric-exit-code` 子案例；Python 的 writer 以 `int` 型別保證，因此這條語意由 receiver 守，處置是拒收該 archive 而非 writer 自己退出） | — |
| C5 | `redact_file`：Password/SECRET/token/keyring/p… | ported | `test_python_content_safety.RedactionLineSelectionTests.test_every_sensitive_keyword_blanks_its_whole_line`（五個關鍵字逐一、整行換成 `[REDACTED]`、前後安全行不動、log 點名檔案）<br>`test_python_content_safety.RedactionLineSelectionTests.test_a_file_without_secrets_is_left_alone_and_still_logged`<br>`test_python_content_safety.CollectContentSafetyTests.test_collect_redacts_sensitive_text_by_default`（端到端）<br>`test_python_content_safety.CollectContentSafetyTests.test_redaction_handles_ascii_whitespace_and_a_long_single_line` | mixed-full-collection-redacted |
| C6 | `redact_file`：`-----BEGIN ... PRIVATE KEY----… | ported | `test_python_content_safety.RedactionLineSelectionTests.test_every_sensitive_keyword_blanks_its_whole_line`（`private_key`／`private-key`／`private key` 三種拼法）<br>`test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C7 | `redact_file`：多行 PEM 本體（含 base64 行與 END 行）整段遮… | ported | `test_python_content_safety.RedactionLineSelectionTests.test_a_multi_line_pem_body_is_blanked_end_to_end`（BEGIN／base64／END 三行逐行斷言，前後安全行保留）<br>`test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C8 | `redact_file`：Ceph key 素材（`key = AQB...==`、`"… | ported | `test_python_content_safety.RedactionLineSelectionTests.test_ceph_key_material_is_blanked_without_over_redacting_prose`（兩種拼法遮蔽、含 `key` 的一般句子不遮）<br>`test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C9 | `redact_file` 保留原檔權限（640） | ported | `test_python_content_safety.RedactionFilePermissionTests.test_plain_redaction_keeps_the_original_permission_mode`<br>`test_python_content_safety.RedactionFilePermissionTests.test_compressed_redaction_keeps_the_original_permission_mode` | — |
| C10 | `redact_gz_file`：gzip 檔解壓-遮蔽-重壓，正常內容保留、秘密不外洩、… | ported | `test_python_content_safety.CollectContentSafetyTests.test_compressed_text_is_redacted_but_opaque_raw_evidence_is_unchanged`（秘密不外洩、安全內容保留）<br>`test_python_content_safety.RedactionFilePermissionTests.test_compressed_redaction_keeps_the_original_permission_mode`（mode 保留；端到端 artifact 一律 0600，故只有直呼 seam 才驗得到「保留」） | mixed-full-collection-redacted |
| C11 | `redact_compressed_file` 支援 xz / bz2 / zst 三種… | ported | `test_python_content_safety.CollectContentSafetyTests.test_all_supported_compressed_text_codecs_are_redacted` | — |
| C12 | 重壓縮失敗時回非 0、原壓縮檔內容/mode 原封不動（不破壞原 artifact） | ported | `test_python_content_safety.CollectContentSafetyTests.test_recompress_failure_preserves_original_and_continues_other_redactions`<br>`test_python_content_safety.RedactionFilePermissionTests.test_recompress_failure_keeps_the_original_bytes_and_mode` | — |
| C13 | `redact_bundle_text`：早期壓縮檔遮蔽失敗 → 整體回 2，但**繼續*… | ported | `test_python_content_safety.CollectContentSafetyTests.test_recompress_failure_preserves_original_and_continues_other_redactions` | — |
| C14 | `redact_bundle_text`：`merged/` 純文字要遮蔽；`raw/` … | ported | `test_python_content_safety.CollectContentSafetyTests.test_compressed_text_is_redacted_but_opaque_raw_evidence_is_unchanged`<br>`test_python_content_safety.CollectContentSafetyTests.test_prometheus_metric_exclusion_is_anchored_to_the_cluster_layer` | mixed-full-collection-redacted |
| C15 | `enforce_node_log_caps`：遮蔽後超過 cap → 回 2、丟棄 me… | ported | `test_python_content_safety.CollectContentSafetyTests.test_post_redaction_payload_cap_discards_node_log_payload`<br>`test_python_content_safety.CollectContentSafetyTests.test_post_redaction_cap_counts_a_regular_payload_root` | — |
| C16 | `progress`：預設輸出訊息；`CEPH_INCIDENT_QUIET=1` 時完全… | ported | **對應關係**：Python 沒有 `CEPH_INCIDENT_QUIET` 環境變數，等價開關是 `--quiet` 旗標。<br>`test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages`（預設有進度訊息；`--quiet` 成功跑完 stderr 完全為空——「完全靜默」是逐字元斷言，不是「沒看到那幾句」） | — |
| C17 | `progress` 只寫 stderr、不污染 stdout | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages`（兩種模式下 stdout 都恰為 `bundle: …` 一行，且 `bundle:` 不出現在 stderr）<br>`test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node` | — |
| C18 | `run_capture` 成功路徑：artifact 首行 `# host: ...` … | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_seed_collects_json_and_text_evidence`（首行 `# host:` 標頭與指令輸出）<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture`（每筆 manifest 的 artifact 路徑與 `exit_code=0`） | — |
| C19 | `run_capture` 失敗路徑：回傳指令的非 0 碼（7）、輸出仍寫入 artifa… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_failed_required_command_is_partial_and_keeps_other_evidence`（本列的 7 與 CA2 的 17 兩個碼各跑一次：artifact 保留失敗輸出、`errors.log` 記 `exit=<該碼>`、manifest 記 `exit_code=<該碼>`。跑兩個碼是為了斷言「原樣傳回」而不是「等於 fixture 剛好用的那個數字」） | — |
| C20 | `run_capture` 缺 `--` 分隔符 → 致命錯誤並說明 | not-ported | `--` 呼叫慣例是 shell API 細節 | — |
| C21 | `run_capture` 以預設 20s timeout 包住指令，artifact 標… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_the_default_timeout_is_the_one_recorded_in_every_artifact`（不帶 `--timeout` 跑完整 collect，每個 artifact 標頭都是 `# timeout: 20s`——兩個子句合在同一次執行裡驗）<br>`test_python_collect_cli.CollectCliContractTests.test_timeout_defaults_match_the_shell_reference`（預設值本身）<br>`test_python_collect_ceph.DirectCephFailureSemanticsTests.test_timed_out_command_is_truncated_and_writes_ssh_debug`（timeout 真的會截斷）<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_the_command_timeout_bounds_every_request` | — |
| C22 | artifact 檔名以 `-` 開頭仍可正確建立 | not-ported | 以 `-` 開頭的檔名是 shell 重導陷阱；`open()` 無此問題 | — |
| C23 | `run_capture` 不改變呼叫端 errexit 狀態 | not-ported | `errexit` 狀態是純 bash 語意 | — |

## 3. `tests/test-cephadm-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| CA1 | Happy path：產出 json/text 全套 artifact（status、he… | ported | **對應關係**：shell 這一列的 ssh log 斷言是 `sudo -n cephadm shell -- ceph …`＋固定的 `ConnectTimeout=30`／`ServerAliveInterval=30`。Python 撤掉 cephadm-shell runner（見 O1、CA4／CA5），遠端指令是純 `ceph`／`sudo -n ceph`；連線選項不是常數而是跟著 `--timeout` 走（預設 20 → `ConnectTimeout=20`），由 C21 那一列的測試斷言。<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_seed_collects_json_and_text_evidence`（json／text 全套 artifact）<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_recent_crash_inspection_caps_at_ten_and_avoids_collisions`（上限 10 筆、`crash/02`→`crash_02`、`crash:02` 加 `-2` 防碰撞）<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture`（manifest 34 行） | cephadm-direct-no-prometheus |
| CA2 | 單一指令失敗（osd perf）→ 整體回 2 但**繼續收集**後續 artifact；… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_failed_required_command_is_partial_and_keeps_other_evidence` | cephadm-partial-command-failure |
| CA3 | `crash ls` 回非 JSON → 寫 `crash-info-skip.txt`（… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_unparseable_crash_list_is_skipped_without_failing_collection`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_empty_crash_list_collects_no_crash_info` | — |
| CA4 | runner=`direct`：遠端跑純 `ceph ...`，**不得**出現 `cep… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_direct_runner_runs_plain_ceph_without_sudo`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_runs_plain_ceph_argv_over_ssh` | cephadm-direct-no-prometheus |
| CA5 | runner=`sudo`：遠端跑 `sudo -n ceph ...`，不得出現 `ce… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_sudo_runner_runs_sudo_n_ceph_and_never_cephadm_shell`<br>`test_python_collect_ceph.FakeSshArgvContractTests.test_cephadm_shell_is_never_answered` | cephadm-sudo-fallback |

## 4. `tests/test-node-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| N1 | Happy path 全套執行 → exit 0（失敗時傾印 errors.log 供除錯） | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_public_collect_streams_one_node_and_saves_basic_evidence` | — |
| N2 | 產出固定 artifact 清單：system/、resources/、storage/、… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_the_node_evidence_surface_is_complete_and_indexed` | — |
| N3 | 各 artifact 內容來自對應指令輸出（cephadm-ls 的 `"style":"… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_the_node_evidence_surface_is_complete_and_indexed`<br>`test_python_collect_node.NodeEvidenceSurfaceTests.test_an_absent_var_lib_ceph_is_a_marker_not_a_failure` | — |
| N4 | optional 指令不存在（ntpq）→ artifact 寫 `SKIPPED: co… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_a_missing_optional_tool_is_skipped_without_failing_the_node` | — |
| N5 | timesyncd 設定檔與 conf.d 逐檔複製進 `time/systemd-tim… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_timesyncd_config_is_copied_file_by_file`<br>`test_python_collect_node.NodeEvidenceSurfaceTests.test_a_failed_copy_names_the_evidence_it_lost`<br>`test_python_collect_node.NodeEvidenceSurfaceTests.test_a_failed_symlinked_copy_reports_the_path_it_was_asked_for` | — |
| N6 | log family 合併：`ceph.log.2.gz`＋`.1`＋現行檔合併為 `.m… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest`（兩個 family 各自合併、壓縮輪替內容在內、超大檔逐位元組整檔進 merged——「不截斷」是把整份 payload 比對回來，不是比對長度） | — |
| N7 | `var-lib-ceph-configs/` 複製 config、**排除 keyrin… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_var_lib_ceph_configs_are_copied_without_credentials` | — |
| N8 | optional 工具收到正確 argv（`iostat -xz 1 3`、`pvs/vg… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_optional_tools_receive_their_exact_argv` | — |
| N9 | dmesg 經 `sudo -n` 執行 | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_privileged_reads_go_through_noninteractive_sudo`<br>`test_python_collect_node.NodeEvidenceSurfaceTests.test_privileged_reads_without_sudo_are_skipped_not_faked` | — |
| N10 | dmesg 與 ceph journal 用加重 timeout（120s，非 `--ti… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_dmesg_and_the_ceph_journal_get_the_heavier_timeout` | — |
| N11 | journal 匯出＋/var/log 共用同一 byte cap；溢出 → exit 2… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_and_journal_share_one_fail_closed_payload_cap`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_var_log_cap_discards_all_payload_instead_of_truncating` | node-collection-timeout |
| N12 | 非 ceph 節點沒有 ceph journal（journalctl exit 1）→ … | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_a_node_without_a_ceph_journal_is_not_partial`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_finite_cap_without_sudo_marks_missing_journal_partial`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_skip_logs_writes_only_the_explicit_skip_artifact` | node-partial-and-unusable-archive |
| N13 | timesyncd 全缺（timedatectl/systemctl/journalctl… | ported | `test_python_collect_node.NodeEvidenceSurfaceTests.test_a_node_without_timesyncd_stays_complete` | — |

## 5. `tests/test-var-log-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| V1 | 數字輪替（`.2.gz`→`.1`→現行）合併為單一 `.merged`，順序舊→新；產 … | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest` | — |
| V2 | gz/xz/bz2/zst 四種 codec＋日期式輪替（`-YYYYMMDD.<ext>… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest` | — |
| V3 | opaque 檔（zip、tar.gz、binary wtmp、journal）byte-… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources`（四種 opaque 來源逐一：raw 逐位元組相同、都出現在 `UNREDACTED-OPAQUE.txt`、都沒有對應的 merged 輸出） | mixed-full-collection-redacted |
| V4 | `keep_originals=1` 才保留 `original/`（原始檔與壓縮檔皆逐位… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_keep_original_logs_is_opt_in_and_preserves_stored_bytes`（同一份素材跑兩次：不帶旗標時整棵樹沒有 `original/`，帶旗標時純文字與 `.gz` 皆逐位元組保留） | — |
| V5 | 總量超過 max_bytes → 回 2、`OVER-LIMIT.txt`、merged/… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_cap_discards_all_payload_instead_of_truncating` | — |
| V6 | 壞壓縮檔：raw 保留原檔、其他 family 照常收集、`ERRORS.tsv` 記 `… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_corrupt_and_missing_codecs_are_preserved_raw_and_partial` | — |
| V7 | 不追 symlink；`*.pem`/`*.key`（含壓縮、輪替變體）等敏感路徑不讀不抄… | ported | **規則邊界**：兩邊的判準都是 `*.<ext>` 或 `*.<ext>.*`（`pem`／`key`／`crt`／`pfx`／`p12`），所以 `server.pem.gz`、`server.key.1` 算敏感，日期式輪替 `tls.key-20260721` 兩邊都**不**算——這是共有邊界，不是移植差異。<br>`test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources`（symlink 不追；`.pem`／`.key.1`／`.crt`／`.p12`／`.pem.gz` 五種變體逐一：整棵樹沒有該檔名、`SKIPPED-sensitive.txt` 點名、`dd` argv ledger 從未讀過它） | — |
| V8 | 來源檔內容 / mode / mtime 收集後完全不變 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources` | — |
| V9 | 缺 codec（PATH 無 zstd）→ 回 2、raw 保留、`ERRORS.tsv`… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_corrupt_and_missing_codecs_are_preserved_raw_and_partial`（`ERRORS.tsv` 逐字段斷言 `service.log.1.zst\tmissing-codec:zstd`，點名缺的是哪個 codec） | — |
| V10 | 頂層 family 輸出檔（`app.merged`）與同名目錄（`app/`、`app.… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection`（三個輸出各自比對內容，不只比對存在——內容互換也會失敗） | — |
| V11 | 零填充數字輪替（`.010`/`.09`/`.08`）以十進位排序（不可 octal 崩潰） | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection` | — |
| V12 | 檔案後段才出現 NUL → 視為 binary，raw 保留、不合併為文字 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection`（raw 逐位元組保留，且不存在對應的 `.merged`） | — |
| V13 | 第二階段解壓失敗（第一次探測成功、正式解壓失敗）→ 回 2、壓縮原檔保留在 raw、部分解… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_later_decode_failures_roll_back_and_preserve_raw`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_safe_read_failure_is_partial_without_an_unsafe_fallback` | — |
| V14 | 掃描 metadata 本身有上限：超過 → 回 2、`SCAN-LIMIT.txt`、不… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_metadata_scan_limit_fails_closed_before_payload_reads`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_var_log_entry_count_limit_fails_closed_before_payload_reads` | — |

## 6. `tests/test-rook-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| K1 | 顯式 rook 模式、無 kubectl → exit 2＋`cluster/rook/S… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_local_kubectl_is_a_partial_bundle_with_a_reason` | — |
| K2 | 同上但帶 `--allow-skip`（auto 模式 fallback）→ exit 0… | ported | **對應關係**：Python 沒有 `--allow-skip`。這個旗標在 shell 裡只由 auto 模式的 fallback 路徑帶入，Python 直接把「auto 模式下 rook 層不可用可容忍」做進編排，所以等價條件是 `--mode auto`。<br>`test_python_collect_orchestration.CephRunnerSelectionTests.test_auto_mode_tolerates_an_unavailable_rook_layer_when_ceph_succeeds`（auto 模式下 rook 層不可用仍 exit 0，且照樣留下 `cluster/rook/SKIPPED.txt`） | — |
| K3 | namespace 不存在 → exit 2；SKIPPED 檔含歸類原因（namespa… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_namespace_is_reported_with_the_raw_kubectl_error` | rook-local-namespace-missing |
| K4 | `--kube-context lab` 但 context 不存在 → exit 2；S… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_context_is_reported_with_the_raw_kubectl_error` | — |
| K5 | API server 連不上 → exit 2；SKIPPED 含 cannot conn… | ported | `test_python_collect_rook.RookUnavailableTests.test_unreachable_api_server_is_reported_with_the_raw_kubectl_error` | — |
| K6 | 未設 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC` → toolb… | ported | `test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_toolbox_is_skipped_and_no_exec_is_ever_issued`<br>`test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_toolbox_lookup_preserves_the_shell_read_only_command_ledger`<br>`test_python_collect_rook.FakeKubectlArgvContractTests.test_kubectl_exec_is_never_answered` | rook-remote-kubectl |
| K7 | Happy path（含 toolbox）：pods-wide / events / ro… | ported | **對應關係**：`toolbox-status` 需要 `kubectl exec`，Python 沒有任何路徑通往它（見 K6、O1），所以這一列的 toolbox artifact 在 Python 永遠是 `toolbox-SKIPPED.txt`。<br>`test_python_collect_rook.LocalKubectlRunnerTests.test_local_kube_mode_collects_rook_evidence`（四個 artifact 各含對應輸出；第一個 kubectl 呼叫就是 namespace 偵測；operator logs 有被呼叫）<br>`test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_toolbox_is_skipped_and_no_exec_is_ever_issued`（toolbox 那半）<br>`test_python_collect_rook.RookOptionalArtifactTests.test_operator_log_uses_the_since_window` | rook-remote-kubectl |
| K8 | external cluster：`--namespace rook-ceph-exter… | ported | `test_python_collect_rook.RookNamespaceTests.test_external_cluster_splits_resource_and_operator_namespaces`<br>`test_python_collect_rook.RookNamespaceTests.test_operator_namespace_has_its_own_rook_ceph_default` | — |
| K9 | remote 模式（`--ssh-target --ssh-key --kube-cont… | ported | `test_python_collect_rook.RemoteKubectlRunnerTests.test_remote_kube_mode_runs_kubectl_on_the_inventory_node`<br>`test_python_collect_rook.RemoteKubectlRunnerTests.test_kube_context_is_forwarded_to_every_kubectl_invocation` | rook-remote-kubectl |
| K10 | operator pod 查詢失敗不可讓收集中止（set -e 回歸）：exit 0＋`o… | ported | `test_python_collect_rook.RookOptionalArtifactTests.test_operator_pod_lookup_failure_skips_only_the_operator_log` | — |

## 7. `tests/test-prom-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| P1 | duration 解析：`90/45s/30m/24h/7d/2w` 正確換算；拒絕 `y… | ported | `test_python_collect_prometheus.PrometheusGrammarTests.test_the_duration_grammar_converts_every_documented_unit`（六個單位逐一換算，含 `010h`→36000、`008`→8 的十進位）<br>`test_python_collect_prometheus.PrometheusGrammarTests.test_the_duration_grammar_rejects_what_it_cannot_mean`<br>`test_python_collect_prometheus.PrometheusOptionValidationTests.test_unparseable_since_is_rejected_when_the_dump_is_enabled`（同樣的拒絕清單走到 CLI）<br>`test_python_collect_prometheus.PrometheusOptionValidationTests.test_accepted_windows_reach_the_dump` | — |
| P2 | auto step：15s 下限；7d → ceil(604800/10000)=61 | ported | `test_python_collect_prometheus.PrometheusGrammarTests.test_the_auto_step_keeps_a_floor_and_bounds_the_point_count`（短 window 落在 15s 下限、7d→61）<br>`test_python_collect_prometheus.PrometheusHappyPathTests.test_prom_url_collects_metrics_evidence_for_matching_jobs`（24h 端到端 `step_seconds=15`）<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_long_window_raises_the_auto_step_above_the_floor`<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_an_explicit_step_overrides_the_automatic_one` | — |
| P3 | URL 遮蔽：`http://u:sekrit@h` → `u:***@h`；無憑證原樣 | ported | `test_python_collect_prometheus.PrometheusGrammarTests.test_masking_hides_a_password_and_leaves_everything_else_alone`（含憑證與無憑證兩側，以及路徑裡的 `@` 不誤判）<br>`test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_a_masked_url_is_recorded_in_the_bundle_environment`<br>`test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_url_credentials_never_reach_an_artifact_or_a_message` | — |
| P4 | 前置指令檢查：缺 python3 時失敗並點名 python3 | not-ported | 工作機 python3 前置檢查；Python runtime 本身即滿足 | — |
| P5 | Happy path：buildinfo.json、targets.json、dump-i… | ported | `test_python_collect_prometheus.PrometheusHappyPathTests.test_prom_url_collects_metrics_evidence_for_matching_jobs`<br>`test_python_collect_prometheus.PrometheusHappyPathTests.test_requests_match_the_shell_curl_argv` | prometheus-enabled |
| P6 | Prometheus 連不上 → 回 2、`SKIPPED.txt`（not reacha… | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_an_unreachable_server_skips_the_layer_with_the_curl_error` | — |
| P6a | curl 失敗診斷即使回顯含 basic-auth 的完整 URL，也不得把密碼寫入 bu… | ported | `test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_url_credentials_never_reach_an_artifact_or_a_message` | — |
| P7 | `--job-regex` 全不匹配 → 回 2；SKIPPED 說明 no scrape… | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_an_empty_job_listing_names_the_jobs_it_saw` | — |
| P8 | 缺 python3（進入收集後）→ 回 2、SKIPPED 點名 python3 | not-ported | 同 P4：收集期間的 python3 依賴檢查不存在於 Python 實作 | — |
| P9 | 單一 metric query_range 失敗 → 回 2；其他 metric 照常 d… | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_metric_query_leaves_no_dump_and_keeps_the_rest`<br>`test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_malformed_metric_response_is_not_kept_as_evidence` | — |
| P10 | `--budget 0` 觸發截斷 → 回 2；index.txt 記 TRUNCATED… | ported | **對應關係**：shell 的 `--budget` 在 Python 是 `--prom-timeout`（同一個「整個 dump 的秒數預算」語意）。<br>`test_python_collect_prometheus.PrometheusBudgetTests.test_an_exhausted_budget_truncates_the_dump_and_records_it` | — |
| P11 | job 名含不安全字元（`"`）→ 回 2；errors.log 記 unsafe nam… | ported | `test_python_collect_prometheus.PrometheusUnsafeNameTests.test_an_unsafe_job_name_is_skipped_and_the_safe_jobs_are_kept`<br>`test_python_collect_prometheus.PrometheusUnsafeNameTests.test_job_names_never_collide_with_fixed_prometheus_artifacts` | — |
| P12 | 7d window → `step=61` | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_long_window_raises_the_auto_step_above_the_floor` | prometheus-enabled |
| P13 | redaction 排除 `cluster/prometheus/<job>/` 的 me… | ported | `test_python_content_safety.CollectContentSafetyTests.test_prometheus_metric_exclusion_is_anchored_to_the_cluster_layer` | — |
| P14 | targets 抓取失敗 → 回 2；不留 targets.json；buildinfo … | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_targets_fetch_keeps_the_metrics_dump_running` | — |
| P15 | job 列表抓取失敗 → 回 2、SKIPPED 說 job listing failed | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_a_failed_job_listing_skips_the_layer_after_keeping_what_worked`<br>`test_python_collect_prometheus.PrometheusUnavailableTests.test_a_malformed_job_listing_is_a_skip_not_a_crash` | — |
| P16 | metric 名稱列表失敗 → 回 2；index.txt 記 FAILED: metri… | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_metric_listing_marks_its_job_and_keeps_going` | — |
| P17 | `--url` 尾端斜線 → 請求 URL 不得出現 `//api` 雙斜線 | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_trailing_slash_never_doubles_in_a_request_url` | — |
| P18 | `--job-regex '-zzz'`（dash 開頭）→ 回 2 且不得把 regex… | ported | **對應關係**：shell 的旗標名是 `--job-regex`，Python 是 `--prom-job-regex`。<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_dash_leading_job_filter_matches_nothing_without_an_option_error`（回 2、SKIPPED 說明，且 stderr 與 `errors.log` 都沒有 `grep:`——regex 沒被當成選項）<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_the_job_filter_uses_the_shell_posix_ere_dialect` | — |

## 8. `tests/test-verify-bundle.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| B1 | 合法 bundle（目錄與 tar.gz 兩形態）→ exit 0，stdout 恰為 `… | ported | `test_python_verify.VerifyCliTests.test_valid_minimal_directory_passes`<br>`test_python_verify.VerifyCliTests.test_valid_minimal_archive_passes`<br>`test_python_verify.VerifyCliTests.test_shell_public_collect_workdir_and_archive_pass` | — |
| B2 | bundle 內含 symlink → 驗證失敗且訊息提及 symlink | ported | `test_python_verify.VerifyCliTests.test_directory_symlink_is_rejected`<br>`test_python_verify.VerifyCliTests.test_unsafe_archive_members_are_rejected_without_writes` | — |
| B3 | 缺 manifest.jsonl（dir 與 archive）→ 失敗且點名 manife… | ported | `test_python_verify.VerifyCliTests.test_missing_required_content_is_rejected_for_directory_and_archive` | — |
| B4 | 檔名為 `keyring`（dir/archive）→ 失敗且點名 | ported | `test_python_verify.VerifyCliTests.test_secret_paths_are_rejected_for_directory_and_archive` | — |
| B5 | 內含 `.ssh/` 目錄（dir/archive）→ 失敗且點名 | ported | `test_python_verify.VerifyCliTests.test_secret_paths_are_rejected_for_directory_and_archive` | — |
| B6 | 檔名 `id_ed25519`（dir/archive）→ 失敗且點名 | ported | `test_python_verify.VerifyCliTests.test_secret_paths_are_rejected_for_directory_and_archive` | — |
| B7 | 檔名 `private_key`（dir/archive）→ 失敗且點名 | ported | `test_python_verify.VerifyCliTests.test_secret_paths_are_rejected_for_directory_and_archive` | — |
| B8 | 檔名 `*.pem`（dir/archive）→ 失敗且點名 | ported | `test_python_verify.VerifyCliTests.test_secret_paths_are_rejected_for_directory_and_archive` | verify-failure-keeps-workdir |
| B9 | 允許副檔名內夾帶未遮蔽 PEM 本體（`-----BEGIN OPENSSH PRIVAT… | ported | `test_python_verify.VerifyCliTests.test_private_key_and_ceph_key_content_are_rejected` | mixed-full-collection-unredacted |
| B10 | 非法 tar.gz → 失敗且說 invalid archive | ported | `test_python_verify.VerifyCliTests.test_corrupt_and_truncated_archives_are_rejected`<br>`test_python_verify.VerifyCliTests.test_archive_with_invalid_deflate_body_is_rejected_without_traceback` | — |
| B11 | 多餘參數 → 非 0＋Usage | ported | `test_python_verify.VerifyCliTests.test_extra_argument_prints_usage_to_stderr` | — |

## 9. `tests/test-collect.sh`（run/collect.sh 端到端編排）

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| O1 | `--help` → exit 0；usage 文件化所有主要旗標（--kube-cont… | ported | **對應關係**：shell 的旗標清單裡有 `--allow-cephadm-shell` 與 `--allow-kubectl-exec`，Python 兩者都撤掉（沒有實作路徑），因此 usage 不但不列，測試還反過來斷言它們**不**出現；帶上去是 exit 1。<br>`test_python_collect_cli.CollectCliContractTests.test_help_documents_every_supported_collect_option`（23 個支援旗標逐一在、2 個撤掉的逐一不在）<br>`test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_an_opt_in_flag_for_kubectl_exec_does_not_exist` | — |
| O2 | inventory 不存在 → exit 1 | ported | `test_python_collect_cli.CollectCliContractTests.test_a_missing_inventory_names_the_file_and_writes_nothing` | — |
| O3 | inventory 是宣告式資料、**不得**被當 shell 執行：含 `$(touch… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes` | — |
| O4 | host alias 含 `../` → exit 1，且未在輸出根外建立檔案 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes` | — |
| O5 | SSH target 形如 `--ProxyCommand=...` → 失敗且**未曾*… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes`<br>`test_python_collect_ceph.DirectCephSeedSelectionTests.test_unsafe_seed_argument_fails_before_any_ssh` | — |
| O6 | auto 模式雙層收集 happy path：cluster/ceph 來自 ceph 節… | ported | `test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node`（雙層 artifact、逐節點、node 端 config 轉發）<br>`test_python_content_safety.CollectContentSafetyTests.test_the_defaults_trust_the_host_key_and_redact`（預設 `StrictHostKeyChecking=accept-new`——其餘案例一律關掉 host key trust，只有這一條驗得到預設值） | mixed-full-collection-redacted |
| O7 | `--no-trust-ssh-host-key`：不再帶 accept-new，reda… | ported | `test_python_content_safety.CollectContentSafetyTests.test_redaction_flag_precedence_is_independent_from_host_key_trust`（不帶 accept-new 那半）<br>`test_python_content_safety.CollectContentSafetyTests.test_collect_redacts_sensitive_text_by_default`（同一次執行已帶 `--no-trust-ssh-host-key`，仍然遮蔽——兩個開關互相獨立） | — |
| O8 | `--no-redact`：秘密原文保留於 bundle；host key trust 預… | ported | `test_python_content_safety.CollectContentSafetyTests.test_no_redact_keeps_sensitive_text_and_still_writes_the_log`（秘密原文保留、`redactions.log` 為空）<br>`test_python_content_safety.CollectContentSafetyTests.test_redaction_flag_precedence_is_independent_from_host_key_trust`（`--no-redact` 搭配信任 host key 時 accept-new 仍在）<br>`test_python_content_safety.CollectContentSafetyTests.test_the_defaults_trust_the_host_key_and_redact`（host key trust 的預設值本身） | mixed-full-collection-unredacted |
| O9 | 顯式 `--trust-ssh-host-key --redact` 等同預設行為 | ported | `test_python_content_safety.CollectContentSafetyTests.test_redaction_flag_precedence_is_independent_from_host_key_trust`（`--trust-ssh-host-key --redact` 那一組案例：遮蔽開、accept-new 在）<br>`test_python_content_safety.CollectContentSafetyTests.test_the_defaults_trust_the_host_key_and_redact`（不帶任何旗標時的同一組觀察值——「等同預設」是拿這兩個測試的斷言對照出來的） | mixed-full-collection-unredacted |
| O10 | auto、無任何 capable 節點 → exit 2；`cluster/ceph/SK… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_auto_mode_without_any_capable_node_is_partial_but_still_collects_nodes` | auto-without-any-cluster-source |
| O11 | 顯式 `--mode cephadm --seed`：只收 ceph 層，全程**不得**… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_seed_collects_json_and_text_evidence`（bundle 內沒有 `cluster/rook/` 任何東西，ssh argv ledger 也從未出現 `kubectl`）<br>`test_python_collect_ceph.DirectCephRunnerSeamTests.test_direct_runner_runs_plain_ceph_without_sudo` | cephadm-direct-no-prometheus |
| O12 | 顯式 seed 但 direct/sudo runner 都不通、且 cephadm-sh… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_an_unreachable_explicit_seed_never_falls_back_to_inventory`<br>`test_python_collect_ceph.DirectCephRunnerSeamTests.test_sudo_runner_runs_sudo_n_ceph_and_never_cephadm_shell`<br>`test_python_collect_ceph.FakeSshArgvContractTests.test_cephadm_shell_is_never_answered` | — |
| O13 | 兩台 cephadm 節點：cluster ceph 只從**第一台**收，不重複 | ported | `test_python_collect_ceph.DirectCephSeedSelectionTests.test_two_capable_nodes_still_collect_the_cluster_layer_once`（兩台都可用的 inventory：兩台都以 node 身分被收，但 ceph 讀取只發給第一台，且 `ceph status` 只發一次）<br>`test_python_collect_ceph.DirectCephSeedSelectionTests.test_collect_without_a_seed_auto_selects_the_first_capable_node` | — |
| O14 | node 回傳 tar 缺 manifest → 該節點 SKIPPED、整體 exit 2 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction` | node-partial-and-unusable-archive |
| O15 | node 回傳非 tar → SKIPPED、exit 2 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction` | — |
| O16 | 單一 host 收集失敗（remote exit 2）→ 整體 exit 2、bundle… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_one_failed_node_keeps_the_other_nodes_and_bundle_partial`（exit 2、兩台節點的證據都在、bundle `errors.log` 點名失敗的那台且沒有牽連成功的那台）<br>`test_python_collect_node.CollectSingleNodeCliTests.test_valid_archive_is_preserved_when_node_collector_is_partial` | cephadm-partial-command-failure |
| O17 | 中途 abort → trap 清掉 workdir，`--out` 下不留 `tmp.*` | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_interruption_cleans_remote_and_workstation_workspaces` | interrupt-cleans-up |
| O18 | verify 失敗（node 夾帶 `.pem`）→ exit 1、**不產** bund… | ported | `test_python_content_safety.CollectContentSafetyTests.test_pre_package_verification_failure_keeps_a_diagnostic_workdir`<br>`test_python_content_safety.CollectContentSafetyTests.test_packaged_archive_verification_failure_removes_the_candidate` | verify-failure-keeps-workdir |
| O19 | auto、只有 kube 節點且 namespace 不存在、無 ceph → exit … | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_auto_mode_keeps_the_rook_reason_when_there_is_no_ceph_to_fall_back_on`（auto 模式下只有 kube 能力、namespace 不存在：exit 2 不是綠色 0，且 `cluster/rook/SKIPPED.txt` 仍是 namespace 的具體原因，沒被泛用 auto skip 覆寫）<br>`test_python_collect_rook.RookPartialCollectionTests.test_rook_partial_does_not_hide_a_successful_node`<br>`test_python_collect_rook.RookUnavailableTests.test_missing_namespace_is_reported_with_the_raw_kubectl_error` | rook-local-namespace-missing |
| O20 | 能力探測 ssh 失敗的節點 → errors.log 記 `capability pro… | ported | `test_python_collect_ceph.DirectCephSeedSelectionTests.test_a_failed_capability_probe_names_the_target_and_keeps_its_diagnostic`（`errors.log` 逐字點名 `capability probe failed for <target>`、`ssh-debug/capability-probe-*.log` 帶 verbose 重試輸出、該節點不再被當成 cluster source） | — |
| O21 | node 收集 ssh 傳輸失敗 → exit 2＋該 target 的 ssh-debu… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_timeout_cleans_remote_workspace_and_returns_partial`（exit 2，且 bundle 內有 `ssh-debug/node-monitor01-*.log`，標頭記著 label 與 target）<br>`test_python_collect_node.CollectSingleNodeCliTests.test_disconnect_signal_cleans_remote_workspace_and_returns_partial` | node-collection-timeout |
| O22 | cluster ceph ssh 傳輸失敗 → exit 2＋ssh-debug log（… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_ssh_transport_failure_records_debug_log_and_partial_status`（exit 2、debug log 內容含 `# label: cluster-ceph` 與 `# target:`、`errors.log` 記 `exit=255`） | — |
| O23 | `HOSTS=()` 空清單 → exit 1＋明確訊息（HOSTS is empty） | ported | `test_python_collect_cli.CollectCliContractTests.test_an_empty_host_list_is_a_fatal_usage_failure` | — |
| O24 | `--kube-context` 含 shell metacharacter（`bad;c… | ported | `test_python_collect_rook.RookNamespaceTests.test_kube_context_metacharacters_are_rejected_before_any_command`（拒絕那半，斷言的是拒絕措辭而不是 usage 裡也有的旗標名）<br>`test_python_collect_rook.RookNamespaceTests.test_a_real_world_kube_context_passes_validation`（通過那半：`kubernetes-admin@kubernetes` 與 EKS ARN 兩種含 `@ : /` 的 context 通過驗證，隨後才因 inventory 不存在而失敗）<br>`test_python_collect_rook.RookNamespaceTests.test_empty_kube_context_preserves_current_context_semantics` | — |
| O25 | 偏好 direct runner：`ceph -s` 可直連時用純 `ceph`，不用 c… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_environment_records_direct_ceph_source_and_runner`<br>`test_python_collect_ceph.DirectCephSeedSelectionTests.test_inventory_seed_host_selects_the_direct_ceph_source` | cephadm-direct-no-prometheus |
| O26 | direct/sudo 都不通、cephadm 通 → fallback 用 `sudo … | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_ceph_source_falls_back_from_direct_to_noninteractive_sudo` | cephadm-sudo-fallback |
| O27 | `--kube-mode local`：rook 層用本機 kubectl（不經 ssh）… | ported | `test_python_collect_rook.LocalKubectlRunnerTests.test_local_kube_mode_collects_rook_evidence`<br>`test_python_collect_rook.InheritedKubeconfigTests.test_local_kubectl_inherits_the_workstation_kubeconfig` | rook-local-namespace-missing |
| O28 | `--kube-mode bogus` → exit 1＋說明 | ported | `test_python_collect_rook.RookNamespaceTests.test_unsupported_kube_mode_is_rejected_before_any_command` | — |
| O29 | `--prom-url`＋不可解析 `--since` → 前置檢查 exit 1＋說明 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_unparseable_since_is_rejected_when_the_dump_is_enabled`<br>`test_python_collect_prometheus.PrometheusDisabledTests.test_prometheus_options_without_the_url_stay_unused_and_unvalidated` | — |
| O30 | 非數字 `--prom-timeout` → exit 1 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_non_numeric_timeout_is_rejected` | — |
| O31 | `--prom-step 0` → exit 1 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_non_positive_step_is_rejected` | — |
| O32 | `--prom-url` 端到端：prometheus dump 落在 bundle 的 … | ported | `test_python_collect_prometheus.PrometheusHappyPathTests.test_prom_url_collects_metrics_evidence_for_matching_jobs`<br>`test_python_collect_prometheus.PrometheusEnvironmentRecordTests.test_a_partial_dump_still_records_the_jobs_it_matched` | prometheus-enabled |
| O33 | progress 預設開：stderr 顯示節點/探測/收集進度；stdout 只有 `b… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages`（預設 stderr 有進度、stdout 恰為 `bundle: …` 一行、`bundle:` 不出現在 stderr）<br>`test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node` | — |
| O34 | `--quiet`：stdout 仍印 `bundle:`，stderr 進度全部靜默 | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages`（`--quiet` 下 stdout 仍恰為 `bundle: …`，stderr 為空字串） | — |
| O35 | 中斷處理（Ctrl-C 契約）：`on_interrupt` → exit 130、ann… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_interruption_cleans_remote_and_workstation_workspaces`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_packaging_interruption_removes_reserved_archive_and_workdir` | interrupt-cleans-up |
| O36 | `--keep-workdir` 時中斷處理保留 workdir（`CLEANUP_KEE… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_keep_workdir_preserves_the_workstation_workspace_on_interrupt` | — |

## Blocked：目前沒有 blocked 情境

#36 移植了 node evidence surface 的其餘部分（`lsblk`、`dmesg`、ceph journal、
`iostat`、`chronyc`、`ntpq`、`timedatectl` 三連發、`systemd-timesyncd`
status／journal／config、`pvs`／`vgs`／`lvs`、`podman`／`docker ps`、`cephadm ls`、
`/etc` 檔案與 `/var/lib/ceph` 設定），因此 N2、N3、N4、N5、N7、N8、N9、N10、N13
從 blocked 變成 ported，見上表指向的 `NodeEvidenceSurfaceTests`。

移植過程中的契約問題（shell 的 `node_copy_file` 不寫 manifest，而 Python 的
archive acceptance 要求 manifest 與 evidence 一對一）已由 #8 裁定，記錄於
`docs/adr/0010-manifest-as-evidence-index.md` 與 `docs/python-rewrite-plan.md`
的 `## Contract Adjudications`。

inventory 的 138 個情境現在只剩兩種狀態：128 個 ported、10 個分類為 shell 實作
細節的 not-ported。offline gate 的覆蓋邊界仍受 `docs/differential-normalizer.md`
限制——differential run 比較的是工作機端契約，node 端等價性由上表的 N 系列
（黑箱 fake-command 測試）負責，而不是由 differential run 負責。

## Gate 宣告（2026-07-30）

#18 的 offline observable-contract equivalence gate 在此宣告通過。宣告的範圍就是
它證明的範圍，以下逐條寫明證到哪裡：

- 138 個情境全部有狀態（128 ported、10 not-ported），
  `tests/test_python_scenario_ledger.py` 機械檢查 ID 與 inventory 一致、每個
  `ported` 指向的 test class 與 **method** 都存在且通過、`not-ported` 正好是
  inventory 的十項實作細節。**這個檢查證明的是「指向的測試存在且通過」**，不是
  「該測試斷言了該列的每個子句」；後者由宣告前的雙軸 review 抽查，抽到不足的
  C9、C12、C21、B4–B8 已當場補強。其餘 128 列的逐列子句稽核已於 2026-07-31 由 #42
  完成（見 `docs/test-scenario-audit.md`）：42 列補上缺的斷言或缺的對應說明，沒有
  任何一列因此退回 blocked，宣告範圍不變。該檔的指紋機制讓 inventory 之後的任何
  改動都會強制重跑同一套子句稽核。
- 13 個 differential scenarios 在同一個 fake world 雙跑，`make validate` 離線
  可重複全綠（shell suite、Python suite、differential suite、Python 3.11 gate、
  shellcheck）。
- 宣告前的第二輪 Standards ／ Spec 雙軸 review 找出的 contract gaps 都已修正，
  見 `docs/python-rewrite-plan.md` 的 #18 段落。
- 宣告的是**工作機端** observable contract equivalence。node evidence surface
  的等價性是由 N 系列黑箱測試「斷言」，不是由 shell／Python 雙跑「證明」；
  依 ADR 0010，node manifest 的涵蓋範圍本來就刻意與 shell 不同。
- 唯一未結的裁定是 `--skip-logs` marker 的 `exit_code`（等 #8）。它不影響本宣告：
  node manifest 在 differential run 內來自共用的 canned archive，兩邊逐位元組
  相同，不在被比較的差異面內。
- real-lab qualification（#20）尚未執行，因此仍不可宣稱 qualification-ready。
