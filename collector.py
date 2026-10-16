"""Talks to the mock over newline-delimited JSON on TCP, validates every reply and hands it
to DeviceStore. Corrupt / lost replies are recovered with the documented re-read command."""
import json
import logging
import socket
import sqlite3
import time
import zlib

log = logging.getLogger("collector")
CHANNELS = {1: 3, 2: 2}
MIN_V, MAX_V = -(2 ** 23), 2 ** 23 - 1   # signed 24-bit
MAX_BUFFER = 8_000_000


class CorruptReply(Exception):
    def __init__(self, msg, hint=None):
        super().__init__(msg)
        self.hint = hint     # first_index if we could read it (only used to pick a re-read point)


def extract_data_text(raw):
    """The data text exactly as sent: from '[' to the matching ']' (that is what the CRC covers)."""
    key = raw.find('"data"')
    start = raw.find("[", raw.find(":", key)) if key >= 0 else -1
    if start < 0:
        raise CorruptReply("no data array")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "[":
            depth += 1
        elif raw[i] == "]":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    raise CorruptReply("unterminated data array")


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def parse_reply(raw_bytes):
    """Return the validated reply dict or raise CorruptReply."""
    try:
        raw = raw_bytes.decode("utf-8")
        msg = json.loads(raw)
    except ValueError:
        raise CorruptReply("not valid JSON")
    if not isinstance(msg, dict):
        raise CorruptReply("not an object")
    hint = msg.get("first_index") if _is_int(msg.get("first_index")) and msg["first_index"] >= 0 else None
    if "error" in msg:
        raise CorruptReply("mock error: %s" % msg["error"], hint)
    if not (isinstance(msg.get("session_id"), str) and msg.get("sensor") in (1, 2)
            and _is_int(msg.get("first_index")) and msg["first_index"] >= 0
            and _is_int(msg.get("count")) and msg["count"] >= 0
            and isinstance(msg.get("lost"), list) and isinstance(msg.get("session_end"), bool)
            and isinstance(msg.get("crc32"), str) and isinstance(msg.get("data"), list)):
        raise CorruptReply("missing or malformed fields", hint)
    text = extract_data_text(raw)
    try:
        given = int(msg["crc32"], 16)
    except ValueError:
        raise CorruptReply("crc32 not hex", hint)
    if zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF != given:
        raise CorruptReply("crc32 mismatch", hint)
    nch = CHANNELS[msg["sensor"]]
    if len(msg["data"]) != msg["count"]:
        raise CorruptReply("count does not match data", hint)
    for row in msg["data"]:
        if not (isinstance(row, list) and len(row) == nch and all(_is_int(v) and MIN_V <= v <= MAX_V for v in row)):
            raise CorruptReply("bad sample shape or value", hint)
    for item in msg["lost"]:
        if not (isinstance(item, dict) and _is_int(item.get("from_index")) and isinstance(item.get("reason"), str)
                and (item.get("to_index") is None or _is_int(item["to_index"]))):
            raise CorruptReply("bad lost entry", hint)
    return msg


class Collector:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.sock, self.buf = None, b""
        self.storage_error = None
        self.stats = {"packets": 0, "samples": 0, "corrupt": 0, "duplicate_packets": 0,
                      "reconnects": 0, "storage_errors": 0, "restarts_seen": 0}
        self.reread_from = {1: None, 2: None}
        self.reread_fail = {1: 0, 2: 0}
        self._mark_reread_all()   # after a crash/restart, ask the mock to resend from our last index

    # ---- connection ------------------------------------------------------
    def _mark_reread_all(self):
        for s in (1, 2):
            self.reread_from[s] = self.store.expected_next(s)

    def _connect(self):
        self.sock = socket.create_connection((self.cfg.mock_host, self.cfg.mock_port), timeout=self.cfg.mock_timeout)
        self.buf = b""

    def _disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self.buf = None, b""

    def _recv_line(self, timeout):
        deadline = time.monotonic() + timeout          # monotonic: immune to system clock changes
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i], self.buf[i + 1:]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("no reply from mock")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("mock closed the connection")
            self.buf += chunk
            if len(self.buf) > MAX_BUFFER:
                raise ConnectionError("reply too long")

    def _drain_lines(self):
        """Lines already waiting (e.g. a duplicate reply). Handled as extra replies; storage dedups."""
        self.sock.setblocking(False)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("mock closed the connection")
                self.buf += chunk
        except (BlockingIOError, InterruptedError):
            pass
        lines = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            lines.append(line)
        return lines

    # ---- one poll ---------------------------------------------------------
    def poll_once(self, sensor):
        """Request data for one sensor. Returns the number of NEW samples stored."""
        try:
            if self.sock is None:
                self._connect()
            for stray in self._drain_lines():
                self._handle(stray, sensor, strict=False)
            req = {"cmd": "read", "sensor": sensor}
            if self.reread_from[sensor] is not None:
                req["from_index"] = self.reread_from[sensor]
            self.sock.settimeout(self.cfg.mock_timeout)
            self.sock.sendall((json.dumps(req) + "\n").encode())
            raw = self._recv_line(self.cfg.mock_timeout)
        except OSError as e:
            # includes timeouts. We cannot know if the reply is still on its way: reconnect and re-read.
            log.warning("mock connection problem: %s", e)
            self._disconnect()
            self.stats["reconnects"] += 1
            self._mark_reread_all()
            return 0
        return self._handle(raw, sensor, strict=True)

    def _handle(self, raw, requested_sensor, strict):
        try:
            msg = parse_reply(raw)
        except CorruptReply as e:
            if strict:
                self.stats["corrupt"] += 1
                nxt = self.store.expected_next(requested_sensor)
                self.reread_from[requested_sensor] = nxt if nxt is not None else (e.hint or 0)
                self.reread_fail[requested_sensor] += 1
                if self.reread_fail[requested_sensor] > 3:       # give up re-reading; the gap gets recorded
                    self.reread_from[requested_sensor] = None
                    self.reread_fail[requested_sensor] = 0
                log.warning("corrupt reply (%s); will re-read", e)
            return 0
        sensor = msg["sensor"]
        try:
            st = self.store.add_packet(msg["session_id"], sensor, msg["first_index"], msg["data"],
                                       msg["lost"], msg["session_end"])
        except sqlite3.Error as e:
            # Nothing was saved (transaction rolled back). Report it and ask for the same data again.
            self.storage_error = "%s: %s" % (type(e).__name__, e)
            self.stats["storage_errors"] += 1
            nxt = self.store.expected_next(sensor)
            self.reread_from[sensor] = nxt if nxt is not None else msg["first_index"]
            log.error("storage failure, data NOT saved: %s", self.storage_error)
            time.sleep(0.5)
            return 0
        self.storage_error = None
        self.reread_from[sensor] = None
        self.reread_fail[sensor] = 0
        self.stats["packets"] += 1
        self.stats["samples"] += st["inserted"]
        if st["duplicates"] and not st["inserted"]:
            self.stats["duplicate_packets"] += 1
        if st["restart"]:
            self.stats["restarts_seen"] += 1
        return st["inserted"]

    def run(self, stop_event):
        while not stop_event.is_set():
            got = 0
            for sensor in (1, 2):
                got += self.poll_once(sensor)
            if got == 0:
                stop_event.wait(self.cfg.poll_idle if self.sock else 1.0)
        self._disconnect()
