"""SQLite schema + connection settings, shared by the device and the cloud server.

Durability policy: journal_mode=WAL with synchronous=FULL. A transaction is fsynced
to disk before COMMIT returns, so anything we have committed survives a power cut
(assuming the disk honours fsync). We commit once per mock reply.
"""
import sqlite3
from contextlib import contextmanager

SHARED_DDL = """
CREATE TABLE IF NOT EXISTS samples(
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL, idx INTEGER NOT NULL,
    v1 INTEGER NOT NULL, v2 INTEGER NOT NULL, v3 INTEGER,
    PRIMARY KEY(session_id, sensor, idx)) WITHOUT ROWID;

-- explicit list of samples we know are NOT available, with the reason
CREATE TABLE IF NOT EXISTS losses(
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL,
    from_index INTEGER NOT NULL, to_index INTEGER, reason TEXT NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, sensor, from_index, reason));

-- every index below decided_through is either a stored sample or a recorded loss
CREATE TABLE IF NOT EXISTS horizon(
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL,
    decided_through INTEGER NOT NULL DEFAULT 0, ended INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, sensor));

-- heart-rate peak detector state (sensor 1, channel 2), advanced in index order
CREATE TABLE IF NOT EXISTS peak_state(
    session_id TEXT PRIMARY KEY, next_i INTEGER NOT NULL, last_peak INTEGER);
CREATE TABLE IF NOT EXISTS peaks(
    session_id TEXT NOT NULL, idx INTEGER NOT NULL, PRIMARY KEY(session_id, idx)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS results(
    session_id TEXT NOT NULL, w INTEGER NOT NULL, mode TEXT NOT NULL,
    body TEXT NOT NULL, processed_at REAL NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, w)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS anomalies(
    id INTEGER PRIMARY KEY, session_id TEXT, sensor INTEGER, idx INTEGER,
    kind TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

# Device only: how far the cloud has acknowledged, per session and sensor.
DEVICE_DDL = """
CREATE TABLE IF NOT EXISTS sync(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL,
    acked_through INTEGER NOT NULL DEFAULT -1,
    acked_horizon INTEGER NOT NULL DEFAULT 0,
    end_acked INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, sensor));
"""


def connect(path):
    # check_same_thread=False: each connection is still used by ONE thread at a time (created in main, handed to its worker)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)  # we manage transactions
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(conn, device=False):
    conn.executescript(SHARED_DDL + (DEVICE_DDL if device else ""))
    conn.execute("INSERT OR IGNORE INTO counters VALUES('samples_saved', 0)")
    conn.execute("INSERT OR IGNORE INTO counters VALUES('samples_acked', 0)")


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE ... COMMIT, with ROLLBACK on any error (including disk full)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
