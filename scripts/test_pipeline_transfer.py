"""Carrying a pipeline to another PC: its database row exported into its folder, imported there.

The database holds every personal task and note, so it never travels. Checked with
throwaway databases and folders; nothing real is touched.
"""
import json, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
import pipeline_transfer as pt


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def pipeline(conn, plan_id):
    return next((p for p in db.get_pipelines(conn) if p["id"] == plan_id), None)


PROJECT = "Competitor Deep Dive"
tmp = tempfile.mkdtemp(prefix="jarvis_transfer_test_")
conns = []
try:
    this_pc, other_pc = os.path.join(tmp, "pc1"), os.path.join(tmp, "pc2")
    for base in (this_pc, other_pc):
        os.makedirs(os.path.join(base, pt.PROJECTS_DIR_NAME, PROJECT, "Brief"))
    brief_here = os.path.join(this_pc, pt.PROJECTS_DIR_NAME, PROJECT, "Brief", "clarified_brief.md")
    open(brief_here, "w", encoding="utf-8").write("brief")

    def connect(name):
        conns.append(db.get_connection(os.path.join(tmp, name)))
        return conns[-1]

    here = connect("pc1.db")
    plan = {"id": "9", "task": "# Clarified Brief", "project_name": PROJECT, "status": "running",
            "current_gate": "cycle_1_research", "gate_status": "waiting", "phase": "research",
            "timestamp": 123.0, "brief_path": brief_here, "task_summary": "Deep research",
            "completed_stages": [], "approved_blueprints": []}
    db.save_pipeline(here, plan)

    # ---- export ------------------------------------------------------------------
    path = pt.export_pipeline(here, "9", this_pc)
    state = json.load(open(path, encoding="utf-8"))
    check("the state file sits in the pipeline's own project folder",
          path == os.path.join(this_pc, pt.PROJECTS_DIR_NAME, PROJECT, "pipeline_state_9.json"))
    check("it keeps no path that only works on this PC",
          "brief_path" not in state and state["brief_path_in_project"] == "Brief/clarified_brief.md")
    check("and carries the pipeline's state", state["current_gate"] == "cycle_1_research" and state["task_summary"] == "Deep research")
    try:
        pt.export_pipeline(here, "404", this_pc)
        check("exporting a pipeline that doesn't exist says so", False)
    except LookupError:
        check("exporting a pipeline that doesn't exist says so", True)

    # ---- import on the other PC --------------------------------------------------------
    # What git brings over: the folder, with its brief and its state file.
    other_folder = os.path.join(other_pc, pt.PROJECTS_DIR_NAME, PROJECT)
    shutil.copy(path, other_folder)
    open(os.path.join(other_folder, "Brief", "clarified_brief.md"), "w", encoding="utf-8").write("brief")

    there = connect("pc2.db")
    report = pt.import_exported_pipelines(there, other_pc)
    got = pipeline(there, "9")
    check("a pipeline the other PC doesn't have is imported", len(report) == 1 and report[0].startswith("Imported pipeline 9"))
    check("with its state intact", got["current_gate"] == "cycle_1_research" and got["gate_status"] == "waiting"
          and got["task"] == "# Clarified Brief" and got["timestamp"] == 123.0)
    check("and its brief found where it is on that PC", got["brief_path"] == os.path.join(other_folder, "Brief", "clarified_brief.md"))
    check("importing again changes nothing and says nothing", pt.import_exported_pipelines(there, other_pc) == [])

    got["gate_status"] = "approved"
    db.save_pipeline(there, got)
    pt.import_exported_pipelines(there, other_pc)
    check("a pipeline that has moved on on that PC is never rewound by an older state file",
          pipeline(there, "9")["gate_status"] == "approved")

    # ---- what must never happen -----------------------------------------------------------
    taken = connect("pc3.db")
    db.save_pipeline(taken, {**plan, "project_name": "Uncle Video"})
    report = pt.import_exported_pipelines(taken, other_pc)
    check("an id a different project already uses is never overwritten, and it says why",
          len(report) == 1 and "already 'Uncle Video'" in report[0] and pipeline(taken, "9")["project_name"] == "Uncle Video")

    stray_dir = os.path.join(other_pc, pt.PROJECTS_DIR_NAME, "Somewhere Else")
    os.makedirs(stray_dir)
    json.dump({**state, "id": "12"}, open(os.path.join(stray_dir, "pipeline_state_12.json"), "w", encoding="utf-8"))
    open(os.path.join(other_folder, "pipeline_state_13.json"), "w", encoding="utf-8").write("{ not json")
    fresh = connect("pc4.db")
    report = pt.import_exported_pipelines(fresh, other_pc)
    check("a state file sitting in another project's folder is skipped",
          any("pipeline_state_12" in r and "another folder" in r for r in report) and pipeline(fresh, "12") is None)
    check("an unreadable state file is skipped, not fatal",
          any("pipeline_state_13" in r and "not a readable" in r for r in report) and pipeline(fresh, "9") is not None)

    # ---- Jarvis does it on startup --------------------------------------------------------------
    source = open(os.path.join(ROOT, "jarvis.py"), encoding="utf-8").read()
    check("Jarvis imports carried-over pipelines before it loads its pipelines",
          "import_exported_pipelines(" in source
          and source.index("import_exported_pipelines(") < source.index("\nload_pipelines_from_db()\n"))
finally:
    for c in conns:
        c.close()
    shutil.rmtree(tmp, ignore_errors=True)

print("\nAll pipeline transfer checks passed.")
