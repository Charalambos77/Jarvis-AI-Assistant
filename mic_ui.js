/*
 * The mute button, wherever there is somewhere to talk to Jarvis.
 *
 * The microphone is always listening for the wake word, whichever screen is
 * open — but the button to shut it up only existed on the Brain. Inside a
 * section, or in execution mode, you could be talked over with no way to stop
 * it without navigating away first.
 *
 * This puts the same button next to any chat box that does not already have
 * one, wired to the same endpoint the Brain's button uses, so muting anywhere
 * mutes everywhere. The Brain keeps its own button and its own wiring; this
 * only fills in the pages that were missing it.
 */
(function () {
    "use strict";

    // Where a page keeps its chat box, and what its send button is called.
    var ROWS = [".chat-input-wrapper", ".chat-input-row"];
    var SEND_BUTTONS = ["#send-btn", "#chat-send"];

    var MIC_ICON =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>' +
        '<path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>' +
        '<line x1="12" y1="19" x2="12" y2="23"></line>' +
        '<line x1="8" y1="23" x2="16" y2="23"></line></svg>';

    function injectStyle() {
        if (document.getElementById("jarvis-mic-style")) return;
        var style = document.createElement("style");
        style.id = "jarvis-mic-style";
        style.textContent =
            "#jarvis-mic-btn {" +
            "  width: 36px; height: 36px; border-radius: 50%;" +
            "  background: rgba(255,255,255,0.08);" +
            "  border: 1px solid rgba(255,255,255,0.15);" +
            "  color: #FFFFFF; cursor: pointer; flex-shrink: 0;" +
            "  display: flex; align-items: center; justify-content: center;" +
            "  transition: all 0.2s ease; padding: 0; }" +
            "#jarvis-mic-btn svg { width: 14px; height: 14px; }" +
            "#jarvis-mic-btn:hover {" +
            "  background: rgba(255,255,255,0.18);" +
            "  box-shadow: 0 0 10px rgba(255,255,255,0.12); }" +
            // Muted is a state you must be able to see at a glance from across
            // the room, which is the whole reason for pressing it.
            "#jarvis-mic-btn.muted {" +
            "  background: rgba(239,68,68,0.2);" +
            "  border-color: rgba(239,68,68,0.4); color: #EF4444; }" +
            "#jarvis-mic-btn.muted:hover {" +
            "  background: rgba(239,68,68,0.3);" +
            "  box-shadow: 0 0 10px rgba(239,68,68,0.2); }";
        document.head.appendChild(style);
    }

    function findRow() {
        for (var i = 0; i < ROWS.length; i++) {
            var row = document.querySelector(ROWS[i]);
            if (row) return row;
        }
        return null;
    }

    function findSend(row) {
        for (var i = 0; i < SEND_BUTTONS.length; i++) {
            var btn = row.querySelector(SEND_BUTTONS[i]);
            if (btn) return btn;
        }
        return null;
    }

    var button = null;

    function paint(muted) {
        if (!button) return;
        button.classList.toggle("muted", !!muted);
        button.title = muted ? "Unmute microphone" : "Mute microphone";
    }

    function refresh() {
        fetch("/jarvis/mic")
            .then(function (r) { return r.json(); })
            .then(function (data) { paint(data.mic_muted); })
            .catch(function () { /* Jarvis restarting; the next tick retries. */ });
    }

    function mount() {
        var row = findRow();
        // Nothing to talk to on this page, or the page brought its own button.
        if (!row || document.getElementById("mic-mute-btn")) return;
        if (document.getElementById("jarvis-mic-btn")) return;

        injectStyle();
        button = document.createElement("button");
        button.id = "jarvis-mic-btn";
        button.type = "button";
        button.title = "Mute microphone";
        button.innerHTML = MIC_ICON;
        button.addEventListener("click", function () {
            fetch("/jarvis/mute-mic", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}"
            })
                .then(function (r) { return r.json(); })
                .then(function (data) { paint(data.mic_muted); })
                .catch(function () { /* leave the button as it was */ });
        });

        // In front of Send, the same order as the Brain.
        var send = findSend(row);
        if (send) {
            row.insertBefore(button, send);
        } else {
            row.appendChild(button);
        }

        refresh();
        // Muting on one screen has to show on the others, so this follows the
        // real state rather than only what was last clicked here.
        setInterval(refresh, 4000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mount);
    } else {
        mount();
    }
})();
