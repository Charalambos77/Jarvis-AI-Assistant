/*
 * One navigation bar, on every page.
 *
 * Each screen grew its own set of buttons at a different time, so the Brain had
 * all of them, the plan page had three, and the new pages had their own three.
 * Getting from the Library to the APIs page meant going via the Brain.
 *
 * This does not rebuild those bars — each page's own buttons keep their own
 * wiring — it fills in whatever is missing, marks the one you are on, and
 * leaves everything else alone. Pages that already have a button for a
 * destination keep theirs; pages that do not get one that matches their styling.
 *
 * Execution mode is the one variation. Inside it, "Brain" is not where you came
 * from, so the first button becomes "Execution" and every link carries the
 * context on, which is the rule the plan and APIs pages already followed.
 *
 * Load this BEFORE sections_ui.js: that script appends "View Sections" to the
 * nav it can find, and this is what makes sure every page has one to find.
 */
(function () {
    "use strict";

    var DESTINATIONS = [
        { key: "home", label: "Brain", href: "command_center.html",
          // "Execution" fills the same slot, so a page offering that is not
          // missing its home button.
          aliases: ["brain", "execution"] },
        { key: "plan", label: "Plan", href: "plan.html", aliases: ["plan"] },
        { key: "library", label: "Library", href: "library.html", aliases: ["library"] },
        { key: "commands", label: "Commands", href: "commands.html", aliases: ["commands"] },
        { key: "apis", label: "APIs/MCPs", href: "provider_comparison.html",
          aliases: ["apis/mcps", "apis", "apis/mcp"] }
    ];

    // Where each page keeps its buttons. First one found wins.
    var CONTAINERS = [".top-right-nav", ".nav-group", ".nav-group-header"];

    function pageName() {
        var path = (window.location.pathname || "").split("/").pop().toLowerCase();
        if (!path || path === "command-center") return "command_center.html";
        return path;
    }

    function param(name) {
        try {
            return new URLSearchParams(window.location.search).get(name) || "";
        } catch (e) {
            return "";
        }
    }

    var page = pageName();
    var inExecution = page === "execution.html" || param("from") === "execution";

    // Which button should read as "you are here".
    var CURRENT = {
        "command_center.html": "home",
        "execution.html": "home",
        "plan.html": "plan",
        "library.html": "library",
        "commands.html": "commands",
        "provider_comparison.html": "apis"
    }[page] || "";

    function hrefFor(dest) {
        if (dest.key === "home") {
            return inExecution ? "execution.html" : "command_center.html";
        }
        // Carry the context so the page you land on knows to send you back to
        // execution rather than to the Brain.
        return inExecution ? dest.href + "?from=execution" : dest.href;
    }

    function labelFor(dest) {
        if (dest.key === "home" && inExecution) return "Execution";
        return dest.label;
    }

    /* A label with any count or decoration stripped, for comparing what a page
       already has against what it is missing. */
    function normalise(text) {
        return String(text || "")
            .replace(/\(.*?\)/g, "")
            .trim()
            .toLowerCase();
    }

    function findContainer() {
        for (var i = 0; i < CONTAINERS.length; i++) {
            var found = document.querySelector(CONTAINERS[i]);
            if (found) return found;
        }
        return null;
    }

    function injectStyle() {
        if (document.getElementById("jarvis-nav-style")) return;
        var style = document.createElement("style");
        style.id = "jarvis-nav-style";
        // Six buttons in a row does not fit every header, and a nav that runs
        // off the edge of the page is worse than one that takes two lines.
        style.textContent =
            // Only the bars that run across a header need this. Left alone,
            // six buttons in a row take the whole width and crush the page
            // title next to them; wrapped and capped, they fold onto a second
            // line instead. The Brain and execution screens stack their
            // buttons in a column, where none of this applies.
            ".jarvis-nav-row {" +
            "  flex-wrap: wrap; row-gap: 8px;" +
            "  justify-content: flex-end;" +
            "  max-width: 620px; }" +
            ".jarvis-nav-btn.jarvis-nav-waiting {" +
            "  border-color: rgba(251,191,36,0.55) !important;" +
            "  color: #FBBF24 !important; }";
        document.head.appendChild(style);
    }

    /* New buttons copy an existing one so they look native to the page — the
       APIs page styles its nav with inline borders, the Brain with a class. */
    function template(container) {
        var buttons = container.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
            if (!buttons[i].classList.contains("active")) return buttons[i];
        }
        return buttons[0] || null;
    }

    function mount() {
        var container = findContainer();
        if (!container) return;

        injectStyle();
        var laidOut = window.getComputedStyle(container);
        if (laidOut.display.indexOf("flex") !== -1 &&
            laidOut.flexDirection.indexOf("column") === -1) {
            container.classList.add("jarvis-nav-row");
        }
        // sections_ui.js looks for this class to hang "View Sections" on, so
        // tagging the container is what gets that button onto every page too.
        container.classList.add("top-right-nav");

        var model = template(container);
        var existing = {};
        var buttons = container.querySelectorAll("button");
        for (var i = 0; i < buttons.length; i++) {
            existing[normalise(buttons[i].textContent)] = buttons[i];
        }

        DESTINATIONS.forEach(function (dest) {
            var found = null;
            for (var i = 0; i < dest.aliases.length; i++) {
                if (existing[dest.aliases[i]]) {
                    found = existing[dest.aliases[i]];
                    break;
                }
            }

            if (!found) {
                found = document.createElement("button");
                found.textContent = labelFor(dest);
                found.className = model ? model.className.replace(/\bactive\b/g, "").trim()
                                        : "nav-btn";
                if (model && model.getAttribute("style")) {
                    found.setAttribute("style", model.getAttribute("style"));
                }
                found.addEventListener("click", function () {
                    window.location.href = hrefFor(dest);
                });
                container.appendChild(found);
            }

            found.classList.add("jarvis-nav-btn");
            found.setAttribute("data-nav", dest.key);
            if (dest.key === CURRENT && !found.classList.contains("active")) {
                found.classList.add("active");
            }
            // Re-appending puts the six into the same order everywhere, rather
            // than leaving the new ones tacked on after whatever the page
            // already had. Anything else in the bar (a session dropdown, Task
            // Logs) keeps its place in front of them.
            container.appendChild(found);
        });
    }

    /* The Commands button carries the number of commands waiting on an answer.
       They expire after ten minutes, so this belongs on every page rather than
       only on the page you would have to already be looking at. */
    function watchWaiting() {
        function tick() {
            fetch("/commands/pending")
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    var count = (data.pending || []).length;
                    var btn = document.querySelector('[data-nav="commands"]');
                    if (!btn) return;
                    btn.textContent = count ? "Commands (" + count + ")" : "Commands";
                    btn.classList.toggle("jarvis-nav-waiting", count > 0);
                })
                .catch(function () { /* Jarvis restarting; the next tick retries. */ });
        }
        tick();
        setInterval(tick, 3000);
    }

    function start() {
        mount();
        watchWaiting();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
