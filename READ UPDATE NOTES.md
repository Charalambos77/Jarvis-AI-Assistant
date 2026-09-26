# Read update notes: everything new in Jarvis

This branch, `claude/jarvis-test-all`, puts every new Jarvis feature together in one place so you can try them all on your own PC before anything goes into `main`. Nothing has been merged into `main`, and no pull requests were opened for this branch.

## 1. Get the branch on your PC

Open a terminal (PowerShell or Command Prompt) and run:

```
cd "C:\Users\Charalambos Michael\Desktop\Work\Applications (Github Repositary for Antigravity)\Jarvis-AI-Assistant"
git status
git fetch origin
git checkout claude/jarvis-test-all
```

If `git status` shows files you changed, commit or stash them first (`git stash`), or the checkout will refuse to run.

Then update the Python packages, because a few were added:

```
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pip install "mcp<2"
venv\Scripts\python -m playwright install chromium
```

`mcp<2` matters: version 2 of the `mcp` package removed something Jarvis's MCP test server uses. Start Jarvis the way you normally do.

To go back to your old version at any time: `git checkout main`.

## 2. What is in this branch

| # | Feature | Where to find it in the app |
|---|---|---|
| 3 | Control room: tool discovery, reviews, risky-call approvals, spending limits, APIs as tools | **Control room** button in the top nav (the old APIs/MCPs button opens it too) |
| 4 | Pause, resume and stop a running pipeline | Control room, "Running pipelines" at the top |
| 5 | Jarvis understands every task and researches it first | Plan page, Human Gate card |
| 6 | Suggestions: other ways to do each part of a plan | **Suggestions** button in the top nav |
| 7 | Sections plan a whole project | Plan page, a finished pipeline's "make a section" button, then the section's dashboard |
| 8 | Start a section from a folder | **IDE** page, "◆ Make a section from this folder" |
| 9 | The in-app IDE (Workbench) | **IDE** button in the top nav |
| 10 | The coding agent can run on the Claude CLI | `.env` settings |

The branch also carries earlier work that was already waiting on other branches and is not in `main` yet: the pipeline details and clarification window, the pipeline review fixes, the website inspector, the Antigravity CLI connection, your own "Logic Changes" and "Finished sections" commits, and your 6 September install script update from `main`.

## 3. Control room and automatic tool discovery

**What changed**
- Everything about connected APIs and MCP servers now lives in the **Control room**. The old APIs/MCPs buttons open it.
- When you switch on an MCP server, Jarvis lists its tools and gives each one a risk label: read, write, destructive or costs money.
- When you connect an API, Jarvis looks for its published description (OpenAPI, Swagger or a Google Discovery document) and turns each operation into a real tool. If it can't find one, the Control room says "no description found" and lets you paste the link.
- A **Tool Researcher** reads the service's documentation and writes a card for each tool (what it does, when to use it, example arguments, limits). Agents see that card.
- Read and write tools wait for your review in the Control room. If you don't answer within 10 minutes, they approve themselves.
- Tools that cost money or are destructive never approve themselves. Every call to one is held in the Control room until you press **Allow once** or **Deny**, however long that takes. You can also set a tool to **always ask**, **always allow** or **blocked**.
- **Spending limits** per service and per pipeline. A money call over the limit is held even if the tool is on "always allow".
- When Jarvis starts, he checks every connected service again. Only new or changed tools go up for review.
- After the Plugging Gate, a pipeline waits while a service it needs still has tools being researched or reviewed, and says so in its log.

**How to test**
1. Open the **Control room**. You should see sections for waiting calls, new risky tools, tools to review, spending limits, rules and connected services.
2. Under connected services, switch on an MCP server. After Jarvis has researched them, its tools appear under "new tools to review", with risk badges. Approve all, approve read only, or tick some.
3. If the server has a delete or pay tool, it appears under new risky tools. Leave it: it should still be waiting after 10 minutes.
4. Connect an API (for example YouTube). Jarvis finds its description and it gets tools. For a service with no description, paste its spec link and check the tools appear.
5. Set a spending limit on a service, then run a pipeline that would use a paid tool. The call should wait in "waiting calls". Try **Deny** and check the agent says the action did not happen.
6. Close and reopen Jarvis. Your decisions should still be there.

