# Offline CPython 3.10 qualification record

> **Historical evidence:** Do not read this document or treat its qualified snapshot as a current
> requirement unless investigating the historical qualification.

## Verdict

**PASS — evidence-only qualification.** The exact post-#101 head completed the fully offline installed-product suite under actual CPython 3.10. No production code, production test seam, or architecture changed for this qualification.

This is offline contract evidence, not real-lab acceptance. It uses executable fake SSH and `kubectl` programs, fixture filesystems, and loopback Prometheus; it does not contact a lab or other infrastructure.

## Immutable subject and runtime

| Item | Recorded value |
| --- | --- |
| Qualified commit | `a0f8aeb53eeb7494e682def222ba08226b0600d7` |
| Branch at start | `codex/issue-102-offline-qualification` |
| Production interpreter | `/Users/ikaros/.pyenv/versions/3.10.19/bin/python3.10` |
| Runtime identity | CPython 3.10.19; `version_info = [3, 10, 19, "final", 0]` |
| Built wheel | `ceph_incident_bundle-0.1.0-py3-none-any.whl` |
| Wheel SHA-256 | `e0c00cd0f1e44d398f720dce4c64fd98774473bbb13f614ab47d47d35f2aba91` |

The interpreter was pre-provisioned and selected by its absolute path. The qualification did not change global Python, install a production dependency, or select a different remote interpreter.

## Executed gate

```text
make validate PYTHON=/Users/ikaros/.pyenv/versions/3.10.19/bin/python3.10
```

`validation/run_offline.py` copied the exact source into a fresh temporary directory, built one wheel with `pip wheel --no-index --no-build-isolation --no-deps`, created a fresh venv, installed only that wheel with `--no-index --no-deps`, set `CEPH_INCIDENT_BUNDLE_COMMAND` to the installed console command, and ran the full `unittest discover -s tests/python -v` suite in one invocation.

Final exact-head result: **193 tests passed in 449.847 s; gate exit 0** (wall time 457.76 s). The same command initially hit the execution sandbox's local-loopback bind restriction: 12 tests failed before their `127.0.0.1:0` servers started with `PermissionError: [Errno 1] Operation not permitted`. It was rerun unchanged with local loopback permitted; that is the passing result above. This was an execution-environment restriction, not an asserted product failure.

## Clause audit and live evidence

