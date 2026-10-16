# 5-minute demo script (network cut, process kill, power cut)

Prep (before recording): board + cloud running, 3 terminals visible (T1 mock+pipeline logs, T2 commands, T3 cloud). Use `--mode signal`. Zoom the font.
Do a dry run first; keep each step short. Replace SID with the session id from `pipeline status` (`"sessions"`).

| time | action | say |
|---|---|---|
| 0:00 | T3: `python3 cloud_server.py --port 8000 --db cloud.db`. T1: `biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100 &` then `pipeline run` | "Mock on the Pi, cloud is a free-tier server." |
| 0:30 | T2: `pipeline status` (twice, 5 s apart) | "online, saved count rising, backlog near zero" |
| 0:50 | `pipeline results --session SID \| tail -3` | "one result per second, mode online, processed in cloud" |
| 1:10 | **Network cut**: `sudo ip link set wlan0 down` (or block cloud: `sudo iptables -A OUTPUT -p tcp --dport 8000 -j DROP`). Run `watch -n1 pipeline status` | "within 10 s it flips to offline, saved count keeps rising, backlog grows" |
| 1:50 | `pipeline results --session SID \| tail -3` | "same logic locally, mode offline" |
| 2:10 | **Process kill**: `pkill -9 -f "pipeline.py run"`; wait 3 s; `pipeline status` (shows stopped); restart `pipeline run` | "killed with SIGKILL; restart resumes from the last committed index" |
| 2:40 | `python3 scripts/check_integrity.py \| head -20` | "no silent gaps, every index is a sample or a listed loss" |
| 3:00 | **Network back**: `sudo ip link set wlan0 up`; `watch -n1 pipeline status` | "flips online in seconds, backlog drains to ~0" |
| 3:30 | `pipeline export --session SID --from device > d.jsonl; pipeline export --session SID --from cloud > c.jsonl; diff <(grep values d.jsonl) <(grep values c.jsonl) \| head` (diff only the live-tail difference, or stop pipeline first) | "cloud copy equals device copy" |
| 3:50 | **Power cut**: pull the plug (or `echo b \| sudo tee /proc/sysrq-trigger`). Boot, start mock + `pipeline run` | "power pulled mid-run" |
| 4:30 | `python3 scripts/check_integrity.py \| head -20`; `pipeline export --session OLDSID --from device \| tail -3` | "old session ends with a loss of unknown end; at most about a second lost" |
| 4:50 | Close with the limits: tested only X, not Y | honesty beats polish |

Do NOT show or claim: ground-truth comparison unless you actually ran `check_ground_truth`.
