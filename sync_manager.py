"""Decides online/offline, uploads the backlog, and processes locally while offline.

One loop (step()) does everything so there are no races between probing and uploading:
  online : upload_some(); if nothing to upload, probe /health instead
  offline: probe /health, and process finished windows on the device
Mode flips after `fail_threshold` consecutive failures / `success_threshold` successes.
Only time.monotonic() is used for timing, so system clock changes cannot affect switching."""
import logging
import sqlite3

import processor
from cloud_client import CloudRejected, CloudUnavailable
from schema import transaction

log = logging.getLogger("sync")


class SyncManager:
    def __init__(self, cfg, client, store):
        self.cfg, self.client, self.store = cfg, client, store
        self.mode = "offline"            # we only trust the network after probes succeed
        self.ok_streak = self.fail_streak = 0
        self.last_error = None
        self.last_rejected = False
        self.switches = 0

    # ---- one loop iteration ---------------------------------------------
    def step(self):
        did_work = False
        self.last_rejected = False
        try:
            if self.mode == "online":
                did_work = self.upload_some()
                if not did_work:
                    self.client.health()
            else:
                self.client.health()
            self._record(True)
        except CloudUnavailable as e:
            self._record(False, e)
        except CloudRejected as e:
            self.last_error, self.last_rejected = "rejected by cloud: %s" % e, True
            log.error(self.last_error)
            self._record(True)           # the cloud answered, so the network works
        except sqlite3.Error as e:
            self.last_error = "local database error during sync: %s" % e
            log.error(self.last_error)
        if self.mode == "offline":
            try:
                self.process_local()
            except sqlite3.Error as e:
                self.last_error = "local processing error: %s" % e
                log.error(self.last_error)
        return did_work

    def _record(self, ok, err=None):
        if ok:
            self.ok_streak, self.fail_streak = self.ok_streak + 1, 0
        else:
            self.fail_streak, self.ok_streak, self.last_error = self.fail_streak + 1, 0, str(err)
        if self.mode == "online" and self.fail_streak >= self.cfg.fail_threshold:
            self.mode, self.switches = "offline", self.switches + 1
            log.warning("-> OFFLINE (%s)", err)
        elif self.mode == "offline" and self.ok_streak >= self.cfg.success_threshold:
            self.mode, self.switches = "online", self.switches + 1
            log.warning("-> ONLINE")

    # ---- offline processing ------------------------------------------------
    def cloud_covered_window(self, session):
        """Windows below this are fully acknowledged by the cloud, so the cloud processes them."""
        rows = self.store.conn.execute("SELECT acked_horizon, end_acked FROM sync WHERE session_id=?", (session,)).fetchall()
        if len(rows) < 2:
            return 0
        caps = [(h - 1) // processor.WINDOW if h >= 1 else 0 for h, ended in rows if not ended]
        return min(caps) if caps else 10 ** 12

    def process_local(self):
        for session in self.store.sessions():
            processor.process_ready(self.store.conn, session, "offline", limit=self.cfg.local_window_limit,
                                    start=self.cloud_covered_window(session))

    # ---- uploading -----------------------------------------------------------
    def upload_some(self):
        did = self._upload_results()
        for _ in range(self.cfg.max_batches_per_cycle):      # bounded: never monopolise the loop
            job = self._next_job()
            if job is None:
                break
            resp = self.client.post_samples(job["payload"])
            self._record_ack(job, resp)
            did = True
        return did

    def _upload_results(self):
        c = self.store.conn
        rows = c.execute("SELECT session_id,w,mode,body,processed_at FROM results WHERE uploaded=0 "
                         "ORDER BY session_id,w LIMIT 500").fetchall()
        if not rows:
            return False
        import json
        self.client.post_results([{"session_id": s, "window": w, "mode": m, "body": json.loads(b), "processed_at": t}
                                  for s, w, m, b, t in rows])
        with transaction(c):
            c.executemany("UPDATE results SET uploaded=1 WHERE session_id=? AND w=?", [(r[0], r[1]) for r in rows])
        return True

    def _next_job(self):
        """Oldest (session, sensor) that has anything the cloud has not acknowledged."""
        c = self.store.conn
        c.execute("BEGIN")                       # one consistent read snapshot
        try:
            for sync_id, session, sensor, acked, acked_h, end_acked in c.execute(
                    "SELECT id,session_id,sensor,acked_through,acked_horizon,end_acked FROM sync ORDER BY id").fetchall():
                rows = c.execute("SELECT idx,v1,v2,v3 FROM samples WHERE session_id=? AND sensor=? AND idx>? "
                                 "ORDER BY idx LIMIT ?", (session, sensor, acked, self.cfg.batch_size)).fetchall()
                dev_h, dev_ended = c.execute("SELECT decided_through, ended FROM horizon WHERE session_id=? AND sensor=?",
                                             (session, sensor)).fetchone()
                full = len(rows) == self.cfg.batch_size
                decided = rows[-1][0] + 1 if full else dev_h
                ended = 0 if full else dev_ended
                # Loss records are tiny, so every unsent one is sent (a restart loss starts exactly at the horizon).
                losses = c.execute("SELECT from_index,to_index,reason FROM losses WHERE session_id=? "
                                   "AND sensor=? AND uploaded=0", (session, sensor)).fetchall()
                if not (rows or losses or decided > acked_h or (ended and not end_acked)):
                    continue
                nch = 3 if sensor == 1 else 2
                return {"sync_id": sync_id, "session": session, "sensor": sensor, "last_idx": rows[-1][0] if rows else None,
                        "sent": len(rows), "sent_decided": decided, "losses": losses,
                        "payload": {"session_id": session, "sensor": sensor, "decided_through": decided,
                                    "ended": bool(ended), "mode": "online",
                                    "samples": [list(r[:1 + nch]) for r in rows],
                                    "losses": [{"from_index": a, "to_index": b, "reason": r} for a, b, r in losses]}}
            return None
        finally:
            c.execute("COMMIT")

    def _record_ack(self, job, resp):
        """Move our watermark only as far as the cloud says it persisted."""
        try:
            p, dec, ended = resp["persisted_through"], resp["decided_through"], bool(resp["ended"])
        except (KeyError, TypeError):
            raise CloudRejected("malformed acknowledgment")
        if p is not None and not (p <= job["last_idx"]):
            raise CloudRejected("cloud acknowledged more than was sent")
        c = self.store.conn
        with transaction(c):
            acked = c.execute("SELECT acked_through FROM sync WHERE id=?", (job["sync_id"],)).fetchone()[0]
            newly = 0
            if p is not None and p > acked:
                newly = c.execute("SELECT COUNT(*) FROM samples WHERE session_id=? AND sensor=? AND idx>? AND idx<=?",
                                  (job["session"], job["sensor"], acked, p)).fetchone()[0]
                c.execute("UPDATE sync SET acked_through=? WHERE id=?", (p, job["sync_id"]))
                c.execute("UPDATE counters SET value=value+? WHERE name='samples_acked'", (newly,))
            c.execute("UPDATE sync SET acked_horizon=MAX(acked_horizon,?), end_acked=MAX(end_acked,?) WHERE id=?",
                      (dec, 1 if ended else 0, job["sync_id"]))
            c.executemany("UPDATE losses SET uploaded=1 WHERE session_id=? AND sensor=? AND from_index=? AND reason=?",
                          [(job["session"], job["sensor"], a, r) for a, b, r in job["losses"]])

    # ---- loop for the daemon -----------------------------------------------------
    def run(self, stop_event):
        while not stop_event.is_set():
            did = self.step()
            if not did:
                stop_event.wait(self.cfg.probe_interval)
