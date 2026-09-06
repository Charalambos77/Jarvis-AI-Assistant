"""
Database layer for the second brain.
Plain sqlite3 - no MCP concepts here at all. This file doesn't know
it's going to be used by an AI; it just stores and retrieves tasks and notes.
Keeping this separate from server.py means you can test it on its own.
"""
import sqlite3
import threading
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

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,            -- project folder under "Let Jarvis Handle It"
    brief TEXT NOT NULL DEFAULT '',  -- what this section is about, written at creation
    founding_plan_id TEXT,           -- the pipeline the section grew out of
    created_at TEXT NOT NULL
);

-- A section is a live workspace: the founding pipeline is only the first one.
CREATE TABLE IF NOT EXISTS section_pipelines (
    section_id TEXT NOT NULL REFERENCES sections(id),
    plan_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (section_id, plan_id)
);

-- Chat inside a section persists to it, so Jarvis still remembers weeks later.
CREATE TABLE IF NOT EXISTS section_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id TEXT NOT NULL REFERENCES sections(id),
    role TEXT NOT NULL,              -- 'user' | 'jarvis'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipelines (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    project_name TEXT NOT NULL,
    status TEXT NOT NULL,
    current_gate TEXT,
    gate_status TEXT NOT NULL,
    phase TEXT NOT NULL,
    timestamp REAL NOT NULL,
    data TEXT NOT NULL
);

-- Every terminal command the Antigravity CLI has asked to run, and what was
-- decided about it. This is a log, not a queue: a command still waiting for an
-- answer lives in memory in command_gate.py, and only lands here once it has
-- been allowed, rejected or timed out. Kept so the Commands page can show what
-- a pipeline actually ran rather than only what it asked for.
CREATE TABLE IF NOT EXISTS agy_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL,           -- the command line, verbatim
    signature TEXT NOT NULL,         -- normalised form the rules match on
    project TEXT,                    -- project folder it was asked in
    plan_id TEXT,                    -- pipeline that delegated the build
    section_id TEXT,
    conversation_id TEXT,            -- agy's own id, for tying a run together
    decision TEXT NOT NULL,          -- allowed | rejected
    decided_by TEXT NOT NULL,        -- user | rule | denylist | timeout | unreachable
    reason TEXT NOT NULL DEFAULT '',
    asked_at TEXT NOT NULL,
    decided_at TEXT
);

-- "Always allow" answers. A rule is matched against every segment of a command
-- line, so allowing `npm install` never allows `npm install && something else`.
CREATE TABLE IF NOT EXISTS agy_command_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL,         -- e.g. "npm install", "git status"
    scope TEXT NOT NULL DEFAULT '',  -- '' = every project, else one folder name
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (signature, scope)
);
"""


# The schema and migrations only need running once per process, not on every
# connection. Doing them per connection meant every HTTP request and every agent
# event opened a connection that immediately took a WRITE lock to re-run
# CREATE TABLE IF NOT EXISTS. With a pipeline running (which writes a pipelines
# row per event) the UI's reads piled up behind those writes and the window
# stopped repainting.
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def get_connection(db_path: str) -> sqlite3.Connection:
    # Wait for a busy database rather than failing, and keep readers off the
    # writer's lock entirely via WAL (set once, below — it persists on the file).
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    if db_path in _SCHEMA_READY:
        return conn

    with _SCHEMA_LOCK:
        if db_path in _SCHEMA_READY:
            return conn
        _initialise_schema(conn)
        _SCHEMA_READY.add(db_path)
    return conn


def _initialise_schema(conn: sqlite3.Connection) -> None:
    """Create tables and apply migrations. Runs once per process, per database."""
    try:
        # WAL lets the UI keep reading while a pipeline writes. Persistent once set.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception as e:
        print(f"Could not enable WAL mode: {e}")

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

        # Sections: tasks and notes made inside a section belong to it, and the
        # brain's own lists filter them out rather than mixing them in.
        if 'section_id' not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN section_id TEXT REFERENCES sections(id)")
            conn.commit()
        if 'section_id' not in note_columns:
            conn.execute("ALTER TABLE notes ADD COLUMN section_id TEXT REFERENCES sections(id)")
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

        # Pipelines table migration
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pipelines'")
        if not cursor.fetchone():
            conn.execute("""CREATE TABLE pipelines (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                project_name TEXT NOT NULL,
                status TEXT NOT NULL,
                current_gate TEXT,
                gate_status TEXT NOT NULL,
                phase TEXT NOT NULL,
                timestamp REAL NOT NULL,
                data TEXT NOT NULL
            )""")
            conn.commit()
    except Exception as e:
        print(f"Migration error: {e}")


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
    section_id: str | None = None,
) -> int:
    actual_priority = priority or 'medium'
    if parent_id == 0:
        parent_id = None
    cur = conn.execute(
        """INSERT INTO tasks (content, effort_estimate, priority, scheduled_at, due_date, parent_id, section_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (content, effort_estimate, actual_priority, scheduled_at, due_date, parent_id, section_id, _now()),
    )
    task_id = cur.lastrowid
    for dep_id in depends_on_ids or []:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)",
            (task_id, dep_id),
        )
    conn.commit()
    return task_id


