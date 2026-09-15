# Jarvis — Problems to Work On

Found while reviewing pipeline 6 ("Competitor Website Blueprint", 2026-09-13).
Each problem lists why it matters, where it lives in the code, and what "done" looks like.

---

## 1. No button to show the detailed plans from agents

**Status:** Done (2026-09-14) — "Detailed plan" button under each agent's brief in the review
stage (`plan.html`), backed by `GET /api/plans/<plan_id>/agents/<agent_id>` in `jarvis.py`.
Shows the cycle and its goal, the full brief, which tools really work and which the agent
asked for but won't get, its memory query and what it must deliver.

**Problem**
Every research agent writes a detailed plan/output to disk, but the UI gives no way to open it.
To see what an agent actually planned you have to dig through the project folder by hand.

**Why it matters**
Gates ask for approval of a cycle without letting you read what the agents intend to do.
In pipeline 6 cycle 1 was approved even though its competitor list targeted the wrong kind
of company (SaaS products like Salesforce/Canva instead of development agencies) — that
would have been obvious from the plan.

**Where the data already is**
- `Let Jarvis Handle It/<project>/Implementation plan/Agents/agent_plan_<id>.md`
- `Let Jarvis Handle It/<project>/Implementation plan/Agents/research_<agent>_<id>.md`
- `Let Jarvis Handle It/<project>/Implementation plan/Agents/cycle_blueprint_<n>_<id>.md`
- `Let Jarvis Handle It/<project>/Implementation plan/Final Plans/master_blueprint_<id>.md`
- Pages to add the button to: `plan.html` (gate modal, plan detail view), `execution.html` (agent side panel)

**Done when**
- A "View detailed plan" button exists on the plan page and in each gate modal.
- Clicking it shows the agent plan / per-agent plans, readable (rendered markdown, not raw JSON).
- It works while the pipeline is still running, not only after it finishes.

---

## 2. No button to show what the agents have found so far

**Status:** Done (2026-09-14) — "Findings so far" button next to it, same endpoint. Shows
status and confidence, findings, recommendation, sources, every tool call with failures
marked, and what blocked an agent. Refreshes every 4 seconds while open; falls back to the
saved findings file after a restart. An execution agent that hasn't run shows the approved
research it will work from.

**Problem**
There is no single place to see the findings gathered so far. Findings are spread across
per-agent files, memory files and cycle blueprints, and the execution page only shows
small "finding" dots on the constellation.

**Why it matters**
You can't judge progress or quality mid-run. In pipeline 6 only 3 companies received a
real UI/UX review and 3 agents failed ("Exceeded max tool-call turns") — none of that was
visible without opening files.

**Where the data already is**
- `Let Jarvis Handle It/<project>/Implementation plan/Agents/research_*.md` (per-agent findings)
- `Let Jarvis Handle It/<project>/Implementation plan/Agents/cycle_blueprint_*.md` (synthesised per cycle)
- `Let Jarvis Handle It/<project>/memory/general/*.json`, `memory/high_value/*.json`
- `Let Jarvis Handle It/<project>/Deliverables/`
- Live: `findings_discovered` events and `AGENT_REGISTRY[agent_id]["output"]` in `jarvis.py`

**Done when**
- A "Findings so far" button exists on the plan and execution pages.
- It shows findings grouped by cycle and agent, updating live as agents complete.
- Failed / partial agents and their reason (e.g. `blocked_reason`, tool-turn limit) are shown too,
  so gaps are as visible as results.

---

## 3. Jarvis cannot open a web page (give it a real website-inspection tool)

**Status:** Done (2026-09-14) — `inspect_website`, an always-on local tool in
`agents/website_inspector.py`. It opens the page in headless Chromium (Playwright 1.62.0,
pinned in `requirements.txt`), reads fonts, type scale, colours (as hex), CTAs, navigation,
headings, images, effects and motion from the live CSS, screenshots it at 375/768/1440 px
into `Let Jarvis Handle It/<project>/Website Inspections/`, and has Gemini analyse the
screenshots. No download of its own browser: it falls back to an existing Playwright
Chromium, then Edge, then Chrome. Only public http(s) sites can be inspected (redirects
included). `web_scraper`, "Visual Analysis Tool", "UX/UI Analysis Platforms" and
cross-browser testing names now resolve to it instead of web search. Tests:
`scripts/test_website_inspection.py`.

**Problem**
No agent can actually open a URL. The only real connectors are web search, arXiv, Google Docs
and local files (`REGISTRY_TOOLS` in `agents/tool_executor.py`). Worse, `web_scraper` is marked
`"up"` in `api_registry.json` but has no real handler — the token rules in
`_resolve_tool_key` silently turn `web_scraper` into `google_search`, so agents think they have
a scraper and get search summaries instead.

**Why it matters**
Any brief about websites (fonts, colours, imagery, whitespace, CTAs, animations, responsiveness,
emotional journey) cannot be answered from search results. In pipeline 6 the cycle 3 blueprint
itself says "Web search tools cannot interpret visual elements", and the pipeline dropped two
tool suggestions (Advanced Web Scraping, Visual Analysis Tool) that were pointing at this exact need.

**The fix — one free tool that covers both dropped suggestions**
1. Open a URL (headless browser, e.g. Playwright).
2. Read its HTML and CSS — computed fonts, font pairings, colour palette, layout structure, CTAs.
3. Take screenshots at 3 widths (mobile ~375px, tablet ~768px, desktop ~1440px).
4. Send the screenshots to Gemini (already multimodal, already used by Jarvis) for visual analysis:
   imagery style, filters, whitespace, visual hierarchy, emotional tone.

This replaces paid tools such as BrowserStack/LambdaTest for responsiveness checks, and needs no API key.

**Where it goes**
- Real handler + declaration in `REGISTRY_TOOLS` (or `ALWAYS_ON_TOOLS`, since it needs no credentials) in `agents/tool_executor.py`
- Remove or re-point the fake `web_scraper` entry in `api_registry.json` so it no longer shows `"up"` without a handler
- Update `_TOKEN_RULES` so scraper/browse names resolve to the new tool, not `google_search`

**Done when**
- An agent asked to analyse a website returns real fonts, colours and screenshot-based observations for that site.
- Nothing shows as connected unless a real handler exists behind it.
