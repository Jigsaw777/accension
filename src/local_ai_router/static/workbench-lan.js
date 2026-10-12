"use strict";
(() => {
  const $ = id => document.getElementById(id), base = "/workbench/api";
  let csrf = "", grants = {}, ws = "", online = false, pending = null, stream = null, sequence = 0, delay = 1000, timer = null;
  const commands = new Map(), revisions = new Map();
  let pageCursor = "", boardGeneration = 0, screen = "board";
  const viewStates = new Map();
  function el(tag, text) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; }
  function status(value, text) { online = value; $("connection").textContent = text || (value ? "Connected · " + new Date().toLocaleTimeString() : "Offline · changes disabled"); document.querySelectorAll("[data-mutation]").forEach(node => node.disabled = !value); }
  function notice(text) { $("notice").textContent = text; }
  async function request(path, body, method = body === undefined ? "GET" : "POST") {
    let response;
    try { response = await fetch(path, {method, credentials: "same-origin", headers: body === undefined ? {} : {"content-type": "application/json", "x-csrf-token": csrf}, body: body === undefined ? undefined : JSON.stringify(body)}); }
    catch (error) { status(false); throw Error("Connection lost. Refresh to reconnect."); }
    const data = await response.json();
    if (!response.ok) { if (response.status === 401) expired(); throw Error(response.status === 409 ? "Conflict: refresh before retrying this action." : data.error?.message || "Request failed"); }
    return data;
  }
  function expired() { if (stream) stream.close(); status(false, "Session expired or revoked · pair again"); $("workspace-view").hidden = true; $("pairing").hidden = false; $("content").replaceChildren(); $("detail").replaceChildren(); grants = {}; csrf = ""; }
  function button(text, action, mutation = false) { const node = el("button", text); node.type = "button"; if (mutation) { node.dataset.mutation = "true"; node.disabled = !online; } node.onclick = async () => { node.disabled = true; try { await action(); } catch (error) { notice(error.message); } finally { node.disabled = mutation && !online; } }; return node; }
  function query() { return "?" + new URLSearchParams({workspace_id: ws}); }
  async function command(id, body) {
    if (!online) throw Error("Offline. Reconnect before changing work.");
    const key = id + JSON.stringify(body);
    if (!commands.has(key)) commands.set(key, crypto.randomUUID());
    await request(base + "/work-items/" + id + "/commands" + query(), {...body, command_id: commands.get(key)});
    commands.delete(key); await board(); await detail(id);
  }
  function filters() { return viewStates.get(ws) || {query: "", project_id: ""}; }
  async function board() {
    screen = "board"; const scope = ws, generation = ++boardGeneration, fields = {...filters(), workspace_id: ws, limit: 20};
    if (!fields.project_id) delete fields.project_id;
    const data = await request(base + "/board?" + new URLSearchParams(fields));
    if (scope !== ws || generation !== boardGeneration || screen !== "board") return;
    const root = $("content"), form = el("form"), search = el("input"), label = el("label", "Search work items"), projects = el("select"), projectLabel = el("label", "Project");
    search.name = "query"; search.maxLength = 200; search.value = fields.query; label.append(search);
    projects.name = "project_id"; const all = el("option", "All projects"); all.value = ""; projects.append(all); projectLabel.append(projects);
    form.append(label, projectLabel, el("button", "Apply filters"));
    form.onsubmit = event => { event.preventDefault(); viewStates.set(ws, Object.fromEntries(new FormData(form))); board().catch(error => notice(error.message)); };
    root.replaceChildren(el("h2", "Work item board"), form);
    const choices = await request(base + "/projects" + query() + "&limit=50");
    if (scope !== ws || generation !== boardGeneration || screen !== "board") return;
    for (const project of choices.items) { const option = el("option", project.name); option.value = project.id; projects.append(option); } projects.value = fields.project_id || "";
    for (const column of data.columns) {
      const section = el("section"), cards = el("div"); section.append(el("h3", column.state + " · " + column.count), cards);
      const draw = page => { for (const item of page.items) cards.append(button(item.display_key + " · " + item.title + " · " + (item.observed_stage || item.status), () => detail(item.id))); };
      draw(column); let cursor = column.next_cursor;
      const more = button("Load more " + column.state, async () => { const page = await request(base + "/board/columns/" + column.state + "?" + new URLSearchParams({...fields, cursor})); if (scope !== ws || generation !== boardGeneration) return; draw(page.columns[0]); cursor = page.columns[0].next_cursor; more.hidden = !cursor; });
      more.hidden = !cursor; section.append(more); root.append(section);
    }
  }
  async function timeline(kind, id, root, cursor) {
    const scope = ws, data = await request(base + "/" + kind + "/" + id + "/activity" + query() + "&limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
    if (scope !== ws || !root.isConnected) return;
    if (!cursor) root.append(el("h3", "Activity"));
    for (const entry of data.items) root.append(el("p", new Date(entry.occurred_at).toLocaleString() + " · " + entry.event_type + (entry.metadata.summary ? " · " + entry.metadata.summary : "")));
    if (data.has_more) root.append(button("Older activity", async () => { await timeline(kind, id, root, data.next_cursor); }));
  }
  async function agents(cursor) {
    screen = "agents"; ++boardGeneration; const scope = ws, data = await request(base + "/agents" + query() + "&limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
    if (scope !== ws || screen !== "agents") return;
    const root = $("content"); root.replaceChildren(el("h2", "Agents"));
    for (const agent of data.items) root.append(button(agent.name + " · " + agent.state, async () => {
      const profile = await request(base + "/agents/" + agent.id + query()); if (scope !== ws) return;
      const panel = $("detail"); panel.replaceChildren(el("h2", agent.name), el("p", "Current work: " + profile.load + " / capacity " + profile.capacity), el("pre", JSON.stringify(profile.definition.model_bindings, null, 2)));
      for (const connector of profile.connectors) panel.append(el("p", connector.name + " · " + connector.status));
      const end = Date.now(), metrics = await request(base + "/agents/" + agent.id + "/metrics" + query() + "&from=" + (end - 30 * 86400000) + "&to=" + end); if (scope !== ws) return;
      panel.append(el("h3", "Measured performance · last 30 days"), el("pre", JSON.stringify(metrics, null, 2))); await timeline("agents", agent.id, panel);
    }));
    if (cursor) root.append(button("First page", () => agents()));
    if (data.has_more) root.append(button("Next agents", () => agents(data.next_cursor)));
  }
  async function models(cursor) {
    screen = "models"; ++boardGeneration; const scope = ws, data = await request(base + "/model-options" + query() + "&purpose=executor&limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
    if (scope !== ws || screen !== "models") return;
    const root = $("content"); root.replaceChildren(el("h2", "Registered execution models"), el("p", "Eligibility follows this workspace's current privacy policy."));
    for (const model of data.items) root.append(el("p", model.id + " · " + model.locality + " · " + (model.eligible ? "eligible" : model.reason_codes.join(", "))));
    if (data.has_more) root.append(button("Next models", () => models(data.next_cursor)));
  }
  async function detail(id) {
    const item = await request(base + "/work-items/" + id + query());
    const root = $("detail"); root.replaceChildren(el("h2", item.title), el("p", item.display_key + " · " + item.status + " · revision " + item.revision));
    root.append(el("pre", JSON.stringify(item.spec, null, 2)));
    await timeline("work-items", id, root);
    const allowed = new Set(item.allowed_actions || []), capabilities = new Set(grants[ws] || []);
    if (capabilities.has("work.write")) for (const action of ["ready", "cancel", "retry", "reopen"]) if (allowed.has(action)) root.append(button(action, () => command(id, {command: action, expected_revision: item.revision}), true));
    const attempts = await request(base + "/work-items/" + id + "/attempts" + query());
    for (const attempt of attempts.items) root.append(button("Attempt " + attempt.ordinal + " · " + attempt.state, async () => {
      for (const section of ["receipt", "verification", "artifacts"]) { const evidence = await request(base + "/attempts/" + attempt.id + "/" + section + query()); root.append(el("h3", section), el("pre", JSON.stringify(evidence, null, 2))); }
    }));
    if (capabilities.has("artifact.read") && ["REVIEW", "WAITING_FOR_HUMAN", "DONE"].includes(item.status)) {
      const evidence = await request(base + "/work-items/" + id + "/evidence" + query());
      root.append(el("h3", "Completion evidence"), el("pre", JSON.stringify(evidence, null, 2)));
      if (capabilities.has("review.approve")) for (const action of ["approve_completion", "reject_completion"]) if (allowed.has(action)) root.append(button(action, () => command(id, {command: action, expected_revision: item.revision, contract_revision: evidence.contract_revision, evidence_hash: evidence.evidence_hash, reason: "Reviewed displayed completion evidence"}), true));
    }
    if (allowed.has("approve_execution") && capabilities.has("execution.approve")) {
      const contract = await request(base + "/work-items/" + id + "/execution-preflight" + query(), {});
      root.append(el("h3", "Execution contract"), el("pre", JSON.stringify(contract, null, 2)), button("approve_execution", () => command(id, {command: "approve_execution", expected_revision: item.revision, contract_revision: item.contract_revision, contract_hash: contract.contract_hash, policy_revision_hash: contract.policy_revision_hash, reason: "Reviewed displayed execution contract"}), true));
    }
    if (allowed.has("request_execution") && capabilities.has("execution.request")) root.append(button("request_execution", () => command(id, {command: "request_execution", expected_revision: item.revision}), true));
  }
  async function snapshot() {
    const page = await request(base + "/board?" + new URLSearchParams({workspace_id: ws, limit: 1}));
    sequence = page.sequence; revisions.clear();
    for (const column of page.columns) for (const item of column.items) revisions.set(item.id, item.revision);
  }
  function connect() {
    if (stream) stream.close();
    stream = new EventSource(base + "/events" + query() + "&after=" + sequence);
    stream.onopen = () => { delay = 1000; status(true); };
    stream.addEventListener("workbench", event => {
      let data; try { data = JSON.parse(event.data); } catch (error) { return reconnect(true); }
      if (data.schema_version !== 1) return reconnect(true);
      if (data.sequence <= sequence) return;
      const previous = revisions.get(data.entity_id) || 0;
      if (data.entity_revision < previous) return reconnect(true);
      revisions.set(data.entity_id, data.entity_revision); sequence = data.sequence;
      if (revisions.size > 1000) revisions.delete(revisions.keys().next().value);
      clearTimeout(timer); if (screen === "board") timer = setTimeout(() => board().catch(error => notice(error.message)), 150);
    });
    stream.addEventListener("resync", event => { const code = JSON.parse(event.data).code; if (["UNAUTHENTICATED", "FORBIDDEN", "WORKSPACE_LOCAL_ONLY"].includes(code)) expired(); else reconnect(true); });
    stream.onerror = () => reconnect(false);
  }
  function reconnect(reset) { if (stream) stream.close(); status(false); clearTimeout(timer); timer = setTimeout(async () => { try { await request("/session").then(data => {csrf = data.csrf; grants = data.grants;}); if (reset) await snapshot(); connect(); await board(); } catch (error) { notice(error.message); if (Object.keys(grants).length) reconnect(reset); } }, delay + Math.random() * 250); delay = Math.min(delay * 2, 30000); }
  async function start() {
    const session = await request("/session"); csrf = session.csrf; grants = session.grants;
    const scopes = await request(base + "/workspaces"); $("workspace").replaceChildren();
    for (const scope of scopes.items) { const option = el("option", scope.name); option.value = scope.id; $("workspace").append(option); }
    ws = $("workspace").value; $("pairing").hidden = true; $("workspace-view").hidden = false;
    status(true); await snapshot(); connect(); await board(); notice("");
  }
  $("pair-form").onsubmit = async event => { event.preventDefault(); try { pending = await request("/pairing/request", Object.fromEntries(new FormData(event.target))); $("phrase").textContent = "Compare with host: " + pending.verification_phrase; $("claim").hidden = false; event.target.elements.code.value = ""; notice("Waiting for host approval. Then connect."); } catch (error) { notice(error.message); } };
  $("claim").onclick = async () => { try { await request("/pairing/claim", {id: pending.id, challenge: pending.challenge, browser: true}); pending = null; $("phrase").textContent = ""; $("claim").hidden = true; await start(); } catch (error) { notice(error.message); } };
  $("refresh").onclick = () => start().catch(error => notice(error.message));
  const nav = $("refresh").parentElement;
  nav.append(button("Board", () => board()), button("Agents", () => agents()), button("Models", () => models()));
  $("logout").onclick = async () => { try { await request("/session/logout", {}); expired(); } catch (error) { notice(error.message); } };
  $("workspace").onchange = async () => { ws = $("workspace").value; pageCursor = ""; $("detail").replaceChildren(); await snapshot(); connect(); await board(); };
  window.addEventListener("offline", () => { if (stream) stream.close(); status(false); });
  window.addEventListener("online", () => reconnect(true));
  start().catch(() => status(false, "Pair this browser"));
})();
