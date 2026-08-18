# Live Agent Spider Web — Real-Time Pipeline Visualization

Replace the hardcoded department constellation in `execution.html`'s Active View with a **live, dynamically growing spider web** that visualizes the actual agents spawned by the pipeline in real time. 3D models represent each node type, a cinematic branch-following camera system handles navigation, and the web grows progressively as pipeline events fire.

---

## Resolved Decisions

> [!NOTE]
> **3D Model Assets — Procedural (for now)**: Using procedural Three.js geometry (simplified low-poly SVG icons) to keep the aesthetic consistent and avoid external dependencies. This is designed to be **swappable to GLTF/GLB model files later** without changing the rest of the architecture — each model factory function can be replaced individually.

---

## Proposed Changes

This is broken into **10 sequential steps**. Each step is self-contained and testable before moving to the next.

---

### Step 1: Pipeline Persistence Across All Pages

**Goal**: Store the currently viewed pipeline ID so that navigating to *any* page and back remembers which pipeline you were viewing.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html)

**What changes:**
- On pipeline status poll, store the active pipeline ID in `localStorage`
- On page load, read it back to restore context

```diff
 // Inside pollPipelineStatus()
 const data = await res.json();
 const shouldBeRunning = !!data.active;
+
+// Persist pipeline ID across all page navigations
+if (data.active_ids && data.active_ids.length > 0) {
+    localStorage.setItem('jarvis_active_pipeline_id', data.active_ids[0]);
+}
```

```diff
 // At the top of the <script> block, after variable declarations
+const STORED_PIPELINE_ID = localStorage.getItem('jarvis_active_pipeline_id');
```

#### [MODIFY] [plan.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/plan.html)

**What changes:**
- When user switches pipelines in plan.html, update `localStorage.jarvis_active_pipeline_id`

```diff
 // Inside pipeline switch handler
 function selectPipeline(planId) {
+    localStorage.setItem('jarvis_active_pipeline_id', planId);
     // ... existing switch logic
 }
```

#### [MODIFY] [agent_talk.task_log.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/agent_talk.task_log.html)

**What changes:**
- Read `localStorage.jarvis_active_pipeline_id` on load to filter logs to the correct pipeline

---

### Step 2: New Backend Endpoint — Live Agent Graph Data

**Goal**: Create a dedicated API endpoint that returns the full live graph structure (cycles, agents, their states, findings, memories) for a specific pipeline, so the frontend can build the spider web from real data.

#### [MODIFY] [jarvis.py](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/jarvis.py)

**What changes:**
- Add a new `/api/agent_graph/<plan_id>` endpoint that returns the full graph structure

```python
@app.route("/api/agent_graph/<plan_id>", methods=["GET"])
def get_agent_graph(plan_id):
    """Returns the full live spider-web graph for a pipeline run.
    Structure: { plan_id, phase, cycles: [{ cycle_id, domain, agents: [
        { agent_id, role, status, findings, memories_recalled, plan_contributions }
    ]}], execution_agents: [...] }
    """
    with PLAN_STORE_LOCK:
        plan = None
        for p in PLAN_STORE:
            if p["id"] == plan_id:
                plan = p
                break
        if not plan:
            return jsonify({"error": "Plan not found"}), 404

    agent_plan = plan.get("agent_plan", {})
    cycles = agent_plan.get("cycles", [])
    
    graph_cycles = []
    for cycle in cycles:
        cycle_agents = []
        all_agent_configs = [cycle.get("lead_specialist", {})] + cycle.get("advisory_agents", [])
        
        for agent_cfg in all_agent_configs:
            agent_id = agent_cfg.get("agent_id", "")
            # Get live status from AGENT_REGISTRY
            with AGENT_OBS_LOCK:
                registry_entry = AGENT_REGISTRY.get(agent_id, {})
            
            cycle_agents.append({
                "agent_id": agent_id,
                "role": agent_cfg.get("role", ""),
                "brief": agent_cfg.get("brief", ""),
                "tools_needed": agent_cfg.get("tools_needed", []),
                "memory_query": agent_cfg.get("memory_query", ""),
                "is_lead": agent_cfg == cycle.get("lead_specialist"),
                "status": registry_entry.get("status", "pending"),
                "streamed_thoughts": registry_entry.get("streamed_thoughts", ""),
                "output": registry_entry.get("output"),
                "config": registry_entry.get("config"),
            })
        
        graph_cycles.append({
            "cycle_id": cycle.get("cycle_id"),
            "domain": cycle.get("domain", ""),
            "goal": cycle.get("goal", ""),
            "agents": cycle_agents,
        })

    # Execution agents
    exec_agents_cfg = agent_plan.get("execution_agents", [])
    exec_agents = []
    for cfg in exec_agents_cfg:
        agent_id = cfg.get("agent_id", "")
        with AGENT_OBS_LOCK:
            reg = AGENT_REGISTRY.get(agent_id, {})
        exec_agents.append({
            "agent_id": agent_id,
            "role": cfg.get("role", ""),
            "brief": cfg.get("brief", ""),
            "status": reg.get("status", "pending"),
            "output": reg.get("output"),
        })

    return jsonify({
        "plan_id": plan_id,
        "phase": plan.get("phase", "planning"),
        "task": plan.get("task", ""),
        "cycles": graph_cycles,
        "execution_agents": exec_agents,
    })
```

