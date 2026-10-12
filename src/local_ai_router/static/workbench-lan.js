"use strict";
(() => {
  const $ = id => document.getElementById(id), base = "/workbench/api";
  let csrf = "", grants = {}, ws = "", online = false, pending = null, stream = null, sequence = 0, delay = 1000, timer = null;
  const commands = new Map(), revisions = new Map();
  let pageCursor = "";
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
  async function board(cursor = pageCursor) {
    const data = await request(base + "/work-items" + query() + "&limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
    pageCursor = cursor;
    $("content").replaceChildren(el("h2", "Work items"));
    for (const item of data.items) $("content").append(button(item.display_key + " · " + item.title + " · " + item.status, () => detail(item.id)));
    if (pageCursor) $("content").append(button("First page", () => board("")));
    if (data.has_more) $("content").append(button("Next page", () => board(data.next_cursor)));
  }
  async function detail(id) {
    const item = await request(base + "/work-items/" + id + query());
    const root = $("detail"); root.replaceChildren(el("h2", item.title), el("p", item.display_key + " · " + item.status + " · revision " + item.revision));
    root.append(el("pre", JSON.stringify(item.spec, null, 2)));
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
    let page = await request(base + "/snapshots", {workspace_id: ws, filters: {kinds: ["work_item"]}, limit: 100});
    sequence = page.sequence; revisions.clear();
    for (;;) { for (const item of page.items) revisions.set(item.payload.id, item.payload.revision); if (!page.next_cursor) break; page = await request(base + "/snapshots/" + page.snapshot_id + "?cursor=" + encodeURIComponent(page.next_cursor)); }
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
      clearTimeout(timer); timer = setTimeout(() => board().catch(error => notice(error.message)), 150);
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
  $("logout").onclick = async () => { try { await request("/session/logout", {}); expired(); } catch (error) { notice(error.message); } };
  $("workspace").onchange = async () => { ws = $("workspace").value; pageCursor = ""; $("detail").replaceChildren(); await snapshot(); connect(); await board(); };
  window.addEventListener("offline", () => { if (stream) stream.close(); status(false); });
  window.addEventListener("online", () => reconnect(true));
  start().catch(() => status(false, "Pair this browser"));
})();
