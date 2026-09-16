"""No made-up links, and no disagreement settled by who said it. Checked without a real model.

Pipeline 9's Cycle 1 lead cited nine links like https://www.web_search_output.com/...:
web_search gave agents a summary with nothing real to cite. Then the lead reviewed its
own work and every disagreement went its way "as it aligns with the lead specialist's
chosen output"; Pakistan, the advisor's pick with the clearest evidence, vanished
without a reason.
"""
import asyncio, inspect, json, os, shutil, sys
from types import SimpleNamespace as ns

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

import multi_agent_coordinator as mac
from agents import links, research_agent, synthesis, tool_executor

PROJECT = "__evidence_rules_test__"
PROJECT_DIR = os.path.join(mac.BASE_DIR, "Let Jarvis Handle It", PROJECT)
AGENTS_DIR = os.path.join(PROJECT_DIR, "Implementation plan", "Agents")
FAKE = "https://www.web_search_output.com/Singapore_national_AI_strategy_growth"
REAL = "https://www.dawn.com/news/1927634"
OTHER_REAL = "https://www.brecorder.com/news/40375484/pakistan-govt-approves-national-ai-policy-2025"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


class _Models:
    """Hands back scripted replies in order and records every prompt."""
    def __init__(self, replies):
        self.replies, self.contents = list(replies), []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.contents.append(contents)
        return ns(text=self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


def use_models(*replies):
    models = _Models(replies)
    genai.Client = lambda *a, **k: ns(models=models)
    return models


# ---- 1. what counts as a real link ------------------------------------------------------------
ev = links.Evidence(['{"sources": [{"title": "dawn.com", "url": "https://www.dawn.com/news/1927634"}]}',
                     "Top agencies include Acme Digital (acme.ie) and others."])
check("a link a tool returned is real, however it is spelled", ev.has(REAL) and ev.has("http://dawn.com/news/1927634/"))
check("a made-up domain is not", not ev.has(FAKE))
check("a made-up page on a real site is not", not ev.has("https://www.dawn.com/news/made-up-story"))
check("a homepage the results name is real", ev.has("https://acme.ie") and ev.has("https://www.acme.ie/"))
check("a lookalike of a named site is not", not ev.has("https://acme.ie.fake.com") and not ev.has("https://notacme.ie"))

value = {"countries": [{"name": "Pakistan", "note": f"Policy approved July 2025 ({REAL}); see also ({FAKE})"}],
         "links": [REAL, FAKE], "website": FAKE,
         "recommended_tools": [{"doc_url": "https://developers.google.com/docs/api"}]}
cleaned, removed = links.strip_unevidenced_links(value, ev)
check("made-up links are cut out of sentences, lists and fields",
      cleaned["countries"][0]["note"] == f"Policy approved July 2025 ({REAL}); see also"
      and cleaned["links"] == [REAL] and cleaned["website"] is None and FAKE not in json.dumps(cleaned))
check("each removed link is reported once", removed == [FAKE])
check("a recommended tool's documentation link is left alone",
      cleaned["recommended_tools"][0]["doc_url"] == "https://developers.google.com/docs/api")

capped = links.cap_for_invented_links({"confidence": 0.95}, [FAKE, FAKE])
check("an agent that made links up has its confidence capped, and says why",
      capped["confidence"] == 0.6 and capped["invented_links_removed"] == 1 and "1 link(s)" in capped["confidence_note"])
check("nothing made up, nothing changed", links.cap_for_invented_links({"confidence": 0.9}, []) == {"confidence": 0.9})


# ---- 2. web_search returns the pages it drew on -------------------------------------------------
def redirect(n):
    return f"https://vertexaisearch.cloud.google.com/grounding-api-redirect/{n}"


resolve_for_real = tool_executor._resolve_grounding_link
grounded = ns(text="Pakistan approved its AI policy on 30 July 2025.", candidates=[ns(grounding_metadata=ns(
    grounding_chunks=[ns(web=ns(title="dawn.com", uri=redirect("a"))),
                      ns(web=ns(title="brecorder.com", uri=redirect("b"))),
                      ns(web=ns(title="dawn.com", uri=redirect("a")))],
    grounding_supports=[ns(segment=ns(text="approved its AI policy on 30 July 2025"), grounding_chunk_indices=[0, 1, 2])],
))])
genai.Client = lambda *a, **k: ns(models=ns(generate_content=lambda **kw: grounded))
tool_executor._resolve_grounding_link = {redirect("a"): REAL, redirect("b"): OTHER_REAL}.get
tool_executor.GEMINI_API_KEY = "test-key"
out = tool_executor.web_search_impl(PROJECT, "agent", "Pakistan AI policy approval date")
check("web_search returns the real pages, redirects resolved and repeats merged",
      [s["url"] for s in out["sources"]] == [REAL, OTHER_REAL] and out["sources"][0]["title"] == "dawn.com")
check("and which sentence each page backs",
      out["citations"] == [{"claim": "approved its AI policy on 30 July 2025", "sources": [1, 2]}])

genai.Client = lambda *a, **k: ns(models=ns(generate_content=lambda **kw: ns(text="x", candidates=[ns(grounding_metadata=None)])))
out = tool_executor.web_search_impl(PROJECT, "agent", "q")
check("a search that returned no pages says not to cite a link", out["sources"] == [] and "no source links" in out["note"])

tool_executor._resolve_grounding_link = resolve_for_real
real_head = tool_executor.requests.head
try:
    tool_executor.requests.head = lambda uri, allow_redirects, timeout: ns(status_code=302, headers={"Location": REAL})
    check("a grounding redirect resolves to the publisher's own address", resolve_for_real(redirect("a")) == REAL)
    check("any other link is left as it is", resolve_for_real(OTHER_REAL) == OTHER_REAL)

    def offline(*a, **k):
        raise tool_executor.requests.RequestException("offline")
    tool_executor.requests.head = offline
    check("a redirect that can't be reached stays the redirect it was", resolve_for_real(redirect("a")) == redirect("a"))
finally:
    tool_executor.requests.head = real_head


# ---- 3. a research agent keeps only links its tools returned -------------------------------------
check("research agents are told to cite only links a tool returned",
      "13. LINKS: only write a link that one of your tools returned" in inspect.getsource(research_agent.run_research_agent))

research_agent.get_tools_for_execution_agent = lambda *a, **k: (
    [{"name": "web_search", "description": "Search.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}],
    {"web_search": None}, [])
research_agent.run_tool = lambda *a, **k: {"status": "ok", "summary": "Pakistan approved its AI policy.",
                                           "sources": [{"n": 1, "title": "dawn.com", "url": REAL}]}
answer = json.dumps({"confidence": 0.95,
                     "findings": {"pakistan": f"Approved July 2025 ({REAL})", "extra": f"More at {FAKE}"},
                     "sources": ["web_search: Pakistan AI policy", REAL, FAKE]})
replies = [ns(text=None, function_calls=[ns(name="web_search", args={"query": "Pakistan AI policy"})]),
           ns(text=answer, function_calls=None)]
chat = ns(send_message=lambda m, config=None: replies.pop(0))
genai.Client = lambda *a, **k: ns(chats=ns(create=lambda **kw: chat))
result = asyncio.run(research_agent.run_research_agent(
    {"agent_id": "a1", "role": "Analyst", "brief": "B", "tools_needed": ["web_search"]}, project_name=PROJECT))
check("the link its search returned is kept, as a source and in its findings",
      result["sources"] == ["web_search: Pakistan AI policy", REAL] and REAL in result["findings"]["pakistan"])
check("the link it made up is gone from everywhere, the unverified list included", FAKE not in json.dumps(result))
check("it is counted and its confidence capped", result["invented_links_removed"] == 1 and result["confidence"] == 0.6)
check("the links its tools returned travel with its result", result["evidence_links"] == [REAL])


# ---- 4. the lead's review --------------------------------------------------------------------------
lead = {"sources": ["web_search: countries by GDP"], "evidence_links": ["https://www.imf.org/en/Publications/WEO"]}
advisors = [{"agent_id": "adv_2", "sources": ["web_search: Pakistan AI policy"], "evidence_links": [REAL]}]
final = {"confidence": 0.98,
         "findings": {"pakistan": f"Policy 2025 ({REAL})", "ireland": f"Strategy 2021 ({FAKE})"},
         "disagreements": [{"description": "x", "decided_by": f"see {FAKE}"}],
         "sources": ["web_search: countries by GDP", REAL, FAKE, "IMF estimates"]}
mac.merge_review_sources(final, lead, advisors, extra_evidence=[{"approved": "https://prior.example/report"}])
check("the review keeps links its agents' tools returned", REAL in final["sources"] and REAL in final["findings"]["pakistan"])
check("and takes out every link none of them returned",
      FAKE not in json.dumps(final) and final["invented_links_removed"] == 1 and final["confidence"] == 0.6)
check("a source named without a link is still flagged as unverified", final["unverified_sources"] == ["IMF estimates"])
check("links from research approved in earlier cycles count as evidence",
      links.Evidence(final["evidence_links"]).has("https://prior.example/report"))

models = use_models(json.dumps({"findings": {"x": 1}, "sources": []}))
asyncio.run(mac.run_lead_review({"agent_id": "lead1", "role": "Lead", "brief": "Lead."},
                                {"agent_id": "lead1", "findings": {}}, [], []))
prompt = models.contents[-1]
check("the lead weighs its own findings like any advisor's",
      "no more weight than any advisor's" in prompt and "Do not overwrite your core domain focus" not in prompt)
check("the lead settles disagreements by evidence, never by who found it", synthesis.DISAGREEMENT_RULES in prompt)
check("the lead must account for every advisor pick it leaves out", '"not_adopted"' in prompt)
check("the lead review checks links against its agents' and earlier cycles' evidence",
      "extra_evidence=[approved_blueprints]" in inspect.getsource(mac.run_lead_review))


# ---- 5. the blueprint ----------------------------------------------------------------------------
lead_out = [{"agent_id": "lead", "findings": {"countries": ["Nigeria", "Ireland"]},
             "sources": ["web_search: gdp"], "evidence_links": [REAL]}]
agent_results = [
    {"agent_id": "lead", "findings": {"countries": ["Nigeria", "Ireland"]}},
    {"agent_id": "adv_2", "findings": {"countries": ["Pakistan", "Nigeria"]},
     "sources": ["web_search: Pakistan AI policy"], "evidence_links": ["https://bookkeeping.example/only"],
     "general_memory": {"countries_considered_and_rejected": [{"country": "Singapore", "reason": "mature AI ecosystem"}]}},
]
circular = {"description": "Nominal GDP 2023 for Ireland",
            "resolution_note": "The figure from the lead is used as it aligns with the lead specialist's chosen output, "
                               "indicating it is better supported in the context of this report's internal consistency."}
redefined = {"description": "Is Ireland just starting with AI",
             "resolution_note": "The criteria of emerging adoption was applied by the lead specialist, so Ireland's "
                                "inclusion is better supported by this specific definition."}
evidenced = {"description": "Pakistan's policy date", "resolution": "adv_2",
             "decided_by": f"Dawn's report of the cabinet approval ({REAL})"}
bare = {"description": "New Zealand business adoption", "resolution": "lead"}
# How Malaysia got into pipeline 9's five: the agent said it had checked, and that counted.
self_vouching = {"description": "Malaysia counts as English-speaking", "resolution": "adv_2",
                 "decided_by": "The AI Adoption Indicator Specialist's explicit mention that it resolved "
                               "the GDP and English official language data inconsistencies"}
by_search = {"description": "Pakistan's AI policy year", "resolution": "adv_2",
             "decided_by": "the approval reported for 2025",
             "decided_by_source": "web_search: Pakistan AI policy"}
blueprint = {"summary": "s", "disagreements": [circular, redefined, evidenced, bare, self_vouching, by_search],
             "sources": [REAL, FAKE], "note": f"see {FAKE}"}
models = use_models(json.dumps({"has_conflicts": False, "conflicts": []}), json.dumps(blueprint))
result = asyncio.run(synthesis.run_synthesis_agent(lead_out, agent_results=agent_results))
check("the conflict check sees what each agent found before the lead's review",
      "LEAD SPECIALIST'S REVIEW OF WHAT EACH AGENT FOUND" in models.contents[0] and "Pakistan" in models.contents[0])
check("so a pick the review dropped without a reason is a conflict", "dropped, overrode or contradicted" in models.contents[0])
check("everything each agent reported goes in, general_memory included",
      "countries_considered_and_rejected" in models.contents[0] and "mature AI ecosystem" in models.contents[0])
check("but not its bookkeeping", "bookkeeping.example" not in models.contents[0])
blocking, reconcilable = synthesis.split_conflicts([
    {"description": "review dropped Pakistan", "kind": "review", "needs_new_research": True},
    {"description": "fonts", "kind": "contradiction", "needs_new_research": True},
])
check("a conflict about what the review did goes to the gate, never back to research",
      [c["description"] for c in reconcilable] == ["review dropped Pakistan"]
      and [c["description"] for c in blocking] == ["fonts"]
      and '"kind": "review"' in models.contents[0])
check("the blueprint step sees the originals and the evidence rules",
      "WHAT EACH AGENT FOUND BEFORE THE LEAD'S REVIEW" in models.contents[1]
      and synthesis.DISAGREEMENT_RULES in models.contents[1] and '"not_adopted"' in models.contents[1])
bp = result["blueprint"]["disagreements"]
check("a disagreement settled by who said it is marked unresolved",
      bp[0]["resolution"] == "unresolved" and "who said it" in bp[0]["flag"])
check("so is one settled by redefining the brief's words for the lead's pick", bp[1]["resolution"] == "unresolved")
check("one settled by a link the tools returned stands", bp[2]["resolution"] == "adv_2" and "flag" not in bp[2])
check("one settled with no evidence given is unresolved", bp[3]["resolution"] == "unresolved" and "No evidence" in bp[3]["flag"])
check("an agent vouching for its own diligence does not settle anything",
      bp[4]["resolution"] == "unresolved" and "its own work" in bp[4]["flag"])
check("one that names the search it came from stands", bp[5]["resolution"] == "adv_2" and "flag" not in bp[5])
check("the gate is told how many are unresolved", result["unresolved_disagreements"] == 4)
check("agents are told to name the source, and that their own word is not evidence",
      "decided_by_source" in synthesis.DISAGREEMENT_SHAPE
      and "vouching for itself" in synthesis.DISAGREEMENT_RULES)
check("the blueprint keeps real links and loses made-up ones",
      result["blueprint"]["sources"] == [REAL] and FAKE not in json.dumps(result["blueprint"])
      and result["blueprint"]["invented_links_removed"] == 1)
check("without the originals the prompts still work", "LEAD SPECIALIST'S REVIEW" not in synthesis._detect_conflicts_prompt("{}"))


# ---- 6. research reused after a restart, the findings file and the pipeline -------------------------
shutil.rmtree(PROJECT_DIR, ignore_errors=True)
try:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    before_the_fix = {"status": "ok", "confidence": 0.95, "findings": {"countries": ["Nigeria"]},
                      "sources": ["web_search: countries by GDP"], "unverified_sources": [FAKE, "IMF estimates"]}
    mac.save_research_findings_file("99", "lead_cycle1_lead", before_the_fix, PROJECT, brief="Pick countries.")
    reused = mac.load_saved_cycle_research("99", {"cycle_id": 1, "advisory_agents": [],
                                                  "lead_specialist": {"agent_id": "lead_cycle1_lead", "brief": "Pick countries."}},
                                           PROJECT)["lead_cycle1_lead"]
    check("research saved before links were checked loses its made-up links when a restart reuses it",
          FAKE not in json.dumps(reused) and reused["unverified_sources"] == ["IMF estimates"]
          and reused["invented_links_removed"] == 1 and reused["confidence"] == 0.6)

    mac.save_research_findings_file("99", "adv_cycle1_adv_1", {"status": "ok", "invented_links_removed": 3, "sources": []}, PROJECT)
    text = open(os.path.join(AGENTS_DIR, "research_adv_cycle1_adv_1_99.md"), encoding="utf-8").read()
    check("the findings file says how many links were taken out", "## Links removed" in text and "3 link(s)" in text)
finally:
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)

source = inspect.getsource(mac.run_full_pipeline)
check("the blueprint step is given every agent's own findings", 'agent_results=research_output.get("agent_results", [])' in source)
check("the gate carries how many disagreements are unresolved", source.count('"unresolved_disagreements": unresolved') == 2)

print("\nAll evidence rules checks passed.")
