"""Regression test for the plan page's Reject button.

The page opens a waiting gate even when no plan is explicitly selected, so the
Reject button used to submit an empty plan_id. The backend refused the call and
the page closed the modal anyway, leaving the pipeline waiting forever with no
sign that anything had gone wrong.
"""
import re
import sys

import jarvis

app = jarvis.app.test_client()


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


# ---- backend: what the old, empty plan_id did ------------------------------
with jarvis.PIPELINE_LOCK:
    state = jarvis._get_gate_state("42")
    state["current_gate"] = "cycle_1_research"
    state["gate_status"] = "waiting"

r = app.post("/gate/reject", json={"plan_id": "", "redirect_note": "wrong shape",
                                   "rejected_steps": ["agent_1"]})
check("empty plan_id is refused by the backend", r.status_code == 400)
with jarvis.PIPELINE_LOCK:
    check("the gate is still waiting after that refusal",
          jarvis._get_gate_state("42")["gate_status"] == "waiting")

# ---- backend: a properly addressed rejection lands -------------------------
r = app.post("/gate/reject", json={"plan_id": "42", "redirect_note": "wrong shape",
                                   "rejected_steps": ["agent_1"]})
body = r.get_json()
check("rejection accepted", r.status_code == 200 and body["status"] == "rejected")
with jarvis.PIPELINE_LOCK:
    st = jarvis._get_gate_state("42")
    check("gate marked rejected", st["gate_status"] == "rejected")
    check("note reaches the pipeline", st["redirect_note"] == "wrong shape")
    check("rejected steps reach the pipeline", st["rejected_steps"] == ["agent_1"])

# ---- frontend: the page must name the plan and honour failures -------------
page = open("plan.html", encoding="utf-8").read()
submit = page[page.index("function submitApproval"):]
submit = submit[:submit.index("\n        function ")]

check("modal tracks which plan its gate belongs to", "let modalPlanId" in page)
check("modal records the plan it renders a gate for",
      re.search(r"selectedPlanId === plan\.id\) \{\s*\n\s*modalPlanId = plan\.id;", page) is not None)
check("submit falls back to the plan on screen", "selectedPlanId || modalPlanId" in submit)
check("submit refuses to post an empty plan id", "if (!planId)" in submit)
check("submit no longer posts selectedPlanId directly",
      "plan_id: selectedPlanId" not in submit)
check("submit checks the response status", "if (!ok)" in submit)
check("a failed call keeps the modal open",
      submit.index("if (!ok)") < submit.index("userClosedModal = true"))
check("openApprovalModal falls back too", "selectedPlanId || modalPlanId" in page)

print("\nAll checks passed.")
