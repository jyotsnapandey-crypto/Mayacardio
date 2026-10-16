"""Minimal cloud service (Flask + SQLite). Run:  python cloud_server.py --port 8000

POST /v1/samples   one batch for one (session, sensor); idempotent; acks what it persisted
POST /v1/results   results the device computed offline (first result per window wins)
GET  /v1/status    counts            GET /v1/export?session=ID    GET /v1/results?session=ID
GET  /health
"""
import argparse
import gzip
import json
import re
import sqlite3
import sys

from flask import Flask, Response, jsonify, request

import processor
import schema
from schema import transaction

SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
NCH = {1: 3, 2: 2}


class Conflict(Exception):
    pass


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def validate_samples(p):
    if not isinstance(p, dict):
        return "body must be a JSON object"
    if not (isinstance(p.get("session_id"), str) and SESSION_RE.match(p["session_id"])):
        return "bad session_id"
    if p.get("sensor") not in (1, 2):
        return "sensor must be 1 or 2"
    if not (_is_int(p.get("decided_through")) and p["decided_through"] >= 0):
        return "bad decided_through"
    if not isinstance(p.get("ended"), bool):
        return "ended must be boolean"
    if not (isinstance(p.get("samples"), list) and isinstance(p.get("losses"), list)):
        return "samples and losses must be lists"
    prev = -1
    for s in p["samples"]:
        if not (isinstance(s, list) and len(s) == 1 + NCH[p["sensor"]] and all(_is_int(v) for v in s)):
            return "bad sample"
        if s[0] <= prev or s[0] < 0:
            return "sample indexes must be unique, ascending and >= 0"
        prev = s[0]
    for l in p["losses"]:
        if not (isinstance(l, dict) and _is_int(l.get("from_index")) and isinstance(l.get("reason"), str)
                and (l.get("to_index") is None or _is_int(l["to_index"]))):
            return "bad loss"
    return None


def read_json():
    """Request body as JSON (None if invalid). Accepts Content-Encoding: gzip."""
    raw = request.get_data()
    try:
        if request.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except (OSError, EOFError, ValueError):
        return None