#### [MODIFY] [multi_agent_coordinator.py](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/multi_agent_coordinator.py)

**What changes:**
- Add new event types for memory recall and findings so the frontend can create nodes in real-time:

```diff
 # Inside run_research_phase_for_cycle, after memory patterns are fetched
 if patterns:
     memory_context = "\n".join(...)
+    if event_logger:
+        event_logger({
+            "event_type": "memory_recalled",
+            "agent_id": agent_id,
+            "data": {"patterns": patterns, "query": memory_query}
+        })
```

```diff
 # Inside run_research_phase_for_cycle, when agent completes
 if event_logger:
     event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})
+    # Emit findings as separate event for spider web leaf nodes
+    if r.get("findings"):
+        event_logger({
+            "event_type": "findings_discovered",
+            "agent_id": agent_id,
+            "data": r["findings"]
+        })
```

---

### Step 3: Remove Hardcoded Departments & Build Dynamic Data Layer

**Goal**: Replace the static `departments` array with a polling system that fetches live agent graph data and builds the Cytoscape elements dynamically.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — Active View Section (~L1189-1708)

**What changes:**
- Delete the entire hardcoded `departments` array (L1193-1201)
- Replace `initActiveView()` with a new `initLiveAgentWeb()` function
- Add polling system that calls `/api/agent_graph/<plan_id>` and `/agents/events?since=<ts>`

```javascript
// REPLACE the entire initActiveView() IIFE with:
(function initLiveAgentWeb() {
    // ---- State ----
    let liveGraphData = null;        // Latest graph from /api/agent_graph
    let lastEventTimestamp = 0;       // For incremental event polling
    let renderedNodeIds = new Set();  // Track what's already in the scene
    let currentPlanId = localStorage.getItem('jarvis_active_pipeline_id');

    // ---- Cycle color palette (assigned dynamically) ----
    const CYCLE_COLORS = [
        '#22D3EE', '#60A5FA', '#34D399', '#F87171',
        '#FB7185', '#FCD34D', '#A78BFA', '#818CF8'
    ];

    // ---- Three.js scene setup (keep existing nebula core code) ----
    // ... (reuse the existing Three.js nebula/core setup verbatim)

    // ---- Polling loop ----
    async function pollAgentGraph() {
        if (!currentPlanId) {
            currentPlanId = localStorage.getItem('jarvis_active_pipeline_id');
            if (!currentPlanId) return;
        }
        try {
            const res = await fetch(`/api/agent_graph/${currentPlanId}`);
            if (!res.ok) return;
            const graph = await res.json();
            liveGraphData = graph;
            renderSpiderWeb(graph);
        } catch(e) {}
    }

    // Also poll events for real-time node additions
    async function pollEvents() {
        try {
            const res = await fetch(`/agents/events?since=${lastEventTimestamp}`);
            if (!res.ok) return;
            const data = await res.json();
            for (const evt of data.events) {
                lastEventTimestamp = Math.max(lastEventTimestamp, evt.timestamp || 0);
                handleLiveEvent(evt);
            }
        } catch(e) {}
    }

    setInterval(pollAgentGraph, 3000);
    setInterval(pollEvents, 1500);
    pollAgentGraph();

    // ---- Build Cytoscape elements from graph data ----
    function renderSpiderWeb(graph) {
        // Build elements array dynamically from graph.cycles
        // Each cycle becomes a "dept" node, each agent becomes an "agent" node
        // Findings/memories/plan contributions become leaf nodes
        // Cross-links form spider web pattern
        // ... (detailed in Step 5)
    }

    function handleLiveEvent(evt) {
        // spawned → add agent node with grow animation
        // memory_recalled → add brain-model nodes to agent
        // findings_discovered → add magnifying-glass nodes to agent
        // completed → mark agent node as done, add plan contribution paper nodes
        // ... (detailed in Step 6)
    }
})();
```

---

### Step 4: 3D Node Models (Procedural Three.js Geometry)

**Goal**: Create four distinct 3D models to replace the flat Cytoscape dots: mini human (agents), magnifying glass (findings), paper document (plan contributions), brain (memories).

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html)

**What changes:**
- Add a new `<script>` section with factory functions for each 3D model
- These are rendered as SVG data URIs for Cytoscape node backgrounds (since Cytoscape is 2D) with Three.js used only for the central nebula