| #102 obligation | Existing public-seam evidence exercised by the passing gate |
| --- | --- |
| Installed CPython 3.10 product and standalone remote subprocess | `test_cli.InstalledCliTests.test_installed_cli_entrypoint_uses_tested_cpython_3_10`; `test_remote_collector.RemoteCollectorTests.test_external_subprocess_runs_the_full_catalog_in_documented_order` |
| Highest installed CLI; direct collection, inventory, archive, and publication seams | `test_cli.InstalledCliTests.test_collect_uses_one_ssh_and_delivers_one_complete_bundle`; `test_collect.TopLevelCollectionTests.test_unexpected_node_exception_still_publishes_truthful_partial_bundle`; `test_inventory.DraftInventoryTests.test_hosts_are_converted_in_order_with_all_generated_defaults`; `test_inventory.LoadInventoryTests.test_invalid_inventory_reports_all_practical_problems_together`; `test_node_archive.NodeArchiveAdmissionTests.test_complete_archive_is_privately_extracted_then_promoted`; `test_bundle.IncidentBundlePublicationTests.test_admitted_state_is_published_with_the_exact_bundle_surface` |
| Shared ordered boundary flow and ordinary-exception continuation | `test_cli.InstalledCliTests.test_installed_cli_runs_all_configured_sources_in_one_fixed_order`; `test_collect.TopLevelCollectionTests.test_node_problems_are_accumulated_in_inventory_order_after_an_exception`; `test_collect.TopLevelCollectionTests.test_unexpected_kubernetes_exception_keeps_publication_isolated`; `test_collect.TopLevelCollectionTests.test_unexpected_prometheus_exception_keeps_private_staging_out_of_bundle` |
| One SSH, unchanged source stdin, fixed remote Python, diagnostics, aggregate partial | `test_cli.InstalledCliTests.test_collect_uses_one_ssh_and_delivers_one_complete_bundle`; `test_cli.InstalledCliTests.test_installed_collect_runs_direct_ceph_only_in_selected_existing_session`; `test_cli.InstalledCliTests.test_ssh_diagnostics_are_incrementally_escaped_without_losing_delivery`; `test_cli.InstalledCliTests.test_large_ssh_diagnostics_and_nonzero_exit_keep_complete_evidence` |
| Remote all-success and complete-archive partial exits | `test_remote_collector.RemoteCollectorTests.test_hostname_probe_is_streamed_as_a_complete_node_archive`; `test_remote_collector.RemoteCollectorTests.test_failed_hostname_still_streams_capture_and_removes_remote_workspace`; `test_cli.InstalledCliTests.test_complete_archive_with_selected_file_failure_is_admitted_as_partial` |
| Complete-before-extract archive admission and all unsafe categories | `test_node_archive.NodeArchiveAdmissionTests.test_every_unsafe_archive_is_rejected_before_extraction`; `test_node_archive.NodeArchiveAdmissionTests.test_links_devices_fifos_and_other_special_members_are_rejected`; `test_node_archive.NodeArchiveAdmissionTests.test_required_shape_and_ceph_authorization_are_fail_closed`; `test_node_archive.NodeArchiveAdmissionTests.test_corrupt_and_truncated_streams_are_rejected` |
| Kubernetes explicit scope, fixed read-only surface, fan-out, isolation, timeout | `test_kubernetes.KubernetesCollectionTests.test_fixed_get_catalog_is_captured_in_order_with_explicit_scope`; `test_kubernetes.KubernetesCollectionTests.test_successful_control_schedules_each_container_log_in_stable_order`; `test_kubernetes.KubernetesCollectionTests.test_failed_and_unparseable_controls_do_not_stop_independent_probes`; `test_kubernetes.KubernetesCollectionTests.test_timed_out_log_preserves_partial_bytes_and_continues` |
| Prometheus loopback GET/order/raw/discovery/filter/pair isolation/timeout/credentials/no caps | `test_prometheus.PrometheusCollectionTests.test_fixed_controls_and_per_job_discovery_are_raw_ordered_and_path_safe`; `test_prometheus.PrometheusCollectionTests.test_filtered_ranges_keep_pair_order_raw_failures_and_complete_admission`; `test_prometheus.PrometheusCollectionTests.test_timeout_is_per_blocking_progress_and_preserves_partial_bytes`; `test_prometheus.PrometheusCollectionTests.test_embedded_credentials_remain_exact_without_content_special_cases` |
| Installed delivery, partial and metadata-only outcomes, exact terminal output/nondelivery | `test_cli.InstalledCliTests.test_collect_rejected_at_startup_has_controlled_nondelivery`; `test_cli.InstalledCliTests.test_connection_failure_delivers_metadata_only_partial_bundle`; `test_cli.InstalledCliTests.test_partial_bundle_is_delivered_when_stderr_cannot_be_written`; `test_cli.InstalledCliTests.test_publication_failure_has_controlled_installed_cli_nondelivery` |
| Cleanup, interrupt, private staging, no overwrite, owned-state safety | `test_cli.InstalledCliTests.test_ctrl_c_requests_remote_cleanup_and_delivers_no_bundle`; `test_cli.InstalledCliTests.test_workstation_cleanup_residue_is_a_truthful_partial_delivery`; `test_bundle.IncidentBundlePublicationTests.test_existing_final_destination_is_never_replaced`; `test_bundle.IncidentBundlePublicationTests.test_workspace_swap_at_cleanup_entry_never_deletes_replacement` |
| Post-cutover historical product paths, stale active instructions, and wheel surface | `test_python_cutover.PythonOnlyRepositorySurfaceTests.test_historical_product_and_qualification_paths_are_absent`; `test_python_cutover.PythonOnlyRepositorySurfaceTests.test_repository_has_no_legacy_executable_or_stale_active_invocation`; `test_python_cutover.InstalledWheelSurfaceTests.test_clean_wheel_contains_only_python_product_and_two_subcommands` |

The listed tests are existing module or installed-process interfaces. This qualification adds no private-helper assertion and no production test switch.

## Phase 2 deferrals — exactly five matrices

The following are deliberately deferred and are not V1 blockers:

1. Simultaneous large SSH stdin/stdout/stderr stress.
2. Forced termination of an uncooperative timed-out process.
3. Oversized archive, capacity, and disk-exhaustion stress.
4. Exhaustive mid-write or package-publication fault injection.
5. Concurrent overwrite or replacement races.

These deferrals do **not** waive ordinary stream draining or per-operation timeout behavior, basic atomic no-overwrite publication, operationally read-only behavior, or complete fail-closed archive path confinement. Those V1 behaviors are exercised by the passing tests named above.

## Related architecture evidence

Issue #101 is closed at this qualified head. Its final fix `f3038e1ee9be8af7a01f2773f767114fda82dcc5` removed the two direct production-private-helper test dependencies without changing production code. PR #134 records its CPython-3.10 193/193 pass and three Claude Sonnet 5 medium reviews (whole-system, Standards, and Spec), all ACCEPT with zero findings.

No raw bundle, inventory, external endpoint, credential, or temporary log is included in this record.
