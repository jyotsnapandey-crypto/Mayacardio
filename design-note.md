# Design note — Never Lose a Heartbeat

## Architecture
Device: one process, two threads, one SQLite file (`device.db`, WAL, `synchronous=FULL`).
- **Collector thread** polls both sensors over TCP. Every reply is validated (JSON shape, channel count, 24-bit range, CRC-32 over the exact `data` text) and stored in **one transaction**: samples, loss records, the new `decided_through` index, counters. Primary key `(session, sensor, index)` gives exactly-once storage; a repeated reply is compared and ignored, a repeated id with different values is kept out and logged in `anomalies`.
- **Sync thread** is a small state machine: online after 2 successful probes/uploads, offline after 2 consecutive failures (HTTP timeout 2 s, 1 s gap). Online it uploads; offline it processes finished windows locally. Only monotonic time is used, so clock changes cannot disturb it.
- **Cloud** (Flask + SQLite) stores batches idempotently, acknowledges only what it committed (`persisted_through`), and computes windows as data arrives.
- **Processing** (`processor.py`) is one module reading the same tables on both sides. The peak rule depends on the previous accepted peak, so peaks are decided once, in order, with state kept in the DB.

Honest gaps: for every sensor, every index below `decided_through` is either a stored sample or inside a loss record. Mock-reported ranges keep the mock's reason; anything else missing is `unreported_gap`; an unfinished old session after a simulator restart is `session_restart` with `to_index: null`. Nothing is interpolated.

Upload: per (session, sensor) a watermark `acked_through` is advanced only to what the cloud acknowledges. Lost acks, partial acceptance, restarts and repeated outages just resend; the server deduplicates. Catch-up is bounded (10 batches of 3000 per loop) so collection never waits for it. Offline results are uploaded before samples so the cloud does not recompute them; the first stored result per window wins.

## Guarantees
- Every reply that passed validation and was committed survives process kill and, given an fsync-honouring disk, power loss (WAL + `synchronous=FULL`).
- No sample is stored twice; none is silently missing — it is stored or listed as lost with a reason.
- Mode switches never touch stored data. Tested: SIGKILL ×3 mid-run, network black-holing, lost acks, partial batches, corrupt/duplicate/silent mock, restart, sensor stop, simulated disk-full.

## Not guaranteed / limitations
- **Maximum loss on power cut: about 1 second of both sensors** (up to ~400 samples per sensor): data the mock handed over that was not yet committed, plus data accumulated since the last poll (poll gap 0.5 s + reply time). This is a design argument, **not yet measured on hardware**. If the mock is stopped by the same power cut, anything it had not delivered is gone and shows as a `session_restart` loss with unknown end. Cheap SD cards that ignore fsync can lose more.
- A conflicting duplicate in the cloud makes the cloud refuse that batch; the device keeps retrying (visible as `cloud_error`). Needs a person.
- During a long catch-up, live data waits behind the backlog on the cloud side. Device-side results for that period already exist (computed offline), so no window is missing, but live windows can exceed the 5 s target until the backlog drains. Loopback benchmark drained 480k samples in 3.3 s; real uplink untested.
- Heart rate counts "missing in the 5 s interval" for sensor 1 only (the rule is about sensor 1). Rounding uses Python `round` (half-to-even on binary floats).
- Behaviour of the real mock's re-read when the session changed, and whether it accepts reconnects during a stall, is assumed from the written spec; verified only against my stand-in.
- No authentication on the cloud API (out of scope; use a private URL or add a token before real use).
- Disk writes are high because of fsync per commit; the one-hour figure is pending.