```javascript
// ---- 3D-style SVG Icon Factories (rendered as node backgrounds) ----

function createAgentSVG(color) {
    // Mini human silhouette — head (circle) + body (trapezoid) + arms
    return `data:image/svg+xml;utf8,${encodeURIComponent(`
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
            <defs>
                <radialGradient id="glow" cx="50%" cy="50%" r="50%">
                    <stop offset="0%" stop-color="${color}" stop-opacity="0.4"/>
                    <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
                </radialGradient>
            </defs>
            <circle cx="24" cy="24" r="22" fill="url(#glow)"/>
            <!-- Head -->
            <circle cx="24" cy="14" r="6" fill="${color}" opacity="0.9"/>
            <!-- Body -->
            <path d="M16 24 C16 20 32 20 32 24 L30 38 L18 38 Z" fill="${color}" opacity="0.8"/>
            <!-- Arms -->
            <line x1="16" y1="24" x2="10" y2="32" stroke="${color}" stroke-width="2.5" stroke-linecap="round" opacity="0.7"/>
            <line x1="32" y1="24" x2="38" y2="32" stroke="${color}" stroke-width="2.5" stroke-linecap="round" opacity="0.7"/>
        </svg>
    `)}`;
}

function createMagnifyingGlassSVG(color) {
    // Findings — magnifying glass icon
    return `data:image/svg+xml;utf8,${encodeURIComponent(`
        <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">
            <defs>
                <radialGradient id="glow" cx="50%" cy="50%" r="50%">
                    <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
                    <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
                </radialGradient>
            </defs>
            <circle cx="18" cy="18" r="16" fill="url(#glow)"/>
            <circle cx="15" cy="15" r="8" fill="none" stroke="${color}" stroke-width="2.5" opacity="0.9"/>
            <line x1="21" y1="21" x2="30" y2="30" stroke="${color}" stroke-width="3" stroke-linecap="round" opacity="0.9"/>
            <circle cx="15" cy="15" r="4" fill="${color}" opacity="0.15"/>
        </svg>
    `)}`;
}

function createPaperSVG(color) {
    // Plan contributions — document/paper icon
    return `data:image/svg+xml;utf8,${encodeURIComponent(`
        <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">
            <defs>
                <radialGradient id="glow" cx="50%" cy="50%" r="50%">
                    <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
                    <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
                </radialGradient>
            </defs>
            <circle cx="18" cy="18" r="16" fill="url(#glow)"/>
            <path d="M10 4 L22 4 L28 10 L28 32 L10 32 Z" fill="${color}" opacity="0.15" stroke="${color}" stroke-width="1.5"/>
            <path d="M22 4 L22 10 L28 10" fill="none" stroke="${color}" stroke-width="1.5"/>
            <line x1="14" y1="16" x2="24" y2="16" stroke="${color}" stroke-width="1.5" opacity="0.6"/>
            <line x1="14" y1="20" x2="24" y2="20" stroke="${color}" stroke-width="1.5" opacity="0.6"/>
            <line x1="14" y1="24" x2="20" y2="24" stroke="${color}" stroke-width="1.5" opacity="0.6"/>
        </svg>
    `)}`;
}

function createBrainSVG(color) {
    // Memories — brain icon
    return `data:image/svg+xml;utf8,${encodeURIComponent(`
        <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">
            <defs>
                <radialGradient id="glow" cx="50%" cy="50%" r="50%">
                    <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
                    <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
                </radialGradient>
            </defs>
            <circle cx="18" cy="18" r="16" fill="url(#glow)"/>
            <path d="M18 6 C12 6 8 10 8 15 C8 17 9 19 10 20 C9 21 8 23 9 25 C10 28 13 30 16 30 L18 30"
                  fill="none" stroke="${color}" stroke-width="1.8" opacity="0.9"/>
            <path d="M18 6 C24 6 28 10 28 15 C28 17 27 19 26 20 C27 21 28 23 27 25 C26 28 23 30 20 30 L18 30"
                  fill="none" stroke="${color}" stroke-width="1.8" opacity="0.9"/>
            <line x1="18" y1="8" x2="18" y2="30" stroke="${color}" stroke-width="1" opacity="0.4"/>
            <path d="M12 14 Q15 16 18 14" fill="none" stroke="${color}" stroke-width="1" opacity="0.5"/>
            <path d="M18 14 Q21 16 24 14" fill="none" stroke="${color}" stroke-width="1" opacity="0.5"/>
            <path d="M11 20 Q14 22 18 20" fill="none" stroke="${color}" stroke-width="1" opacity="0.5"/>
            <path d="M18 20 Q22 22 25 20" fill="none" stroke="${color}" stroke-width="1" opacity="0.5"/>
        </svg>
    `)}`;
}
```

**Cytoscape node styles updated to use these icons:**

```javascript
// New Cytoscape style selectors for each node type
{ selector: 'node[type="agent"]', style: {
    'background-image': 'data(icon)',
    'background-fit': 'contain',
    'background-color': '#1C1E32',
    'width': 44, 'height': 44,
    'border-width': 2,
    'border-color': 'data(color)',
    'label': 'data(label)',
    'color': '#fff', 'font-size': '9px',
    'text-valign': 'bottom', 'text-margin-y': 8,
    'shadow-blur': 25, 'shadow-color': 'data(color)', 'shadow-opacity': 0.6
}},
{ selector: 'node[type="finding"]', style: {
    'background-image': 'data(icon)',
    'background-fit': 'contain',
    'background-color': 'transparent',
    'width': 28, 'height': 28,
    'border-width': 0,
    'label': 'data(label)',
    'color': '#9CA3AF', 'font-size': '8px',
    'text-valign': 'bottom', 'text-margin-y': 5,
}},
{ selector: 'node[type="memory"]', style: { /* brain icon, similar sizing */ }},
{ selector: 'node[type="plan_contribution"]', style: { /* paper icon */ }},
```

