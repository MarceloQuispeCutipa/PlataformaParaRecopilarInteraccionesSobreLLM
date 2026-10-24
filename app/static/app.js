(() => {
  const $ = (id) => document.getElementById(id);
  const S = { cid: null, data: null, threshold: 600, sending: false, modelOk: false };
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch (_) {}
    if (!res.ok) { const e = new Error((body && body.error && body.error.message) || `Error HTTP ${res.status}`); e.status = res.status; e.body = body; throw e; }
    return body;
  }
  function alertMsg(text) {
    const d = document.createElement("div"); d.className = "alert"; d.textContent = text; $("alerts").prepend(d);
    setTimeout(() => d.remove(), 12000);
  }
  const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  // ---------- estado del modelo ----------
  async function checkHealth() {
    try {
      const h = await api("/api/health");
      S.threshold = h.inactivity_threshold_seconds || S.threshold;
      S.modelOk = h.service_available && h.model_installed;
      $("dot").className = "dot " + (S.modelOk ? "ok" : "bad");
      $("modelStatus").textContent = S.modelOk ? `Modelo ${h.model} listo` : (h.error || "Modelo no disponible");
    } catch (e) { S.modelOk = false; $("dot").className = "dot bad"; $("modelStatus").textContent = "Sin conexión con el servidor"; }
    updateControls();
  }

  // ---------- lista de conversaciones ----------
  async function loadList() {
    const list = await api("/api/conversations");
    const ul = $("convList"); ul.innerHTML = "";
    if (!list.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = "Aún no tienes conversaciones."; ul.append(li); return; }
    for (const c of list) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      if (c.conversation_id === S.cid) li.setAttribute("aria-current", "true");
      const t = document.createElement("span"); t.className = "t"; t.textContent = c.title || "Conversación sin título";
      const m = document.createElement("span"); m.className = "m";
      m.textContent = `${c.status === "closed" ? "Finalizada · " : ""}${c.message_count} mensajes · ${new Date(c.last_activity_at).toLocaleString()}`;
      li.append(t, m);
      const open = () => openConversation(c.conversation_id);
      li.addEventListener("click", open); li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
      ul.append(li);
    }
  }

  // ---------- render ----------
  function render() {
    const box = $("messages"); box.innerHTML = "";
    const msgs = S.data ? S.data.messages : [];
    if (!msgs.length) {
      box.innerHTML = '<div class="empty-state"><strong>¿En qué te ayudo?</strong>Escribe abajo. El modelo corre en este equipo y las respuestas pueden tardar unos segundos.</div>';
    }
    let lastSession = null;
    for (const m of msgs) {
      if (lastSession && m.session_id !== lastSession) {
        const b = document.createElement("div"); b.className = "sess-break"; b.textContent = `Nueva sesión · ${new Date(m.created_at).toLocaleString()}`; box.append(b);
      }
      lastSession = m.session_id;
      box.append(renderMsg(m));
    }
    if (S.sending) { const t = document.createElement("div"); t.className = "typing"; t.textContent = "El modelo está generando la respuesta"; box.append(t); }
    box.scrollTop = box.scrollHeight;
    updateControls(); tickLedger();
  }
  function renderMsg(m) {
    const d = document.createElement("div");
    d.className = `msg ${m.role}` + (m.status === "failed" || m.status === "interrupted" ? " failed" : "");
    const body = document.createElement("div"); body.className = "body";
    if (m.role === "assistant" && m.status !== "completed") {
      body.textContent = m.status === "generating" ? "Generando…" : "No se pudo generar esta respuesta.";
    } else body.textContent = m.content;
    d.append(body);
    if (m.role === "assistant" && m.status === "completed") {
      const meta = document.createElement("div"); meta.className = "meta";
      const t = document.createElement("span"); t.textContent = `${(m.response_time_ms / 1000).toFixed(1)} s`;
      const fb = (S.data.feedback || {})[m.message_id];
      const up = document.createElement("button"); up.type = "button"; up.textContent = "Fue útil"; up.setAttribute("aria-pressed", fb === "useful");
      const down = document.createElement("button"); down.type = "button"; down.className = "neg"; down.textContent = "No me sirvió"; down.setAttribute("aria-pressed", fb === "not_useful");
      up.onclick = () => sendFeedback(m.message_id, "useful", true);
      down.onclick = () => sendFeedback(m.message_id, "not_useful", false);
      meta.append(t, up, down); d.append(meta);
    }
    return d;
  }
  function updateControls() {
    const closed = S.data && S.data.conversation.status === "closed";
    const has = !!S.cid && S.data && S.data.messages.length > 0;
    $("send").disabled = S.sending || closed;
    $("input").disabled = S.sending || closed;
    $("input").placeholder = closed ? "Conversación finalizada. Inicia una nueva." : "Escribe tu pregunta o instrucción";
    $("finishConv").disabled = !has || closed || S.sending;
    $("exportConv").disabled = !has;
    $("deleteConv").disabled = !S.cid || S.sending;
  }

  // franja de sesión: estado del registro y cuenta atrás de inactividad
  function tickLedger() {
    const el = $("ledger"); const d = S.data;
    const active = d && d.sessions.find((s) => s.status === "active");
    if (!active) {
      const closed = d && d.conversation.status === "closed";
      el.className = "ledger idle";
      el.innerHTML = closed ? "<b>Conversación finalizada</b><span>Se guardó lo registrado hasta ahora.</span>"
                            : "<b>Sin sesión activa</b><span>Tu próximo mensaje iniciará una sesión.</span>";
      return;
    }
    const idle = (Date.now() - new Date(active.last_activity_at).getTime()) / 1000;
    const left = S.threshold - idle;
    el.className = "ledger";
    const tail = left > 0 ? `Si no escribes, se cerrará por inactividad en ${mmss(left)}.` : "Inactividad detectada: el servidor la cerrará en breve y tu próximo mensaje abrirá una sesión nueva.";
    el.innerHTML = `<b>Sesión activa desde ${fmtTime(active.started_at)}</b><span>${active.total_user_messages} mensajes tuyos registrados.</span><span class="sp"></span><span>${tail}</span>`;
  }
  setInterval(tickLedger, 1000);

  // ---------- acciones ----------
  async function openConversation(cid) {
    S.cid = cid; S.data = await api(`/api/conversations/${cid}`); render(); loadList();
  }
  function newConversation() { S.cid = null; S.data = null; render(); loadList(); $("input").focus(); }

  async function send(text) {
    S.sending = true;
    const tmp = { role: "user", content: text, status: "processing", session_id: null, created_at: new Date().toISOString() };
    if (!S.data) S.data = { conversation: { status: "open" }, messages: [], sessions: [], feedback: {} };
    S.data.messages.push(tmp); render();
    try {
      if (!S.cid) { const c = await api("/api/conversations", { method: "POST", body: "{}" }); S.cid = c.conversation_id; }
      try {
        await api("/api/messages", { method: "POST", body: JSON.stringify({ conversation_id: S.cid, content: text, client_message_id: uuid() }) });
      } catch (e) {
        if (e.status === 502 && e.body && e.body.error) alertMsg(e.body.error.message);
        else throw e;
      }
    } catch (e) { alertMsg(e.message); }
    S.sending = false;
    if (S.cid) { try { S.data = await api(`/api/conversations/${S.cid}`); } catch (e) { alertMsg(e.message); } }
    render(); loadList(); checkHealth();
  }
  async function sendFeedback(mid, rating, goal) {
    try { await api(`/api/messages/${mid}/feedback`, { method: "POST", body: JSON.stringify({ rating, goal_completed: goal }) });
      S.data.feedback[mid] = rating; render(); } catch (e) { alertMsg(e.message); }
  }
  async function finish() {
    if (!S.data) return;
    if (!confirm("¿Finalizar esta conversación? Ya no podrás enviar más mensajes en ella.")) return;
    const sid = S.data.active_session_id || S.data.last_session_id;
    try { await api(`/api/sessions/${sid}/finish`, { method: "POST", body: JSON.stringify({ close_conversation: true }) }); await openConversation(S.cid); }
    catch (e) { alertMsg(e.message); }
  }
  async function remove() {
    if (!confirm("Se borrarán de forma definitiva los mensajes y registros de esta conversación. ¿Continuar?")) return;
    try { await api(`/api/conversations/${S.cid}`, { method: "DELETE" }); newConversation(); } catch (e) { alertMsg(e.message); }
  }
  function download() {
    const lines = S.data.messages.map((m) => `[${m.created_at}] ${m.role === "user" ? "Tú" : "Asistente"}: ${m.content}`);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n\n")], { type: "text/plain;charset=utf-8" }));
    a.download = `conversacion-${S.cid.slice(0, 8)}.txt`; a.click(); URL.revokeObjectURL(a.href);
  }

  // señales auxiliares (no cierran la sesión en el servidor)
  document.addEventListener("visibilitychange", () => {
    if (!S.cid) return;
    const type = document.visibilityState === "hidden" ? "page_hidden" : "page_visible";
    navigator.sendBeacon("/api/client-events", new Blob([JSON.stringify({ type, conversation_id: S.cid })], { type: "text/plain" }));
  });

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("input").value.trim(); if (!text || S.sending) return;
    $("input").value = ""; $("input").style.height = "auto"; send(text);
  });
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("input").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; });
  $("newConv").onclick = newConversation; $("finishConv").onclick = finish; $("deleteConv").onclick = remove; $("exportConv").onclick = download;
  $("accept").onclick = () => { localStorage.setItem("consent_v1", "1"); $("notice").hidden = true; };
  if (!localStorage.getItem("consent_v1")) $("notice").hidden = false;

  render(); loadList(); checkHealth(); setInterval(checkHealth, 20000);
})();
