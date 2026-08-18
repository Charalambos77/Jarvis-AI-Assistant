# Live Agent Constellation — Real-Time Pipeline Visualization

Replace the hardcoded department constellation in `execution.html`'s Active View with a **live constellation** that visualizes the actual agents spawned by the pipeline in real time. Keep the existing constellation visual style (white circles, straight edges, department labels), the existing drill-down/zoom behavior, and the existing nebula core — but power everything from real pipeline data instead of a hardcoded array.

---

## Proposed Changes

This is broken into **6 sequential steps**. Each step is self-contained and testable before moving to the next.

---

### Step 1: Pipeline Persistence Across All Pages

**Goal**: Store the currently viewed pipeline ID so that navigating to *any* page and back remembers which pipeline you were viewing.

---

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — Pipeline status poller (~L698-733)

**What changes:**
- On pipeline status poll, store the active pipeline ID in `localStorage`
- On page load, read it back

```diff
  // Inside pollPipelineStatus(), after line 703: const shouldBeRunning = !!data.active;
  const shouldBeRunning = !!data.active;
+
+ // Persist pipeline ID across all page navigations
+ if (data.active_ids && data.active_ids.length > 0) {
+     localStorage.setItem('jarvis_active_pipeline_id', data.active_ids[0]);
+ }
```

```diff
  // At top of script block (~L657), after variable declarations
  let isInitialLoad = true;
+
+ const STORED_PIPELINE_ID = localStorage.getItem('jarvis_active_pipeline_id');
```

---

#### [MODIFY] [plan.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/plan.html) — selectPlan function (~L1122)

**What changes:**
- When user switches pipelines, update localStorage

```diff
  function selectPlan(planId) {
      selectedPlanId = planId;
+     localStorage.setItem('jarvis_active_pipeline_id', planId);
      viewMode = "detail";
```

---

#### [MODIFY] [agent_talk.task_log.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/agent_talk.task_log.html)

**What changes:**
- Read `localStorage.jarvis_active_pipeline_id` on load to filter logs to the correct pipeline (add near the top of the script block)

```javascript
const STORED_PIPELINE_ID = localStorage.getItem('jarvis_active_pipeline_id');
```

---

### Step 2: New Backend Endpoint — Live Agent Graph Data

**Goal**: Create a dedicated API endpoint that returns the full live graph structure (cycles, agents, their states, findings, memories) for a specific pipeline, so the frontend can build the constellation from real data.

---

#### [MODIFY] [jarvis.py](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/jarvis.py) — Add new endpoint after `/plans/<plan_id>` (~L1523)

**What changes:**
- Add a new `/api/agent_graph/<plan_id>` endpoint

```python
@app.route("/api/agent_graph/<plan_id>", methods=["GET"])
def get_agent_graph(plan_id):
    """Returns the full live constellation graph for a pipeline run.
    Structure: { plan_id, phase, task, cycles: [{ cycle_id, domain, goal, agents: [
        { agent_id, role, brief, tools_needed, memory_query, is_lead, status,
          streamed_thoughts, output, config }
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

---

#### [MODIFY] [multi_agent_coordinator.py](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/multi_agent_coordinator.py) — Research phase (~L56-97)

**What changes:**
- Add `memory_recalled` event after memory patterns are fetched (~L70)
- Add `findings_discovered` event after agent completes (~L97)

```diff
  # ~L69-75: Inside run_research_phase_for_cycle, after patterns are fetched
  patterns = db.search_memory_patterns(db_conn, memory_query, task_type=task_type)
  if patterns:
      memory_context = "\n".join(...)
+     if event_logger:
+         event_logger({
+             "event_type": "memory_recalled",
+             "agent_id": agent_id,
+             "data": {"patterns": [{"pattern": p["pattern"], "outcome": p["outcome"]} for p in patterns], "query": memory_query}
+         })
```

```diff
  # ~L94-97: Inside results loop, after completed event
  if event_logger:
      event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})