---

### Step 5: Multi-Branch Spider Web Layout with Cross-Links

**Goal**: Each cycle node fans out multiple branches (one per agent) and cross-links form the spider web pattern near the origin.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — inside `renderSpiderWeb()`

**What changes:**
- Build the layout with cycles around the center, agents fanning out from each cycle, and cross-links between adjacent agents

```javascript
function renderSpiderWeb(graph) {
    const elements = [];
    const positions = {};
    const CENTER_X = window.innerWidth / 2;
    const CENTER_Y = (window.innerHeight / 2) + 40;
    const CYCLE_RADIUS = 200;
    const AGENT_RADIUS_START = 280;
    const AGENT_SPACING = 70;

    // --- Cycle Nodes ---
    graph.cycles.forEach((cycle, ci) => {
        const angle = -Math.PI / 2 + (ci * (2 * Math.PI / graph.cycles.length));
        const color = CYCLE_COLORS[ci % CYCLE_COLORS.length];
        const cx = CENTER_X + CYCLE_RADIUS * Math.cos(angle);
        const cy = CENTER_Y + CYCLE_RADIUS * Math.sin(angle);

        // Anchor at nebula edge
        const anchorId = `anchor_${cycle.cycle_id}`;
        elements.push({ data: { id: anchorId, type: 'anchor' } });
        positions[anchorId] = {
            x: CENTER_X + 90 * Math.cos(angle),
            y: CENTER_Y + 90 * Math.sin(angle)
        };

        // Cycle node
        const cycleId = `cycle_${cycle.cycle_id}`;
        elements.push({ data: {
            id: cycleId, label: '', type: 'cycle',
            color, cycleId: cycle.cycle_id, domain: cycle.domain
        }});
        positions[cycleId] = { x: cx, y: cy };

        // Conduit from anchor to cycle
        elements.push({ data: { source: anchorId, target: cycleId, color } });

        // --- Agent branches fanning out ---
        const agents = cycle.agents || [];
        const fanSpread = Math.PI * 0.4; // Total fan angle
        const fanStart = angle - fanSpread / 2;
        const fanStep = agents.length > 1 ? fanSpread / (agents.length - 1) : 0;

        agents.forEach((agent, ai) => {
            const branchAngle = agents.length === 1 ? angle : fanStart + ai * fanStep;
            const agentDist = AGENT_RADIUS_START + ai * 15;
            const ax = CENTER_X + agentDist * Math.cos(branchAngle);
            const ay = CENTER_Y + agentDist * Math.sin(branchAngle);
            const agentId = agent.agent_id;

            elements.push({ data: {
                id: agentId, label: agent.role,
                type: 'agent', color, cycleId: cycle.cycle_id,
                icon: createAgentSVG(color), agentData: JSON.stringify(agent)
            }});
            positions[agentId] = { x: ax, y: ay };

            // Edge: cycle → agent
            elements.push({ data: {
                source: cycleId, target: agentId, color, cycleId: cycle.cycle_id
            }});

            // --- Cross-links between adjacent agents (spider web) ---
            if (ai > 0) {
                const prevAgent = agents[ai - 1];
                elements.push({ data: {
                    source: prevAgent.agent_id, target: agentId,
                    type: 'web_cross', color, cycleId: cycle.cycle_id
                }});
            }

            // --- Sub-nodes: findings, memories, plan contributions ---
            // These are added dynamically in handleLiveEvent() as events fire
            // But if agent already has output (page loaded mid-run), render now
            if (agent.output && agent.output.findings) {
                addFindingNodes(elements, positions, agent, color, ax, ay, branchAngle);
            }
        });

        // Close the fan: cross-link last agent to first (completes the web)
        if (agents.length > 2) {
            elements.push({ data: {
                source: agents[0].agent_id, target: agents[agents.length - 1].agent_id,
                type: 'web_cross', color, cycleId: cycle.cycle_id
            }});
        }
    });

    // Labels
    let labelsHTML = '';
    graph.cycles.forEach((cycle, ci) => {
        const angle = -Math.PI / 2 + (ci * (2 * Math.PI / graph.cycles.length));
        const color = CYCLE_COLORS[ci % CYCLE_COLORS.length];
        const lx = CENTER_X + 530 * Math.cos(angle);
        const ly = CENTER_Y + 530 * Math.sin(angle);
        labelsHTML += `<div class="department-label" id="label_cycle_${cycle.cycle_id}"
            style="left:${lx}px;top:${ly}px;color:${color}">${cycle.domain.toUpperCase()}</div>`;
    });
    document.getElementById('labels-container').innerHTML = labelsHTML;

    // Initialize or update Cytoscape
    if (!window.cyActive) {
        window.cyActive = cytoscape({
            container: document.getElementById('cy-active'),
            elements, style: [ /* ... styles from Step 4 ... */ ],
            layout: { name: 'preset', positions, fit: false },
            userZoomingEnabled: false, userPanningEnabled: false
        });
    } else {
        // Diff and add only new elements (incremental update)
        updateCytoscapeIncrementally(elements, positions);
    }
}
```

