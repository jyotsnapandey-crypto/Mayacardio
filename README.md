# Never Lose a Heartbeat — offline-first sensor pipeline

Python 3 + SQLite on the device, Flask + SQLite in the cloud. No paid services, no extra infrastructure.
Tested on Python 3.12 / x86-64 only. **Not yet run on ARM64 / a Raspberry Pi** (see `COMPLIANCE.md`).

## Files
| file | job |
|---|---|
| `pipeline.py` / `pipeline` | CLI (`run`, `status`, `export`, `results`) |
| `collector.py` | TCP to the mock, CRC-32 + shape validation, re-read on corrupt/lost replies, reconnects |
| `storage.py`, `schema.py` | SQLite: one transaction per reply (samples + losses + index + counters) |
| `processor.py` | exact section-3 maths; **the same code runs on device and in the cloud** |
| `sync_manager.py` | online/offline switching, resumable upload, local processing while offline |
| `cloud_server.py`, `cloud_client.py` | cloud API (idempotent, acknowledges what it persisted) |
| `tools/fake_mock.py` | **my stand-in for the mock** (the assessment's download link was blank) |
| `scripts/` | `measure_resources.py`, `check_integrity.py`, `catchup_benchmark.py` |
| `tests/` | 65 automated tests, `TEST_REPORT.md` has the real output |

## Install (fresh Raspberry Pi OS / Debian)
```bash
sudo apt install -y python3 python3-venv git
cd mayacardio
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
sudo ln -sf "$PWD/pipeline" /usr/local/bin/pipeline     # optional: so `pipeline status` works anywhere
```
Needs SQLite >= 3.24 (Raspberry Pi OS ships newer). Without the symlink use `./pipeline ...` or `python3 pipeline.py ...`.

## Run
**Cloud** (any machine/VM with a public URL; see "Free cloud" below):
```bash
python3 cloud_server.py --host 0.0.0.0 --port 8000 --db /var/lib/mayacardio/cloud.db
```
**Device:**
```bash
biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100 &     # the provided mock (not included here)
export CLOUD_URL=https://your-cloud-host MOCK_PORT=7100                  # config via env vars
pipeline run                                                             # long-running; Ctrl-C stops it cleanly
```
No real mock yet? `python3 tools/fake_mock.py --mode signal --port 7100` is a stand-in (**not** the provided simulator).
Auto-start at boot: `systemd/pipeline.service`.

## Commands
```bash
pipeline status
# {"mode":"online","samples_saved_local":812400,"samples_in_cloud":811600,"backlog_samples":800,...}
pipeline export  --session ID --from device      # JSON lines: samples, then loss records
pipeline export  --session ID --from cloud
pipeline results --session ID                    # one JSON line per one-second window
```
Global options go BEFORE the command: `pipeline --data-dir D --cloud-url U status`. Env vars: `PIPELINE_DATA_DIR` (default `~/.mayacardio`), `MOCK_HOST`, `MOCK_PORT`, `CLOUD_URL`, `HTTP_TIMEOUT`, `BATCH_SIZE`.
Exit codes: 0 ok, 2 bad arguments, 3 cloud unreachable (cloud export), 4 unknown session, 1 other error.

### Definitions used in `status`
- `mode`: `online`/`offline` as decided by the running daemon; `stopped` if no daemon is alive.
- `samples_saved_local`: samples committed to the device DB (all sessions, both sensors).
- `samples_in_cloud`: the cloud's own count, asked live. If the cloud can't be reached, it is the number the cloud has acknowledged to the device, and `cloud_count_source` says `device_ack_counter` instead of `cloud`.
- `backlog_samples`: saved locally minus acknowledged by the cloud. `backlog_loss_records`: loss records not yet uploaded.

### Output formats
Sample: `{"sensor":1,"index":120000,"values":[720,1200,1560]}`. Loss: `{"sensor":2,"from_index":4000,"to_index":4399,"reason":"buffer_overflow"}`;
`to_index` is `null` when a simulator restart made the end unknown. Reasons come from the mock, plus ours:
`session_restart` (old session's unread tail) and `unreported_gap` (indexes missing that nobody explained — never filled with values).
Result line: `{"session_id","window","missing_1","missing_2","complete","mean":{"s1":[c1,c2,c3],"s2":[c1,c2]},"min":{..},"max":{..},"magnitude","heart_rate","mode","processed_at"}`.
`results` shows windows processed on the device plus, when reachable, the cloud's online windows (a window appears once).

## Tests
```bash
pytest -q                    # ~80 s, 65 tests
pytest -q -k "not e2e"       # faster
```
Tests use `tools/fake_mock.py`. Run `check_ground_truth` yourself once you have it:
`pipeline export --session ID --from device > dev.jsonl` then feed it to the tool.

## Manual hardware tests (cannot be done in a sandbox)
1. **Power cut**: run for a few minutes, pull the plug (or on a test VM: `echo b | sudo tee /proc/sysrq-trigger`, which reboots without syncing). Boot, `pipeline run`, then
   `python3 scripts/check_integrity.py` (expects `"ok": true`, no `silent_gaps`) and compare `last_index` with the mock's ground truth to measure what the cut cost.
2. **Reboot**: `sudo reboot` while running; check the service resumes (`systemd/pipeline.service`).
3. **Disk full**: `fallocate -l <almost all free space> /home/pi/filler`, watch `pipeline status` show `storage_error`, delete the filler, check data resumes. Unit-tested with SQLite's `max_page_count`, not on a real full SD card.
4. **Network cut**: unplug / `sudo ip link set wlan0 down`. Time the `mode` flip. Tested here with SIGSTOP on the cloud process only.
5. **Clock change**: `sudo date -s '+1 day'`; the pipeline uses monotonic time for all timing. Unit-tested by patching `time.time`.
6. **1-hour resources**: see below.

## One-hour resource report
```bash
PP=$(pgrep -f "pipeline.py run"); MP=$(pgrep -f biosignal-mock)
python3 scripts/measure_resources.py --pipeline-pid $PP --mock-pid $MP --duration 3600 --data-dir ~/.mayacardio --out resources.json
```
Run it online for the full hour on the ARM64 board and write the environment into your report (Pi model / QEMU / VM, cores, RAM).
Limit the VM to 4 cores / 2 GB (QEMU `-smp 4 -m 2048`). **No 1-hour figures exist yet.**
Only a 15-second x86 smoke run of the script was done (1.3 % of one core, 33 MB RSS, 9.8 MB written; meaningless as a result).

## Free cloud (you must verify the current terms yourself)
Needs: a process that can run Flask + a persistent disk for `cloud.db`. I did **not** check any provider's current free-tier terms.
Check before choosing: no payment card required, disk persists across restarts (many free web tiers wipe it), always-on.
A fully reproducible fallback the assessment allows to document: run `cloud_server.py` on any free-tier VM or your own machine and expose it with a free tunnel.
Whatever you pick, put the URL in `CLOUD_URL` and keep `cloud.db` backed by a persistent disk.

## Troubleshooting
- `status` says `stopped`: no daemon; start `pipeline run`. "already running": another instance holds `~/.mayacardio/run.lock`.
- `storage_error` in status: the last write failed (disk full?). Nothing was pretended saved; the collector retries with a re-read.
- Mode stuck `offline`: `curl $CLOUD_URL/health`. A 4xx from the cloud shows in `runtime.json` as `cloud_error`.
- Cloud returns 409: same sample id arrived with different values. The batch is refused whole and stays in the backlog (needs a human).
- Mock never answers: collector reconnects every ~3 s; check port and that only one client is connected.
