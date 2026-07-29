import os
import sys

# Redirect stdout to stderr during imports to prevent output contamination on stdio
original_stdout = sys.stdout
sys.stdout = sys.stderr

try:
    from mcp.server.fastmcp import FastMCP
    import db
finally:
    sys.stdout = original_stdout

# Define the absolute path to the database
DB_PATH = "d:/Charalambos/Desktop/AI/second-brain-voice/second_brain.db"

# Initialize FastMCP Server
mcp = FastMCP("Jarvis")

def get_db_conn():
    return db.get_connection(DB_PATH)

# ---------- Task Tools ----------

@mcp.tool()
def get_tasks(status: str = None) -> list[dict]:
    """
    Get all tasks. Optionally filter by status ('open', 'in_progress', 'done').
    """
    conn = get_db_conn()
    try:
        return db.get_tasks(conn, status=status)
    finally:
        conn.close()

@mcp.tool()
def add_task(
    content: str,
    effort_estimate: str = None,
    priority: str = None,
    scheduled_at: str = None,
    due_date: str = None,
    depends_on_ids: list[int] = None,
    parent_id: int = None
) -> int:
    """
    Add a new task.
    - effort_estimate: 'small', 'medium', or 'large'
    - priority: 'low', 'medium', or 'high'
    - scheduled_at: Fixed ISO datetime commitment (e.g. 2026-07-28T10:00:00Z)
    - due_date: Soft deadline (YYYY-MM-DD format)
    - depends_on_ids: List of task IDs this task depends on
    - parent_id: Parent task ID for subtasks
    """
    conn = get_db_conn()
    try:
        task_id = db.add_task(
            conn,
            content=content,
            effort_estimate=effort_estimate,
            priority=priority,
            scheduled_at=scheduled_at,
            due_date=due_date,
            depends_on_ids=depends_on_ids,
            parent_id=parent_id
        )
        return task_id
    finally:
        conn.close()

@mcp.tool()
def add_subtasks(parent_id: int, steps: list[str]) -> list[int]:
    """
    Add multiple subtasks to a parent task.
    - parent_id: The ID of the parent task
    - steps: A list of task descriptions/steps to create as subtasks
    """
    conn = get_db_conn()
    try:
        return db.add_subtasks(conn, parent_id=parent_id, steps=steps)
    finally:
        conn.close()

@mcp.tool()
def complete_task(task_id: int) -> bool:
    """
    Mark a task as completed (status = 'done').
    """
    conn = get_db_conn()
    try:
        return db.complete_task(conn, task_id=task_id)
    finally:
        conn.close()

@mcp.tool()
def delete_task(task_id: int) -> bool:
    """
    Permanently delete a task and clean up its dependencies.
    """
    conn = get_db_conn()
    try:
        return db.delete_task(conn, task_id=task_id)
    finally:
        conn.close()

# ---------- Note Tools ----------

@mcp.tool()
def add_note(content: str, tags: str = None, task_id: int = None) -> int:
    """
    Add a note to the second brain.
    - tags: Comma-separated list of tags
    - task_id: Optional ID of a task to link this note to
    """
    conn = get_db_conn()
    try:
        return db.add_note(conn, content=content, tags=tags, task_id=task_id)
    finally:
        conn.close()

@mcp.tool()
def update_note(
    note_id: int,
    content: str = None,
    tags: str = None,
    status: str = None,
    task_id: int = None
) -> bool:
    """
    Update details of an existing note.
    """
    conn = get_db_conn()
    try:
        return db.update_note(
            conn,
            note_id=note_id,
            content=content,
            tags=tags,
            status=status,
            task_id=task_id
        )
    finally:
        conn.close()

@mcp.tool()
def complete_note(note_id: int) -> bool:
    """
    Mark a note as completed/archived (status = 'done').
    """
    conn = get_db_conn()
    try:
        return db.complete_note(conn, note_id=note_id)
    finally:
        conn.close()

@mcp.tool()
def delete_note(note_id: int) -> bool:
    """
    Permanently delete a note.
    """
    conn = get_db_conn()
    try:
        return db.delete_note(conn, note_id=note_id)
    finally:
        conn.close()

@mcp.tool()
def search_notes(query: str) -> list[dict]:
    """
    Search for notes containing query in content or tags.
    """
    conn = get_db_conn()
    try:
        return db.search_notes(conn, query=query)
    finally:
        conn.close()

if __name__ == "__main__":
    mcp.run()
