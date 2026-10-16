#!/usr/bin/env python3
"""pipeline - offline-first sensor pipeline.

  pipeline run                       start collecting + syncing (long running)
  pipeline status                    one JSON line
  pipeline export --session ID --from device|cloud
  pipeline results --session ID

Exit codes: 0 ok | 2 bad arguments | 3 cloud unreachable (cloud export) | 4 unknown session | 1 other error
"""
import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time

from cloud_client import CloudClient, CloudRejected, CloudUnavailable
from collector import Collector
from config import Config
from storage import DeviceStore
from sync_manager import SyncManager


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


# ---------- runtime file (lets `status` in another process see the daemon) ----------
def write_runtime(cfg, data):
    tmp = cfg.runtime_file + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, cfg.runtime_file)
    except OSError:
        pass          # e.g. disk full: never let reporting kill the pipeline


def read_runtime(cfg):
    try:
        with open(cfg.runtime_file) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------- commands ----------
def cmd_run(cfg, args):
    os.makedirs(cfg.data_dir, exist_ok=True)
    lock = open(cfg.lock_file, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("pipeline is already running", file=sys.stderr)
        return 1
    store_c, store_s = DeviceStore(cfg.device_db), DeviceStore(cfg.device_db)   # one connection per thread
    collector = Collector(cfg, store_c)
    client = CloudClient(cfg.cloud_url, cfg.http_timeout)
    sync = SyncManager(cfg, client, store_s)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    threads = [threading.Thread(target=collector.run, args=(stop,), name="collector"),
               threading.Thread(target=sync.run, args=(stop,), name="sync")]
    for t in threads:
        t.start()
    while not stop.is_set():
        write_runtime(cfg, {"pid": os.getpid(), "mode": sync.mode, "storage_error": collector.storage_error,
                            "cloud_error": sync.last_error, "bytes_sent": client.bytes_sent,
                            "bytes_received": client.bytes_received, "collector": collector.stats,
                            "mode_switches": sync.switches, "updated_wall_clock": time.time()})
        stop.wait(1.0)
    for t in threads:
        t.join(timeout=10)
    return 0


def cmd_status(cfg, args):
    store = DeviceStore(cfg.device_db)
    rt = read_runtime(cfg)
    alive = pid_alive(rt.get("pid"))
    saved, acked = store.counter("samples_saved"), store.counter("samples_acked")
    try:
        in_cloud, source = CloudClient(cfg.cloud_url, 2.0).status()["samples"], "cloud"
    except (CloudUnavailable, CloudRejected):
        in_cloud, source = acked, "device_ack_counter"     # cloud unreachable: what the cloud has acknowledged to us
    print(dumps({
        "mode": rt.get("mode", "offline") if alive else "stopped",
        "samples_saved_local": saved,
        "samples_in_cloud": in_cloud,
        "backlog_samples": saved - acked,
        "backlog_loss_records": store.unsent_losses(),
        "cloud_count_source": source,
        "storage_error": rt.get("storage_error") if alive else None,
        "sessions": store.sessions()[-5:],
    }))
    return 0


def cmd_export(cfg, args):
    if args.source == "device":
        store = DeviceStore(cfg.device_db)
        if not store.session_exists(args.session):
            print("unknown session %r on this device" % args.session, file=sys.stderr)
            return 4
        for sensor, idx, values in store.iter_samples(args.session):
            sys.stdout.write(dumps({"sensor": sensor, "index": idx, "values": values}) + "\n")
        for sensor, a, b, reason in store.iter_losses(args.session):
            sys.stdout.write(dumps({"sensor": sensor, "from_index": a, "to_index": b, "reason": reason}) + "\n")
        return 0
    try:
        for line in CloudClient(cfg.cloud_url, 30.0).export_lines(args.session):
            sys.stdout.write(line + "\n")
    except CloudUnavailable as e:
        print("cloud unreachable: %s" % e, file=sys.stderr)
        return 3
    except CloudRejected as e:
        print("cloud refused the export: %s" % e, file=sys.stderr)
        return 4
    return 0


def cmd_results(cfg, args):
    store = DeviceStore(cfg.device_db)
    merged = {}
    for body, ts in store.conn.execute("SELECT body, processed_at FROM results WHERE session_id=?", (args.session,)):
        r = json.loads(body)
        r["processed_at"] = ts
        merged[r["window"]] = r
    try:        # fill in windows the cloud processed online
        for line in CloudClient(cfg.cloud_url, 30.0).result_lines(args.session):
            r = json.loads(line)
            merged.setdefault(r["window"], r)
    except (CloudUnavailable, CloudRejected) as e:
        print("warning: cloud unreachable, showing only windows processed on the device (%s)" % e, file=sys.stderr)
    if not merged and not store.session_exists(args.session):
        print("unknown session %r" % args.session, file=sys.stderr)
        return 4
    for w in sorted(merged):
        sys.stdout.write(dumps(merged[w]) + "\n")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir")
    ap.add_argument("--mock-host")
    ap.add_argument("--mock-port", type=int)
    ap.add_argument("--cloud-url")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    e = sub.add_parser("export")
    e.add_argument("--session", required=True)
    e.add_argument("--from", dest="source", required=True, choices=["device", "cloud"])
    r = sub.add_parser("results")
    r.add_argument("--session", required=True)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)      # argparse exits with code 2 on bad usage
    cfg = Config.from_env()
    if args.data_dir:
        cfg.data_dir = os.path.expanduser(args.data_dir)
    if args.mock_host:
        cfg.mock_host = args.mock_host
    if args.mock_port:
        cfg.mock_port = args.mock_port
    if args.cloud_url:
        cfg.cloud_url = args.cloud_url
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        return {"run": cmd_run, "status": cmd_status, "export": cmd_export, "results": cmd_results}[args.command](cfg, args)
    except BrokenPipeError:
        return 0
    except Exception as e:                       # noqa: BLE001 - last-resort message instead of a traceback
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
