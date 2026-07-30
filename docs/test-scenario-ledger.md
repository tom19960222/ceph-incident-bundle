# 移植情境 Ledger（每個 shell 測試情境的 Python 覆蓋對照）

> 對應 issue #18（offline observable-contract equivalence gate）。
> 來源清單：`docs/test-scenario-inventory.md`；本檔逐項指出每個情境現在由哪些
> 通過的 Python test 覆蓋。`tests/test_python_scenario_ledger.py` 會機械檢查
> 這份對照：ID 必須與 inventory 完全一致，`ported` 的每個 test 都必須存在，
> `not-ported` 必須正好是 inventory 分類為【實作細節-不移植】的那十項。

## 總覽

| 狀態 | 數量 | 意義 |
|---|---|---|
| ported | 119 | 已有通過的 Python test 覆蓋該情境語意 |
| blocked | 9 | Python 實作尚未移植該行為，測試無從撰寫（見下方 blocked 清單） |
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
| R7 | 依序執行 8 個子測試檔並要求 exit 0 | not-ported | 測試 harness 本身，由 unittest discovery 取代 | — |

## 2. `tests/test-common.sh`（common.sh / bundle.sh helpers）

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| C1 | `json_escape` 正確跳脫 `"` 與 `\` | not-ported | `json_escape` 不存在；Python 用 `json` 模組 | — |
| C2 | `json_escape` 不呼叫 python3（shell-native） | not-ported | 「不呼叫 python3」是純 shell 約束 | — |
| C3 | `manifest_add` 寫出一行 JSONL，欄位 host/collector/a… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture` | — |
| C4 | `manifest_add` 拒絕非數字 exit_code（非 0 退出並說明 exit… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction` | — |
| C5 | `redact_file`：Password/SECRET/token/keyring/p… | ported | `test_python_content_safety.CollectContentSafetyTests.test_collect_redacts_sensitive_text_by_default`<br>`test_python_content_safety.CollectContentSafetyTests.test_redaction_handles_ascii_whitespace_and_a_long_single_line` | mixed-full-collection-redacted |
| C6 | `redact_file`：`-----BEGIN ... PRIVATE KEY----… | ported | `test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C7 | `redact_file`：多行 PEM 本體（含 base64 行與 END 行）整段遮… | ported | `test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C8 | `redact_file`：Ceph key 素材（`key = AQB...==`、`"… | ported | `test_python_content_safety.CollectContentSafetyTests.test_ceph_key_material_and_private_key_blocks_are_redacted` | — |
| C9 | `redact_file` 保留原檔權限（640） | ported | `test_python_content_safety.CollectContentSafetyTests.test_all_supported_compressed_text_codecs_are_redacted` | — |
| C10 | `redact_gz_file`：gzip 檔解壓-遮蔽-重壓，正常內容保留、秘密不外洩、… | ported | `test_python_content_safety.CollectContentSafetyTests.test_compressed_text_is_redacted_but_opaque_raw_evidence_is_unchanged` | mixed-full-collection-redacted |
| C11 | `redact_compressed_file` 支援 xz / bz2 / zst 三種… | ported | `test_python_content_safety.CollectContentSafetyTests.test_all_supported_compressed_text_codecs_are_redacted` | — |
| C12 | 重壓縮失敗時回非 0、原壓縮檔內容/mode 原封不動（不破壞原 artifact） | ported | `test_python_content_safety.CollectContentSafetyTests.test_recompress_failure_preserves_original_and_continues_other_redactions` | — |
| C13 | `redact_bundle_text`：早期壓縮檔遮蔽失敗 → 整體回 2，但**繼續*… | ported | `test_python_content_safety.CollectContentSafetyTests.test_recompress_failure_preserves_original_and_continues_other_redactions` | — |
| C14 | `redact_bundle_text`：`merged/` 純文字要遮蔽；`raw/` … | ported | `test_python_content_safety.CollectContentSafetyTests.test_compressed_text_is_redacted_but_opaque_raw_evidence_is_unchanged`<br>`test_python_content_safety.CollectContentSafetyTests.test_prometheus_metric_exclusion_is_anchored_to_the_cluster_layer` | mixed-full-collection-redacted |
| C15 | `enforce_node_log_caps`：遮蔽後超過 cap → 回 2、丟棄 me… | ported | `test_python_content_safety.CollectContentSafetyTests.test_post_redaction_payload_cap_discards_node_log_payload`<br>`test_python_content_safety.CollectContentSafetyTests.test_post_redaction_cap_counts_a_regular_payload_root` | — |
| C16 | `progress`：預設輸出訊息；`CEPH_INCIDENT_QUIET=1` 時完全… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages` | — |
| C17 | `progress` 只寫 stderr、不污染 stdout | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages`<br>`test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node` | — |
| C18 | `run_capture` 成功路徑：artifact 首行 `# host: ...` … | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture` | — |
| C19 | `run_capture` 失敗路徑：回傳指令的非 0 碼（7）、輸出仍寫入 artifa… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_failed_required_command_is_partial_and_keeps_other_evidence` | — |
| C20 | `run_capture` 缺 `--` 分隔符 → 致命錯誤並說明 | not-ported | `--` 呼叫慣例是 shell API 細節 | — |
| C21 | `run_capture` 以預設 20s timeout 包住指令，artifact 標… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_timed_out_command_is_truncated_and_writes_ssh_debug`<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_the_command_timeout_bounds_every_request` | — |
| C22 | artifact 檔名以 `-` 開頭仍可正確建立 | not-ported | 以 `-` 開頭的檔名是 shell 重導陷阱；`open()` 無此問題 | — |
| C23 | `run_capture` 不改變呼叫端 errexit 狀態 | not-ported | `errexit` 狀態是純 bash 語意 | — |

## 3. `tests/test-cephadm-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| CA1 | Happy path：產出 json/text 全套 artifact（status、he… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_seed_collects_json_and_text_evidence`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_recent_crash_inspection_caps_at_ten_and_avoids_collisions`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_manifest_records_every_capture` | cephadm-direct-no-prometheus |
| CA2 | 單一指令失敗（osd perf）→ 整體回 2 但**繼續收集**後續 artifact；… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_failed_required_command_is_partial_and_keeps_other_evidence` | cephadm-partial-command-failure |
| CA3 | `crash ls` 回非 JSON → 寫 `crash-info-skip.txt`（… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_unparseable_crash_list_is_skipped_without_failing_collection`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_empty_crash_list_collects_no_crash_info` | — |
| CA4 | runner=`direct`：遠端跑純 `ceph ...`，**不得**出現 `cep… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_direct_runner_runs_plain_ceph_without_sudo`<br>`test_python_collect_ceph.CollectDirectCephCliTests.test_direct_ceph_runs_plain_ceph_argv_over_ssh` | cephadm-direct-no-prometheus |
| CA5 | runner=`sudo`：遠端跑 `sudo -n ceph ...`，不得出現 `ce… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_sudo_runner_runs_sudo_n_ceph_and_never_cephadm_shell`<br>`test_python_collect_ceph.FakeSshArgvContractTests.test_cephadm_shell_is_never_answered` | cephadm-sudo-fallback |

