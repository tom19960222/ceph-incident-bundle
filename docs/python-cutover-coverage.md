# Python cutover coverage record

> **Historical record:** Do not read this document or treat its scenario ledger as a current
> requirement unless investigating the Python cutover.

This is a non-runnable deletion audit for the shell qualification suite removed at the Python
cutover. It is not operator guidance and none of the historical paths, commands, flags, layouts,
or exit conventions below are supported interfaces. Current usage is in `README.md`; rollback is
through Git history, a tag, or a previous release artifact.

The audit uses the authoritative 145-scenario inventory, including the seven evidence-window
scenarios frozen after the original 138-scenario count. “Covered” means that the
still-required incident-collection behavior has a live Python public-seam test. “Obsolete” is one
of the 134 behavior-bearing historical scenarios whose old contract issue #85 explicitly replaced
or excluded. “Shell-only” is one of the 11 implementation details that never represented portable
product behavior. Neither disposition is a missing Python behavior. Test names below are
intentionally public black-box or component-seam tests, not private-helper contracts.

| Deleted scenarios | Disposition | Live Python evidence or reason |
|---|---|---|
| R1, R2, R7 | Shell-only | Shell source/layout and shell harness implementation details. |
| R3, R6 | Covered | `test_cli.InstalledCliTests.test_collect_rejected_at_startup_has_controlled_nondelivery`; `test_inventory.LoadInventoryTests.test_invalid_inventory_reports_all_practical_problems_together`. |
| R4, R5 | Obsolete | The standalone verifier is outside issue #85 and is deleted, not replaced. |
| C1, C2, C20, C22, C23 | Shell-only | Shell library loading, shell traps, and shell temporary-file mechanics. |
| C3, C4 | Obsolete | The shell JSONL manifest writer and its untyped exit-code check are outside issue #85; the Python bundle intentionally has no manifest. |
| C18, C19, C21 | Covered | `test_remote_collector.RemoteCollectorTests.test_failed_probe_does_not_stop_the_next_fixed_probe`; `test_bundle.IncidentBundlePublicationTests.test_admitted_state_is_published_with_the_exact_bundle_surface`; `test_node_archive.NodeArchiveAdmissionTests.test_every_unsafe_archive_is_rejected_before_extraction`. |
| C5, C6, C7, C8, C9, C10, C11, C12, C13, C14, C15 | Obsolete | Shell redaction heuristics were explicitly replaced by raw evidence in issue #85. |
| C16, C17 | Obsolete | Shell progress/quiet flags are not in the Python public CLI. |
| CA1, CA2, CA3 | Covered | `test_remote_collector.RemoteCollectorTests.test_selected_ceph_source_runs_the_exact_catalog_then_ten_crash_details`; `test_remote_collector.RemoteCollectorTests.test_ceph_probe_failure_preserves_raw_bytes_and_later_probes_continue`; `test_remote_collector.RemoteCollectorTests.test_malformed_successful_crash_list_is_partial_without_detail_probes`. |
| CA4, CA5 | Obsolete | Retired shell runner discovery, sudo, and `cephadm shell`; the Python collector is direct-session only. |
| N1, N2, N3, N4, N5, N7, N8, N9, N10, N12, N13 | Covered | `test_remote_collector.RemoteCollectorTests.test_node_probe_catalog_is_exactly_the_non_journal_baseline_and_time_catalog`; `test_remote_collector.RemoteCollectorTests.test_fixed_configuration_files_are_copied_as_raw_regular_bytes`; `test_remote_collector.RemoteCollectorTests.test_timeout_preserves_partial_streams_and_records_a_timeout_error`; `test_remote_collector.RemoteCollectorTests.test_failed_probe_does_not_stop_the_next_fixed_probe`. |
| N6, N11 | Obsolete | Shell-side merged log files and proactive byte caps were replaced by raw per-source captures without a total cap. |
| V1, V2, V4, V6, V9, V10, V11, V12, V13 | Obsolete | Shell log merging, codec sniffing, decompression, and original-file naming are not Python contracts. |
| V3, V7, V8 | Covered | `test_remote_collector.RemoteCollectorTests.test_one_node_cutoff_selects_log_mtimes_and_the_journal_probe`; `test_remote_collector.RemoteCollectorTests.test_log_directory_type_races_are_normal_omissions`; `test_remote_collector.RemoteCollectorTests.test_log_file_failures_are_partial_and_do_not_stop_later_files`. |
| V5, V14 | Obsolete | Proactive shell file and total-log caps were explicitly replaced by uncapped raw evidence. |
| V15 | Covered | `test_remote_collector.RemoteCollectorTests.test_one_node_cutoff_selects_log_mtimes_and_the_journal_probe`. The Python contract applies one normalized cutoff to journal and raw regular log selection. |
| V16, V17, V18, V19 | Obsolete | Shell family/stream cross-boundary merging, unknown-mtime inclusion, and “window before byte cap” rules were replaced by raw per-file selection with no proactive byte cap. Current uncapped archive admission is independently covered by `test_node_archive.NodeArchiveAdmissionTests.test_highly_compressible_archive_is_admitted_without_a_total_cap`; it does not preserve the retired merge-and-cap ordering contract. |
| V20 | Shell-only | The shell seam accepted an untyped cutoff string; the Python remote seam requires canonical integer seconds, so the shell-only runtime type check has no Python contract. |
| K1, K2, K3, K4, K5, K7, K8, K10 | Covered | `test_kubernetes.KubernetesCollectionTests.test_fixed_get_catalog_is_captured_in_order_with_explicit_scope`; `test_kubernetes.KubernetesCollectionTests.test_successful_control_schedules_each_container_log_in_stable_order`; `test_kubernetes.KubernetesCollectionTests.test_failed_and_unparseable_controls_do_not_stop_independent_probes`; `test_kubernetes.KubernetesCollectionTests.test_timed_out_log_preserves_partial_bytes_and_continues`. |
| K6 | Obsolete | The opt-in `kubectl exec` fallback is prohibited. |
| K9 | Obsolete | Remote kubectl discovery is not a supported Python path; Kubernetes collection is local. |
| P1, P2, P5, P6, P6a, P7, P9, P11, P12, P13, P14, P15, P16, P17, P18 | Covered | `test_prometheus.PrometheusCollectionTests.test_fixed_controls_and_per_job_discovery_are_raw_ordered_and_path_safe`; `test_prometheus.PrometheusCollectionTests.test_filtered_ranges_keep_pair_order_raw_failures_and_complete_admission`; `test_prometheus.PrometheusCollectionTests.test_timeout_is_per_blocking_progress_and_preserves_partial_bytes`; `test_prometheus.PrometheusCollectionTests.test_embedded_credentials_remain_exact_without_content_special_cases`. |
| P3 | Obsolete | Shell URL masking was replaced by raw exact captures. |
| P4, P8 | Shell-only | Shell subprocess/prerequisite behavior; Python uses the standard-library HTTP client. |
| P10 | Obsolete | The shell total-budget contract was replaced by per-blocking-progress timeouts. |
| B1, B2, B3, B4, B5, B6, B7, B8, B9, B10, B11 | Obsolete | All are standalone verifier behaviors, outside issue #85. |
| O1, O3, O4, O5, O6, O7, O8, O10, O11, O12, O13, O15, O18, O19, O21, O22, O23, O24, O25, O27, O28, O29, O30, O31, O32, O33, O34, O35, O36 | Covered | `test_cli.InstalledCliTests.test_installed_cli_runs_all_configured_sources_in_one_fixed_order`; `test_cli.InstalledCliTests.test_multi_node_failure_keeps_later_admitted_evidence_in_inventory_order`; `test_cli.InstalledCliTests.test_ctrl_c_requests_remote_cleanup_and_delivers_no_bundle`; `test_cli.InstalledCliTests.test_workstation_cleanup_residue_is_a_truthful_partial_delivery`; `test_bundle.IncidentBundlePublicationTests.test_existing_final_destination_is_never_replaced`; `test_collect.TopLevelCollectionTests.test_invalid_startup_never_reaches_the_ssh_boundary`. |
| O2, O9, O14, O16, O17, O20, O26 | Obsolete | Retired shell flags, shell dependency checks, shell output layout, and standalone verifier orchestration. |
| O37 | Covered | `test_cli.InstalledCliTests.test_enormous_since_is_rejected_before_workspace_or_ssh`; `test_cli.InstalledCliTests.test_unrenderable_normalized_since_is_rejected_before_activity`; `test_collect.TopLevelCollectionTests.test_enormous_since_is_controlled_before_workspace_creation`. Invalid evidence windows fail before collection activity. |

The matrix accounts for R1–R7, C1–C23, CA1–CA5, N1–N13, V1–V20, K1–K10,
P1–P18 plus P6a, B1–B11, and O1–O37: 145 scenarios total. No behavior-bearing
scenario required by issue #85 lacks a live Python test.