## 4. Pause, resume and stop a pipeline

**What changed**
- The Control room has a new **Running pipelines** list at the top. Each running pipeline shows what it is working on, with **Pause**, **Resume** and **Stop**.
- **Pause** holds the run after the model call it is making. **Resume** carries on from the same point.
- **Stop** ends the run within about a second and keeps its saved progress. Stopped pipelines stay listed, and **Resume** (or the CONTINUE badge on the Plan page) starts them again from their saved progress.
- Stop also ends right away any wait the run is in: an approval gate, a held money or destructive call (which is denied, not run), an agent's question, a tool request or a tool review.
- By voice or chat: "pause pipeline 12" and "stop pipeline 12".

**How to test**
1. Start any pipeline and open the Control room. Press **Pause** while research runs. The card turns "paused" and says where it waits. Press **Resume**.
2. Press **Stop** during research. It turns "stopped" within a second or two. Press **Resume** and the run starts again.
3. Let a run reach an approval gate, then press **Stop**. It stops at once and the gate closes.

## 5. Jarvis understands every task and researches it first

**What changed**
- Before planning, Jarvis writes down what he understands the task to be: the goal, deliverables, audience, constraints, success criteria, open questions, assumptions, services involved, and how deep the research should go (focused 2-3 cycles, standard 3-5, deep 6+). The saved agent plan file shows it under "Task Understanding".
- Every task is researched, with never fewer than 2 cycles.
- If a task uses an API or MCP that no research cycle covers, a cycle on how that service and its tools work is added.
- The execution agents, the quality checker and every re-plan see the goal, constraints and success criteria.
- For a task Jarvis judges simple, the Plan page asks **Research first** or **Skip research** before any research starts. The answer is saved, so a restart doesn't ask again.

**How to test**
1. Start a small task, for example "Turn these notes into 5 bullets: a, b, c, d, e". On the Plan page, the Human Gate card asks the question. Click **Skip research**: no research runs, and the next gate is the execution plan.
2. Repeat and click **Research first**: research runs as usual.
3. Start a bigger task, such as competitor research. There is no question, research runs, and `agent_plan_<id>.md` has a Task Understanding section.
4. Start a simple-looking task that names a service, for example "Upload this video to YouTube". There is no question, and the plan has a cycle called "How the services work".

## 6. Suggestions page

**What changed**
- A new **Suggestions** button in the nav on every page. It shows a count when a plan is waiting for you with ideas to look at.
- After research, Jarvis splits the execution plan into named parts. For each part, and for the whole plan, he lists the best way, the cheapest and the one with the best result (cost, time, quality, pros, cons, services needed). If you said how to do something, your way stays the plan and the others are there to compare.
- You can swap an idea in for one part or for the whole plan, undo, go back to Jarvis's plan, or ask for new ideas. Swaps are allowed until you approve the execution plan. After that the plan is locked.
- The Plan page's approval checklist links to Suggestions and shows the swapped-in agents straight away.

**How to test**
1. Start a pipeline and approve the research cycles.
2. When the execution plan waits for approval, open **Suggestions** (or "Open Suggestions" in the approval checklist).
3. Swap in an idea for one part, then check the approval checklist shows the new agents.
4. Try **Undo**, "Back to Jarvis's plan" and "Use for the whole plan".
5. Approve. The agents that run are the ones you picked, and the Suggestions page says the plan is final.
6. Optional: close Jarvis while the approval is waiting, reopen, resume the pipeline, and check your swaps are still there.

## 7. Sections plan a whole project

**What changed**
- A section is now a whole project built around your first pipeline. Jarvis researches what the project needs, decides whether the finished pipeline is its beginning or one part of it, and plans every part with as many agents as it needs.
- Every ask you made is listed under "Everything you asked for", and each one must be covered by a part and an agent.
- Jarvis also works out what the project needs that you did not mention ("What it needs that you did not mention"), runs deeper searches where research was thin, and adds parts for those. Each one has a tick box: untick it and it is left out, along with any agents that were there only for it.
- The section dashboard has a **The plan** card: every part in order, with its agents, and a **Start** button on each part that can start now. **Research and re-plan** redoes the plan and keeps what you left out, out.

