"""The UI must keep reading while a pipeline writes.

Navigating between pages during a running pipeline froze the window. Every
connection re-ran the schema (a write), and in rollback-journal mode a writer
blocks all readers — so the burst of reads a page load fires queued behind the
event logger's per-event pipeline writes.
"""
import os
import sys
import tempfile
import threading
import time

import db


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


path = os.path.join(tempfile.mkdtemp(), "contention.db")

# ---- schema work happens once, not per connection --------------------------
conn = db.get_connection(path)
check("first connection initialises the schema", path in db._SCHEMA_READY)

journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
check(f"WAL is enabled (got {journal!r})", journal.lower() == "wal")
check("a busy database is waited on, not failed",
      conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000)
conn.close()

calls = {"n": 0}
real_initialise = db._initialise_schema


def counting_initialise(conn):
    calls["n"] += 1
    return real_initialise(conn)


db._initialise_schema = counting_initialise
for _ in range(25):
    db.get_connection(path).close()
db._initialise_schema = real_initialise
check("later connections skip the schema write entirely", calls["n"] == 0)

# ---- reads stay responsive while a writer hammers the database -------------
stop = threading.Event()
write_errors = []


def writer():
    """Stands in for pipeline_event_logger: a pipeline row per agent event."""
    i = 0
    while not stop.is_set():
        try:
            c = db.get_connection(path)
            db.save_pipeline(c, {
                "id": "1", "task": "t" * 500, "project_name": "P", "status": "running",
                "gate_status": "idle", "phase": "research", "timestamp": time.time(),
            })
            c.close()
            i += 1
        except Exception as e:
            write_errors.append(e)
            break


t = threading.Thread(target=writer, daemon=True)
t.start()

# Stand in for a page load: a burst of reads while the writer is going.
slowest = 0.0
read_errors = []
for _ in range(60):
    started = time.perf_counter()
    try:
        c = db.get_connection(path)
        c.execute("SELECT * FROM tasks").fetchall()
        c.execute("SELECT * FROM pipelines").fetchall()
        c.close()
    except Exception as e:
        read_errors.append(e)
        break
    slowest = max(slowest, time.perf_counter() - started)

stop.set()
t.join(timeout=5)

check("no write errors under contention", not write_errors)
check("no read errors under contention", not read_errors)
check(f"the slowest read stayed responsive ({slowest*1000:.0f} ms)", slowest < 1.0)

print("\nAll checks passed.")