## 4. `tests/test-node-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| N1 | Happy path 全套執行 → exit 0（失敗時傾印 errors.log 供除錯） | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_public_collect_streams_one_node_and_saves_basic_evidence` | — |
| N2 | 產出固定 artifact 清單：system/、resources/、storage/、… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N3 | 各 artifact 內容來自對應指令輸出（cephadm-ls 的 `"style":"… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N4 | optional 指令不存在（ntpq）→ artifact 寫 `SKIPPED: co… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N5 | timesyncd 設定檔與 conf.d 逐檔複製進 `time/systemd-tim… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N6 | log family 合併：`ceph.log.2.gz`＋`.1`＋現行檔合併為 `.m… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest` | — |
| N7 | `var-lib-ceph-configs/` 複製 config、**排除 keyrin… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N8 | optional 工具收到正確 argv（`iostat -xz 1 3`、`pvs/vg… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N9 | dmesg 經 `sudo -n` 執行 | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N10 | dmesg 與 ceph journal 用加重 timeout（120s，非 `--ti… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |
| N11 | journal 匯出＋/var/log 共用同一 byte cap；溢出 → exit 2… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_and_journal_share_one_fail_closed_payload_cap`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_var_log_cap_discards_all_payload_instead_of_truncating` | node-collection-timeout |
| N12 | 非 ceph 節點沒有 ceph journal（journalctl exit 1）→ … | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_finite_cap_without_sudo_marks_missing_journal_partial`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_skip_logs_writes_only_the_explicit_skip_artifact` | node-partial-and-unusable-archive |
| N13 | timesyncd 全缺（timedatectl/systemctl/journalctl… | blocked | blocked: node evidence surface not ported yet (#36); shell 端仍是唯一實作 | — |

## 5. `tests/test-var-log-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| V1 | 數字輪替（`.2.gz`→`.1`→現行）合併為單一 `.merged`，順序舊→新；產 … | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest` | — |
| V2 | gz/xz/bz2/zst 四種 codec＋日期式輪替（`-YYYYMMDD.<ext>… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_rotations_and_supported_codecs_merge_oldest_to_newest` | — |
| V3 | opaque 檔（zip、tar.gz、binary wtmp、journal）byte-… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources` | mixed-full-collection-redacted |
| V4 | `keep_originals=1` 才保留 `original/`（原始檔與壓縮檔皆逐位… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_keep_original_logs_is_opt_in_and_preserves_stored_bytes` | — |
| V5 | 總量超過 max_bytes → 回 2、`OVER-LIMIT.txt`、merged/… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_cap_discards_all_payload_instead_of_truncating` | — |
| V6 | 壞壓縮檔：raw 保留原檔、其他 family 照常收集、`ERRORS.tsv` 記 `… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_corrupt_and_missing_codecs_are_preserved_raw_and_partial` | — |
| V7 | 不追 symlink；`*.pem`/`*.key`（含壓縮、輪替變體）等敏感路徑不讀不抄… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources` | — |
| V8 | 來源檔內容 / mode / mtime 收集後完全不變 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_preserves_opaque_bytes_skips_sensitive_paths_and_never_mutates_sources` | — |
| V9 | 缺 codec（PATH 無 zstd）→ 回 2、raw 保留、`ERRORS.tsv`… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_corrupt_and_missing_codecs_are_preserved_raw_and_partial` | — |
| V10 | 頂層 family 輸出檔（`app.merged`）與同名目錄（`app/`、`app.… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection` | — |
| V11 | 零填充數字輪替（`.010`/`.09`/`.08`）以十進位排序（不可 octal 崩潰） | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection` | — |
| V12 | 檔案後段才出現 NUL → 視為 binary，raw 保留、不合併為文字 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_collision_safe_tree_base10_order_and_late_nul_detection` | — |
| V13 | 第二階段解壓失敗（第一次探測成功、正式解壓失敗）→ 回 2、壓縮原檔保留在 raw、部分解… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_later_decode_failures_roll_back_and_preserve_raw`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_safe_read_failure_is_partial_without_an_unsafe_fallback` | — |
| V14 | 掃描 metadata 本身有上限：超過 → 回 2、`SCAN-LIMIT.txt`、不… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_var_log_metadata_scan_limit_fails_closed_before_payload_reads`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_var_log_entry_count_limit_fails_closed_before_payload_reads` | — |

## 6. `tests/test-rook-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| K1 | 顯式 rook 模式、無 kubectl → exit 2＋`cluster/rook/S… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_local_kubectl_is_a_partial_bundle_with_a_reason` | — |
| K2 | 同上但帶 `--allow-skip`（auto 模式 fallback）→ exit 0… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_auto_mode_tolerates_an_unavailable_rook_layer_when_ceph_succeeds` | — |
| K3 | namespace 不存在 → exit 2；SKIPPED 檔含歸類原因（namespa… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_namespace_is_reported_with_the_raw_kubectl_error` | rook-local-namespace-missing |
| K4 | `--kube-context lab` 但 context 不存在 → exit 2；S… | ported | `test_python_collect_rook.RookUnavailableTests.test_missing_context_is_reported_with_the_raw_kubectl_error` | — |
| K5 | API server 連不上 → exit 2；SKIPPED 含 cannot conn… | ported | `test_python_collect_rook.RookUnavailableTests.test_unreachable_api_server_is_reported_with_the_raw_kubectl_error` | — |
| K6 | 未設 `CEPH_INCIDENT_ALLOW_KUBECTL_EXEC` → toolb… | ported | `test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_toolbox_is_skipped_and_no_exec_is_ever_issued`<br>`test_python_collect_rook.KubectlExecIsNeverEnabledTests.test_toolbox_lookup_preserves_the_shell_read_only_command_ledger`<br>`test_python_collect_rook.FakeKubectlArgvContractTests.test_kubectl_exec_is_never_answered` | rook-remote-kubectl |
| K7 | Happy path（含 toolbox）：pods-wide / events / ro… | ported | `test_python_collect_rook.LocalKubectlRunnerTests.test_local_kube_mode_collects_rook_evidence`<br>`test_python_collect_rook.RookOptionalArtifactTests.test_operator_log_uses_the_since_window` | rook-remote-kubectl |
| K8 | external cluster：`--namespace rook-ceph-exter… | ported | `test_python_collect_rook.RookNamespaceTests.test_external_cluster_splits_resource_and_operator_namespaces`<br>`test_python_collect_rook.RookNamespaceTests.test_operator_namespace_has_its_own_rook_ceph_default` | — |
| K9 | remote 模式（`--ssh-target --ssh-key --kube-cont… | ported | `test_python_collect_rook.RemoteKubectlRunnerTests.test_remote_kube_mode_runs_kubectl_on_the_inventory_node`<br>`test_python_collect_rook.RemoteKubectlRunnerTests.test_kube_context_is_forwarded_to_every_kubectl_invocation` | rook-remote-kubectl |
| K10 | operator pod 查詢失敗不可讓收集中止（set -e 回歸）：exit 0＋`o… | ported | `test_python_collect_rook.RookOptionalArtifactTests.test_operator_pod_lookup_failure_skips_only_the_operator_log` | — |

## 7. `tests/test-prom-collector.sh`

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| P1 | duration 解析：`90/45s/30m/24h/7d/2w` 正確換算；拒絕 `y… | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_accepted_windows_reach_the_dump`<br>`test_python_collect_prometheus.PrometheusOptionValidationTests.test_unparseable_since_is_rejected_when_the_dump_is_enabled` | — |
| P2 | auto step：15s 下限；7d → ceil(604800/10000)=61 | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_long_window_raises_the_auto_step_above_the_floor`<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_an_explicit_step_overrides_the_automatic_one` | — |
| P3 | URL 遮蔽：`http://u:sekrit@h` → `u:***@h`；無憑證原樣 | ported | `test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_a_masked_url_is_recorded_in_the_bundle_environment`<br>`test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_url_credentials_never_reach_an_artifact_or_a_message` | — |
| P4 | 前置指令檢查：缺 python3 時失敗並點名 python3 | not-ported | 工作機 python3 前置檢查；Python runtime 本身即滿足 | — |
| P5 | Happy path：buildinfo.json、targets.json、dump-i… | ported | `test_python_collect_prometheus.PrometheusHappyPathTests.test_prom_url_collects_metrics_evidence_for_matching_jobs`<br>`test_python_collect_prometheus.PrometheusHappyPathTests.test_requests_match_the_shell_curl_argv` | prometheus-enabled |
| P6 | Prometheus 連不上 → 回 2、`SKIPPED.txt`（not reacha… | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_an_unreachable_server_skips_the_layer_with_the_curl_error` | — |
| P6a | curl 失敗診斷即使回顯含 basic-auth 的完整 URL，也不得把密碼寫入 bu… | ported | `test_python_collect_prometheus.PrometheusCredentialMaskingTests.test_url_credentials_never_reach_an_artifact_or_a_message` | — |
| P7 | `--job-regex` 全不匹配 → 回 2；SKIPPED 說明 no scrape… | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_an_empty_job_listing_names_the_jobs_it_saw` | — |
| P8 | 缺 python3（進入收集後）→ 回 2、SKIPPED 點名 python3 | not-ported | 同 P4：收集期間的 python3 依賴檢查不存在於 Python 實作 | — |
| P9 | 單一 metric query_range 失敗 → 回 2；其他 metric 照常 d… | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_metric_query_leaves_no_dump_and_keeps_the_rest`<br>`test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_malformed_metric_response_is_not_kept_as_evidence` | — |
| P10 | `--budget 0` 觸發截斷 → 回 2；index.txt 記 TRUNCATED… | ported | `test_python_collect_prometheus.PrometheusBudgetTests.test_an_exhausted_budget_truncates_the_dump_and_records_it` | — |
| P11 | job 名含不安全字元（`"`）→ 回 2；errors.log 記 unsafe nam… | ported | `test_python_collect_prometheus.PrometheusUnsafeNameTests.test_an_unsafe_job_name_is_skipped_and_the_safe_jobs_are_kept`<br>`test_python_collect_prometheus.PrometheusUnsafeNameTests.test_job_names_never_collide_with_fixed_prometheus_artifacts` | — |
| P12 | 7d window → `step=61` | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_long_window_raises_the_auto_step_above_the_floor` | prometheus-enabled |
| P13 | redaction 排除 `cluster/prometheus/<job>/` 的 me… | ported | `test_python_content_safety.CollectContentSafetyTests.test_prometheus_metric_exclusion_is_anchored_to_the_cluster_layer` | — |
| P14 | targets 抓取失敗 → 回 2；不留 targets.json；buildinfo … | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_targets_fetch_keeps_the_metrics_dump_running` | — |
| P15 | job 列表抓取失敗 → 回 2、SKIPPED 說 job listing failed | ported | `test_python_collect_prometheus.PrometheusUnavailableTests.test_a_failed_job_listing_skips_the_layer_after_keeping_what_worked`<br>`test_python_collect_prometheus.PrometheusUnavailableTests.test_a_malformed_job_listing_is_a_skip_not_a_crash` | — |
| P16 | metric 名稱列表失敗 → 回 2；index.txt 記 FAILED: metri… | ported | `test_python_collect_prometheus.PrometheusPartialCollectionTests.test_a_failed_metric_listing_marks_its_job_and_keeps_going` | — |
| P17 | `--url` 尾端斜線 → 請求 URL 不得出現 `//api` 雙斜線 | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_trailing_slash_never_doubles_in_a_request_url` | — |
| P18 | `--job-regex '-zzz'`（dash 開頭）→ 回 2 且不得把 regex… | ported | `test_python_collect_prometheus.PrometheusQueryShapeTests.test_a_dash_leading_job_filter_matches_nothing_without_an_option_error`<br>`test_python_collect_prometheus.PrometheusQueryShapeTests.test_the_job_filter_uses_the_shell_posix_ere_dialect` | — |

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
| B9 | 允許副檔名內夾帶未遮蔽 PEM 本體（`-----BEGIN OPENSSH PRIVAT… | ported | `test_python_verify.VerifyCliTests.test_private_key_and_ceph_key_content_are_rejected` | verify-failure-keeps-workdir |
| B10 | 非法 tar.gz → 失敗且說 invalid archive | ported | `test_python_verify.VerifyCliTests.test_corrupt_and_truncated_archives_are_rejected`<br>`test_python_verify.VerifyCliTests.test_archive_with_invalid_deflate_body_is_rejected_without_traceback` | — |
| B11 | 多餘參數 → 非 0＋Usage | ported | `test_python_verify.VerifyCliTests.test_extra_argument_prints_usage_to_stderr` | — |