---

### Step 6: Real-Time Growth — Progressive Node Animation

**Goal**: As pipeline events fire (`spawned`, `memory_recalled`, `findings_discovered`, `completed`), new nodes animate into existence by growing from the core outward.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — inside `handleLiveEvent()`

```javascript
function handleLiveEvent(evt) {
    if (!window.cyActive || !liveGraphData) return;
    const agentId = evt.agent_id;

    switch (evt.event_type) {
        case 'spawned': {
            // Agent node should already exist from renderSpiderWeb
            // but animate it: start at cycle node position, grow to target
            const node = window.cyActive.getElementById(agentId);
            if (node && node.length) {
                // Find parent cycle node position
                const cycleId = node.data('cycleId');
                const cycleNode = window.cyActive.getElementById(`cycle_${cycleId}`);
                if (cycleNode && cycleNode.length) {
                    const targetPos = { ...node.position() };
                    node.position(cycleNode.position());
                    node.style('opacity', 0);
                    node.animate({
                        position: targetPos,
                        style: { opacity: 1 }
                    }, { duration: 800, easing: 'ease-out-cubic' });
                }
            }
            break;
        }

        case 'memory_recalled': {
            // Add brain-icon nodes extending from the agent
            const patterns = evt.data?.patterns || [];
            patterns.forEach((pattern, idx) => {
                const memId = `${agentId}_mem_${idx}`;
                if (renderedNodeIds.has(memId)) return;
                renderedNodeIds.add(memId);

                const agentNode = window.cyActive.getElementById(agentId);
                if (!agentNode || !agentNode.length) return;
                const agentPos = agentNode.position();
                const color = agentNode.data('color');

                const offsetAngle = (idx * 0.5) - (patterns.length * 0.25);
                const targetX = agentPos.x + Math.cos(offsetAngle) * 60;
                const targetY = agentPos.y + Math.sin(offsetAngle) * 60;

                window.cyActive.add([
                    { data: {
                        id: memId, label: 'RECALLED',
                        type: 'memory', color,
                        icon: createBrainSVG(color),
                        cycleId: agentNode.data('cycleId'),
                        memoryData: JSON.stringify(pattern)
                    }},
                    { data: { source: agentId, target: memId, color,
                        cycleId: agentNode.data('cycleId') }}
                ]);

                const memNode = window.cyActive.getElementById(memId);
                memNode.position(agentPos);
                memNode.style('opacity', 0);
                memNode.animate({
                    position: { x: targetX, y: targetY },
                    style: { opacity: 1 }
                }, { duration: 600, easing: 'ease-out-cubic' });
            });
            break;
        }

        case 'findings_discovered': {
            // Add magnifying-glass nodes extending from agent
            const findings = evt.data;
            if (!findings || typeof findings !== 'object') return;
            const findingKeys = Object.keys(findings);

            findingKeys.forEach((key, idx) => {
                const findId = `${agentId}_find_${idx}`;
                if (renderedNodeIds.has(findId)) return;
                renderedNodeIds.add(findId);

                const agentNode = window.cyActive.getElementById(agentId);
                if (!agentNode || !agentNode.length) return;
                const agentPos = agentNode.position();
                const color = agentNode.data('color');

                const offsetAngle = Math.PI + (idx * 0.5) - (findingKeys.length * 0.25);
                const targetX = agentPos.x + Math.cos(offsetAngle) * 55;
                const targetY = agentPos.y + Math.sin(offsetAngle) * 55;

                window.cyActive.add([
                    { data: {
                        id: findId, label: key.substring(0, 15),
                        type: 'finding', color,
                        icon: createMagnifyingGlassSVG(color),
                        cycleId: agentNode.data('cycleId'),
                        findingData: JSON.stringify(findings[key])
                    }},
                    { data: { source: agentId, target: findId, color,
                        cycleId: agentNode.data('cycleId') }}
                ]);

                const node = window.cyActive.getElementById(findId);
                node.position(agentPos);
                node.style('opacity', 0);
                node.animate({
                    position: { x: targetX, y: targetY },
                    style: { opacity: 1 }
                }, { duration: 600, easing: 'ease-out-cubic' });
            });
            break;
        }

        case 'completed': {
            // Mark agent as completed + add plan contribution (paper) node
            const agentNode = window.cyActive.getElementById(agentId);
            if (!agentNode || !agentNode.length) return;
            const color = agentNode.data('color');
            agentNode.data('status', 'completed');

            const planId = `${agentId}_plan`;
            if (!renderedNodeIds.has(planId)) {
                renderedNodeIds.add(planId);
                const agentPos = agentNode.position();
                const targetX = agentPos.x + 50;
                const targetY = agentPos.y - 40;

                window.cyActive.add([
                    { data: {
                        id: planId, label: 'Plan',
                        type: 'plan_contribution', color,
                        icon: createPaperSVG(color),
                        cycleId: agentNode.data('cycleId')
                    }},
                    { data: { source: agentId, target: planId, color,
                        cycleId: agentNode.data('cycleId') }}
                ]);

                const node = window.cyActive.getElementById(planId);
                node.position(agentPos);
                node.style('opacity', 0);
                node.animate({
                    position: { x: targetX, y: targetY },
                    style: { opacity: 1 }
                }, { duration: 600, easing: 'ease-out-cubic' });
            }
            break;
        }
    }
}
```