def create_app(db_path, max_batch=5000):
    app = Flask(__name__)
    c = schema.connect(db_path)
    schema.init(c)
    c.close()

    def db():
        return schema.connect(db_path)

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.post("/v1/samples")
    def post_samples():
        p = read_json()
        err = validate_samples(p)
        if err:
            return jsonify(error=err), 400
        session, sensor = p["session_id"], p["sensor"]
        samples = p["samples"]
        truncated = len(samples) > max_batch          # partial acceptance: persist a prefix, say so
        samples = samples[:max_batch]
        persisted_through = samples[-1][0] if samples else None
        decided = p["decided_through"]
        if truncated:
            decided = min(decided, persisted_through + 1)
        ended = p["ended"] and not truncated
        conn = db()
        try:
            try:
                with transaction(conn):
                    inserted, duplicates = 0, 0
                    if samples:
                        existing = {r[0]: tuple(r[1:]) for r in conn.execute(
                            "SELECT idx,v1,v2,v3 FROM samples WHERE session_id=? AND sensor=? AND idx BETWEEN ? AND ?",
                            (session, sensor, samples[0][0], samples[-1][0]))}
                        new = []
                        for s in samples:
                            row = tuple(s[1:]) + (None,) * (3 - len(s[1:]))
                            if s[0] in existing:
                                if existing[s[0]] != row:   # same id, different values: refuse whole batch
                                    raise Conflict("conflicting values for sensor %d index %d" % (sensor, s[0]))
                                duplicates += 1
                            else:
                                new.append((session, sensor, s[0]) + row)
                        conn.executemany("INSERT INTO samples(session_id,sensor,idx,v1,v2,v3) VALUES(?,?,?,?,?,?)", new)
                        inserted = len(new)
                        conn.execute("UPDATE counters SET value=value+? WHERE name='samples_saved'", (inserted,))
                    for l in p["losses"]:
                        conn.execute(
                            "INSERT INTO losses(session_id,sensor,from_index,to_index,reason) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(session_id,sensor,from_index,reason) DO UPDATE SET "
                            "to_index=COALESCE(losses.to_index, excluded.to_index)",
                            (session, sensor, l["from_index"], l.get("to_index"), l["reason"]))
                    conn.execute(
                        "INSERT INTO horizon(session_id,sensor,decided_through,ended) VALUES(?,?,?,?) "
                        "ON CONFLICT(session_id,sensor) DO UPDATE SET "
                        "decided_through=MAX(decided_through,excluded.decided_through), ended=MAX(ended,excluded.ended)",
                        (session, sensor, decided, 1 if ended else 0))
                    h = conn.execute("SELECT decided_through, ended FROM horizon WHERE session_id=? AND sensor=?",
                                     (session, sensor)).fetchone()
            except Conflict as e:
                return jsonify(error="conflict", detail=str(e)), 409
            except sqlite3.Error as e:
                return jsonify(error="storage_error", detail=str(e)), 503
            # The data is committed (fsynced) above; only now do we answer. Processing failures do not
            # affect what we acknowledge.
            try:
                processed = processor.process_ready(conn, session, "online", limit=300)
            except sqlite3.Error:
                processed = -1
            return jsonify(ok=True, persisted_through=persisted_through, inserted=inserted,
                           duplicates=duplicates, decided_through=h[0], ended=bool(h[1]), processed_windows=processed)
        finally:
            conn.close()

    @app.post("/v1/results")
    def post_results():
        p = read_json()
        items = p.get("results") if isinstance(p, dict) else None
        if not isinstance(items, list):
            return jsonify(error="results must be a list"), 400
        rows = []
        for it in items:
            if not (isinstance(it, dict) and isinstance(it.get("session_id"), str) and SESSION_RE.match(it["session_id"])
                    and _is_int(it.get("window")) and it["window"] >= 0 and it.get("mode") in ("online", "offline")
                    and isinstance(it.get("body"), dict) and isinstance(it.get("processed_at"), (int, float))):
                return jsonify(error="bad result entry"), 400
            rows.append((it["session_id"], it["window"], it["mode"],
                         json.dumps(it["body"], separators=(",", ":")), it["processed_at"]))
        conn = db()
        try:
            try:
                with transaction(conn):
                    stored = 0
                    for r in rows:
                        stored += conn.execute("INSERT OR IGNORE INTO results(session_id,w,mode,body,processed_at,uploaded)"
                                               " VALUES(?,?,?,?,?,1)", r).rowcount
            except sqlite3.Error as e:
                return jsonify(error="storage_error", detail=str(e)), 503
            return jsonify(ok=True, stored=stored, already_present=len(rows) - stored)
        finally:
            conn.close()

    @app.get("/v1/status")
    def status():
        conn = db()
        try:
            n = conn.execute("SELECT value FROM counters WHERE name='samples_saved'").fetchone()[0]
            sessions = [r[0] for r in conn.execute("SELECT DISTINCT session_id FROM horizon")]
            nres = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            return jsonify(samples=n, sessions=sessions, results=nres)
        finally:
            conn.close()

    @app.get("/v1/export")
    def export():
        session = request.args.get("session", "")
        if not SESSION_RE.match(session):
            return jsonify(error="bad session"), 400
        conn = db()
        if not conn.execute("SELECT 1 FROM horizon WHERE session_id=?", (session,)).fetchone():
            conn.close()
            return jsonify(error="unknown session"), 404

        def lines():
            try:
                cur = conn.execute("SELECT sensor,idx,v1,v2,v3 FROM samples WHERE session_id=? ORDER BY sensor,idx",
                                   (session,))
                while True:
                    rows = cur.fetchmany(5000)
                    if not rows:
                        break
                    for s, i, v1, v2, v3 in rows:
                        yield json.dumps({"sensor": s, "index": i, "values": [v1, v2, v3] if s == 1 else [v1, v2]},
                                         separators=(",", ":")) + "\n"
                for s, a, b, reason in conn.execute(
                        "SELECT sensor,from_index,to_index,reason FROM losses WHERE session_id=? ORDER BY sensor,from_index",
                        (session,)):
                    yield json.dumps({"sensor": s, "from_index": a, "to_index": b, "reason": reason},
                                     separators=(",", ":")) + "\n"
            finally:
                conn.close()
        return Response(lines(), mimetype="application/x-ndjson")

    @app.get("/v1/results")
    def results():
        session = request.args.get("session", "")
        if not SESSION_RE.match(session):
            return jsonify(error="bad session"), 400
        conn = db()
        try:
            out = [json.loads(b) for (b,) in conn.execute(
                "SELECT body FROM results WHERE session_id=? ORDER BY w", (session,))]
            stamps = dict(conn.execute("SELECT w, processed_at FROM results WHERE session_id=?", (session,)).fetchall())
        finally:
            conn.close()
        for r in out:
            r["processed_at"] = stamps[r["window"]]
        return Response("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in out),
                        mimetype="application/x-ndjson")

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="cloud.db")
    a = ap.parse_args()
    app = create_app(a.db)
    try:
        from waitress import serve
        serve(app, host=a.host, port=a.port, threads=8)
    except ImportError:
        print("waitress not installed, using Flask's dev server", file=sys.stderr)
        app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
