# AI-use disclosure (DRAFT — edit so it is true for you)

**Tool:** Claude (Anthropic), chat interface with a code sandbox. Used to design the architecture, write all code, tests and documents in this repository, and to run the tests.

**What the AI did:** wrote the Python modules, the stand-in mock, tests, scripts and docs; found and fixed several bugs while testing (SQLite thread affinity, a loss record that was never uploaded, a flaw in my stand-in mock).

**What was actually verified (by running, in the AI's sandbox, x86-64):** 65 automated tests pass (`TEST_REPORT.md`); real SIGKILL restarts; cloud black-hole via SIGSTOP; export equality device vs cloud.

**What YOU must verify yourself before claiming it (fill this in):**
- [ ] I read and can explain `storage.add_packet`, `processor.advance_peaks`, `sync_manager._next_job/_record_ack`.
- [ ] I ran it on ARM64 / Raspberry Pi: ______
- [ ] I ran the real `biosignal-mock` and `check_ground_truth`: ______
- [ ] I did the power-pull test and measured the loss: ______
- [ ] I measured the 1-hour resources: ______

**Claim I trust least (suggested, change if you disagree):** "a power cut loses at most about 1 second" — it is reasoned from SQLite's `synchronous=FULL` semantics and the poll interval, never measured, and it depends on the SD card honouring fsync and on how the real mock behaves after a restart.

**Not verified at all:** behaviour against the real mock (its re-read after a session change, reconnect during stalls); equality with the judges' reference results; any real cloud deployment.