**How to test**
1. On the Plan page, find a finished pipeline (for example your competitor research) and press its make-a-section button.
2. Answer Jarvis's questions and let him research. The window lists everything you asked for and what he found you need. Untick one requirement.
3. Open the section's dashboard. **The plan** card shows each part. Check the unticked requirement is listed as not added, and nothing else is missing.
4. Press **Start** on a part. A pipeline starts inside the section.
5. Press **Research and re-plan** and check your unticked requirement stays out.

## 8. Start a section from a folder

**What changed**
- A section can now start from a folder on your PC instead of a finished pipeline. Jarvis reads the folder (he never writes to it) and plans the section from what is there.
- This is what the IDE's "make a section from a folder" button uses.

- The section's own files go in a new folder beside the one you picked, such as "Hello App (2)".

**How to test**
1. Open the **IDE**, pick a project, and click "◆ Make a section from this folder" at the top of the file list (or the ◆ next to any subfolder).
2. The "Make this a section" window opens and says Jarvis is reading the folder. Go through it like section 7, up to **Create section**.
3. You land in the new section. Check the folder you picked is unchanged.

## 9. The in-app IDE (Workbench)

**What changed**
- **New IDE page.** It has an **IDE** button in the nav on every page (`ide.html`, backend `ide.py`). It opens any project folder under `Let Jarvis Handle It/` and has three panes:
  - **Files** on the left. You can add a file or folder, and rename or delete anything.
  - **Code editor** in the middle, with tabs. `Ctrl+S` saves. If Jarvis or a CLI changed the file on disk since you opened it, saving asks you first instead of overwriting.
  - **Terminal** underneath, toggled with `Ctrl+backtick`. It runs in the project folder. It still refuses the hard-denylist commands from the Commands page.
- **Missions** (the right-hand panel). Tell Jarvis what to build, fix or explain.
  - **Review** mode: he shows what he understood and his plan first. You approve it, optionally with notes, and then accept or reject each change as a side-by-side diff.
  - **Autopilot**: he applies his changes, and each one can be reverted.
  - **Commands**: the ones he suggests are never run by him. Each has a "Run in terminal" button.
  - **Other controls**: reply on the same mission to keep going, and use Stop while he's working. The **Missions** switch at the top makes this panel fill the whole screen.
- **Who does the work** (the dropdown in the Missions panel):
  - **Jarvis:** his own model, either a Gemini model or a local Ollama model, so the IDE works with no CLI connected.
  - **Antigravity CLI:** its edits arrive already applied, still with diffs and Revert.
  - **Claude:** the coding agent from section 10. It needs the Claude CLI or the SDK set up, and shows "not set up" otherwise.
- **Make a section from a folder.** The "◆ Make a section from this folder" button sits at the top of the file list, and every subfolder has a ◆ that does the same. Both open the same window as making a section from a pipeline: brief, questions, the written brief, research and the whole-project plan, the requirements to tick, then Create section.
  - The picked folder is only read. The section's own files go in a new folder beside it, such as "Hello App (2)", and that folder also shows up in the IDE's project list.
  - A folder that's already a section shows its name at the top instead, and clicking it opens the section.
- **Safety:** the IDE only answers requests from this PC. Jarvis's server otherwise listens on your whole network. Set `JARVIS_IDE_ALLOW_REMOTE=1` if you really want other machines to reach it.
- **Offline:** the code editor loads from the internet (cdnjs). Without it, you get a plain text box and everything else still works.

**How to test**
1. Offline checks: `python scripts/test_ide.py` should give 61 passed. It spends no tokens.
2. Start Jarvis and click **IDE** in the nav. Pick a project from the dropdown at the top, or make one with ＋.
3. Open a file, type something and press `Ctrl+S`. The dot on the tab goes away, and the file on disk changes.
4. Pick the Jarvis engine and a model at the top of the Missions panel. Leave the mode on **Review**, and ask something like "add a function that adds two numbers and a test for it". Then:
   - You'll see what he understood and his plan. Click **Approve plan**.
   - Click a changed file to see the diff, then **Accept** it.
   - Click **Run in terminal** on the command he suggests.
   - Try **Revert** on one change.