---

### Step 7: Continuous Light Pulses Along All Branches

**Goal**: Energy particles travel along every branch continuously while the pipeline is running. Not triggered by events — always active.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — inside the Three.js animation loop

**What changes:**
- Keep the existing pulse system from the Three.js conduits but make it dynamic: whenever new edges exist in Cytoscape, create matching Three.js pulse sprites along those edge paths

```javascript
// Inside animateActiveGlobe(), after existing nebula animation:

// Dynamic pulse system - creates pulses for every edge in Cytoscape
if (window.cyActive) {
    const edges = window.cyActive.edges();
    // Ensure we have at least 2 pulses per edge
    while (pulses.length < edges.length * 2) {
        edges.forEach(edge => {
            const srcPos = edge.source().position();
            const tgtPos = edge.target().position();
            const color = edge.data('color') || '#ffffff';

            const spriteMat = new THREE.SpriteMaterial({
                map: texture, color: new THREE.Color(color),
                transparent: true, opacity: 0.9,
                blending: THREE.AdditiveBlending
            });
            const sprite = new THREE.Sprite(spriteMat);
            sprite.scale.set(0.08, 0.08, 1);
            conduitsGroup.add(sprite);

            pulses.push({
                sprite, edgeId: edge.id(),
                srcX: srcPos.x, srcY: srcPos.y,
                tgtX: tgtPos.x, tgtY: tgtPos.y,
                progress: Math.random(),
                speed: 0.003 + Math.random() * 0.004,
                color: new THREE.Color(color)
            });
        });
    }
}

// Animate existing pulses
pulses.forEach(p => {
    p.progress += p.speed;
    if (p.progress >= 1) p.progress %= 1;
    // Convert screen coordinates to Three.js NDC
    const sx = ((p.srcX + (p.tgtX - p.srcX) * p.progress) / window.innerWidth) * 2 - 1;
    const sy = -((p.srcY + (p.tgtY - p.srcY) * p.progress) / window.innerHeight) * 2 + 1;
    p.sprite.position.set(sx * 2.5, sy * 2.5, 0); // Scale to camera FOV
    let alpha = p.progress < 0.15 ? (p.progress / 0.15) :
                (p.progress > 0.85 ? (1.0 - p.progress) / 0.15 : 1);
    p.sprite.material.opacity = Math.max(0, Math.min(1, alpha)) * 0.7;
});
```

---

### Step 8: Camera Animation System — Branch-Following Zoom

**Goal**: Cinematic camera that starts from above, dips down to branch origin, tilts to side perspective, and follows the branch path to the target node.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — replace existing `enterDrillDown()` and node click handlers

**What changes:**
- Replace the existing instant `enterDrillDown()` with a multi-step animated camera:

```javascript
// Camera state
let cameraAnimating = false;
let cameraZoomLevel = 0; // 0 = overview, 1 = cycle, 2 = agent

async function animateCameraToBranch(targetNodeId) {
    if (cameraAnimating) return;
    cameraAnimating = true;

    const targetNode = window.cyActive.getElementById(targetNodeId);
    if (!targetNode || !targetNode.length) { cameraAnimating = false; return; }

    const targetPos = targetNode.position();
    const cycleId = targetNode.data('cycleId');
    const cycleNodeId = `cycle_${cycleId}`;
    const cycleNode = window.cyActive.getElementById(cycleNodeId);

    // Step 1: Dim all non-related elements
    window.cyActive.elements().addClass('dimmed');
    window.cyActive.elements(`[cycleId = "${cycleId}"]`).removeClass('dimmed').addClass('highlighted');

    // Step 2: Pan camera to the cycle node first (the branch origin)
    const cyclePanTarget = {
        x: (window.innerWidth / 2) - (cycleNode.position().x * 1.5),
        y: (window.innerHeight / 2) - (cycleNode.position().y * 1.5)
    };

    await new Promise(resolve => {
        window.cyActive.animate({
            zoom: 1.5,
            pan: cyclePanTarget
        }, { duration: 600, easing: 'ease-in-out-cubic', complete: resolve });
    });

    // Step 3: Continue to target node (following the branch)
    const finalZoom = 2.5;
    const finalPan = {
        x: (window.innerWidth / 2) + 150 - (targetPos.x * finalZoom),
        y: (window.innerHeight / 2) - (targetPos.y * finalZoom)
    };

    await new Promise(resolve => {
        window.cyActive.animate({
            zoom: finalZoom,
            pan: finalPan
        }, { duration: 800, easing: 'ease-out-cubic', complete: resolve });
    });

    cameraAnimating = false;
    cameraZoomLevel = targetNode.data('type') === 'cycle' ? 1 : 2;

    // Hide globe and labels during zoom
    document.getElementById('active-globe-container').style.opacity = '0';
    document.getElementById('labels-container').style.opacity = '0.3';
}

async function animateCameraBack() {
    if (cameraAnimating) return;
    cameraAnimating = true;

    // Reverse: zoom back out
    await new Promise(resolve => {
        window.cyActive.animate({
            zoom: 1.0, pan: { x: 0, y: 0 }
        }, { duration: 600, easing: 'ease-out-cubic', complete: resolve });
    });

    window.cyActive.elements().removeClass('dimmed highlighted');
    document.getElementById('active-globe-container').style.opacity = '1';
    document.getElementById('labels-container').style.opacity = '1';
    closeActivePanel();

    cameraAnimating = false;
    cameraZoomLevel = 0;
}
```

