/*
 * Jarvis asking you a question, wherever you are in the app.
 *
 * An agent that is unsure used to guess and carry on: pipeline 9 redefined "just
 * starting to incorporate AI" to fit a list it had already picked. Now it can call
 * ask_user and wait, and this is where the question reaches you — on whichever page
 * you happen to be on, one question at a time, as many as the agent needs.
 *
 * Loaded by every page beside nav_ui.js. It owns nothing else on the page.
 */
(function () {
    if (window.__jarvisQuestionsUi) return;
    window.__jarvisQuestionsUi = true;

    const POLL_MS = 3000;
    let current = null;          // the question on screen
    let sending = false;
    let dismissed = {};          // request_id -> true, for "answer later"

    function el(tag, style, text) {
        const node = document.createElement(tag);
        if (style) node.setAttribute('style', style);
        if (text !== undefined) node.textContent = text;
        return node;
    }

    const overlay = el('div', [
        'position:fixed', 'inset:0', 'z-index:100000', 'display:none',
        'align-items:center', 'justify-content:center',
        'background:rgba(2,6,14,0.72)', 'backdrop-filter:blur(3px)',
        'font-family:inherit'
    ].join(';'));

    const card = el('div', [
        'width:min(560px, calc(100vw - 32px))', 'max-height:calc(100vh - 32px)', 'overflow:auto',
        'background:#0b1220', 'border:1px solid rgba(34,211,238,0.35)', 'border-radius:12px',
        'box-shadow:0 18px 60px rgba(0,0,0,0.6)', 'padding:18px 18px 14px', 'color:#e6edf7'
    ].join(';'));
    overlay.appendChild(card);

    const kicker = el('div', 'font-size:11px;letter-spacing:.14em;color:#22d3ee;margin-bottom:6px', 'JARVIS // A QUESTION FOR YOU');
    const who = el('div', 'font-size:11px;color:#8b9bb4;margin-bottom:10px');
    const question = el('div', 'font-size:15px;font-weight:600;line-height:1.45;margin-bottom:8px');
    const why = el('div', 'font-size:12px;color:#9fb0c9;line-height:1.5;margin-bottom:10px');
    const options = el('div', 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px');
    const input = el('textarea', [
        'width:100%', 'min-height:84px', 'resize:vertical', 'box-sizing:border-box',
        'background:rgba(0,0,0,0.45)', 'border:1px solid rgba(255,255,255,0.12)', 'border-radius:8px',
        'color:#fff', 'padding:10px', 'font-family:inherit', 'font-size:13px'
    ].join(';'));
    input.setAttribute('placeholder', 'Your answer…  (Ctrl+Enter to send)');

    const status = el('div', 'font-size:11px;color:#8b9bb4;margin:8px 0 10px');
    const row = el('div', 'display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap');

    function button(label, primary) {
        return el('button', [
            'padding:8px 14px', 'border-radius:8px', 'font-family:inherit', 'font-size:12px', 'cursor:pointer',
            primary ? 'background:#22d3ee' : 'background:rgba(255,255,255,0.06)',
            primary ? 'color:#04121a' : 'color:#e6edf7',
            primary ? 'border:1px solid #22d3ee' : 'border:1px solid rgba(255,255,255,0.14)',
            primary ? 'font-weight:700' : 'font-weight:500'
        ].join(';'), label);
    }

    const later = button('Answer later', false);
    const skip = button('Let it decide', false);
    const send = button('Send answer', true);
    row.append(later, skip, send);
    card.append(kicker, who, question, why, options, input, status, row);
    document.addEventListener('DOMContentLoaded', () => document.body.appendChild(overlay));
    if (document.body) document.body.appendChild(overlay);

    function close() {
        overlay.style.display = 'none';
        current = null;
    }

    function show(item, waitingCount) {
        current = item;
        who.textContent = [item.role || item.agent_id, item.plan_id ? `pipeline ${item.plan_id}` : '']
            .filter(Boolean).join(' · ');
        question.textContent = item.question;
        why.textContent = item.why ? `Why it is asking: ${item.why}` : '';
        why.style.display = item.why ? 'block' : 'none';

        options.innerHTML = '';
        (item.options || []).forEach(text => {
            const pick = button(text, false);
            pick.onclick = () => { input.value = text; input.focus(); };
            options.appendChild(pick);
        });
        options.style.display = (item.options || []).length ? 'flex' : 'none';

        const more = waitingCount > 1 ? ` · ${waitingCount - 1} more waiting` : '';
        const mins = Math.max(0, Math.round((item.expires_in || 0) / 60));
        status.textContent = `It is waiting for you${mins ? ` — it carries on by itself in about ${mins} min` : ''}${more}`;
        input.value = '';
        overlay.style.display = 'flex';
        setTimeout(() => input.focus(), 30);
    }

    async function respond(payload) {
        if (!current || sending) return;
        sending = true;
        send.textContent = 'Sending…';
        try {
            const res = await fetch('/questions/answer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: current.request_id, ...payload })
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                status.textContent = data.error || 'That could not be sent. Try again.';
                return;
            }
            close();
            poll();                       // the agent may have another question straight away
        } catch (e) {
            status.textContent = 'Jarvis could not be reached. Try again.';
        } finally {
            sending = false;
            send.textContent = 'Send answer';
        }
    }

    send.onclick = () => {
        if (!input.value.trim()) {
            status.textContent = 'Write an answer, or choose "Let it decide".';
            return;
        }
        respond({ answer: input.value });
    };
    skip.onclick = () => respond({ skipped: true });
    later.onclick = () => { if (current) dismissed[current.request_id] = true; close(); };
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send.onclick();
        if (e.key === 'Escape') later.onclick();
    });

    async function poll() {
        try {
            const res = await fetch('/questions/pending');
            if (!res.ok) return;
            const items = (await res.json()).pending || [];
            const ids = {};
            items.forEach(i => { ids[i.request_id] = true; });
            Object.keys(dismissed).forEach(id => { if (!ids[id]) delete dismissed[id]; });

            if (current && !ids[current.request_id]) close();   // answered elsewhere, or it gave up
            if (current) return;
            const next = items.find(i => !dismissed[i.request_id]);
            if (next) show(next, items.length);
        } catch (e) { /* Jarvis restarting: try again on the next tick */ }
    }

    setInterval(poll, POLL_MS);
    poll();
})();
