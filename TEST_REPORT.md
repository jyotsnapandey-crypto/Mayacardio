# Test report

Run in the AI sandbox, NOT on ARM64 and NOT against the provided biosignal-mock (stand-in: tools/fake_mock.py).
```
x86_64
Python 3.12.3
pytest 9.1.1
Wed Oct 14 06:27:20 UTC 2026
```
Command: `pytest tests -v -s`. Raw output below (HTTP access-log noise removed).
```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/claude/mayacardio
collecting ... collected 65 items
tests/test_cloud.py::test_upload_is_idempotent PASSED
tests/test_cloud.py::test_conflicting_values_rejected_atomically PASSED
tests/test_cloud.py::test_partial_acceptance_acks_only_what_was_persisted PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad0] PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad1] PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad2] PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad3] PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad4] PASSED
tests/test_cloud.py::test_invalid_payloads_are_rejected[bad5] PASSED
tests/test_cloud.py::test_not_json_rejected PASSED
tests/test_cloud.py::test_online_results_produced_once_both_sensors_arrive PASSED
tests/test_cloud.py::test_device_results_first_one_wins PASSED
tests/test_cloud.py::test_export_lists_samples_then_losses_and_unknown_session_404 PASSED
tests/test_collector_integration.py::test_clean_run_stores_everything_unchanged PASSED
tests/test_collector_integration.py::test_corrupt_replies_are_reread_and_nothing_is_lost corrupt reply (crc32 mismatch); will re-read
corrupt reply (crc32 mismatch); will re-read
corrupt reply (crc32 mismatch); will re-read
corrupt reply (crc32 mismatch); will re-read
corrupt reply (crc32 mismatch); will re-read
corrupt reply (crc32 mismatch); will re-read
PASSED
tests/test_collector_integration.py::test_duplicate_replies_are_not_stored_twice PASSED
tests/test_collector_integration.py::test_mock_stops_answering_then_recovers mock connection problem: timed out
mock connection problem: timed out
mock connection problem: timed out
PASSED
tests/test_collector_integration.py::test_one_sensor_stops_and_the_loss_is_recorded_with_reason PASSED
tests/test_collector_integration.py::test_simulator_restart_lists_unread_old_session_with_unknown_end PASSED
tests/test_collector_integration.py::test_collector_restart_resumes_without_gap PASSED
tests/test_collector_integration.py::test_downtime_longer_than_mock_buffer_is_listed_as_loss PASSED
tests/test_collector_integration.py::test_storage_failure_is_reported_and_data_recovered_by_reread storage failure, data NOT saved: OperationalError: database or disk is full
storage failure, data NOT saved: OperationalError: database or disk is full
storage failure, data NOT saved: OperationalError: database or disk is full
storage failure, data NOT saved: OperationalError: database or disk is full
PASSED
tests/test_collector_integration.py::test_signal_mode_end_to_end_heart_rate PASSED
tests/test_collector_parse.py::test_valid_reply PASSED
tests/test_collector_parse.py::test_crc_covers_data_text_exactly_as_sent PASSED
tests/test_collector_parse.py::test_wrong_crc_rejected PASSED
tests/test_collector_parse.py::test_garbage_and_truncated_rejected PASSED
tests/test_collector_parse.py::test_wrong_channel_count_rejected PASSED
tests/test_collector_parse.py::test_out_of_range_value_rejected PASSED
tests/test_collector_parse.py::test_empty_reply_with_loss_is_valid PASSED
tests/test_e2e_processes.py::test_network_cut_and_restore_switches_within_10s_and_cloud_ends_up_complete switch to offline took 6.8 s (includes <=1 s status-file lag)
switch to online took 1.9 s
PASSED
tests/test_process_kill.py::test_sigkill_and_restart_loses_nothing PASSED
tests/test_process_kill.py::test_cli_status_export_results_and_exit_codes PASSED
tests/test_processor.py::test_complete_window_stats_and_magnitude PASSED
tests/test_processor.py::test_incomplete_window_counts_missing_and_uses_available_only PASSED
tests/test_processor.py::test_windows_only_become_ready_when_next_sample_known PASSED
tests/test_processor.py::test_heart_rate_null_before_window_4 PASSED
tests/test_processor.py::test_heart_rate_60_bpm_first_valid_window PASSED
tests/test_processor.py::test_heart_rate_other_rates_and_rounding PASSED
tests/test_processor.py::test_mean_gap_uses_all_consecutive_gaps PASSED
tests/test_processor.py::test_peak_must_exceed_half_of_previous_800_max PASSED
tests/test_processor.py::test_peak_needs_100_samples_after_previous_peak PASSED
tests/test_processor.py::test_peak_must_be_strictly_greater_than_neighbours PASSED
tests/test_processor.py::test_fewer_than_two_peaks_gives_null PASSED
tests/test_processor.py::test_no_peak_without_800_samples_before PASSED
tests/test_processor.py::test_missing_sample_in_five_second_interval_gives_null PASSED
tests/test_storage.py::test_unique_insert_and_counter PASSED
tests/test_storage.py::test_repeated_packet_is_ignored PASSED
tests/test_storage.py::test_conflicting_duplicate_keeps_original_and_is_logged PASSED
tests/test_storage.py::test_unreported_gap_is_recorded_not_filled PASSED
tests/test_storage.py::test_reported_loss_is_not_reported_twice PASSED
tests/test_storage.py::test_sensor_specific_gap PASSED
tests/test_storage.py::test_session_restart_records_unknown_end PASSED
tests/test_storage.py::test_cleanly_ended_session_has_no_restart_loss PASSED
tests/test_storage.py::test_failed_transaction_rolls_back_everything PASSED
tests/test_storage.py::test_disk_full_saves_nothing_and_recovers PASSED
PASSED
PASSED
PASSED
PASSED
PASSED
PASSED
PASSED
PASSED
PASSED
======================== 65 passed in 81.35s (0:01:21) =========================
```

Not covered here: real power cut, reboot, real full disk, real WiFi drop, real mock, ARM64, 1-hour resources, real cloud host.
