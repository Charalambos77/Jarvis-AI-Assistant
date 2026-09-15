"""Carry a pipeline from one PC to another through the repository.

A pipeline lives in two places: its project folder under "Let Jarvis Handle It/",
and its row in second_brain.db. The database can't travel, because it also holds
every personal task and note. So the row is exported into the project folder as
pipeline_state_<id>.json, and Jarvis imports it on startup on any PC the folder
reaches.

    python pipeline_transfer.py export 9     # write or refresh the state file before pushing
"""
import glob
import json
import os
import sys

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR_NAME = "Let Jarvis Handle It"
STATE_PREFIX = "pipeline_state_"
BRIEF_KEY = "brief_path"
RELATIVE_BRIEF_KEY = "brief_path_in_project"


def _project_dir(base_dir: str, project_name: str) -> str:
    return os.path.join(base_dir, PROJECTS_DIR_NAME, project_name)


def export_pipeline(conn, plan_id: str, base_dir: str = BASE_DIR) -> str:
    """Write one pipeline's database row into its project folder. Returns the file's path."""
    plan = next((p for p in db.get_pipelines(conn) if str(p.get("id")) == str(plan_id)), None)
    if plan is None:
        raise LookupError(f"No pipeline {plan_id} in the database.")
    project_dir = _project_dir(base_dir, plan["project_name"])
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"Pipeline {plan_id}'s project folder is missing: {project_dir}")

    state = dict(plan)
    # An absolute path only works on the PC that wrote it; keep the brief's place in the folder.
    brief = state.pop(BRIEF_KEY, None)
    if brief:
        try:
            relative = os.path.relpath(brief, project_dir)
        except ValueError:            # on another drive: not inside the folder
            relative = None
        if relative and not relative.startswith(".."):
            state[RELATIVE_BRIEF_KEY] = relative.replace(os.sep, "/")

    path = os.path.join(project_dir, f"{STATE_PREFIX}{plan_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return path


def import_exported_pipelines(conn, base_dir: str = BASE_DIR) -> list[str]:
    """Add every exported pipeline this database doesn't have yet. Returns one line per thing done.

    A pipeline already here under the same id and project is left alone: this PC's
    copy is the one that has been running, and an older state file must not rewind
    it. An id a different project already uses is never overwritten.
    """
    existing = {str(p.get("id")): p for p in db.get_pipelines(conn)}
    report = []
    pattern = os.path.join(base_dir, PROJECTS_DIR_NAME, "*", f"{STATE_PREFIX}*.json")
    for path in sorted(glob.glob(pattern)):
        where = os.path.relpath(path, base_dir)
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            plan_id, project = str(state["id"]), state["project_name"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            report.append(f"Skipped {where}: not a readable pipeline state ({e}).")
            continue
        # The folder it sits in is the project it belongs to; anything else is a stray file.
        if os.path.basename(os.path.dirname(path)) != project:
            report.append(f"Skipped {where}: it names project '{project}' but sits in another folder.")
            continue

        here = existing.get(plan_id)
        if here is not None:
            if here.get("project_name") != project:
                report.append(f"Pipeline {plan_id} ({project}) not imported: id {plan_id} is already "
                              f"'{here.get('project_name')}' on this PC.")
            continue

        relative = state.pop(RELATIVE_BRIEF_KEY, None)
        brief = os.path.join(_project_dir(base_dir, project), *relative.split("/")) if relative else None
        state[BRIEF_KEY] = brief if brief and os.path.isfile(brief) else None
        for key, default in (("task", ""), ("status", "running"), ("gate_status", "idle"),
                             ("phase", "research"), ("timestamp", 0)):
            state.setdefault(key, default)
        db.save_pipeline(conn, state)
        existing[plan_id] = state
        report.append(f"Imported pipeline {plan_id} ({project}) from {where}.")
    return report


def _main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "export":
        print(__doc__)
        return 2
    conn = db.get_connection(os.path.join(BASE_DIR, "second_brain.db"))
    try:
        print(f"Wrote {export_pipeline(conn, argv[1])}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
