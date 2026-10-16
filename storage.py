"""Device-side persistence. Every mock reply is stored in ONE transaction:
samples + losses + new 'decided_through' index + counters. Either all of it is on disk or none."""
import os
import sqlite3

import schema
from schema import transaction


def uncovered(start, end_excl, covered):
    """Index ranges inside [start, end_excl) not covered by any (a, b) inclusive range."""
    gaps, pos = [], start
    for a, b in sorted(covered):
        if b < pos:
            continue
        if a > pos:
            gaps.append((pos, min(a, end_excl) - 1))
        pos = max(pos, b + 1)
        if pos >= end_excl:
            break
    if pos < end_excl:
        gaps.append((pos, end_excl - 1))
    return [g for g in gaps if g[0] <= g[1]]


class DeviceStore:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = schema.connect(path)
        schema.init(self.conn, device=True)

    # ---- queries -------------------------------------------------------
    def current(self, sensor):
        """(session_id, decided_through, ended) of the newest session seen for this sensor."""
        return self.conn.execute(
            "SELECT s.session_id, h.decided_through, h.ended FROM sync s "
            "JOIN horizon h ON h.session_id=s.session_id AND h.sensor=s.sensor "
            "WHERE s.sensor=? ORDER BY s.id DESC LIMIT 1", (sensor,)).fetchone()

    def expected_next(self, sensor):
        cur = self.current(sensor)
        return cur[1] if cur and not cur[2] else None

    def counter(self, name):
        return self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    def sessions(self):
        return [r[0] for r in self.conn.execute("SELECT DISTINCT session_id FROM sync ORDER BY id")]

    def session_exists(self, session):
        return self.conn.execute("SELECT 1 FROM horizon WHERE session_id=?", (session,)).fetchone() is not None

    def unsent_losses(self):
        return self.conn.execute("SELECT COUNT(*) FROM losses WHERE uploaded=0").fetchone()[0]

    def iter_samples(self, session):
        cur = self.conn.execute(
            "SELECT sensor, idx, v1, v2, v3 FROM samples WHERE session_id=? ORDER BY sensor, idx", (session,))
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                return
            for sensor, idx, v1, v2, v3 in rows:
                yield sensor, idx, ([v1, v2, v3] if sensor == 1 else [v1, v2])

    def iter_losses(self, session):
        return self.conn.execute(
            "SELECT sensor, from_index, to_index, reason FROM losses WHERE session_id=? "
            "ORDER BY sensor, from_index", (session,))

    # ---- the one write path used by the collector ------------------------
    def _add_loss(self, session, sensor, a, b, reason):
        return self.conn.execute(
            "INSERT OR IGNORE INTO losses(session_id,sensor,from_index,to_index,reason) VALUES(?,?,?,?,?)",
            (session, sensor, a, b, reason)).rowcount

    def add_packet(self, session, sensor, first_index, rows, lost, session_end):
        """Store one validated reply. rows = list of value lists. Raises sqlite3.Error on
        any storage failure, in which case NOTHING from this reply is saved."""
        c, n = self.conn, len(rows)
        stats = {"inserted": 0, "duplicates": 0, "conflicts": 0, "gaps": 0, "restart": False}
        with transaction(c):
            cur = self.current(sensor)
            if cur and cur[0] != session:
                # The simulator restarted. Whatever we had not read of the old session is gone.
                if not cur[2]:
                    self._add_loss(cur[0], sensor, cur[1], None, "session_restart")
                    c.execute("UPDATE horizon SET ended=1 WHERE session_id=? AND sensor=?", (cur[0], sensor))
                stats["restart"] = True
            c.execute("INSERT OR IGNORE INTO sync(session_id,sensor) VALUES(?,?)", (session, sensor))
            c.execute("INSERT OR IGNORE INTO horizon(session_id,sensor,decided_through) VALUES(?,?,0)",
                      (session, sensor))
            nxt = c.execute("SELECT decided_through FROM horizon WHERE session_id=? AND sensor=?",
                            (session, sensor)).fetchone()[0]

            # samples: insert new ones, compare repeated ones (never overwrite stored data)
            existing = {}
            if n:
                for idx, v1, v2, v3 in c.execute(
                        "SELECT idx,v1,v2,v3 FROM samples WHERE session_id=? AND sensor=? AND idx BETWEEN ? AND ?",
                        (session, sensor, first_index, first_index + n - 1)):
                    existing[idx] = (v1, v2, v3)
            new_rows = []
            for k, values in enumerate(rows):
                idx = first_index + k
                padded = tuple(values) + (None,) * (3 - len(values))
                if idx in existing:
                    if existing[idx] == padded:
                        stats["duplicates"] += 1
                    else:
                        stats["conflicts"] += 1
                        c.execute("INSERT INTO anomalies(session_id,sensor,idx,kind,detail) VALUES(?,?,?,?,?)",
                                  (session, sensor, idx, "conflicting_duplicate",
                                   "stored=%s received=%s" % (existing[idx], padded)))
                else:
                    new_rows.append((session, sensor, idx) + padded)
            c.executemany("INSERT INTO samples(session_id,sensor,idx,v1,v2,v3) VALUES(?,?,?,?,?,?)", new_rows)
            stats["inserted"] = len(new_rows)

            # losses reported by the mock, then any gap nobody explained
            covered = [(first_index, first_index + n - 1)] if n else []
            max_end = first_index + n if n else 0
            for item in lost:
                a, b = item["from_index"], item.get("to_index")
                self._add_loss(session, sensor, a, b, item["reason"])
                if b is not None:
                    covered.append((a, b))
                    max_end = max(max_end, b + 1)
            if max_end > nxt:
                for a, b in uncovered(nxt, max_end, covered):
                    self._add_loss(session, sensor, a, b, "unreported_gap")
                    stats["gaps"] += 1
            c.execute("UPDATE horizon SET decided_through=MAX(decided_through,?), ended=MAX(ended,?) "
                      "WHERE session_id=? AND sensor=?", (max_end, 1 if session_end else 0, session, sensor))
            if new_rows:
                c.execute("UPDATE counters SET value=value+? WHERE name='samples_saved'", (len(new_rows),))
        return stats