## 9. `tests/test-collect.sh`（run/collect.sh 端到端編排）

| ID | 情境 | 狀態 | Python 覆蓋 / 理由 | differential |
|---|---|---|---|---|
| O1 | `--help` → exit 0；usage 文件化所有主要旗標（--kube-cont… | ported | `test_python_collect_cli.CollectCliContractTests.test_help_documents_every_supported_collect_option` | — |
| O2 | inventory 不存在 → exit 1 | ported | `test_python_collect_cli.CollectCliContractTests.test_a_missing_inventory_names_the_file_and_writes_nothing` | — |
| O3 | inventory 是宣告式資料、**不得**被當 shell 執行：含 `$(touch… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes` | — |
| O4 | host alias 含 `../` → exit 1，且未在輸出根外建立檔案 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes` | — |
| O5 | SSH target 形如 `--ProxyCommand=...` → 失敗且**未曾*… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_unsafe_inventory_is_rejected_before_ssh_or_output_writes`<br>`test_python_collect_ceph.DirectCephSeedSelectionTests.test_unsafe_seed_argument_fails_before_any_ssh` | — |
| O6 | auto 模式雙層收集 happy path：cluster/ceph 來自 ceph 節… | ported | `test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node` | mixed-full-collection-redacted |
| O7 | `--no-trust-ssh-host-key`：不再帶 accept-new，reda… | ported | `test_python_content_safety.CollectContentSafetyTests.test_redaction_flag_precedence_is_independent_from_host_key_trust` | — |
| O8 | `--no-redact`：秘密原文保留於 bundle；host key trust 預… | ported | `test_python_content_safety.CollectContentSafetyTests.test_no_redact_keeps_sensitive_text_and_still_writes_the_log` | mixed-full-collection-unredacted |
| O9 | 顯式 `--trust-ssh-host-key --redact` 等同預設行為 | ported | `test_python_content_safety.CollectContentSafetyTests.test_redaction_flag_precedence_is_independent_from_host_key_trust` | mixed-full-collection-unredacted |
| O10 | auto、無任何 capable 節點 → exit 2；`cluster/ceph/SK… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_an_unreachable_explicit_seed_never_falls_back_to_inventory` | auto-without-any-cluster-source |
| O11 | 顯式 `--mode cephadm --seed`：只收 ceph 層，全程**不得**… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_direct_runner_runs_plain_ceph_without_sudo` | cephadm-direct-no-prometheus |
| O12 | 顯式 seed 但 direct/sudo runner 都不通、且 cephadm-sh… | ported | `test_python_collect_ceph.DirectCephRunnerSeamTests.test_sudo_runner_runs_sudo_n_ceph_and_never_cephadm_shell`<br>`test_python_collect_ceph.FakeSshArgvContractTests.test_cephadm_shell_is_never_answered` | — |
| O13 | 兩台 cephadm 節點：cluster ceph 只從**第一台**收，不重複 | ported | `test_python_collect_ceph.DirectCephSeedSelectionTests.test_collect_without_a_seed_auto_selects_the_first_capable_node` | — |
| O14 | node 回傳 tar 缺 manifest → 該節點 SKIPPED、整體 exit 2 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction` | node-partial-and-unusable-archive |
| O15 | node 回傳非 tar → SKIPPED、exit 2 | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_untrusted_node_archives_are_rejected_before_extraction` | — |
| O16 | 單一 host 收集失敗（remote exit 2）→ 整體 exit 2、bundle… | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_one_failed_node_keeps_the_other_nodes_and_bundle_partial`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_valid_archive_is_preserved_when_node_collector_is_partial` | cephadm-partial-command-failure |
| O17 | 中途 abort → trap 清掉 workdir，`--out` 下不留 `tmp.*` | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_interruption_cleans_remote_and_workstation_workspaces` | interrupt-cleans-up |
| O18 | verify 失敗（node 夾帶 `.pem`）→ exit 1、**不產** bund… | ported | `test_python_content_safety.CollectContentSafetyTests.test_pre_package_verification_failure_keeps_a_diagnostic_workdir`<br>`test_python_content_safety.CollectContentSafetyTests.test_packaged_archive_verification_failure_removes_the_candidate` | verify-failure-keeps-workdir |
| O19 | auto、只有 kube 節點且 namespace 不存在、無 ceph → exit … | ported | `test_python_collect_rook.RookPartialCollectionTests.test_rook_partial_does_not_hide_a_successful_node`<br>`test_python_collect_rook.RookUnavailableTests.test_missing_namespace_is_reported_with_the_raw_kubectl_error` | rook-local-namespace-missing |
| O20 | 能力探測 ssh 失敗的節點 → errors.log 記 `capability pro… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_ssh_transport_failure_records_debug_log_and_partial_status` | — |
| O21 | node 收集 ssh 傳輸失敗 → exit 2＋該 target 的 ssh-debu… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_timeout_cleans_remote_workspace_and_returns_partial`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_disconnect_signal_cleans_remote_workspace_and_returns_partial` | node-collection-timeout |
| O22 | cluster ceph ssh 傳輸失敗 → exit 2＋ssh-debug log（… | ported | `test_python_collect_ceph.DirectCephFailureSemanticsTests.test_ssh_transport_failure_records_debug_log_and_partial_status` | — |
| O23 | `HOSTS=()` 空清單 → exit 1＋明確訊息（HOSTS is empty） | ported | `test_python_collect_cli.CollectCliContractTests.test_an_empty_host_list_is_a_fatal_usage_failure` | — |
| O24 | `--kube-context` 含 shell metacharacter（`bad;c… | ported | `test_python_collect_rook.RookNamespaceTests.test_kube_context_metacharacters_are_rejected_before_any_command`<br>`test_python_collect_rook.RookNamespaceTests.test_empty_kube_context_preserves_current_context_semantics` | — |
| O25 | 偏好 direct runner：`ceph -s` 可直連時用純 `ceph`，不用 c… | ported | `test_python_collect_ceph.CollectDirectCephCliTests.test_environment_records_direct_ceph_source_and_runner`<br>`test_python_collect_ceph.DirectCephSeedSelectionTests.test_inventory_seed_host_selects_the_direct_ceph_source` | cephadm-direct-no-prometheus |
| O26 | direct/sudo 都不通、cephadm 通 → fallback 用 `sudo … | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_ceph_source_falls_back_from_direct_to_noninteractive_sudo` | cephadm-sudo-fallback |
| O27 | `--kube-mode local`：rook 層用本機 kubectl（不經 ssh）… | ported | `test_python_collect_rook.LocalKubectlRunnerTests.test_local_kube_mode_collects_rook_evidence`<br>`test_python_collect_rook.InheritedKubeconfigTests.test_local_kubectl_inherits_the_workstation_kubeconfig` | rook-local-namespace-missing |
| O28 | `--kube-mode bogus` → exit 1＋說明 | ported | `test_python_collect_rook.RookNamespaceTests.test_unsupported_kube_mode_is_rejected_before_any_command` | — |
| O29 | `--prom-url`＋不可解析 `--since` → 前置檢查 exit 1＋說明 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_unparseable_since_is_rejected_when_the_dump_is_enabled`<br>`test_python_collect_prometheus.PrometheusDisabledTests.test_prometheus_options_without_the_url_stay_unused_and_unvalidated` | — |
| O30 | 非數字 `--prom-timeout` → exit 1 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_non_numeric_timeout_is_rejected` | — |
| O31 | `--prom-step 0` → exit 1 | ported | `test_python_collect_prometheus.PrometheusOptionValidationTests.test_non_positive_step_is_rejected` | — |
| O32 | `--prom-url` 端到端：prometheus dump 落在 bundle 的 … | ported | `test_python_collect_prometheus.PrometheusHappyPathTests.test_prom_url_collects_metrics_evidence_for_matching_jobs`<br>`test_python_collect_prometheus.PrometheusEnvironmentRecordTests.test_a_partial_dump_still_records_the_jobs_it_matched` | prometheus-enabled |
| O33 | progress 預設開：stderr 顯示節點/探測/收集進度；stdout 只有 `b… | ported | `test_python_collect_orchestration.MultiSourceOrchestrationTests.test_one_collect_combines_all_evidence_paths_and_every_inventory_node`<br>`test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages` | — |
| O34 | `--quiet`：stdout 仍印 `bundle:`，stderr 進度全部靜默 | ported | `test_python_collect_orchestration.CephRunnerSelectionTests.test_quiet_suppresses_default_progress_messages` | — |
| O35 | 中斷處理（Ctrl-C 契約）：`on_interrupt` → exit 130、ann… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_interruption_cleans_remote_and_workstation_workspaces`<br>`test_python_collect_node.CollectSingleNodeCliTests.test_packaging_interruption_removes_reserved_archive_and_workdir` | interrupt-cleans-up |
| O36 | `--keep-workdir` 時中斷處理保留 workdir（`CLEANUP_KEE… | ported | `test_python_collect_node.CollectSingleNodeCliTests.test_keep_workdir_preserves_the_workstation_workspace_on_interrupt` | — |