**Node click handler update:**

```javascript
// Replace existing cyActive.on('tap', 'node', ...) with:
window.cyActive.on('tap', 'node', function(evt) {
    const node = evt.target;
    const d = node.data();
    if (d.type === 'anchor') return;

    // Any node click → follow the branch
    animateCameraToBranch(node.id());

    // Open side panel after animation
    setTimeout(() => {
        openAgentPanel(d);
    }, 1400); // After both animation stages complete
});
```

---

### Step 9: Bottom-of-Screen Back Trigger

**Goal**: Hovering the mouse all the way to the bottom of the screen triggers a reverse camera animation back to the full overview.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html)

**What changes:**
- Add an invisible hot zone at the bottom of the screen
- When mouse enters it while zoomed in, trigger `animateCameraBack()`

```html
<!-- Add to the active-view-container HTML -->
<div id="back-trigger-zone" style="
    position: fixed; bottom: 0; left: 0; width: 100%; height: 40px;
    z-index: 200; cursor: default; pointer-events: auto;
"></div>
```

```javascript
// Back trigger — hover bottom of screen to zoom out
document.getElementById('back-trigger-zone').addEventListener('mouseenter', () => {
    if (cameraZoomLevel > 0 && !cameraAnimating) {
        animateCameraBack();
    }
});
```

**Replace existing `exitDrillDown()` mousemove listener** (L1659-1668) with the above approach, removing the old `deptNode` y-position check.

---

### Step 10: Side Panel — Real Agent Data with Correct Order

**Goal**: Show real agent data in the side panel. Order: Config → Findings → Thinking → Memory (RECALLED only) → Conversation.

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — replace side panel HTML and handler

**What changes:**
- Redesign the active side panel HTML with five sections in the correct order
- Add `openAgentPanel(data)` function that populates from real data

```html
<!-- Replace existing active-side-panel content -->
<div id="active-side-panel">
    <button class="close-btn" onclick="closeActivePanel()">×</button>
    <div class="panel-header">
        <h2 id="active-panel-title">Agent Node</h2>
        <span class="panel-tag" id="active-panel-status">PENDING</span>
    </div>

    <!-- 1. Config -->
    <div class="section-title">AGENT CONFIG</div>
    <div id="panel-config" class="panel-section">
        <div id="panel-role" class="panel-field"></div>
        <div id="panel-brief" class="panel-field"></div>
        <div id="panel-tools" class="panel-field"></div>
    </div>

    <!-- 2. Findings (magnifying glass items) -->
    <div class="section-title">FINDINGS</div>
    <div id="panel-findings" class="panel-section"></div>

    <!-- 3. Live Thinking -->
    <div class="section-title">LIVE THINKING</div>
    <div id="panel-thinking" class="panel-section"
         style="max-height: 200px; overflow-y: auto; font-size: 11px; color: #9CA3AF;">
    </div>

    <!-- 4. Memory (RECALLED only) -->
    <div class="section-title">MEMORY</div>
    <div id="panel-memory" class="panel-section"></div>

    <!-- 5. Conversation Log -->
    <div class="section-title">CONVERSATION LOG</div>
    <div id="panel-conversation" class="panel-section"
         style="max-height: 200px; overflow-y: auto;"></div>

    <div class="action-box">
        <button class="action-btn" onclick="window.location.href='agent_talk.task_log.html'">
            Open Live Agent Console
        </button>
    </div>
</div>
```