def get_tasks(conn: sqlite3.Connection, status: str | None = None,
              section_id: str | None = None, include_sections: bool = False) -> list[dict]:
    """Tasks, scoped to one section or to the brain.

    A task made inside a section belongs to that section, so the brain's own
    list must not mix them in: with no `section_id` and `include_sections`
    off, only unsectioned tasks come back. Pass `include_sections` when you
    genuinely want everything (search across the whole brain, for instance).
    """
    query = "SELECT * FROM tasks"
    where: list[str] = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if section_id:
        where.append("section_id = ?")
        params.append(section_id)
    elif not include_sections:
        where.append("section_id IS NULL")
    if where:
        query += " WHERE " + " AND ".join(where)
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


def update_task(
    conn: sqlite3.Connection,
    task_id: int,
    content: str | None = None,
    priority: str | None = None,
    effort_estimate: str | None = None,
    scheduled_at: str | None = None,
    due_date: str | None = None,
    status: str | None = None,
) -> bool:
    fields = []
    params = []
    if content is not None:
        fields.append("content = ?")
        params.append(content)
    if priority is not None:
        fields.append("priority = ?")
        params.append(priority)
    if effort_estimate is not None:
        fields.append("effort_estimate = ?")
        params.append(effort_estimate)
    if scheduled_at is not None:
        fields.append("scheduled_at = ?")
        params.append(scheduled_at)
    if due_date is not None:
        fields.append("due_date = ?")
        params.append(due_date)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if not fields:
        return False
    params.append(task_id)
    query = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"
    cur = conn.execute(query, params)
    conn.commit()
    return cur.rowcount > 0


def batch_create_tasks(conn: sqlite3.Connection, tasks: list[dict]) -> list[int]:
    created_ids = []
    with conn:
        for t in tasks:
            content = t.get("content")
            priority = t.get("priority", "medium")
            effort_estimate = t.get("effort_estimate")
            scheduled_at = t.get("scheduled_at")
            due_date = t.get("due_date")
            parent_id = t.get("parent_id")
            if parent_id == 0:
                parent_id = None
            
            cur = conn.execute(
                """INSERT INTO tasks (content, effort_estimate, priority, scheduled_at, due_date, parent_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (content, effort_estimate, priority, scheduled_at, due_date, parent_id, _now()),
            )
            created_ids.append(cur.lastrowid)
    return created_ids


def batch_delete_tasks(conn: sqlite3.Connection, task_ids: list[int]) -> int:
    if not task_ids:
        return 0
    placeholders = ",".join("?" * len(task_ids))
    with conn:
        conn.execute(f"DELETE FROM task_dependencies WHERE task_id IN ({placeholders}) OR depends_on_id IN ({placeholders})", task_ids + task_ids)
        cur = conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        count = cur.rowcount
    return count



# ---------- notes ----------

def add_note(conn: sqlite3.Connection, content: str, tags: str | None = None, task_id: int | None = None,
             section_id: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO notes (content, tags, task_id, section_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (content, tags, task_id, section_id, _now()),
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


def batch_create_notes(conn: sqlite3.Connection, notes: list[dict]) -> list[int]:
    created_ids = []
    with conn:
        for n in notes:
            content = n.get("content")
            tags = n.get("tags")
            task_id = n.get("task_id")
            cur = conn.execute(
                "INSERT INTO notes (content, tags, task_id, created_at) VALUES (?, ?, ?, ?)",
                (content, tags, task_id, _now()),
            )
            created_ids.append(cur.lastrowid)
    return created_ids


def batch_delete_notes(conn: sqlite3.Connection, note_ids: list[int]) -> int:
    if not note_ids:
        return 0
    placeholders = ",".join("?" * len(note_ids))
    with conn:
        cur = conn.execute(f"DELETE FROM notes WHERE id IN ({placeholders})", note_ids)
        count = cur.rowcount
    return count



def search_notes(conn: sqlite3.Connection, query: str, section_id: str | None = None,
                 include_sections: bool = False) -> list[dict]:
    """Notes matching `query`, scoped the same way get_tasks() is."""
    like = f"%{query}%"
    sql = "SELECT * FROM notes WHERE (content LIKE ? OR tags LIKE ?)"
    params: list = [like, like]
    if section_id:
        sql += " AND section_id = ?"
        params.append(section_id)
    elif not include_sections:
        sql += " AND section_id IS NULL"
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


# ---------- pipelines ----------

def save_pipeline(conn: sqlite3.Connection, plan: dict):
    import json
    data_dict = {
        "cycles": plan.get("cycles", []),
        "master_blueprint": plan.get("master_blueprint", {}),
        "exec_results": plan.get("exec_results", []),
        "deploy_result": plan.get("deploy_result", {}),
        "agent_plan": plan.get("agent_plan", {}),
        "approved_blueprints": plan.get("approved_blueprints", []),
        # Clarification intake: the short one-liner the user originally asked for
        # (the `task` column now holds the full clarified brief) and the path to
        # the brief file on disk, so a resumed pipeline can re-read it.
        "task_summary": plan.get("task_summary"),
        "brief_path": plan.get("brief_path"),
        # Which stages actually finished (gates the user approved, execution that
        # passed QA, deployment). `phase` alone can't answer that — it moves when a
        # stage *starts*, so a pipeline killed while waiting at a gate came back
        # looking like the gate had already been cleared, and the resume skipped it.
        "completed_stages": sorted(set(plan.get("completed_stages") or []))
    }
    data_str = json.dumps(data_dict, ensure_ascii=False)
    
    conn.execute(
        """INSERT OR REPLACE INTO pipelines (id, task, project_name, status, current_gate, gate_status, phase, timestamp, data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan["id"],
            plan["task"],
            plan["project_name"],
            plan["status"],
            plan.get("current_gate"),
            plan["gate_status"],
            plan["phase"],
            plan["timestamp"],
            data_str
        )
    )
    conn.commit()

