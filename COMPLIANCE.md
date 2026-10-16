# Requirement checklist (honest status)

Legend: DONE-TESTED = automated test passes here (x86-64 sandbox, stand-in mock). CODE-ONLY = implemented, not verified. PENDING = needs you / hardware.

| # | Requirement | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Device export equals mock ground truth, exactly once, unchanged | DONE-TESTED vs stand-in | `test_collector_integration.py`, `test_process_kill.py`. **PENDING:** real `biosignal-mock` + `check_ground_truth` (not supplied) |
| 2 | Missing samples listed with reason | DONE-TESTED vs stand-in | `coverage_ok` checks in integration tests; `scripts/check_integrity.py` |
| 3a | Survives process crash | DONE-TESTED | SIGKILL x3 test with real CLI process |
| 3b | Survives reboot / power cut; max loss stated | CODE-ONLY | WAL + synchronous=FULL; stated max ~1 s. **PENDING:** real power-pull test, then fill the measured loss into the design note |
| 4 | Online result per second within 5 s | CODE-ONLY | cloud computes on every committed batch; `processed_at` stored. No latency measurement yet |
| 5 | Mode switch <= 10 s, no loss/dup | DONE-TESTED (cloud black-holed) | e2e (3 runs): 5.8-6.8 s to offline, 1.8-1.9 s to online (read from runtime.json, includes <=1 s reporting lag). Not tested with a real WiFi drop |
| 6 | Cloud complete within 15 min after <=2 h outage | CODE-ONLY | logic tested on small data; loopback benchmark 480k samples/3.3 s (144k samples/s, x86). **PENDING:** 2 h outage over a real uplink (needs ~1.8 Mbit/s+ ; ~200 MB raw JSON, gzip smaller) |
| 7 | Results follow section 3 exactly | DONE-TESTED vs my reading | 13 unit tests. **PENDING:** compare with the judges' reference implementation. Ambiguities: sensor-1-only availability for the 5 s rule; rounding mode |
| 8 | ARM64, <=4 cores/2 GB, CPU/mem/disk/net reported | PENDING | `scripts/measure_resources.py` ready; no ARM64 run, no 1 h run |
| - | Free cloud account deployed | PENDING | code is ready; you must choose a host and verify it needs no card |
| - | Design note <= 2 pages | DONE | `design-note.md` (check page count when exported) |
| - | Failure evidence | PARTIAL | `TEST_REPORT.md`; hardware-only tests listed in README |
| - | Demo video | PENDING | `DEMO_SCRIPT.md` |
| - | AI disclosure | DRAFT | `AI_DISCLOSURE.md` — you must fill in your own verification |

Failure-mode coverage: process kill (real), corrupt reply, duplicate reply, mock silent, one sensor stopped, simulator restart, disk full (simulated with SQLite max_page_count), clock change (simulated), lost ack, partial batch, repeated outages, cloud down at startup. Network cut = SIGSTOP of cloud process. Reboot and power cut: not tested.
