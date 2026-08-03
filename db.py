"""
Database layer for the second brain.
Plain sqlite3 - no MCP concepts here at all. This file doesn't know
it's going to be used by an AI; it just stores and retrieves tasks and notes.
Keeping this separate from server.py means you can test it on its own.
"""
import sqlite3
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    effort_estimate TEXT,          -- 'small' | 'medium' | 'large', nullable
    priority TEXT NOT NULL DEFAULT 'medium', -- 'low' | 'medium' | 'high'
    scheduled_at TEXT,             -- fixed datetime commitment, nullable
    due_date TEXT,                 -- soft deadline (date only), nullable
    status TEXT NOT NULL DEFAULT 'open',  -- open | in_progress | done
    parent_id INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT,                     -- comma-separated, kept simple on purpose
    status TEXT NOT NULL DEFAULT 'open', -- open | done
    task_id INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,       -- the insight (e.g. "hook under 5 words performs 2x better")
    task_type   TEXT,                -- e.g. "video", "code", "marketing"
    metric_name TEXT,                -- e.g. "retention_rate", "ctr", "error_rate"
    metric_value REAL,               -- e.g. 0.82 (82%)
    outcome     TEXT NOT NULL DEFAULT 'win',  -- 'win' | 'loss'
    created_at  TEXT NOT NULL
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    
    # Dynamic migration for existing databases:
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'priority' not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'")
            conn.commit()
            
        cursor.execute("PRAGMA table_info(notes)")
        note_columns = [row[1] for row in cursor.fetchall()]
        if 'status' not in note_columns:
            conn.execute("ALTER TABLE notes ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
            conn.commit()
        if 'task_id' not in note_columns:
            conn.execute("ALTER TABLE notes ADD COLUMN task_id INTEGER REFERENCES tasks(id)")
            conn.commit()

        # Memory patterns table migration
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_patterns'")
        if not cursor.fetchone():
            conn.execute("""CREATE TABLE memory_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                task_type TEXT,
                metric_name TEXT,
                metric_value REAL,
                outcome TEXT NOT NULL DEFAULT 'win',
                created_at TEXT NOT NULL
            )""")
            conn.commit()
    except Exception as e:
        print(f"Migration error: {e}")
        
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- tasks ----------

def add_task(
    conn: sqlite3.Connection,
    content: str,
    effort_estimate: str | None = None,
    priority: str | None = None,
    scheduled_at: str | None = None,
    due_date: str | None = None,
    depends_on_ids: list[int] | None = None,
    parent_id: int | None = None,
) -> int:
    actual_priority = priority or 'medium'
    cur = conn.execute(
        """INSERT INTO tasks (content, effort_estimate, priority, scheduled_at, due_date, parent_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (content, effort_estimate, actual_priority, scheduled_at, due_date, parent_id, _now()),
    )
    task_id = cur.lastrowid
    for dep_id in depends_on_ids or []:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)",
            (task_id, dep_id),
        )
    conn.commit()
    return task_id


def get_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM tasks"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at"
    rows = conn.execute(query, params).fetchall()
    tasks = [dict(r) for r in rows]

    for t in tasks:
        dep_rows = conn.execute(
            "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?",
            (t["id"],),
        ).fetchall()
        dep_ids = [r["depends_on_id"] for r in dep_rows]
        t["depends_on"] = dep_ids
        if dep_ids:
            placeholders = ",".join("?" * len(dep_ids))
            dep_statuses = conn.execute(
                f"SELECT status FROM tasks WHERE id IN ({placeholders})", dep_ids
            ).fetchall()
            t["is_blocked"] = any(r["status"] != "done" for r in dep_statuses)
        else:
            t["is_blocked"] = False
            
        # Retrieve notes attached to this task
        note_rows = conn.execute(
            "SELECT * FROM notes WHERE task_id = ?",
            (t["id"],),
        ).fetchall()
        t["notes"] = [dict(r) for r in note_rows]
        
    return tasks


def add_subtasks(conn: sqlite3.Connection, parent_id: int, steps: list[str]) -> list[int]:
    ids = []
    for step in steps:
        ids.append(add_task(conn, content=step, parent_id=parent_id))
    return ids


def complete_task(conn: sqlite3.Connection, task_id: int) -> bool:
    cur = conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_task(conn: sqlite3.Connection, task_id: int) -> bool:
    """Permanently removes a task, unlike complete_task which just marks it done.
    Also cleans up any dependency rows that reference it, either as the
    dependent task or as something another task depends on."""
    conn.execute("DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_id = ?", (task_id, task_id))
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------- notes ----------

def add_note(conn: sqlite3.Connection, content: str, tags: str | None = None, task_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO notes (content, tags, task_id, created_at) VALUES (?, ?, ?, ?)",
        (content, tags, task_id, _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_note(
    conn: sqlite3.Connection,
    note_id: int,
    content: str | None = None,
    tags: str | None = None,
    status: str | None = None,
    task_id: int | None = None,
) -> bool:
    fields = []
    params = []
    if content is not None:
        fields.append("content = ?")
        params.append(content)
    if tags is not None:
        fields.append("tags = ?")
        params.append(tags)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if task_id is not None:
        fields.append("task_id = ?")
        params.append(task_id)
    if not fields:
        return False
    params.append(note_id)
    query = f"UPDATE notes SET {', '.join(fields)} WHERE id = ?"
    cur = conn.execute(query, params)
    conn.commit()
    return cur.rowcount > 0


def complete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("UPDATE notes SET status = 'done' WHERE id = ?", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def search_notes(conn: sqlite3.Connection, query: str) -> list[dict]:
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM notes WHERE content LIKE ? OR tags LIKE ? ORDER BY created_at DESC",
        (like, like),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- memory patterns ----------

def save_memory_pattern(conn: sqlite3.Connection, pattern: str, task_type: str | None = None,
                        metric_name: str | None = None, metric_value: float | None = None, outcome: str = 'win'):
    conn.execute(
        """INSERT INTO memory_patterns
           (pattern, task_type, metric_name, metric_value, outcome, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (pattern, task_type, metric_name, metric_value, outcome, _now())
    )
    conn.commit()

def search_memory_patterns(conn: sqlite3.Connection, query: str, task_type: str | None = None, outcome: str | None = None) -> list[dict]:
    like = f"%{query}%"
    sql = "SELECT * FROM memory_patterns WHERE pattern LIKE ?"
    params = [like]
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    if outcome:
        sql += " AND outcome = ?"
        params.append(outcome)
    sql += " ORDER BY COALESCE(metric_value, -1) DESC, created_at DESC LIMIT 20"
    # [FIX #2] COALESCE handles NULL metric_value — patterns without a metric
    # (qualitative insights) would otherwise sink to the bottom in DESC sort.
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
