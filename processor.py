"""Section-3 processing. The SAME functions run on the device (offline) and in the cloud (online).
They only read the shared tables in schema.py, so both sides give identical numbers.

JSON output per window (channel arrays are in channel order):
  {"session_id", "window", "missing_1", "missing_2", "complete",
   "mean": {"s1": [c1,c2,c3] , "s2": [c1,c2]}, "min": {...}, "max": {...},
   "magnitude", "heart_rate", "mode"}
"""
import json
import math
import time

from schema import transaction

WINDOW = 400        # samples per sensor per second
LOOKBACK = 800      # samples a peak needs before it
MIN_PEAK_GAP = 100  # samples between accepted peaks
RATE = 400


def ready_limit(conn, session):
    """Windows w < ready_limit can be processed: every index they (and the heart-rate rule's
    'sample after') depend on has been decided, i.e. stored or recorded as lost."""
    h = {s: (d, e) for s, d, e in conn.execute(
        "SELECT sensor, decided_through, ended FROM horizon WHERE session_id=?", (session,))}
    if 1 not in h or 2 not in h:
        return 0
    total = (max(h[1][0], h[2][0]) + WINDOW - 1) // WINDOW
    caps = [(d - 1) // WINDOW if d >= 1 else 0 for d, e in h.values() if not e]
    return min(min(caps), total) if caps else total


def _load_ch2(conn, session, lo, hi):
    """Sensor 1 channel 2 for indexes lo..hi inclusive; None where unavailable."""
    vals = [None] * (hi - lo + 1)
    for idx, v in conn.execute(
            "SELECT idx, v2 FROM samples WHERE session_id=? AND sensor=1 AND idx BETWEEN ? AND ?",
            (session, lo, hi)):
        vals[idx - lo] = v
    return vals


def advance_peaks(conn, session, upto):
    """Decide every peak with index < upto (sample upto must already be decided). Peaks depend on
    the previous accepted peak, so we scan the session in order, once, and remember the state."""
    row = conn.execute("SELECT next_i, last_peak FROM peak_state WHERE session_id=?", (session,)).fetchone()
    next_i, last_peak = row if row else (0, None)
    while next_i < upto:
        stop = min(upto, next_i + 2000)
        lo = max(0, next_i - LOOKBACK)
        vals = _load_ch2(conn, session, lo, stop)
        found = []
        for i in range(max(next_i, LOOKBACK), stop):
            k = i - lo
            cur, left, right = vals[k], vals[k - 1], vals[k + 1]
            if cur is None or left is None or right is None:
                continue
            if not (cur > left and cur > right):           # must beat both neighbours
                continue
            before = vals[k - LOOKBACK:k]
            if None in before:                              # all 800 earlier samples needed
                continue
            if 2 * cur <= max(before):                      # cur > max/2  <=>  2*cur > max
                continue
            if last_peak is not None and i - last_peak < MIN_PEAK_GAP:
                continue
            found.append(i)
            last_peak = i
        conn.executemany("INSERT OR IGNORE INTO peaks(session_id, idx) VALUES(?,?)",
                         [(session, i) for i in found])
        conn.execute("INSERT INTO peak_state(session_id,next_i,last_peak) VALUES(?,?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET next_i=excluded.next_i, last_peak=excluded.last_peak",
                     (session, stop, last_peak))
        next_i = stop


def heart_rate(conn, session, w):
    if w < 4:
        return None
    a, b = WINDOW * (w - 4), WINDOW * (w + 1) - 1          # the last 5 seconds
    present = conn.execute("SELECT COUNT(*) FROM samples WHERE session_id=? AND sensor=1 AND idx BETWEEN ? AND ?",
                           (session, a, b)).fetchone()[0]
    if present != b - a + 1:                                # any missing sample -> no rate
        return None
    advance_peaks(conn, session, b + 1)
    peaks = [r[0] for r in conn.execute(
        "SELECT idx FROM peaks WHERE session_id=? AND idx BETWEEN ? AND ? ORDER BY idx", (session, a, b))]
    if len(peaks) < 2:
        return None
    mean_gap_samples = (peaks[-1] - peaks[0]) / (len(peaks) - 1)   # mean of consecutive gaps
    return round(60 / (mean_gap_samples / RATE), 1)


def _stats(conn, session, sensor, lo, hi, nch):
    cols = ",".join("AVG(v%d),MIN(v%d),MAX(v%d)" % (c, c, c) for c in range(1, nch + 1))
    row = conn.execute("SELECT COUNT(*)," + cols + " FROM samples WHERE session_id=? AND sensor=? "
                       "AND idx BETWEEN ? AND ?", (session, sensor, lo, hi)).fetchone()
    count = row[0]
    mean = [None if row[1 + 3 * c] is None else round(row[1 + 3 * c], 1) for c in range(nch)]
    mn = [row[2 + 3 * c] for c in range(nch)]
    mx = [row[3 + 3 * c] for c in range(nch)]
    return count, mean, mn, mx


def compute_window(conn, session, w, mode):
    lo, hi = WINDOW * w, WINDOW * w + WINDOW - 1
    c1, mean1, min1, max1 = _stats(conn, session, 1, lo, hi, 3)
    c2, mean2, min2, max2 = _stats(conn, session, 2, lo, hi, 2)
    mags = [math.sqrt(a * a + b * b) for a, b in conn.execute(
        "SELECT v1, v2 FROM samples WHERE session_id=? AND sensor=2 AND idx BETWEEN ? AND ?", (session, lo, hi))]
    return {
        "session_id": session, "window": w,
        "missing_1": WINDOW - c1, "missing_2": WINDOW - c2,
        "complete": c1 == WINDOW and c2 == WINDOW,
        "mean": {"s1": mean1, "s2": mean2}, "min": {"s1": min1, "s2": min2}, "max": {"s1": max1, "s2": max2},
        "magnitude": round(sum(mags) / len(mags), 1) if mags else None,
        "heart_rate": heart_rate(conn, session, w),
        "mode": mode,
    }


def process_ready(conn, session, mode, limit=100, start=0):
    """Compute (and store) results for ready windows >= start that have no result yet.
    Returns how many were computed. Safe to call repeatedly and from several processes."""
    upto = ready_limit(conn, session)
    if start >= upto:
        return 0
    done = {r[0] for r in conn.execute(
        "SELECT w FROM results WHERE session_id=? AND w>=? AND w<?", (session, start, upto))}
    count = 0
    for w in range(start, upto):
        if w in done:
            continue
        with transaction(conn):          # peak state + result are saved together
            body = compute_window(conn, session, w, mode)
            conn.execute("INSERT OR IGNORE INTO results(session_id,w,mode,body,processed_at) VALUES(?,?,?,?,?)",
                         (session, w, mode, json.dumps(body, separators=(",", ":")), time.time()))
        count += 1
        if count >= limit:
            break
    return count
