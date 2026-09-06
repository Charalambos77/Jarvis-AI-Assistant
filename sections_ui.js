/*
 * Sections — the shared sidebar, and the window that turns a finished pipeline
 * into one.
 *
 * Lives in one file because the Brain, Execution and Plan pages are separate
 * standalone documents: triplicating this would guarantee they drift apart.
 * Include it with <script src="sections_ui.js"></script> and it wires itself up.
 *
 *   JarvisSections.openSidebar()            open the list of sections
 *   JarvisSections.createFrom(planId, name) the "Make this a section" window
 */
(function () {
    "use strict";

    if (window.JarvisSections) return;

    // A page is "inside" a section when it was opened from the sidebar. The id
    // travels in the URL so a reload stays in the section, and so the execution
    // page knows not to let a different running pipeline pull it away.
    function currentSectionId() {
        try {
            return new URLSearchParams(window.location.search).get("section") || "";
        } catch (e) {
            return "";
        }
    }

    var STYLE = `
    .jsec-btn {
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.08);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        color: #9CA3AF;
        font-size: 11px; font-weight: 600; letter-spacing: 1.5px;
        text-transform: uppercase;
        padding: 12px 24px; border-radius: 8px; cursor: pointer;
        transition: all 0.3s cubic-bezier(0.4,0,0.2,1);
        /* Same footprint as the pages' own .nav-btn so the nav stays one column
           of identical buttons; nowrap keeps a label from growing the height. */
        width: 160px; white-space: nowrap; text-align: center;
        box-shadow: 0 4px 30px rgba(0,0,0,0.2);
        font-family: inherit;
    }
    .jsec-btn:hover {
        background: rgba(96,165,250,0.10); color: #FFFFFF;
        border-color: rgba(96,165,250,0.45);
        box-shadow: 0 0 15px rgba(96,165,250,0.15);
    }

    /* ---- the sidebar ---- */
    .jsec-scrim {
        position: fixed; inset: 0; z-index: 9000;
        background: rgba(0,0,0,0.55);
        backdrop-filter: blur(2px);
        opacity: 0; pointer-events: none; transition: opacity .25s ease;
    }
    .jsec-scrim.open { opacity: 1; pointer-events: auto; }

    .jsec-sidebar {
        position: fixed; top: 0; right: 0; bottom: 0; width: 380px; z-index: 9001;
        background: rgba(10,12,16,0.92);
        backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
        border-left: 1px solid rgba(255,255,255,0.10);
        transform: translateX(100%); transition: transform .3s cubic-bezier(0.4,0,0.2,1);
        display: flex; flex-direction: column;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #E2E8F0;
    }
    .jsec-sidebar.open { transform: translateX(0); }

    .jsec-head {
        flex: 0 0 auto; padding: 22px 22px 16px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        display: flex; align-items: center; justify-content: space-between;
    }
    .jsec-title {
        font-size: 12px; font-weight: 700; letter-spacing: 2px;
        text-transform: uppercase; color: #FFFFFF;
    }
    .jsec-close {
        background: none; border: none; color: #6B7280; cursor: pointer;
        font-size: 20px; line-height: 1; padding: 4px 8px; border-radius: 6px;
    }
    .jsec-close:hover { color: #FFFFFF; background: rgba(255,255,255,0.06); }

    /* Every section holds one block of the sidebar: however many there are,
       they divide the height evenly between them. */
    .jsec-list {
        flex: 1 1 auto; min-height: 0;
        display: flex; flex-direction: column;
        padding: 14px; gap: 10px;
    }
    .jsec-block {
        flex: 1 1 0; min-height: 0; overflow: hidden;
        background: rgba(255,255,255,0.025);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px; padding: 14px 16px; cursor: pointer;
        transition: all .2s ease; position: relative;
        display: flex; flex-direction: column; justify-content: center;
    }
    .jsec-block:hover {
        background: rgba(96,165,250,0.08);
        border-color: rgba(96,165,250,0.45);
        transform: translateX(-2px);
    }
    .jsec-block-name {
        font-size: 14px; font-weight: 600; color: #FFFFFF;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .jsec-block-brief {
        font-size: 11px; color: #9CA3AF; margin-top: 6px; line-height: 1.5;
        overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
    }
    .jsec-block-meta {
        font-size: 10px; color: #4B5563; margin-top: 8px;
        letter-spacing: 1px; text-transform: uppercase;
        display: flex; align-items: center; gap: 8px;
    }
    .jsec-live {
        width: 6px; height: 6px; border-radius: 50%; background: #34D399;
        box-shadow: 0 0 8px #34D399; animation: jsec-pulse 1.6s infinite;
    }
    @keyframes jsec-pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }

    .jsec-empty {
        flex: 1 1 auto; display: flex; align-items: center; justify-content: center;
        text-align: center; font-size: 12px; color: #4B5563; padding: 30px; line-height: 1.7;
    }

    /* ---- the create window ---- */
    .jsec-modal {
        position: fixed; inset: 0; z-index: 9100;
        display: none; align-items: center; justify-content: center;
        background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .jsec-modal.open { display: flex; }
    .jsec-panel {
        width: min(900px, 92vw); max-height: 88vh; display: flex; flex-direction: column;
        background: rgba(12,14,18,0.97);
        border: 1px solid rgba(255,255,255,0.12); border-radius: 14px;
        box-shadow: 0 24px 80px rgba(0,0,0,0.6); color: #E2E8F0;
    }
    .jsec-panel-head { padding: 24px 28px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); }
    .jsec-panel-head h2 {
        margin: 0; font-size: 15px; font-weight: 700; letter-spacing: 1.5px;
        text-transform: uppercase; color: #FFFFFF;
    }
    .jsec-panel-head p { margin: 8px 0 0; font-size: 12px; color: #9CA3AF; line-height: 1.6; }
    .jsec-panel-body { padding: 20px 28px; overflow-y: auto; flex: 1 1 auto; }
    .jsec-label {
        display: block; font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
        text-transform: uppercase; color: #6B7280; margin: 0 0 8px;
    }
    .jsec-input, .jsec-area {
        width: 100%; box-sizing: border-box;
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.10); border-radius: 8px;
        color: #E2E8F0; font-family: inherit; font-size: 13px; padding: 12px 14px;
        outline: none; transition: border-color .2s ease;
    }
    .jsec-input:focus, .jsec-area:focus { border-color: rgba(96,165,250,0.6); }
    .jsec-area { min-height: 200px; resize: vertical; line-height: 1.7; }

    .jsec-drop {
        margin-top: 8px; border: 1px dashed rgba(255,255,255,0.16); border-radius: 10px;
        padding: 22px; text-align: center; font-size: 12px; color: #6B7280;
        cursor: pointer; transition: all .2s ease;
    }
    .jsec-drop:hover, .jsec-drop.over {
        border-color: rgba(96,165,250,0.6); color: #93C5FD;
        background: rgba(96,165,250,0.05);
    }
    .jsec-files { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .jsec-chip {
        background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
        border-radius: 20px; padding: 6px 12px; font-size: 11px; color: #CBD5E1;
        display: flex; align-items: center; gap: 8px;
    }
    .jsec-chip button {
        background: none; border: none; color: #6B7280; cursor: pointer;
        font-size: 14px; line-height: 1; padding: 0;
    }
    .jsec-chip button:hover { color: #FB7185; }

    .jsec-panel-foot {
        padding: 16px 28px 22px; border-top: 1px solid rgba(255,255,255,0.08);
        display: flex; justify-content: flex-end; gap: 10px; align-items: center;
    }
    .jsec-action {
        background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.12);
        color: #9CA3AF; font-family: inherit; font-size: 11px; font-weight: 600;
        letter-spacing: 1.2px; text-transform: uppercase;
        padding: 11px 22px; border-radius: 8px; cursor: pointer; transition: all .2s ease;
    }
    .jsec-action:hover { color: #FFFFFF; border-color: rgba(255,255,255,0.3); }
    .jsec-action.primary {
        background: rgba(96,165,250,0.14); border-color: rgba(96,165,250,0.5); color: #93C5FD;
    }
    .jsec-action.primary:hover {
        background: rgba(96,165,250,0.25); color: #FFFFFF;
        box-shadow: 0 0 18px rgba(96,165,250,0.25);
    }
    .jsec-action.ghost { border-color: rgba(255,255,255,0.08); color: #6B7280; }
    .jsec-action.danger { color: #9CA3AF; }
    .jsec-action.danger:hover { color: #FB7185; border-color: rgba(251,113,133,0.45); }
    .jsec-action:disabled { opacity: .45; cursor: not-allowed; }
    .jsec-status { flex: 1 1 auto; font-size: 11px; color: #6B7280; }

    /* ---- the clarification stages inside the create window ---- */
    .jsec-counter {
        font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
        text-transform: uppercase; color: #60A5FA; margin-bottom: 14px;
    }
    .jsec-question { font-size: 17px; line-height: 1.6; color: #FFFFFF; margin-bottom: 18px; }
    .jsec-note {
        font-size: 12px; line-height: 1.7; color: #9CA3AF;
        background: rgba(251,191,36,0.06); border: 1px solid rgba(251,191,36,0.20);
        border-radius: 8px; padding: 12px 14px; margin: 0 0 16px;
    }
    .jsec-editor { min-height: 320px; }
    .jsec-waiting {
        display: flex; align-items: center; justify-content: center;
        min-height: 220px; font-size: 13px; color: #9CA3AF; text-align: center;
    }
    .jsec-fade { animation: jsec-fade-in .28s ease; }
    @keyframes jsec-fade-in {
        from { opacity: 0; transform: translateY(6px); }
        to { opacity: 1; transform: none; }
    }

    /* The brief is a document you read, not source you should have to parse. */
    .jsec-prose { font-size: 13.5px; line-height: 1.8; color: #CBD5E1; }
    .jsec-prose h3, .jsec-prose h4, .jsec-prose h5, .jsec-prose h6 {
        margin: 22px 0 10px; color: #FFFFFF; font-size: 14px;
        letter-spacing: .5px; font-weight: 700;
    }
    .jsec-prose h3:first-child { margin-top: 0; }
    .jsec-prose p { margin: 0 0 12px; }
    .jsec-prose ul { margin: 0 0 14px; padding-left: 22px; }
    .jsec-prose li { margin-bottom: 6px; }
    .jsec-prose strong { color: #F3F4F6; }
    .jsec-prose code {
        background: rgba(255,255,255,0.06); border-radius: 4px;
        padding: 1px 5px; font-family: 'SF Mono', Consolas, monospace; font-size: 12px;
    }
    .jsec-wikilink { color: #93C5FD; }

    /* ---- the crew stage: the standing agents, before anything is created ---- */
    .jsec-crew-note {
        border: 1px dashed rgba(255,255,255,0.14); border-radius: 10px;
        padding: 14px 16px; font-size: 12px; color: #9CA3AF;
        line-height: 1.8; margin-bottom: 16px;
    }
    .jsec-crew-note b { color: #93C5FD; font-weight: 600; }
    .jsec-dept {
        border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
        margin-bottom: 14px; overflow: hidden; background: rgba(255,255,255,0.015);
    }
    .jsec-dept-head {
        display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .jsec-dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; margin-top: 4px; }
    .jsec-grow { flex: 1 1 auto; min-width: 0; }
    .jsec-dept-name {
        font-size: 12px; font-weight: 700; letter-spacing: 2.5px;
        text-transform: uppercase; color: #FFFFFF;
    }
    .jsec-dept-goal { font-size: 11.5px; color: #9CA3AF; margin-top: 5px; line-height: 1.6; }
    .jsec-src {
        font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
        padding: 4px 9px; border-radius: 5px; flex: 0 0 auto; white-space: nowrap;
    }
    .jsec-src.founding { background: rgba(167,139,250,0.14); color: #A78BFA; }
    .jsec-src.merged { background: rgba(252,211,77,0.13); color: #FCD34D; }
    .jsec-src.brief { background: rgba(96,165,250,0.14); color: #93C5FD; }
    .jsec-ag {
        display: flex; gap: 12px; padding: 13px 16px;
        border-top: 1px solid rgba(255,255,255,0.045);
    }
    .jsec-ag:first-of-type { border-top: none; }
    .jsec-tag {
        font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
        border-radius: 4px; padding: 3px 7px; height: fit-content; margin-top: 2px;
        flex: 0 0 auto;
    }
    .jsec-tag.lead { background: #EAE6EE; color: #0A0D13; }
    .jsec-tag.adv { border: 1px solid rgba(255,255,255,0.14); color: #6B7280; }
    .jsec-role { font-size: 13px; color: #FFFFFF; font-weight: 600; }
    .jsec-ag-brief { font-size: 11.5px; color: #9CA3AF; line-height: 1.65; margin-top: 5px; }
    .jsec-why { font-size: 10px; color: #5b6879; line-height: 1.6; margin-top: 6px; }
    .jsec-why code {
        background: rgba(255,255,255,0.05); border-radius: 3px;
        padding: 1px 4px; color: #93C5FD; font-size: 9.5px;
    }
    .jsec-acts { flex: 0 0 auto; display: flex; gap: 6px; align-items: flex-start; }
    .jsec-tiny {
        background: transparent; border: 1px solid rgba(255,255,255,0.10); color: #6B7280;
        font-family: inherit; font-size: 9px; font-weight: 600; letter-spacing: .8px;
        text-transform: uppercase; padding: 5px 9px; border-radius: 5px; cursor: pointer;
        transition: all .2s ease;
    }
    .jsec-tiny:hover { color: #FFFFFF; border-color: rgba(255,255,255,0.3); }
    .jsec-tiny.warn:hover { color: #F87171; border-color: rgba(248,113,113,0.5); }
    .jsec-addrow { padding: 11px 16px; border-top: 1px solid rgba(255,255,255,0.045); }
    .jsec-field {
        width: 100%; background: rgba(0,0,0,0.35); border: 1px solid rgba(96,165,250,0.4);
        border-radius: 7px; color: #E2E8F0; font-family: inherit; font-size: 12.5px;
        padding: 9px 11px; outline: none; margin-bottom: 8px;
    }
    textarea.jsec-field { min-height: 78px; resize: vertical; line-height: 1.65; }
    .jsec-empty-crew { font-size: 12.5px; color: #6B7280; line-height: 1.8; }

    /* ---- the section information overlay ---- */
    .jsec-info {
        position: fixed; inset: 0; z-index: 9050;
        display: none; align-items: center; justify-content: center;
        background: rgba(0,0,0,0.72); backdrop-filter: blur(4px);
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .jsec-info.open { display: flex; }
    .jsec-info-panel {
        width: min(1500px, 94vw); height: 90vh;
        display: flex; flex-direction: column; overflow: hidden;
        background: rgba(8,10,14,0.98);
        border: 1px solid rgba(255,255,255,0.12); border-radius: 14px;
        box-shadow: 0 24px 90px rgba(0,0,0,0.7);
    }
    .jsec-info-head {
        flex: 0 0 auto; padding: 16px 22px;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        display: flex; align-items: center; justify-content: space-between;
        color: #E2E8F0;
    }
    .jsec-info-title {
        font-size: 12px; font-weight: 700; letter-spacing: 2px;
        text-transform: uppercase; color: #FFFFFF;
    }
    /* The dashboard is embedded rather than reimplemented, so there is exactly
       one copy of it to maintain. */
    .jsec-info iframe {
        flex: 1 1 auto; width: 100%; border: 0; background: #05070A;
    }
    `;

    function injectStyle() {
        if (document.getElementById("jsec-style")) return;
        var el = document.createElement("style");
        el.id = "jsec-style";
        el.textContent = STYLE;
        document.head.appendChild(el);
    }

    function esc(text) {
        return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // ---- sidebar -----------------------------------------------------------

    var scrim, sidebar, listEl;

    function buildSidebar() {
        if (sidebar) return;

        scrim = document.createElement("div");
        scrim.className = "jsec-scrim";
        scrim.addEventListener("click", closeSidebar);

        sidebar = document.createElement("div");
        sidebar.className = "jsec-sidebar";
        sidebar.innerHTML =
            '<div class="jsec-head">' +
                '<span class="jsec-title">Sections</span>' +
                '<button class="jsec-close" title="Close">&times;</button>' +
            '</div>' +
            '<div class="jsec-list"></div>';
        sidebar.querySelector(".jsec-close").addEventListener("click", closeSidebar);

        document.body.appendChild(scrim);
        document.body.appendChild(sidebar);
        listEl = sidebar.querySelector(".jsec-list");
    }

    function renderSections(sections) {
        if (!sections.length) {
            listEl.innerHTML =
                '<div class="jsec-empty">No sections yet.<br>' +
                'Finish a pipeline, then turn it into one from the Plan page.</div>';
            return;
        }
        listEl.innerHTML = "";
        sections.forEach(function (s) {
            var block = document.createElement("div");
            block.className = "jsec-block";
            block.innerHTML =
                '<div class="jsec-block-name">' + esc(s.name) + '</div>' +
                (s.brief ? '<div class="jsec-block-brief">' + esc(s.brief) + '</div>' : '') +
                '<div class="jsec-block-meta">' +
                    (s.running ? '<span class="jsec-live"></span>' : '') +
                    '<span>' + s.pipeline_count +
                    (s.pipeline_count === 1 ? ' pipeline' : ' pipelines') + '</span>' +
                '</div>';
            block.addEventListener("click", function () { enterSection(s); });
            listEl.appendChild(block);
        });
    }

    // Opening a section shows it the way execution mode shows a pipeline: the
    // section's own work, on the page that already knows how to draw it.
    function enterSection(section) {
        var target = section.latest_plan_id || section.founding_plan_id;
        if (target) {
            try { localStorage.setItem("jarvis_active_pipeline_id", target); } catch (e) {}
        }
        fetch("/sections/" + encodeURIComponent(section.id) + "/enter", { method: "POST" })
            .catch(function () {})
            .then(function () {
                window.location.href = "execution.html?section=" + encodeURIComponent(section.id);
            });
    }

    var infoEl;

    function openInfo(sectionId) {
        injectStyle();
        sectionId = sectionId || currentSectionId();
        if (!sectionId) return;

        if (!infoEl) {
            infoEl = document.createElement("div");
            infoEl.className = "jsec-info";
            infoEl.innerHTML =
                '<div class="jsec-info-panel">' +
                    '<div class="jsec-info-head">' +
                        '<span class="jsec-info-title">Section Information</span>' +
                        '<button class="jsec-close" title="Close">&times;</button>' +
                    '</div>' +
                    '<iframe title="Section information"></iframe>' +
                '</div>';
            infoEl.querySelector(".jsec-close").addEventListener("click", closeInfo);
            infoEl.addEventListener("click", function (e) {
                if (e.target === infoEl) closeInfo();
            });
            document.body.appendChild(infoEl);
        }

        // Loaded fresh each time so the tasks, notes and chat are current.
        infoEl.querySelector("iframe").src =
            "section.html?embed=1&id=" + encodeURIComponent(sectionId);
        infoEl.classList.add("open");
    }

    function closeInfo() {
        if (!infoEl) return;
        infoEl.classList.remove("open");
        // Drop the document so its polling and audio stop with the panel.
        infoEl.querySelector("iframe").src = "about:blank";
    }

    function openSidebar() {
        injectStyle();
        buildSidebar();
        scrim.classList.add("open");
        sidebar.classList.add("open");
        listEl.innerHTML = '<div class="jsec-empty">Loading&hellip;</div>';
        fetch("/sections")
            .then(function (r) { return r.json(); })
            .then(function (d) { renderSections(d.sections || []); })
            .catch(function () {
                listEl.innerHTML = '<div class="jsec-empty">Could not reach Jarvis.</div>';
            });
    }

    function closeSidebar() {
        if (!sidebar) return;
        scrim.classList.remove("open");
        sidebar.classList.remove("open");
    }

    // ---- the "make this a section" window -----------------------------------
    //
    // The same gate a pipeline goes through, for the same reason: a section is a
    // commitment, and what it is FOR is not something the founding pipeline can
    // tell you. You write the brief and drop the files, Jarvis asks only what it
    // genuinely does not know, then paints the section brief back for you to
    // correct. "Create section" is still the only thing that creates anything —
    // and until it is pressed, cancelling deletes even the files you dropped.

    var modal, pendingFiles = [], draft = null;

    function buildModal() {
        if (modal) return;
        modal = document.createElement("div");
        modal.className = "jsec-modal";
        modal.innerHTML =
            '<div class="jsec-panel">' +
                '<div class="jsec-panel-head">' +
                    '<h2 id="jsec-head-title">Make this a section</h2>' +
                    '<p id="jsec-head-sub"></p>' +
                '</div>' +
                '<div class="jsec-panel-body" id="jsec-panel-body"></div>' +
                '<div class="jsec-panel-foot" id="jsec-panel-foot"></div>' +
            '</div>';
        document.body.appendChild(modal);
        modal.addEventListener("click", function (e) { if (e.target === modal) cancelDraft(); });
    }

    function panelBody() { return modal.querySelector("#jsec-panel-body"); }
    function panelFoot() { return modal.querySelector("#jsec-panel-foot"); }

    function setHead(title, sub) {
        modal.querySelector("#jsec-head-title").textContent = title;
        modal.querySelector("#jsec-head-sub").textContent = sub;
    }

    function footButton(label, kind, onClick) {
        var b = document.createElement("button");
        b.className = "jsec-action" + (kind ? " " + kind : "");
        b.textContent = label;
        b.addEventListener("click", onClick);
        return b;
    }

    function setFooter(buttons) {
        var foot = panelFoot();
        foot.innerHTML = "";
        var status = document.createElement("span");
        status.className = "jsec-status";
        status.id = "jsec-status";
        foot.appendChild(status);
        buttons.forEach(function (b) { foot.appendChild(b); });
    }

    // Used where the screen stays put while a call is in flight, so the same
    // press cannot be made twice.
    function lockFooter(message) {
        Array.prototype.forEach.call(panelFoot().querySelectorAll("button"), function (b) {
            b.disabled = true;
        });
        setStatus(message || "");
    }

    function unlockFooter() {
        Array.prototype.forEach.call(panelFoot().querySelectorAll("button"), function (b) {
            b.disabled = false;
        });
    }

    function setStatus(text) {
        var el = modal.querySelector("#jsec-status");
        if (el) el.textContent = text || "";
    }

    // One place that says "wait": the buttons go, so nothing can be pressed
    // twice while a model call is in flight.
    function setBusy(message) {
        draft.busy = true;
        panelBody().innerHTML = '<div class="jsec-waiting">' + esc(message) + "</div>";
        panelFoot().innerHTML = "";
    }

    function renderMarkdown(text) {
        var lines = esc(text || "").split("\n");
        var out = [], inList = false;
        var inline = function (s) {
            return s
                .replace(/\[\[([^\]]+)\]\]/g, '<span class="jsec-wikilink">$1</span>')
                .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
                .replace(/`([^`]+)`/g, "<code>$1</code>");
        };
        lines.forEach(function (raw) {
            var line = raw.trim();
            var bullet = line.match(/^[-*]\s+(.*)$/);
            if (bullet) {
                if (!inList) { out.push("<ul>"); inList = true; }
                out.push("<li>" + inline(bullet[1]) + "</li>");
                return;
            }
            if (inList) { out.push("</ul>"); inList = false; }
            if (!line) return;
            var head = line.match(/^(#{1,6})\s+(.*)$/);
            if (head) {
                var level = Math.min(head[1].length + 2, 6);
                out.push("<h" + level + ">" + inline(head[2]) + "</h" + level + ">");
                return;
            }
            out.push("<p>" + inline(line) + "</p>");
        });
        if (inList) out.push("</ul>");
        return out.join("");
    }

    function post(url, payload) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok) throw new Error(d.error || "Something went wrong.");
                return d;
            });
        });
    }

    // ---- stage 1: the brief and the files ----

    function renderBriefStage() {
        draft.busy = false;
        setHead("Make this a section",
            "Tell Jarvis what this section is about — what it is for and where you are " +
            "taking it. The pipeline’s own work comes with it; this is what frames it. " +
            "Jarvis will ask about anything it still needs before writing it up.");

        panelBody().innerHTML =
            '<label class="jsec-label" for="jsec-name">Section name</label>' +
            '<input class="jsec-input" id="jsec-name" autocomplete="off">' +
            '<label class="jsec-label" style="margin-top:20px" for="jsec-brief">Section brief</label>' +
            '<textarea class="jsec-area" id="jsec-brief" placeholder="What is this section about?"></textarea>' +
            '<label class="jsec-label" style="margin-top:20px">Files</label>' +
            '<div class="jsec-drop" id="jsec-drop">Drop files here, or click to choose</div>' +
            '<input type="file" id="jsec-file-input" multiple style="display:none">' +
            '<div class="jsec-files" id="jsec-files"></div>';

        modal.querySelector("#jsec-name").value = draft.name || "";
        modal.querySelector("#jsec-brief").value = draft.brief || "";
        wireDropZone();
        renderFiles();

        setFooter([
            footButton("Cancel", "danger", cancelDraft),
            footButton("Create section without questions", "ghost", function () {
                readBriefStage();
                createSection();
            }),
            footButton("Continue", "primary", continueToQuestions)
        ]);
        setTimeout(function () { modal.querySelector("#jsec-brief").focus(); }, 50);
    }

    function readBriefStage() {
        var name = modal.querySelector("#jsec-name");
        var brief = modal.querySelector("#jsec-brief");
        if (name) draft.name = name.value.trim();
        if (brief) draft.brief = brief.value.trim();
    }

    function wireDropZone() {
        var drop = modal.querySelector("#jsec-drop");
        var fileInput = modal.querySelector("#jsec-file-input");
        drop.addEventListener("click", function () { fileInput.click(); });
        fileInput.addEventListener("change", function () {
            addFiles(fileInput.files);
            fileInput.value = "";
        });
        ["dragenter", "dragover"].forEach(function (ev) {
            drop.addEventListener(ev, function (e) {
                e.preventDefault(); drop.classList.add("over");
            });
        });
        ["dragleave", "drop"].forEach(function (ev) {
            drop.addEventListener(ev, function (e) {
                e.preventDefault(); drop.classList.remove("over");
            });
        });
        drop.addEventListener("drop", function (e) {
            if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
        });
    }

    function addFiles(fileList) {
        Array.prototype.forEach.call(fileList, function (f) { pendingFiles.push(f); });
        renderFiles();
    }

    function renderFiles() {
        var box = modal.querySelector("#jsec-files");
        if (!box) return;
        box.innerHTML = "";
        pendingFiles.forEach(function (f, i) {
            var chip = document.createElement("div");
            chip.className = "jsec-chip";
            chip.innerHTML = '<span>' + esc(f.name) + '</span><button title="Remove">&times;</button>';
            chip.querySelector("button").addEventListener("click", function () {
                pendingFiles.splice(i, 1);
                renderFiles();
            });
            box.appendChild(chip);
        });
    }

    // ---- stage 2: the questions ----

    function continueToQuestions() {
        readBriefStage();
        setBusy("Reading the pipeline’s work…");

        startDraft()
            .then(uploadPendingFiles)
            .then(function () {
                return post("/sections/intake/questions", {
                    draft_id: draft.draftId,
                    name: draft.name,
                    brief: draft.brief
                });
            })
            .then(function (data) {
                var questions = data.questions || [];
                if (!questions.length) return paintBrief();
                draft.questions = questions;
                draft.index = 0;
                draft.batchDone = false;
                renderQuestion();
            })
            .catch(function (e) {
                renderBriefStage();
                setStatus(e.message || "Could not reach Jarvis. You can create it anyway.");
            });
    }

    // The draft is what makes the questions possible: it is where the answers
    // and the dropped files live before there is any section to hold them.
    function startDraft() {
        if (draft.draftId) return Promise.resolve();
        return post("/sections/intake/start", {
            plan_id: draft.planId,
            name: draft.name,
            brief: draft.brief
        }).then(function (data) {
            draft.draftId = data.draft_id;
            if (!draft.name) draft.name = data.name || "";
        });
    }

    // Uploaded once, when the questions begin, so Jarvis can actually read them
    // before asking. Cancelling deletes exactly these files again.
    function uploadPendingFiles() {
        if (!pendingFiles.length || draft.filesUploaded) return Promise.resolve();
        var form = new FormData();
        form.append("draft_id", draft.draftId);
        pendingFiles.forEach(function (f) { form.append("files", f); });
        return fetch("/sections/intake/upload", { method: "POST", body: form })
            .then(function () { draft.filesUploaded = true; })
            .catch(function () { /* the questions matter more than the drops */ });
    }

    function renderQuestion() {
        draft.busy = false;
        var q = draft.questions[draft.index];
        setHead(draft.name || "Make this a section",
                "One question at a time. Answer it and press Continue.");

        panelBody().innerHTML =
            '<div class="jsec-fade">' +
                '<div class="jsec-counter">Question ' + (draft.index + 1) +
                    " of " + draft.questions.length + "</div>" +
                '<div class="jsec-question">' + esc(q.question) + "</div>" +
                '<textarea class="jsec-area" id="jsec-answer" placeholder="Your answer…"></textarea>' +
            "</div>";

        setFooter([
            footButton("Cancel", "danger", cancelDraft),
            footButton("Skip the rest", "ghost", skipQuestions),
            footButton("Continue", "primary", submitAnswer)
        ]);

        // Jarvis reads it aloud: verbatim when short, the gist when long. The
        // full text stays on screen either way.
        var spoken = q.question.length <= 140 ? q.question : (q.gist || q.question);
        fetch("/jarvis/say", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: spoken })
        }).catch(function () { /* silence is fine, the text is on screen */ });

        setTimeout(function () { modal.querySelector("#jsec-answer").focus(); }, 50);
    }

    function submitAnswer() {
        var answerEl = modal.querySelector("#jsec-answer");
        var q = draft.questions[draft.index];
        // The answer is in flight until the next question is drawn: a second
        // press before then would file this same question twice.
        lockFooter("Saving…");
        post("/sections/intake/answer", {
            draft_id: draft.draftId,
            question: q.question,
            answer: answerEl ? answerEl.value : ""
        })
        .then(function () {
            if (draft.index + 1 < draft.questions.length) {
                draft.index += 1;
                renderQuestion();
            } else {
                renderBatchDone();
            }
        })
        .catch(function (e) {
            unlockFooter();
            setStatus(e.message || "Could not save that answer.");
        });
    }

    function renderBatchDone() {
        draft.busy = false;
        setHead(draft.name || "Make this a section", "That is everything I had to ask for now.");
        panelBody().innerHTML =
            '<div class="jsec-fade">' +
                '<div class="jsec-counter">All answered</div>' +
                '<div class="jsec-question">I will write down what this section is.</div>' +
                '<p class="jsec-note">If anything is still unclear I will ask more questions ' +
                'first; otherwise you get the section brief to correct before anything is ' +
                'created.</p>' +
            "</div>";
        setFooter([
            footButton("Cancel", "danger", cancelDraft),
            footButton("Write the section brief", "primary", paintBrief)
        ]);
    }

    function skipQuestions() {
        post("/sections/intake/skip", { draft_id: draft.draftId })
            .catch(function () { /* the write-up below is what matters */ })
            .then(paintBrief);
    }

    // ---- stage 3: the section brief ----

    function paintBrief() {
        setBusy("Working through what you told me…");
        return post("/sections/intake/picture", { draft_id: draft.draftId })
            .then(function (data) {
                if (data.questions && data.questions.length) {
                    draft.questions = data.questions;
                    draft.index = 0;
                    renderQuestion();
                    return;
                }
                draft.briefText = data.brief_text || "";
                draft.degraded = data.degraded || "";
                draft.editing = false;
                renderBriefText();
            })
            .catch(function (e) {
                renderBriefStage();
                setStatus(e.message || "Could not write that up. You can create it anyway.");
            });
    }

    function renderBriefText() {
        draft.busy = false;
        setHead(draft.name || "Make this a section", draft.editing
            ? "Edit it freely. Nothing is created until you press Create section."
            : "This is what the section will say it is for. Correct anything that is wrong.");

        var note = draft.degraded
            ? '<div class="jsec-note">' + esc(draft.degraded) + "</div>"
            : "";

        if (draft.editing) {
            panelBody().innerHTML = note +
                '<textarea class="jsec-area jsec-editor" id="jsec-editor"></textarea>';
            modal.querySelector("#jsec-editor").value = draft.briefText;
            setFooter([
                footButton("Cancel", "danger", cancelDraft),
                footButton("Discard changes", "ghost", function () {
                    draft.editing = false;
                    renderBriefText();
                }),
                footButton("Save changes", "primary", saveEdit)
            ]);
            setTimeout(function () { modal.querySelector("#jsec-editor").focus(); }, 50);
            return;
        }

        panelBody().innerHTML = note +
            '<div class="jsec-prose jsec-fade">' + renderMarkdown(draft.briefText) + "</div>";
        setFooter([
            footButton("Cancel", "danger", cancelDraft),
            footButton("Edit", "ghost", function () {
                draft.editing = true;
                renderBriefText();
            }),
            footButton("Continue", "primary", proposeCrew)
        ]);
    }

    function saveEdit() {
        var edited = modal.querySelector("#jsec-editor").value;
        if (!edited.trim()) {
            setStatus("The brief cannot be empty.");
            return;
        }
        setBusy("Tidying your words, changing none of them…");
        post("/sections/intake/edit", { draft_id: draft.draftId, edited_text: edited })
            .then(function (data) { draft.briefText = data.brief_text || edited; })
            .catch(function () { draft.briefText = edited; })
            .then(function () {
                draft.editing = false;
                draft.degraded = "";
                renderBriefText();
            });
    }

    // ---- stage 4: the crew ----
    //
    // The last thing before the commit point, and the only stage that could not
    // have run earlier: who this section keeps depends on what the user just
    // said it is for. Every agent shown carries where it came from, because a
    // roster you cannot check is a roster you cannot trust.

    var CREW_COLORS = ["#22D3EE", "#60A5FA", "#34D399", "#F87171",
                       "#FB7185", "#FCD34D", "#A78BFA", "#818CF8"];

    function proposeCrew() {
        setBusy("Working out who this section keeps…");
        return post("/sections/intake/crew", {
            draft_id: draft.draftId,
            brief_text: draft.briefText
        })
        .then(function (data) {
            draft.crew = data.crew || { departments: [] };
            draft.crewDegraded = data.degraded || "";
            draft.editing = null;
            renderCrewStage();
        })
        .catch(function (e) {
            // The crew is not worth losing the section over: the server builds
            // one from the pipeline's own cycles when none is handed to it.
            draft.crew = { departments: [] };
            draft.crewDegraded = (e.message || "Jarvis could not plan the crew.") +
                " Creating the section will stand up the founding pipeline's own cycles instead.";
            renderCrewStage();
        });
    }

    function crewCounts() {
        var depts = (draft.crew && draft.crew.departments) || [];
        var agents = 0;
        depts.forEach(function (d) { agents += (d.agents || []).length; });
        return depts.length + (depts.length === 1 ? " baby section · " : " baby sections · ") +
               agents + (agents === 1 ? " agent" : " agents");
    }

    function renderCrewStage() {
        draft.busy = false;
        var depts = (draft.crew && draft.crew.departments) || [];
        setHead(draft.name || "Make this a section",
                "The standing agents of this section. Every pipeline started here begins " +
                "from them — cut anything that does not earn its place.");

        var html = "";
        if (draft.crewDegraded) html += '<div class="jsec-note">' + esc(draft.crewDegraded) + "</div>";

        html += '<div class="jsec-crew-note">Every agent below is traceable. ' +
            '<b>Founding</b> means the pipeline really ran it and its findings are on disk. ' +
            '<b>Merged</b> means two near-duplicate roles were folded into one. ' +
            '<b>New</b> means it exists because of what you wrote, and nothing on disk ' +
            'covers it yet. Nothing here was invented from the section’s name.</div>';

        if (!depts.length) {
            html += '<div class="jsec-empty-crew">No crew planned. Creating the section will ' +
                    'stand up the founding pipeline’s own cycles and agents.</div>';
        }

        depts.forEach(function (dept, di) {
            var color = CREW_COLORS[di % CREW_COLORS.length];
            html += '<div class="jsec-dept">' +
                '<div class="jsec-dept-head">' +
                    '<span class="jsec-dot" style="background:' + color +
                        ';box-shadow:0 0 12px ' + color + '"></span>' +
                    '<div class="jsec-grow">' + deptHeadHtml(dept, di) + "</div>" +
                    '<span class="jsec-src ' + esc(dept.origin || "brief") + '">' +
                        esc(originLabel(dept.origin)) + "</span>" +
                    '<div class="jsec-acts">' +
                        '<button class="jsec-tiny" data-act="dept-edit" data-d="' + di + '">Rename</button>' +
                        '<button class="jsec-tiny warn" data-act="dept-drop" data-d="' + di + '">Drop</button>' +
                    "</div>" +
                "</div>";

            (dept.agents || []).forEach(function (agent, ai) {
                html += '<div class="jsec-ag">' +
                    '<span class="jsec-tag ' + (agent.is_lead ? "lead" : "adv") + '">' +
                        (agent.is_lead ? "Lead" : "Adv") + "</span>" +
                    '<div class="jsec-grow">' + agentHtml(agent, di, ai) + "</div>" +
                    '<div class="jsec-acts">' +
                        '<button class="jsec-tiny" data-act="ag-edit" data-d="' + di +
                            '" data-a="' + ai + '">Edit</button>' +
                        '<button class="jsec-tiny warn" data-act="ag-drop" data-d="' + di +
                            '" data-a="' + ai + '">Drop</button>' +
                    "</div>" +
                "</div>";
            });

            html += '<div class="jsec-addrow">' +
                '<button class="jsec-tiny" data-act="ag-add" data-d="' + di +
                '">+ Add an agent here</button></div></div>';
        });

        panelBody().innerHTML = '<div class="jsec-fade">' + html + "</div>";
        wireCrewButtons();

        setFooter([
            footButton("Cancel", "danger", cancelDraft),
            footButton("Back to the brief", "ghost", function () {
                draft.editing = false;
                renderBriefText();
            }),
            footButton("Create section", "primary", createSection)
        ]);
        setStatus(crewCounts());
    }

    function originLabel(origin) {
        if (origin === "founding") return "Founding";
        if (origin === "merged") return "Merged";
        return "New";
    }

    function editing(kind, di, ai) {
        var e = draft.editing;
        return e && e.kind === kind && e.d === di && (ai === undefined || e.a === ai);
    }

    function deptHeadHtml(dept, di) {
        if (editing("dept", di)) {
            return '<input class="jsec-field" id="jsec-dept-name" value="' +
                       esc(dept.domain) + '" placeholder="What this baby section is about">' +
                   '<textarea class="jsec-field" id="jsec-dept-goal" ' +
                       'placeholder="What it is for, in one line">' + esc(dept.goal || "") +
                   "</textarea>" +
                   '<button class="jsec-tiny" data-act="save" data-d="' + di + '">Save</button> ' +
                   '<button class="jsec-tiny" data-act="cancel-edit">Cancel</button>';
        }
        return '<div class="jsec-dept-name">' + esc(dept.domain) + "</div>" +
               (dept.goal ? '<div class="jsec-dept-goal">' + esc(dept.goal) + "</div>" : "");
    }

    function agentHtml(agent, di, ai) {
        if (editing("ag", di, ai)) {
            return '<input class="jsec-field" id="jsec-ag-role" value="' + esc(agent.role) +
                       '" placeholder="One role, one job — e.g. Quantization Expert">' +
                   '<textarea class="jsec-field" id="jsec-ag-brief" ' +
                       'placeholder="What this agent owns in this section, for good">' +
                       esc(agent.brief || "") + "</textarea>" +
                   '<button class="jsec-tiny" data-act="save" data-d="' + di +
                       '" data-a="' + ai + '">Save</button> ' +
                   '<button class="jsec-tiny" data-act="cancel-edit">Cancel</button>';
        }
        var why = agent.why ? '<div class="jsec-why">' + renderWhy(agent.why) + "</div>" : "";
        return '<div class="jsec-role">' + esc(agent.role) + "</div>" +
               '<div class="jsec-ag-brief">' + esc(agent.brief || "") + "</div>" + why;
    }

    // Provenance names real agent_ids, so `backticks` are rendered as code.
    function renderWhy(text) {
        return esc(text).replace(/`([^`]+)`/g, "<code>$1</code>");
    }

    function wireCrewButtons() {
        Array.prototype.forEach.call(panelBody().querySelectorAll("[data-act]"), function (btn) {
            btn.addEventListener("click", function () {
                var di = parseInt(btn.getAttribute("data-d"), 10);
                var ai = parseInt(btn.getAttribute("data-a"), 10);
                var depts = draft.crew.departments;
                switch (btn.getAttribute("data-act")) {
                    case "dept-edit": draft.editing = { kind: "dept", d: di }; break;
                    case "dept-drop": depts.splice(di, 1); draft.editing = null; break;
                    case "ag-edit": draft.editing = { kind: "ag", d: di, a: ai }; break;
                    case "ag-drop":
                        depts[di].agents.splice(ai, 1);
                        // A department with nobody in it is a label, not a baby
                        // section — the server drops it, so the screen does too.
                        if (!depts[di].agents.length) depts.splice(di, 1);
                        else if (!depts[di].agents.some(function (a) { return a.is_lead; })) {
                            depts[di].agents[0].is_lead = true;
                        }
                        draft.editing = null;
                        break;
                    case "ag-add":
                        depts[di].agents.push({ role: "", brief: "", is_lead: false,
                                                origin: "brief", from_agent_ids: [],
                                                why: "Added by you at creation." });
                        draft.editing = { kind: "ag", d: di, a: depts[di].agents.length - 1 };
                        break;
                    case "save": saveCrewEdit(di, ai); break;
                    case "cancel-edit": cancelCrewEdit(); break;
                }
                renderCrewStage();
            });
        });
    }

    function saveCrewEdit(di, ai) {
        var depts = draft.crew.departments;
        if (draft.editing && draft.editing.kind === "dept") {
            var name = panelBody().querySelector("#jsec-dept-name");
            var goal = panelBody().querySelector("#jsec-dept-goal");
            if (name && name.value.trim()) depts[di].domain = name.value.trim();
            if (goal) depts[di].goal = goal.value.trim();
        } else {
            var role = panelBody().querySelector("#jsec-ag-role");
            var brief = panelBody().querySelector("#jsec-ag-brief");
            var agent = depts[di].agents[ai];
            if (role) agent.role = role.value.trim();
            if (brief) agent.brief = brief.value.trim();
            // An agent with no role or no brief has no job; the server would
            // drop it anyway, so don't let it linger on screen pretending.
            if (!agent.role || !agent.brief) {
                depts[di].agents.splice(ai, 1);
                if (!depts[di].agents.length) depts.splice(di, 1);
            }
        }
        draft.editing = null;
    }

    function cancelCrewEdit() {
        // A row added and then abandoned was never a real agent.
        if (draft.editing && draft.editing.kind === "ag") {
            var agents = draft.crew.departments[draft.editing.d].agents;
            var agent = agents[draft.editing.a];
            if (agent && !agent.role && !agent.brief) {
                agents.splice(draft.editing.a, 1);
                if (!agents.length) draft.crew.departments.splice(draft.editing.d, 1);
            }
        }
        draft.editing = null;
    }

    // ---- the commit point ----

    function createSection() {
        var createdSection = null;
        var hadCrew = !!(draft.crew && (draft.crew.departments || []).length);
        setBusy("Creating the section…");

        // The crew as the user left it goes up first: the draft is what the
        // creation reads, and this is the last moment it can be corrected.
        var ready = (draft.draftId && draft.crew)
            ? post("/sections/intake/crew/set",
                   { draft_id: draft.draftId, crew: draft.crew }).catch(function () {})
            : Promise.resolve();

        ready
            .then(function () {
                return post("/sections/create", {
                    draft_id: draft.draftId || "",
                    plan_id: draft.planId,
                    name: draft.name,
                    brief: draft.brief
                });
            })
            .then(function (data) {
                createdSection = data.section;
                var id = createdSection.id;
                // Only the skip path still has files in hand: with a draft they
                // were uploaded before the questions.
                if (!pendingFiles.length || draft.filesUploaded) return id;
                var form = new FormData();
                pendingFiles.forEach(function (f) { form.append("files", f); });
                return fetch("/sections/" + id + "/upload", { method: "POST", body: form })
                    .then(function () { return id; });
            })
            .then(function (id) {
                closeModal();
                enterSection(createdSection || { id: id });
            })
            .catch(function (e) {
                if (hadCrew) renderCrewStage();
                else if (draft.briefText) renderBriefText();
                else renderBriefStage();
                setStatus(e.message || "Could not create the section.");
            });
    }

    // Cancel means it never happened: no section, and the files dropped along
    // the way are deleted from the pipeline's folder again.
    function cancelDraft() {
        if (draft && draft.busy) return;
        if (draft && draft.draftId) {
            post("/sections/intake/cancel", { draft_id: draft.draftId }).catch(function () {});
        }
        closeModal();
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove("open");
        pendingFiles = [];
        draft = null;
    }

    function createFrom(planId, suggestedName) {
        injectStyle();
        buildModal();
        pendingFiles = [];
        draft = {
            planId: planId,
            draftId: null,
            name: suggestedName || "",
            brief: "",
            questions: [],
            index: 0,
            briefText: "",
            degraded: "",
            editing: false,
            crew: null,
            crewDegraded: "",
            filesUploaded: false,
            busy: false
        };
        modal.classList.add("open");
        renderBriefStage();
    }

    // ---- wiring ------------------------------------------------------------

    // Wear the host page's own nav button class where there is one, so these sit
    // in the column at exactly the size the page uses. Hard-coding the metrics
    // here meant a 10px mismatch on the execution page.
    function navButtonClass() {
        return document.querySelector(".top-right-nav .nav-btn") ? "nav-btn" : "jsec-btn";
    }

    function mountButton() {
        injectStyle();
        // Embedded in the information overlay, this page is already inside a
        // section — its own "View Sections" button would just be noise.
        try {
            if (new URLSearchParams(window.location.search).get("embed") === "1") return;
        } catch (e) {}
        var nav = document.querySelector(".top-right-nav");
        if (!nav || document.getElementById("jsec-view-btn")) return;
        var btn = document.createElement("button");
        btn.className = navButtonClass();
        btn.id = "jsec-view-btn";
        btn.textContent = "View Sections";
        btn.addEventListener("click", openSidebar);
        nav.appendChild(btn);

        // Everything about the section lives behind this: what it knows, its
        // pipelines, its tasks and notes, and its conversation.
        if (currentSectionId() && !document.getElementById("jsec-info-btn")) {
            var info = document.createElement("button");
            info.className = navButtonClass();
            info.id = "jsec-info-btn";
            info.textContent = "Section Info";
            info.addEventListener("click", function () { openInfo(); });
            nav.appendChild(info);
        }
    }

    document.addEventListener("keydown", function (e) {
        // Escape backs out of the create window the same way Cancel does, so an
        // abandoned draft never leaves files behind in the pipeline's folder.
        if (e.key === "Escape") { closeSidebar(); cancelDraft(); closeInfo(); }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mountButton);
    } else {
        mountButton();
    }

    window.JarvisSections = {
        openSidebar: openSidebar,
        closeSidebar: closeSidebar,
        openInfo: openInfo,
        closeInfo: closeInfo,
        createFrom: createFrom,
        currentSectionId: currentSectionId,
        mountButton: mountButton
    };
})();
