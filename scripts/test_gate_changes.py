"""What the user says when they reject a gate, read as a change to their brief.

Pipeline 9: the user rejected Cycle 1 asking for crowded, competitive markets. The note
reached the Brain's re-plan and nothing else, so the new agents still read the original
brief — "medium economy", "just started incorporating AI" — as what the user wanted.
Recording it as "the newest instruction wins" would swap one mistake for another: the
note would become the whole job, and everything it never mentioned would quietly vanish.
"""
import asyncio, inspect, json, os, shutil, sys, tempfile
from types import SimpleNamespace as ns

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import multi_agent_coordinator as mac
from agents import brief_changes as bc
from agents.user_brief import user_brief_block

BRIEF = ("Find the top 20 competitors in each of 5 English-speaking countries with a medium economy "
         "that just started using AI. Upload the research to Google Docs.")
NOTE = "Now find the countries where a lot of businesses compete with each other, they market this best"
READING = {
    "replaces": [{"was": "countries that just started using AI", "now": "countries where many agencies compete"}],
    "adds": ["study how the agencies in those countries market themselves"],
    "drops": [],
    "still_stands": ["20 competitors in each of 5 countries", "English-speaking countries only",
                     "the research is uploaded to Google Docs"],
    "unclear": ["Does the medium economy range still apply, or is any size fine now?"],
}


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def use_model(reply):
    """A stand-in model; returns the prompts it was given."""
    prompts = []

    def generate_content(model=None, contents=None, config=None, **kwargs):
        prompts.append(contents)
        if isinstance(reply, Exception):
            raise reply
        return ns(text=reply)

    bc.genai.Client = lambda *a, **k: ns(models=ns(generate_content=generate_content))
    bc.GEMINI_API_KEY = "test-key"
    return prompts


tmp = tempfile.mkdtemp(prefix="jarvis_gate_change_test_")
try:
    brief_file = os.path.join(tmp, "clarified_brief.md")
    with open(brief_file, "w", encoding="utf-8") as f:
        f.write(BRIEF)

    # ---- the note is read against the brief ------------------------------------------
    prompts = use_model(json.dumps(READING))
    after = asyncio.run(bc.record_gate_change(brief_file, BRIEF, "cycle 1 research", f"  {NOTE}  "))
    on_disk = open(brief_file, encoding="utf-8").read()

    check("the brief the user wrote is kept, with the change after it", after.startswith(BRIEF) and bc.CHANGES_HEADING in after)
    check("their own words are quoted", f"> {NOTE}" in after)
    check("what the change replaces is spelled out, in the brief's words",
          '- Replaces: "countries that just started using AI" is now: countries where many agencies compete' in after)
    check("what it adds is listed", "- Adds: study how the agencies in those countries market themselves" in after)
    check("what it leaves standing is listed, so it is not read as the whole job",
          "- Unchanged by this, still required: 20 competitors in each of 5 countries" in after
          and "English-speaking countries only" in after)
    check("what it does not settle is flagged, not guessed",
          "- Unclear, ask the user rather than assume: Does the medium economy range still apply" in after)
    check("the rule says a change alters only what it names", bc.CHANGES_RULE in after and "only what it names" in bc.CHANGES_RULE)
    check("nothing claims the newest instruction replaces the brief", "newest wins" not in after.lower())
    check("the brief file on disk says the same", bc.CHANGES_HEADING in on_disk and NOTE in on_disk)

    prompt = prompts[0]
    check("the reading is made against the brief and the note", BRIEF in prompt and NOTE in prompt)
    check("and is told the note changes the brief rather than replacing it",
          "change TO this brief, never a replacement FOR it" in prompt)

    # ---- a second change joins the first ---------------------------------------------
    use_model(json.dumps({"replaces": [], "adds": ["only agencies, never freelancers"],
                          "drops": [], "still_stands": [], "unclear": []}))
    second = asyncio.run(bc.record_gate_change(brief_file, after, "cycle 2 research", "Only agencies, no freelancers"))
    check("a second change goes under the same heading, newest last",
          second.count(bc.CHANGES_HEADING) == 1 and second.index(NOTE) < second.index("Only agencies, no freelancers"))
    check("each change says which gate it came from",
          "### At the cycle 1 research gate the user said:" in second
          and "### At the cycle 2 research gate the user said:" in second)

    # ---- when the reading cannot be made ---------------------------------------------
    use_model(RuntimeError("model unavailable"))
    failed = asyncio.run(bc.record_gate_change(None, BRIEF, "final QA", NOTE))
    check("a note nobody could break down is still recorded, word for word", f"> {NOTE}" in failed)
    check("and says so, instead of overriding the brief",
          bc.UNINTERPRETED_NOTE in failed and "keep every other requirement in the brief" in bc.UNINTERPRETED_NOTE)

    bc.GEMINI_API_KEY = ""
    prompts = use_model(json.dumps(READING))
    bc.GEMINI_API_KEY = ""
    no_key = asyncio.run(bc.record_gate_change(None, BRIEF, "final QA", NOTE))
    check("with no model configured, the note is kept and no call is made",
          bc.UNINTERPRETED_NOTE in no_key and not prompts)

    # ---- nothing to record ------------------------------------------------------------
    bc.GEMINI_API_KEY = "test-key"
    before_empty = open(brief_file, encoding="utf-8").read()
    check("an empty note changes neither the brief nor the file",
          asyncio.run(bc.record_gate_change(brief_file, second, "final QA", "   ")) == second
          and open(brief_file, encoding="utf-8").read() == before_empty)
    use_model(json.dumps(READING))
    check("an unwritable brief file is not fatal",
          NOTE in asyncio.run(bc.record_gate_change(os.path.join(tmp, "nope", "b.md"), BRIEF, "final QA", NOTE)))

    # ---- what the agents are told -----------------------------------------------------
    block = user_brief_block(second)
    check("every agent is shown the changes with the brief", NOTE in block and "Only agencies, no freelancers" in block)
    check("and told to read each one as a change to the brief, not a replacement",
          "change TO the brief, not as a replacement FOR it" in block and "alters only what it names" in block)
    check("and to ask about anything the change leaves unclear", "ask rather than pick a reading that suits you" in block)

    source = inspect.getsource(mac.run_full_pipeline)
    check("every gate records what the user said when they rejected it",
          source.count("user_brief = await record_gate_change(brief_path, user_brief") == 3)
    check("the research gate records it before the cycle is re-planned",
          source.index("record_gate_change(brief_path, user_brief, f\"cycle {cycle_id} research\"")
          < source.index("rejected_steps=rejected_steps, event_logger=event_logger"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nAll gate change checks passed.")