## Blocked：node evidence surface 尚未移植

以下 9 個情境（N2, N3, N4, N5, N7, N8, N9, N10, N13）沒有對應的 Python test，
原因不是測試缺漏，而是 Python node collector 目前只實作 #11 的七個 basic
commands 加上 #12 的 `/var/log`／journal。shell node collector 另外收集
`lsblk`、`dmesg`（加重 timeout）、ceph journal、`iostat`、`chronyc`、`ntpq`、
`timedatectl` 三連發、`systemd-timesyncd` status／journal／config、`pvs`／`vgs`／
`lvs`、`podman`／`docker ps`、`cephadm ls`、`/etc` 檔案與 `/var/lib/ceph` 設定
（排除 keyring）——這些行為在 Python candidate 沒有實作路徑。

移植它至少牽動一個尚未裁定的契約問題：shell 的 `node_copy_file` 複製檔案時
**不寫 manifest**，而 Python 的 node archive acceptance 要求 manifest 與
evidence 一對一。要嘛 Python 為複製檔補 manifest（與 shell 的 manifest 內容
不同），要嘛放寬 acceptance（降低安全邊界）。依 `docs/python-rewrite-plan.md`
的規定，這種等價／安全條件的取捨必須回到 parent spec #8 裁定，不能在本 gate
內自行決定，因此本 gate 只把它記錄成 blocked，並由 #36 承接。

在這些情境移植完成前，offline gate 只能宣稱：**工作機端**（cluster evidence、
orchestration、content safety、verify、bundle lifecycle）已達 observable
contract equivalence；node evidence surface 尚未等價。