def get_pipelines(conn: sqlite3.Connection) -> list[dict]:
    import json
    rows = conn.execute("SELECT * FROM pipelines ORDER BY timestamp DESC").fetchall()
    pipelines = []
    for r in rows:
        plan = dict(r)
        try:
            data_dict = json.loads(plan["data"])
        except Exception:
            data_dict = {}
        plan.update(data_dict)
        plan.pop("data", None)
        pipelines.append(plan)
    return pipelines

def delete_pipeline(conn: sqlite3.Connection, plan_id: str):
    conn.execute("DELETE FROM pipelines WHERE id = ?", (plan_id,))
    conn.commit()




# ---------- sections ----------
#
# A section is a lasting workspace grown out of one finished pipeline. The
# pipeline stops being a one-off run and becomes standing knowledge; new
# pipelines started inside the section build on top of it.

def create_section(conn: sqlite3.Connection, section_id: str, name: str, folder: str,
                   brief: str = "", founding_plan_id: str | None = None) -> str:
    conn.execute(
        """INSERT INTO sections (id, name, folder, brief, founding_plan_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (section_id, name, folder, brief or "", founding_plan_id, _now()),
    )
    if founding_plan_id:
        conn.execute(
            "INSERT OR IGNORE INTO section_pipelines (section_id, plan_id, added_at) VALUES (?, ?, ?)",
            (section_id, founding_plan_id, _now()),
        )
    conn.commit()
    return section_id


def get_sections(conn: sqlite3.Connection) -> list[dict]:
    """Every section, newest first, each carrying its pipeline ids."""
    rows = conn.execute("SELECT * FROM sections ORDER BY created_at DESC").fetchall()
    sections = []
    for r in rows:
        section = dict(r)
        section["plan_ids"] = get_section_plan_ids(conn, section["id"])
        sections.append(section)
    return sections


def get_section(conn: sqlite3.Connection, section_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
    if not row:
        return None
    section = dict(row)
    section["plan_ids"] = get_section_plan_ids(conn, section_id)
    return section


def get_section_by_folder(conn: sqlite3.Connection, folder: str) -> dict | None:
    """The section owning a project folder, if one does.

    Two pipelines can derive the same project name, so the folder is not a key —
    but it is how a running pipeline finds the section it belongs to.
    """
    row = conn.execute("SELECT * FROM sections WHERE folder = ? ORDER BY created_at LIMIT 1",
                       (folder,)).fetchone()
    return get_section(conn, row["id"]) if row else None


def update_section(conn: sqlite3.Connection, section_id: str, name: str | None = None,
                   brief: str | None = None) -> bool:
    fields, params = [], []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if brief is not None:
        fields.append("brief = ?")
        params.append(brief)
    if not fields:
        return False
    params.append(section_id)
    cur = conn.execute(f"UPDATE sections SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    return cur.rowcount > 0


def delete_section(conn: sqlite3.Connection, section_id: str) -> bool:
    """Forget the section. Its folder on disk and its pipelines are left alone —
    deleting a workspace must not destroy the work that was done inside it."""
    conn.execute("UPDATE tasks SET section_id = NULL WHERE section_id = ?", (section_id,))
    conn.execute("UPDATE notes SET section_id = NULL WHERE section_id = ?", (section_id,))
    conn.execute("DELETE FROM section_chat WHERE section_id = ?", (section_id,))
    conn.execute("DELETE FROM section_pipelines WHERE section_id = ?", (section_id,))
    cur = conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    conn.commit()
    return cur.rowcount > 0


def add_pipeline_to_section(conn: sqlite3.Connection, section_id: str, plan_id: str):
    conn.execute(
        "INSERT OR IGNORE INTO section_pipelines (section_id, plan_id, added_at) VALUES (?, ?, ?)",
        (section_id, plan_id, _now()),
    )
    conn.commit()


def get_section_plan_ids(conn: sqlite3.Connection, section_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT plan_id FROM section_pipelines WHERE section_id = ? ORDER BY added_at",
        (section_id,),
    ).fetchall()
    return [r["plan_id"] for r in rows]


def get_section_for_pipeline(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    row = conn.execute(
        "SELECT section_id FROM section_pipelines WHERE plan_id = ? LIMIT 1", (plan_id,)
    ).fetchone()
    return get_section(conn, row["section_id"]) if row else None


# ---------- section chat ----------

def add_section_message(conn: sqlite3.Connection, section_id: str, role: str, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO section_chat (section_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (section_id, role, content, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_section_messages(conn: sqlite3.Connection, section_id: str, limit: int = 200) -> list[dict]:
    """The section's conversation, oldest first.

    `limit` takes the most RECENT messages and then restores reading order, so a
    long-lived section hands the model what was just said rather than what was
    said the day it was created.
    """
    rows = conn.execute(
        "SELECT * FROM section_chat WHERE section_id = ? ORDER BY id DESC LIMIT ?",
        (section_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- Antigravity command log & rules ----------
#
# The Antigravity CLI can edit files on its own, but every terminal command it
# wants to run comes here first. These two tables are what the Commands page
# reads: the log of what was asked and decided, and the standing "always allow"
# answers that let a later run through without asking again.

def log_command(conn: sqlite3.Connection, entry: dict) -> int:
    """Record one decided command. Ignores a repeat of the same request_id."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO agy_commands
           (request_id, command, signature, project, plan_id, section_id,
            conversation_id, decision, decided_by, reason, asked_at, decided_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["request_id"], entry["command"], entry["signature"],
            entry.get("project"), entry.get("plan_id"), entry.get("section_id"),
            entry.get("conversation_id"), entry["decision"], entry["decided_by"],
            entry.get("reason") or "", entry["asked_at"], entry.get("decided_at") or _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_commands(conn: sqlite3.Connection, plan_id: str | None = None,
                 project: str | None = None, limit: int = 300) -> list[dict]:
    """The command log, newest first, optionally narrowed to one run or project."""
    sql = "SELECT * FROM agy_commands"
    clauses, params = [], []
    if plan_id:
        clauses.append("plan_id = ?")
        params.append(plan_id)
    if project:
        clauses.append("project = ?")
        params.append(project)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_command_rule(conn: sqlite3.Connection, signature: str, scope: str = "",
                     reason: str = "") -> int:
    """Store an 'always allow'. Re-allowing the same thing refreshes the reason."""
    conn.execute(
        """INSERT INTO agy_command_rules (signature, scope, reason, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (signature, scope)
           DO UPDATE SET reason = excluded.reason""",
        (signature, scope or "", reason or "", _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM agy_command_rules WHERE signature = ? AND scope = ?",
        (signature, scope or ""),
    ).fetchone()
    return row["id"] if row else 0


def get_command_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM agy_command_rules ORDER BY signature, scope"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_command_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM agy_command_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0