5. Switch to **Autopilot** and ask for another small change. It's applied straight away, and each change has **Revert**.
6. Ask a question with no edits, such as "what does this file do?". He answers and changes nothing.
7. In the terminal, run something long (`ping -t localhost` on Windows) and press **stop**.
8. Click **◆ Make a section from this folder** and go through the window all the way to **Create section**. You land in the new section. Back in the IDE, the top of the file list now shows the section's name.
9. If Antigravity (`agy`) is installed, choose "Antigravity CLI" as the engine and give it a small task. Its changes show as applied, with diffs.

## 10. The coding agent can run on the Claude CLI

**What changed**
- Execution agents can hand a software task to a real coding agent (`code_project`), which writes files, runs them, reads the errors and fixes them until they work.
- It can run through the Claude Agent SDK with an `ANTHROPIC_API_KEY`, or through the `claude` command using whatever account the CLI is logged in with, with no API key.
- New `.env` settings:
  - `JARVIS_CODE_AGENT_BACKEND=auto|sdk|cli`. The default `auto` uses the SDK when it is set up and the CLI otherwise.
  - `JARVIS_CLAUDE_CLI=<name or full path>`, for when `claude` isn't on your PATH.
- Other agents keep working while a coding task runs.
- When neither backend is available, the coding tool isn't offered, the agent is told why, and it writes files with `write_file` instead.

**How to test**
1. Install Claude Code and run `claude` once to log in. Check `claude --version` works in the terminal you start Jarvis from.
2. Offline checks, which spend no tokens: `python scripts/test_code_agent_cli.py` and `python scripts/test_code_agent_pipeline.py`.
3. A real build through the CLI, which uses your Claude account: add `--live` to either of those.
4. In the app, set `JARVIS_CODE_AGENT_BACKEND=cli` in `.env`, restart Jarvis and give him a task that needs working software (for example "build a small Python script that ..."). The task log should say "Handing a coding task to the Claude coding agent (cli)", then list the files it built.
5. To check the fallback, set `JARVIS_CLAUDE_CLI=C:\nowhere\claude.exe` without the SDK installed. The agent should say the coding agent is not enabled and use `write_file`.

## 11. Run the automatic checks

From the Jarvis folder:

In **Command Prompt** (not PowerShell):

```
for %f in (scripts\test_*.py) do venv\Scripts\python %f
```

Each script ends with "All ... checks passed" when it passes.

Before pushing, all 35 test scripts passed on Linux with Python 3.12 (the desktop-only modules stood in, `GEMINI_API_KEY=fake`, `mcp<2`, Playwright 1.62). Every page (Control room, Suggestions, IDE, Plan, Library, Section) also loaded in the merged app.

Fixes made while merging, so you know they're there:
- A coding agent's built folder shows up as a clickable result again.
- Two test scripts were adjusted to the coding agent's extra setting and prompt note.
- Pause/stop, the "research first?" question and Suggestions were joined in the same part of the pipeline code, keeping all three.

## 12. Known limits

- Everything was tested on Linux with Python 3.12, with the model's answers scripted. Nobody has run it with a real Gemini key or on Windows yet, so the model may word things differently from what these notes describe, and Windows-only paths are the most likely place for a surprise.
- The desktop-only parts (voice, microphone, the PyWebView window) were stood in for during the tests, so they were not exercised.
- Held money and destructive calls are kept in memory. If you close Jarvis while one is waiting, the agent that made it has stopped anyway, and the call is gone.
- Semantic Scholar and Supadata have no known description address yet, so they rely on a web search or a link you paste in the Control room.
- The Suggestions page and a section's plan parts are separate for now: swapping an idea on the Suggestions page doesn't change a section's plan.
- If something breaks, tell Claude in the project what you did and what you saw, and `git checkout main` takes you back.
