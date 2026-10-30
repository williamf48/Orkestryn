#!/usr/bin/env python3
"""A short native regression preflight before the complete acceptance suite."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,platform,time,unittest,uuid
CASES=[
 'operations_factory_tests.FactoryContractTests',
 'safety_reliability_tests.ReliabilityTests',
 'safety_tests.SafetyTests',
 'safety_http_tests.SafetyHTTPTests',
 'operations_server_tests.ServerTests.test_session_timing_exposes_only_the_current_deadline_without_renewal',
 'operations_server_tests.ServerTests.test_login_and_workflow_deadlines_are_distinct_and_state_reads_extend_neither',
 'operations_server_tests.ServerTests.test_expired_login_cannot_obtain_session_metadata_or_reopen_a_saved_workflow',
 'judge_evaluation_tests.JudgeEvaluationTests',
 'browser_proposals_tests.BrowserKeyTests',
 'judge_relay_tests.RelayTests',
 'downloadable_demo_tests.DownloadableTests',
 'operations_provider_tests.ProviderSetupTests',
 'operations_proposals_tests.ProposalTests.test_judge_sees_owner_funded_access_without_a_key_or_startup_request',
 'operations_proposals_tests.ProposalTests.test_browser_rejects_ambiguous_key_fields_outside_nvidia_form',
 'hosted_tests.HostedTests.test_hosted_judges_complete_three_fake_live_roles_without_supplying_any_key',
 'operations_server_tests.ServerTests.test_invalid_duration_is_rejected_before_creating_any_run',
 'operations_server_tests.ServerTests.test_hour_run_accepts_review_after_fifteen_minutes_but_blocks_at_its_original_deadline',
 'native_process_tests.ProcessTests',
 'connected_team_tests.ConnectedTests.test_owner_crash_at_each_phase_resumes_one_recorded_decision_without_new_consent',
 'factory_publication_tests.PublicationTests.test_second_case_publication_denial_does_not_repeat_roles_or_stop_campaign',
 'factory_publication_tests.PublicationTests.test_persistent_case_denial_keeps_failure_visible_without_retrying_the_role',
 'operations_server_tests.ServerTests.test_expired_review_returns_json_denial_without_effect_or_lease_renewal',
 'operations_server_tests.ServerTests.test_policy_conflict_returns_its_reason_without_changing_the_plan',
 'operations_server_tests.ServerTests.test_stopped_run_denies_effects_as_json_but_keeps_saved_evidence_readable',
 'windows_compat_tests.CompatibilityTests.test_transient_windows_readiness_denials_retry_the_same_guarded_read',
 'windows_compat_tests.CompatibilityTests.test_persistent_windows_readiness_denial_is_bounded_and_propagates',
 'windows_compat_tests.CompatibilityTests.test_readiness_retry_does_not_hide_other_files_platforms_or_errors',
 'windows_compat_tests.CompatibilityTests.test_invalid_readiness_json_is_not_retried_or_treated_as_healthy',
 'windows_compat_tests.CompatibilityTests.test_readiness_retry_rechecks_path_guard_before_opening',
 'durable_runtime_tests.RuntimeTests.test_failed_initial_startup_waits_for_its_owned_worker_and_releases_log',
 'durable_runtime_tests.RuntimeTests.test_failed_reattachment_does_not_stop_existing_workers',
 'durable_runtime_tests.RuntimeTests.test_initial_startup_rollback_timeout_remains_unconfirmed',
 'mission_tests.MissionTests.test_client_death_after_batch_effect_reconciles_the_completed_cursor',
 'windows_compat_tests.CompatibilityTests.test_transient_windows_replace_denials_retry_only_the_same_complete_file',
 'windows_compat_tests.CompatibilityTests.test_concurrent_publishers_use_independent_pending_files',
 'windows_compat_tests.CompatibilityTests.test_concurrent_publisher_retry_does_not_repeat_peer_rendezvous',
 'windows_compat_tests.CompatibilityTests.test_native_open_reader_and_publication_keep_both_versions_complete',
 'windows_compat_tests.CompatibilityTests.test_native_shutdown_returns_only_after_owned_supervisor_releases_files',
 'windows_compat_tests.CompatibilityTests.test_linux_process_exit_during_stat_read_is_confirmed_absent',
 'windows_compat_tests.CompatibilityTests.test_linux_permission_and_unrelated_procfs_errors_are_not_native_exit',
 'windows_compat_tests.CompatibilityTests.test_dead_worker_probes_do_not_attempt_slow_refused_tcp_connections',
 'windows_compat_tests.CompatibilityTests.test_windows_privilege_fallback_still_rejects_linked_output_before_launch',
 'windows_compat_tests.CompatibilityTests.test_windows_file_link_fallback_excludes_private_target_from_export',
 'durable_runtime_tests.RuntimeTests.test_actual_supervisor_death_preserves_workers_and_resumes_same_runtime',
 'durable_runtime_tests.RuntimeTests.test_pending_setup_reconciles_exact_receipt_before_next_operation',
 'durable_runtime_tests.RuntimeTests.test_blocked_setup_stops_the_fixture_with_reason_and_revision',
 'durable_runtime_tests.RuntimeTests.test_real_crashes_reconcile_intent_effect_and_committed_receipts_for_each_effect',
 'dashboard.DashboardTests.test_http_session_can_reconnect_after_dashboard_close_without_exposing_capabilities',
 'operations_tests.OperationsTests.test_background_supervisor_measures_incident_prepares_review_and_never_approves',
 'operations_server_tests.ServerTests.test_separate_named_browser_approvals_handoff_dead_candidate_and_preserve_customer_job',
 'operations_proposals_tests.ProposalTests.test_three_roles_require_separate_consent_then_existing_exact_effect_reviews',
 'operations_proposals_tests.ProposalTests.test_duplicate_click_and_polling_cannot_repeat_inflight_call',
]
def main(argv=None):
    import project,test_paths
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'preflight_runs');args=parser.parse_args(argv)
    root=args.output.absolute()
    if root.resolve()!=root:raise ValueError('Preflight output cannot be linked.')
    now=datetime.now(timezone.utc);directory=root/(now.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]);directory.mkdir(parents=True,exist_ok=False)
    before=project.source_hashes();test_paths.OBSERVATIONS.clear();began=time.monotonic()
    print('NATIVE PREFLIGHT: native shutdown, restart, dead-worker probes, path guards, observer and named browser approvals. Zero API requests.',flush=True)
    result=unittest.TextTestRunner(verbosity=2,buffer=True).run(unittest.defaultTestLoader.loadTestsFromNames(CASES));source_matches=before==project.source_hashes();ok=result.wasSuccessful() and source_matches
    record={'mode':'NATIVE PREFLIGHT ONLY; NOT FULL ACCEPTANCE','status':'PASSED' if ok else 'FAILED','platform':platform.system(),'python_version':platform.python_version(),'created_at':now.isoformat(),'tests_run':result.testsRun,'failure_count':len(result.failures),'error_count':len(result.errors),'skipped_count':len(result.skipped),'failed_tests':[t.id() for t,_ in result.failures],'error_tests':[t.id() for t,_ in result.errors],'elapsed_seconds':round(time.monotonic()-began,3),'source_sha256':before,'source_unchanged':source_matches,'path_guard_fixture_boundaries':list(test_paths.OBSERVATIONS),'model_calls':0,'native_windows':'PREFLIGHT_ONLY' if platform.system()=='Windows' else 'NOT_RUN_ON_THIS_PLATFORM','scope':'Selected regressions exercise actual local processes/HTTP and modelled Windows OS facts where indicated. No live model, cloud IAM or full native-suite claim.'}
    # Keep the actual assertion/receipt details with the saved report. The old
    # summary contained only test names, making a screenshot the only clue.
    record['failure_details']=[{'test':t.id(),'traceback':detail} for t,detail in result.failures]
    record['error_details']=[{'test':t.id(),'traceback':detail} for t,detail in result.errors]
    (directory/'summary.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8');print('Preflight: '+record['status']+' | '+str(directory),flush=True);return 0 if ok else 1
if __name__=='__main__':raise SystemExit(main())