+     # Emit findings as separate event for constellation leaf nodes
+     if isinstance(r, dict) and r.get("findings"):
+         event_logger({
+             "event_type": "findings_discovered",
+             "agent_id": agent_id,
+             "data": r["findings"]
+         })
```

---

### Step 3: Remove Hardcoded Departments & Build Dynamic Data Layer

**Goal**: Replace the static `departments` array with a polling system that fetches live agent graph data and builds the Cytoscape elements dynamically, keeping the exact same constellation visual style.

---

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — Active View Section (~L1189-1708)

**What changes:**
- Delete the entire hardcoded `departments` array (L1193-1201)
- Replace `initActiveView()` IIFE with a new version that polls live data
- Keep the existing Three.js nebula/core/conduit/pulse code verbatim (L1203-1328)
- Replace the Cytoscape setup section (L1330-1708) with a dynamic builder

The complete replacement for the `initActiveView()` function (L1192-1708):

```javascript
(function initActiveView() {
    // ---- State ----
    let liveGraphData = null;        // Latest graph from /api/agent_graph
    let lastEventTimestamp = 0;       // For incremental event polling
    let renderedNodeIds = new Set();  // Track what's already in the scene
    let currentPlanId = localStorage.getItem('jarvis_active_pipeline_id');
    let departments = [];             // Built dynamically from live data

    // ---- Cycle color palette (assigned dynamically) ----
    const CYCLE_COLORS = [
        '#22D3EE', '#60A5FA', '#34D399', '#F87171',
        '#FB7185', '#FCD34D', '#A78BFA', '#818CF8'
    ];

    // ============ Three.js Nebula Core (UNCHANGED — verbatim from existing) ============
    const container = document.getElementById('active-globe-container');
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    camera.position.z = 2.5;

    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "high-performance" });
    renderer.setSize(600, 600);
    renderer.setPixelRatio(window.devicePixelRatio);
    container.appendChild(renderer.domElement);

    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 256;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.12, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.25, 'rgba(180,210,255,0.85)');
    gradient.addColorStop(0.55, 'rgba(150,180,255,0.25)');
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 256, 256);
    const texture = new THREE.CanvasTexture(canvas);

    const smokeGeo = new THREE.BufferGeometry();
    const smokeCount = 7000;
    const posArray = new Float32Array(smokeCount * 3);
    const colorArray = new Float32Array(smokeCount * 3);
    const dirs = []; const speeds = []; const distances = [];
    const maxDistance = 1.1;
    const baseColor = new THREE.Color(0xaabbff);

    for (let i = 0; i < smokeCount; i++) {
        let theta = Math.random() * 2 * Math.PI;
        let phi = Math.acos(2 * Math.random() - 1);
        let dx = Math.sin(phi) * Math.cos(theta);
        let dy = Math.sin(phi) * Math.sin(theta);
        let dz = Math.cos(phi);

        dirs.push({ x: dx, y: dy, z: dz });
        let dist = Math.random() * maxDistance;
        distances.push(dist);
        speeds.push(0.0015 + Math.random() * 0.0035);

        let idx = i * 3;
        posArray[idx] = dx * dist; posArray[idx + 1] = dy * dist; posArray[idx + 2] = dz * dist;
        let factor = Math.pow(1.0 - (dist / maxDistance), 1.5);
        colorArray[idx] = baseColor.r * factor; colorArray[idx + 1] = baseColor.g * factor; colorArray[idx + 2] = baseColor.b * factor;
    }

    smokeGeo.setAttribute('position', new THREE.BufferAttribute(posArray, 3));
    smokeGeo.setAttribute('color', new THREE.BufferAttribute(colorArray, 3));
    const smokeMat = new THREE.PointsMaterial({
        size: 0.045, map: texture, transparent: true, opacity: 0.8, depthWrite: false, blending: THREE.AdditiveBlending, vertexColors: true
    });
    const nebula = new THREE.Points(smokeGeo, smokeMat); scene.add(nebula);

    const coreGeo = new THREE.BufferGeometry();
    const coreCount = 3500;
    const corePosArray = new Float32Array(coreCount * 3);
    for (let i = 0; i < coreCount * 3; i += 3) {
        let r = Math.pow(Math.random(), 1.2) * 0.58;
        let theta = Math.random() * 2 * Math.PI;
        let phi = Math.acos(2 * Math.random() - 1);
        corePosArray[i] = r * Math.sin(phi) * Math.cos(theta);
        corePosArray[i + 1] = r * Math.sin(phi) * Math.sin(theta);
        corePosArray[i + 2] = r * Math.cos(phi);
    }
    coreGeo.setAttribute('position', new THREE.BufferAttribute(corePosArray, 3));
    const coreMat = new THREE.PointsMaterial({
        size: 0.038, map: texture, transparent: true, opacity: 0.7, depthWrite: false, blending: THREE.AdditiveBlending, color: 0xffffff
    });
    const core = new THREE.Points(coreGeo, coreMat); scene.add(core);

    const pulses = [];
    const conduitsGroup = new THREE.Group();
    scene.add(conduitsGroup);

    // ---- Conduits are rebuilt when graph changes ----
    function rebuildConduits() {
        // Clear existing conduits
        while (conduitsGroup.children.length > 0) {
            conduitsGroup.remove(conduitsGroup.children[0]);
        }
        pulses.length = 0;

        departments.forEach((dept, index) => {
            let angle = -Math.PI / 2 + (index * (2 * Math.PI / departments.length));
            let tx = Math.cos(angle) * 0.69;
            let ty = -Math.sin(angle) * 0.69;

            const points = [new THREE.Vector3(Math.cos(angle) * 0.15, -Math.sin(angle) * 0.15, 0), new THREE.Vector3(tx, ty, 0)];
            const lineGeo = new THREE.BufferGeometry().setFromPoints(points);
            const lineMat = new THREE.LineBasicMaterial({ color: dept.color, transparent: true, opacity: 0.18, blending: THREE.AdditiveBlending });
            conduitsGroup.add(new THREE.Line(lineGeo, lineMat));

            for (let pIdx = 0; pIdx < 3; pIdx++) {
                const spriteMat = new THREE.SpriteMaterial({ map: texture, color: 0xffffff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending });
                const sprite = new THREE.Sprite(spriteMat);
                sprite.scale.set(0.12, 0.12, 1);
                conduitsGroup.add(sprite);
                pulses.push({ sprite: sprite, angle: angle, progress: pIdx / 3, speed: 0.0035, color: new THREE.Color(dept.color) });
            }
        });
    }

    const posAttribute = nebula.geometry.attributes.position;
    const colorAttribute = nebula.geometry.attributes.color;

    function animateActiveGlobe() {
        requestAnimationFrame(animateActiveGlobe);
        nebula.rotation.y -= 0.0008; core.rotation.y += 0.002; core.rotation.x += 0.0005;

        for (let i = 0; i < smokeCount; i++) {
            let dist = distances[i] + speeds[i];
            if (dist > maxDistance) dist = 0;
            distances[i] = dist;
            let dir = dirs[i]; let idx = i * 3;
            posAttribute.array[idx] = dir.x * dist; posAttribute.array[idx + 1] = dir.y * dist; posAttribute.array[idx + 2] = dir.z * dist;
            let factor = Math.pow(1.0 - (dist / maxDistance), 1.5);
            colorAttribute.array[idx] = baseColor.r * factor; colorAttribute.array[idx + 1] = baseColor.g * factor; colorAttribute.array[idx + 2] = baseColor.b * factor;
        }
        posAttribute.needsUpdate = true; colorAttribute.needsUpdate = true;

        pulses.forEach(p => {
            p.progress += p.speed;
            if (p.progress >= 1) p.progress %= 1;
            let r = 0.15 + p.progress * (0.69 - 0.15);
            p.sprite.position.x = Math.cos(p.angle) * r; p.sprite.position.y = -Math.sin(p.angle) * r;
            let alpha = p.progress < 0.15 ? (p.progress / 0.15) : (p.progress > 0.85 ? (1.0 - p.progress) / 0.15 : 1);
            p.sprite.material.opacity = Math.max(0, Math.min(1, alpha)) * 0.95;
            p.sprite.material.color.lerpColors(new THREE.Color(0xffffff), p.color, p.progress);
        });

        renderer.render(scene, camera);
    }
    animateActiveGlobe();


    // ============ Dynamic Constellation Builder ============
    let cyActive = null;
    let elements = [];
    let nodesPositions = {};
    const CENTER_X = window.innerWidth / 2;
    const CENTER_Y = (window.innerHeight / 2) + 40;
    const GLOBE_RADIUS = 90; const DEPT_RADIUS = 200; const LABEL_RADIUS = 530;

    function buildConstellationFromGraph(graph) {
        // Convert live graph cycles into the same departments format
        departments = graph.cycles.map((cycle, index) => ({
            id: cycle.cycle_id || `cycle_${index}`,
            label: (cycle.domain || `Cycle ${index + 1}`).toUpperCase(),
            color: CYCLE_COLORS[index % CYCLE_COLORS.length],
            agents: (cycle.agents || []).map(a => ({
                id: a.agent_id,
                label: a.role || a.agent_id,
                status: a.status || 'pending',
                agentData: a
            }))
        }));

        // Add execution agents as a separate "department" if they exist
        if (graph.execution_agents && graph.execution_agents.length > 0) {
            departments.push({
                id: 'execution',
                label: 'EXECUTION',
                color: '#F472B6',
                agents: graph.execution_agents.map(a => ({
                    id: a.agent_id,
                    label: a.role || a.agent_id,
                    status: a.status || 'pending',
                    agentData: a
                }))
            });
        }

        // Build elements and positions (same layout logic as original)
        elements = [];
        nodesPositions = {};
        let labelsHTML = '';

        departments.forEach((dept, index) => {
            let angle = -Math.PI / 2 + (index * (2 * Math.PI / departments.length));
            let dx = CENTER_X + DEPT_RADIUS * Math.cos(angle);
            let dy = CENTER_Y + DEPT_RADIUS * Math.sin(angle);

            let anchorId = 'anchor_' + dept.id;
            let cx = CENTER_X + GLOBE_RADIUS * Math.cos(angle);
            let cyPos = CENTER_Y + GLOBE_RADIUS * Math.sin(angle);
            elements.push({ data: { id: anchorId, type: 'anchor' } });
            nodesPositions[anchorId] = { x: cx, y: cyPos };

            elements.push({ data: { id: dept.id, label: '', type: 'dept', color: dept.color, deptId: dept.id } });
            nodesPositions[dept.id] = { x: dx, y: dy };

            let lx = CENTER_X + LABEL_RADIUS * Math.cos(angle);
            let ly = CENTER_Y + LABEL_RADIUS * Math.sin(angle);
            labelsHTML += `<div class="department-label" id="label_${dept.id}" style="left: ${lx}px; top: ${ly}px; color: ${dept.color}; transition: opacity 0.3s;">${dept.label}</div>`;

            let currentParent = dept.id;
            let currentRadius = DEPT_RADIUS + 70;

            dept.agents.forEach((agent, aIdx) => {
                let isLarge = aIdx < 2;
                let type = isLarge ? 'agent_large' : 'agent_dot';
                let branchAngle = angle + (Math.sin(aIdx) * 0.2);
                let ax = CENTER_X + currentRadius * Math.cos(branchAngle);
                let ay = CENTER_Y + currentRadius * Math.sin(branchAngle);

                elements.push({ data: {
                    id: agent.id, label: isLarge ? agent.label : '', type: type,
                    deptId: dept.id, agentData: JSON.stringify(agent.agentData || {})
                }});
                nodesPositions[agent.id] = { x: ax, y: ay };

                let cpD = (Math.random() - 0.5) * 30;
                elements.push({ data: { source: currentParent, target: agent.id, deptId: dept.id, color: dept.color, cpDist: cpD, cpWeight: 0.5 } });

                currentParent = agent.id;
                currentRadius += 50 + (Math.random() * 20);

                // Constellation side branches with web cross-links (same as original)
                if (Math.random() > 0.35) {
                    let sideId = agent.id + '_side';
                    let sideAngle = branchAngle + (Math.random() > 0.5 ? 0.3 : -0.3);
                    let sx = CENTER_X + (currentRadius - 35) * Math.cos(sideAngle);
                    let sy = CENTER_Y + (currentRadius - 35) * Math.sin(sideAngle);

                    elements.push({ data: { id: sideId, type: 'agent_dot', deptId: dept.id } });
                    nodesPositions[sideId] = { x: sx, y: sy };
                    elements.push({ data: { source: agent.id, target: sideId, deptId: dept.id, color: dept.color } });

                    if (currentParent && currentParent !== dept.id) {
                        elements.push({ data: { source: currentParent, target: sideId, deptId: dept.id, type: 'web_cross', color: dept.color } });
                    }
                }
            });
        });

        document.getElementById('labels-container').innerHTML = labelsHTML;
        rebuildConduits();

        // Initialize or recreate Cytoscape
        if (cyActive) {
            cyActive.destroy();
        }

        cyActive = cytoscape({
            container: document.getElementById('cy-active'),
            elements: elements,
            style: [
                { selector: 'node[type="anchor"]', style: { 'width': 1, 'height': 1, 'opacity': 0 } },
                {
                    selector: 'node[type="agent_large"]',
                    style: {
                        'label': 'data(label)', 'color': '#fff', 'font-size': '10px', 'font-family': 'Inter',
                        'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 6,
                        'background-color': '#EAE6EE', 'width': 22, 'height': 22, 'border-width': 1, 'border-color': '#fff',
                        'shadow-blur': 10, 'shadow-color': '#EAE6EE', 'shadow-opacity': 0.4
                    }
                },
                {
                    selector: 'node[type="dept"]',
                    style: {
                        'background-color': '#1C1E32', 'width': 44, 'height': 44, 'border-width': 2,
                        'border-color': 'data(color)', 'shadow-blur': 25, 'shadow-color': 'data(color)', 'shadow-opacity': 0.6
                    }
                },
                // Sub-node types: finding, memory, plan_contribution — plain white dots like agent_dot
                { selector: 'node[type="agent_dot"]', style: { 'background-color': '#fff', 'width': 6, 'height': 6, 'label': '', 'shadow-blur': 5, 'shadow-color': '#fff', 'shadow-opacity': 0.8 } },
                { selector: 'node[type="finding"]', style: { 'background-color': '#FCD34D', 'width': 8, 'height': 8, 'label': 'data(label)', 'color': '#9CA3AF', 'font-size': '8px', 'text-valign': 'bottom', 'text-margin-y': 5, 'shadow-blur': 8, 'shadow-color': '#FCD34D', 'shadow-opacity': 0.6 } },
                { selector: 'node[type="memory"]', style: { 'background-color': '#A78BFA', 'width': 8, 'height': 8, 'label': 'data(label)', 'color': '#9CA3AF', 'font-size': '8px', 'text-valign': 'bottom', 'text-margin-y': 5, 'shadow-blur': 8, 'shadow-color': '#A78BFA', 'shadow-opacity': 0.6 } },
                { selector: 'node[type="plan_contribution"]', style: { 'background-color': '#34D399', 'width': 8, 'height': 8, 'label': 'data(label)', 'color': '#9CA3AF', 'font-size': '8px', 'text-valign': 'bottom', 'text-margin-y': 5, 'shadow-blur': 8, 'shadow-color': '#34D399', 'shadow-opacity': 0.6 } },
                {
                    selector: 'edge',
                    style: {
                        'width': 1.2, 'line-color': 'rgba(255, 255, 255, 0.28)',
                        'curve-style': 'straight', 'target-arrow-shape': 'none'
                    }
                },
                {
                    selector: 'edge[type="web_cross"]',
                    style: {
                        'width': 0.8, 'line-color': 'rgba(255, 255, 255, 0.15)',
                        'line-style': 'dashed', 'curve-style': 'straight', 'target-arrow-shape': 'none'
                    }
                },
                { selector: '.dimmed', style: { 'opacity': 0.15 } },
                { selector: 'edge.highlighted', style: { 'line-color': 'data(color)', 'width': 2.5, 'shadow-blur': 15, 'shadow-color': 'data(color)', 'shadow-opacity': 0.8, 'z-index': 100 } },
                { selector: 'node.highlighted', style: { 'border-color': 'data(color)', 'shadow-blur': 30, 'shadow-color': 'data(color)', 'shadow-opacity': 1, 'z-index': 100 } }
            ],
            layout: { name: 'preset', positions: nodesPositions, fit: false },
            userZoomingEnabled: false, userPanningEnabled: false
        });

        // Apply dept SVG icons (same as original)
        cyActive.nodes('[type="dept"]').forEach(n => {
            const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${n.data('color')}" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>`;
            n.style('background-image', 'data:image/svg+xml;utf8,' + encodeURIComponent(svg));
            n.style('background-width', '60%'); n.style('background-height', '60%');
            n.style('background-position-x', '50%'); n.style('background-position-y', '50%');
        });

        // Re-attach all event handlers
        attachCytoscapeHandlers();
    }


    // ============ Polling System ============
    async function pollAgentGraph() {
        if (!currentPlanId) {
            currentPlanId = localStorage.getItem('jarvis_active_pipeline_id');
            if (!currentPlanId) return;
        }
        try {
            const res = await fetch(`/api/agent_graph/${currentPlanId}`);
            if (!res.ok) return;
            const graph = await res.json();

            // Only rebuild if cycle count changed (new cycles added)
            const prevCycleCount = liveGraphData ? liveGraphData.cycles.length : 0;
            liveGraphData = graph;

            if (!cyActive || graph.cycles.length !== prevCycleCount) {
                buildConstellationFromGraph(graph);
            }
        } catch(e) {
            console.error("Agent graph poll error:", e);
        }
    }

    // Poll events for real-time node additions (Step 5)
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


    // ============ Hover, Drill-Down, Side Panel (PRESERVED — same logic as original) ============
    let activeDeptId = null;
    let isDrillDownMode = false;
    let isTransitioning = false;
    let currentDrillDept = null;
    let drillDownPositions = {};

    function attachCytoscapeHandlers() {
        // Sector Mouseover Hover Glow Highlight
        function applyHoverHighlight(deptId) {
            activeDeptId = deptId;
            cyActive.elements().addClass('dimmed');
            let branch = cyActive.elements(`[deptId = "${deptId}"]`);
            branch.removeClass('dimmed').addClass('highlighted');
            cyActive.getElementById('anchor_' + deptId).removeClass('dimmed');

            document.querySelectorAll('#active-view-container .department-label').forEach(el => {
                if (el.id !== 'label_' + deptId) {
                    el.style.opacity = '0.15';
                } else {
                    const deptObj = departments.find(d => d.id === deptId);
                    el.style.opacity = '1';
                    el.style.textShadow = `0 0 20px ${deptObj ? deptObj.color : '#fff'}`;
                }
            });
        }

        function clearHoverHighlight() {
            if (activeDeptId === null) return;
            activeDeptId = null;
            cyActive.elements().removeClass('dimmed highlighted');
            document.querySelectorAll('#active-view-container .department-label').forEach(el => {
                el.style.opacity = '1';
                el.style.textShadow = `0 0 15px rgba(0,0,0,0.8)`;
            });
        }

        window.addEventListener('mousemove', (e) => {
            if (isDrillDownMode || isTransitioning || !document.getElementById('idle-view-container').classList.contains('hidden')) return;

            if (document.getElementById('active-side-panel').classList.contains('open') && e.clientX < 500) {
                clearHoverHighlight();
                return;
            }

            const cx = window.innerWidth / 2;
            const cy = (window.innerHeight / 2) + 40;
            const dx = e.clientX - cx;
            const dy = e.clientY - cy;
            const distance = Math.sqrt(dx * dx + dy * dy) / 0.8;

            if (distance < 180 || distance > 550) {
                clearHoverHighlight();
                return;
            }

            let mouseAngle = Math.atan2(dy, dx);
            const sectorSize = (2 * Math.PI) / departments.length;
            let adjustedAngle = mouseAngle - (-Math.PI / 2) + (sectorSize / 2);
            while (adjustedAngle < 0) adjustedAngle += 2 * Math.PI;
            while (adjustedAngle >= 2 * Math.PI) adjustedAngle -= 2 * Math.PI;

            const deptIndex = Math.floor(adjustedAngle / sectorSize) % departments.length;
            const targetDept = departments[deptIndex];

            if (targetDept && targetDept.id !== activeDeptId) {
                applyHoverHighlight(targetDept.id);
            }
        });

        document.body.addEventListener('mouseleave', () => {
            if (!isDrillDownMode && !isTransitioning) clearHoverHighlight();
        });

        // Breathing float animation
        let animTime = 0;
        setInterval(() => {
            if (isTransitioning || !cyActive) return;
            animTime += 0.05;
            cyActive.nodes().forEach((node, i) => {
                if (node.data('type') === 'anchor') return;
                let basePos = nodesPositions[node.id()];
                if (isDrillDownMode) {
                    if (node.data('deptId') === currentDrillDept && drillDownPositions[node.id()]) {
                        basePos = drillDownPositions[node.id()];
                    } else { return; }
                }
                if (!basePos) return;
                let offsetX = Math.sin(animTime + i) * 1.5;
                let offsetY = Math.cos(animTime + i * 1.5) * 1.5;
                node.position({ x: basePos.x + offsetX, y: basePos.y + offsetY });
            });
        }, 30);

        // Node Click: Enter Drill Down OR Open Side Panel + Camera Zoom
        cyActive.on('tap', 'node', function (evt) {
            let node = evt.target;
            let d = node.data();
            if (d.type === 'anchor') return;

            if (!isDrillDownMode) {
                enterDrillDown(d.deptId);
                return;
            }

            // Open side panel with real agent data
            openAgentPanel(d);

            const targetZoom = 2.0;
            const targetX = (window.innerWidth / 2) + 150;
            const targetY = window.innerHeight / 2;
            cyActive.animate({
                zoom: targetZoom,
                pan: { x: targetX - (node.position().x * targetZoom), y: targetY - (node.position().y * targetZoom) }
            }, { duration: 650, easing: 'ease-out-cubic' });
        });

        cyActive.on('tap', function (evt) {
            if (evt.target === cyActive) {
                if (!isDrillDownMode && activeDeptId) {
                    enterDrillDown(activeDeptId);
                } else {
                    closeActivePanel();
                }
            }
        });
    }

    function enterDrillDown(deptId) {
        isDrillDownMode = true;
        currentDrillDept = deptId;
        isTransitioning = true;

        cyActive.zoom(1.0);
        cyActive.pan({ x: 0, y: 0 });
        cyActive.elements().removeClass('dimmed highlighted');

        document.getElementById('active-globe-container').style.display = 'none';
        document.getElementById('active-globe-click-target').style.display = 'none';
        document.getElementById('labels-container').style.display = 'none';
        conduitsGroup.visible = false;

        let wm = document.getElementById('watermark');
        let deptObj = departments.find(d => d.id === deptId);
        wm.innerText = deptObj ? deptObj.label : deptId;
        wm.style.display = 'block';

        cyActive.elements().style('display', 'none');
        let branch = cyActive.elements(`[deptId = "${deptId}"]`);
        branch.style('display', 'element');

        cyActive.getElementById(deptId).data('label', 'START HERE');

        let deptNode = cyActive.getElementById(deptId);
        let rootX = window.innerWidth / 2;
        let rootY = window.innerHeight - 150;

        drillDownPositions = {};
        drillDownPositions[deptId] = { x: rootX, y: rootY };

        let levels = { 0: [deptNode] };
        let queue = [{ node: deptNode, level: 0 }];
        let visited = new Set([deptId]);

        while (queue.length > 0) {
            let curr = queue.shift();
            let succ = curr.node.outgoers('node');
            succ.forEach(s => {
                if (!visited.has(s.id())) {
                    visited.add(s.id());
                    let nextLevel = curr.level + 1;
                    if (!levels[nextLevel]) levels[nextLevel] = [];
                    levels[nextLevel].push(s);
                    queue.push({ node: s, level: nextLevel });
                }
            });
        }

        let levelSpacingY = -120;
        let spacingX = 180;

        Object.keys(levels).forEach(lvlStr => {
            let lvl = parseInt(lvlStr);
            if (lvl === 0) return;
            let nodesInLvl = levels[lvl];
            let width = (nodesInLvl.length - 1) * spacingX;
            let startX = rootX - width / 2;

            nodesInLvl.forEach((n, idx) => {
                let x = startX + idx * spacingX + (Math.random() - 0.5) * 80;
                let y = rootY + (lvl * levelSpacingY) + (Math.random() - 0.5) * 50;
                drillDownPositions[n.id()] = { x: x, y: y };
            });
        });

        cyActive.nodes(`[deptId = "${deptId}"]`).forEach(n => {
            let pos = drillDownPositions[n.id()];
            if (pos) n.animate({ position: pos }, { duration: 800, easing: 'ease-out-cubic' });
        });

        setTimeout(() => { isTransitioning = false; }, 800);
    }

    window.addEventListener('mousemove', (e) => {
        if (isDrillDownMode && !isTransitioning) {
            const deptNode = cyActive ? cyActive.getElementById(currentDrillDept) : null;
            if (deptNode && deptNode.length > 0) {
                const cyCenterY = (window.innerHeight / 2) + 40;
                const nodeScreenY = (window.innerHeight / 2) + (deptNode.position().y - cyCenterY) * 0.8;
                if (e.clientY >= nodeScreenY + 5) exitDrillDown();
            }
        }
    });

    function exitDrillDown() {
        isDrillDownMode = false;
        isTransitioning = true;

        closeActivePanel();

        document.getElementById('active-globe-container').style.display = 'block';
        document.getElementById('active-globe-click-target').style.display = 'block';
        document.getElementById('labels-container').style.display = 'block';
        conduitsGroup.visible = true;
        document.getElementById('watermark').style.display = 'none';

        if (currentDrillDept) {
            cyActive.getElementById(currentDrillDept).data('label', '');
            currentDrillDept = null;
        }

        cyActive.elements().style('display', 'element');
        document.querySelectorAll('#active-view-container .department-label').forEach(el => {
            el.style.opacity = '1';
            el.style.textShadow = `0 0 15px rgba(0,0,0,0.8)`;
        });

        cyActive.nodes().forEach(n => {
            let pos = nodesPositions[n.id()];
            if (pos) n.animate({ position: pos }, { duration: 250, easing: 'ease-out-quad' });
        });

        setTimeout(() => { isTransitioning = false; }, 250);
    }

    window.closeActivePanel = function() {
        document.getElementById('active-side-panel').classList.remove('open');
        if (isDrillDownMode && !isTransitioning && cyActive) {
            cyActive.animate({ zoom: 1.0, pan: { x: 0, y: 0 } }, { duration: 250, easing: 'ease-out-quad' });
        }
    };


    // ============ handleLiveEvent — Step 5 (growth animation) ============
    function handleLiveEvent(evt) {
        if (!cyActive || !liveGraphData) return;
        const agentId = evt.agent_id;
        if (!agentId) return;

        switch (evt.event_type) {
            case 'spawned': {
                // Agent node should already exist from buildConstellationFromGraph
                // Animate it: start at cycle node position, grow to target
                const node = cyActive.getElementById(agentId);
                if (node && node.length) {
                    const deptId = node.data('deptId');
                    const deptNode = cyActive.getElementById(deptId);
                    if (deptNode && deptNode.length) {
                        const targetPos = { ...nodesPositions[agentId] };
                        node.position(deptNode.position());
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
                // Add small purple dot nodes extending from the agent
                const patterns = evt.data?.patterns || [];
                patterns.forEach((pattern, idx) => {
                    const memId = `${agentId}_mem_${idx}`;
                    if (renderedNodeIds.has(memId)) return;
                    renderedNodeIds.add(memId);

                    const agentNode = cyActive.getElementById(agentId);
                    if (!agentNode || !agentNode.length) return;
                    const agentPos = agentNode.position();
                    const deptId = agentNode.data('deptId');

                    const offsetAngle = (idx * 0.5) - (patterns.length * 0.25);
                    const targetX = agentPos.x + Math.cos(offsetAngle) * 60;
                    const targetY = agentPos.y + Math.sin(offsetAngle) * 60;

                    cyActive.add([
                        { data: {
                            id: memId, label: 'RECALLED',
                            type: 'memory', deptId: deptId,
                            memoryData: JSON.stringify(pattern)
                        }},
                        { data: { source: agentId, target: memId, deptId: deptId } }
                    ]);

                    // Store position for breathing animation
                    nodesPositions[memId] = { x: targetX, y: targetY };

                    const memNode = cyActive.getElementById(memId);
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
                // Add small yellow dot nodes extending from agent
                const findings = evt.data;
                if (!findings || typeof findings !== 'object') return;
                const findingKeys = Object.keys(findings);

                findingKeys.forEach((key, idx) => {
                    const findId = `${agentId}_find_${idx}`;
                    if (renderedNodeIds.has(findId)) return;
                    renderedNodeIds.add(findId);

                    const agentNode = cyActive.getElementById(agentId);
                    if (!agentNode || !agentNode.length) return;
                    const agentPos = agentNode.position();
                    const deptId = agentNode.data('deptId');

                    const offsetAngle = Math.PI + (idx * 0.5) - (findingKeys.length * 0.25);
                    const targetX = agentPos.x + Math.cos(offsetAngle) * 55;
                    const targetY = agentPos.y + Math.sin(offsetAngle) * 55;

                    cyActive.add([
                        { data: {
                            id: findId, label: key.substring(0, 15),
                            type: 'finding', deptId: deptId,
                            findingData: JSON.stringify(findings[key])
                        }},
                        { data: { source: agentId, target: findId, deptId: deptId } }
                    ]);

                    nodesPositions[findId] = { x: targetX, y: targetY };

                    const node = cyActive.getElementById(findId);
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
                // Mark agent as completed + add green plan contribution dot
                const agentNode = cyActive.getElementById(agentId);
                if (!agentNode || !agentNode.length) return;
                const deptId = agentNode.data('deptId');

                const planNodeId = `${agentId}_plan`;
                if (!renderedNodeIds.has(planNodeId)) {
                    renderedNodeIds.add(planNodeId);
                    const agentPos = agentNode.position();
                    const targetX = agentPos.x + 50;
                    const targetY = agentPos.y - 40;

                    cyActive.add([
                        { data: {
                            id: planNodeId, label: 'Plan',
                            type: 'plan_contribution', deptId: deptId
                        }},
                        { data: { source: agentId, target: planNodeId, deptId: deptId } }
                    ]);

                    nodesPositions[planNodeId] = { x: targetX, y: targetY };

                    const node = cyActive.getElementById(planNodeId);
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

})();
```

---

### Step 4: (removed — no SVG icon factories, using plain styled dots)

---

### Step 5: Real-Time Growth — Progressive Node Animation

**Goal**: As pipeline events fire (`spawned`, `memory_recalled`, `findings_discovered`, `completed`), new nodes animate into existence by growing from the parent outward. This is already included in the `handleLiveEvent()` function in Step 3's code above.

**Sub-node types and their visual style:**
| Event | Node type | Color | Size |
|-------|-----------|-------|------|
| `memory_recalled` | `memory` | Purple `#A78BFA` | 8px |
| `findings_discovered` | `finding` | Yellow `#FCD34D` | 8px |
| `completed` | `plan_contribution` | Green `#34D399` | 8px |

All sub-nodes are plain colored circles matching the existing constellation aesthetic, with a subtle color glow via `shadow-blur`.

---

### Step 6: Side Panel — Real Agent Data with Correct Order

**Goal**: Show real agent data in the side panel. Order: Config → Findings → Thinking → Memory (RECALLED only) → Conversation.

---

#### [MODIFY] [execution.html](file:///c:/Users/Charalambos%20Michael/Desktop/Applications%20(Github%20Repositary%20for%20Antigravity%20/Jarvis-AI-Assistant/execution.html) — Side panel HTML (~L637-649) and CSS

**What changes:**
- Replace the existing side panel HTML with five structured sections
- Add `openAgentPanel(data)` function that populates from real data
- Add CSS for `.recalled-badge`, `.finding-item`, `.memory-item`

**Replace the side panel HTML** (L637-649) with:

```html
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

**Add CSS** (inside the `<style>` block, after active-side-panel styles ~L441):

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

.panel-section {
    margin-bottom: 16px;
}

.panel-field {
    padding: 4px 0;
    font-size: 12px;
    color: #D1D5DB;
}
```

**Add `openAgentPanel()` function** (inside the `initActiveView()` IIFE, after `closeActivePanel`):

```javascript
window.openAgentPanel = function(nodeData) {
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
    if (cyActive) {
        const memNodes = cyActive.nodes(`[id ^= "${agentId}_mem_"]`);
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
};
```

---

## Verification Plan

### Automated Tests
- `python jarvis.py` — verify server starts without errors
- Hit `/api/agent_graph/<test_plan_id>` manually to verify the new endpoint returns the correct structure
- Hit `/agents/events?since=0` to verify event log includes new `memory_recalled` and `findings_discovered` event types

### Manual Verification
1. **Step 1**: Navigate between Execution → Plan → Task Logs → back to Execution and verify the pipeline ID persists (check `localStorage` in DevTools)
2. **Step 2**: Start a pipeline and hit `/api/agent_graph/<id>` — verify it returns cycles with agents, statuses, and outputs
3. **Step 3**: Open `execution.html` while a pipeline is running — verify the constellation shows actual agents from the pipeline, not hardcoded departments. Verify it keeps the same visual style: white circles, straight edges, labels at outer edge, nebula in center
4. **Step 5**: Watch the constellation grow in real-time: purple dots for recalled memories, yellow dots for findings, green dots for plan contributions — all animating outward from their parent agent
5. **Step 6**: Click an agent node during drill-down → verify side panel shows Config, Findings, Thinking, Memory (RECALLED only), Conversation in that order