```javascript
function openAgentPanel(nodeData) {
    const panel = document.getElementById('active-side-panel');
    const agentId = nodeData.id;
    let agentInfo = {};

    // Parse stored agent data from node
    try { agentInfo = JSON.parse(nodeData.agentData || '{}'); } catch(e) {}

    // 1. Config
    document.getElementById('active-panel-title').innerText = agentInfo.role || nodeData.label || 'Node';
    document.getElementById('active-panel-status').innerText = (agentInfo.status || 'PENDING').toUpperCase();
    document.getElementById('panel-role').innerHTML =
        `<strong>Role:</strong> ${agentInfo.role || 'Unknown'}`;
    document.getElementById('panel-brief').innerHTML =
        `<strong>Brief:</strong> ${agentInfo.brief || 'No brief available'}`;
    document.getElementById('panel-tools').innerHTML =
        `<strong>Tools:</strong> ${(agentInfo.tools_needed || []).join(', ') || 'None'}`;

    // 2. Findings
    const findingsEl = document.getElementById('panel-findings');
    findingsEl.innerHTML = '';
    if (agentInfo.output && agentInfo.output.findings) {
        const findings = agentInfo.output.findings;
        Object.entries(findings).forEach(([key, value]) => {
            const item = document.createElement('div');
            item.className = 'finding-item';
            item.innerHTML = `<span style="color:#FCD34D;">🔍</span> <strong>${key}:</strong>
                <span style="color:#9CA3AF;">${typeof value === 'string' ? value.substring(0, 120) : JSON.stringify(value).substring(0, 120)}...</span>`;
            findingsEl.appendChild(item);
        });
    } else {
        findingsEl.innerHTML = '<span style="color:#4B5563;">No findings yet...</span>';
    }

    // 3. Live Thinking
    const thinkingEl = document.getElementById('panel-thinking');
    thinkingEl.innerText = agentInfo.streamed_thoughts || 'Agent has not started thinking yet...';

    // 4. Memory (RECALLED only)
    const memoryEl = document.getElementById('panel-memory');
    memoryEl.innerHTML = '';
    // Collect memory nodes from Cytoscape
    const memNodes = window.cyActive.nodes(`[id ^= "${agentId}_mem_"]`);
    if (memNodes.length > 0) {
        memNodes.forEach(memNode => {
            let memData = {};
            try { memData = JSON.parse(memNode.data('memoryData') || '{}'); } catch(e) {}
            const item = document.createElement('div');
            item.className = 'memory-item';
            item.innerHTML = `
                <span style="color:#A78BFA;">🧠</span>
                <span class="recalled-badge">RECALLED</span>
                <span style="color:#9CA3AF;">${memData.pattern || JSON.stringify(memData).substring(0, 100)}</span>`;
            memoryEl.appendChild(item);
        });
    } else {
        memoryEl.innerHTML = '<span style="color:#4B5563;">No memories recalled</span>';
    }

    // 5. Conversation Log
    const convEl = document.getElementById('panel-conversation');
    convEl.innerHTML = '';
    fetch(`/api/agent_conversations`)
        .then(r => r.json())
        .then(data => {
            const convs = (data.conversations || []).filter(c =>
                c.agent_id === agentId || (c.source && c.source.includes(agentId))
            );
            if (convs.length === 0) {
                convEl.innerHTML = '<span style="color:#4B5563;">No conversation yet...</span>';
                return;
            }
            convs.forEach(c => {
                const item = document.createElement('div');
                item.style.cssText = 'margin-bottom:8px; padding:6px; background:rgba(255,255,255,0.02); border-radius:4px; font-size:10px;';
                item.innerHTML = `<strong style="color:${c.type === 'prompt_sent' ? '#60A5FA' : '#34D399'}">
                    ${c.type === 'prompt_sent' ? '→ PROMPT' : '← RESPONSE'}</strong>
                    <div style="color:#9CA3AF; margin-top:4px;">${(c.content || '').substring(0, 200)}...</div>`;
                convEl.appendChild(item);
            });
        });

    panel.classList.add('open');
}
```

**CSS for the RECALLED badge:**

```css
.recalled-badge {
    display: inline-block;
    background: rgba(167, 139, 250, 0.2);
    border: 1px solid rgba(167, 139, 250, 0.4);
    color: #A78BFA;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 6px;
    border-radius: 4px;
    margin: 0 6px;
}

.finding-item, .memory-item {
    padding: 8px;
    margin-bottom: 6px;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.05);
    font-size: 11px;
}
```

---

## Verification Plan

### Automated Tests
- `python jarvis.py` — verify server starts without errors
- Hit `/api/agent_graph/<test_plan_id>` manually to verify the new endpoint returns the correct structure
- Hit `/agents/events?since=0` to verify event log includes new event types

### Manual Verification
1. **Step 1**: Navigate between Execution → Plan → Task Logs → APIs → back to Execution and verify the pipeline ID persists (check `localStorage` in DevTools)
2. **Step 2**: Start a pipeline and hit `/api/agent_graph/<id>` — verify it returns cycles with agents, statuses, and outputs
3. **Step 3**: Open `execution.html` while a pipeline is running — verify the spider web shows actual agents from the pipeline, not hardcoded departments
4. **Step 4**: Verify each 3D icon renders correctly — human for agents, magnifying glass for findings, paper for plans, brain for memories
5. **Step 5**: Verify multiple branches fan out from each cycle node with cross-links forming spider web
6. **Step 6**: Watch the web grow in real-time as agents spawn, recall memories, and discover findings
7. **Step 7**: Verify light pulses travel continuously along all branches while pipeline runs
8. **Step 8**: Click a cycle node → verify camera follows the branch from above. Click an agent → verify camera continues to that agent
9. **Step 9**: While zoomed in, move mouse to bottom of screen → verify camera zooms back to overview
10. **Step 10**: Hover agent node while zoomed → verify side panel shows Config, Findings, Thinking, Memory (RECALLED), Conversation in that order
