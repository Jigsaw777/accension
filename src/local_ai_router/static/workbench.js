"use strict";
(() => {
  try {
    document.documentElement.dataset.theme =
      localStorage.getItem("accension-theme") || "system";
  } catch {
    // Storage can be disabled; the shared stylesheet follows the system theme.
  }
  const $ = (id) => document.getElementById(id),
    base = "/ui/workbench/api";
  const states = [
    "BACKLOG",
    "READY",
    "ASSIGNED",
    "RUNNING",
    "VERIFYING",
    "REVIEW",
    "DONE",
    "BLOCKED",
    "FAILED",
    "WAITING_FOR_HUMAN",
    "CANCELLED",
  ];
  let csrf = "",
    online = false,
    page = "Overview",
    ws = "",
    project = "",
    stream = null,
    sequence = 0,
    reconnectDelay = 1000,
    refreshTimer = null;
  const drafts = new Map();
  const boardViews = new Map();
  let refreshBoard = null;
  const pendingCommands = new Map();
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function message(text, error = false) {
    $("notice").textContent = text;
    $("notice").className = error ? "error" : "";
  }
  function connected(value) {
    online = value;
    $("connection").textContent = value
      ? "Connected · " + new Date().toLocaleTimeString()
      : "Offline · changes disabled";
    document
      .querySelectorAll("[data-mutation]")
      .forEach((b) => (b.disabled = !value));
  }
  async function api(path, body, method) {
    if (body !== undefined && !online)
      throw Error("Offline. Reconnect before changing work.");
    let r, commandKey;
    if (body?.command_id) {
      const { command_id, quote, ...input } = body;
      commandKey = path + JSON.stringify(input);
      if (pendingCommands.has(commandKey))
        body.command_id = pendingCommands.get(commandKey);
      else pendingCommands.set(commandKey, command_id);
    }
    try {
      r = await fetch(base + path, {
        method: method || (body === undefined ? "GET" : "POST"),
        credentials: "same-origin",
        headers:
          body === undefined
            ? {}
            : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      connected(false);
      throw Error("Connection lost. Your draft is retained.");
    }
    const data = r.headers.get("content-type")?.includes("application/json")
      ? await r.json()
      : await r.text();
    if (!r.ok) {
      if (r.status === 409)
        throw Error(
          "Conflict: refresh this item before saving. Your draft is retained.",
        );
      throw Error(data.error?.message || data.detail || "Request failed");
    }
    if (commandKey) pendingCommands.delete(commandKey);
    return data;
  }
  function button(text, fn, mutation = false) {
    const b = el("button", text);
    b.type = "button";
    if (mutation) {
      b.dataset.mutation = "true";
      b.disabled = !online;
    }
    b.onclick = async () => {
      b.disabled = true;
      try {
        await fn();
      } catch (e) {
        message(e.message, true);
      } finally {
        b.disabled = mutation && !online;
      }
    };
    return b;
  }
  function query(extra = {}) {
    const p = new URLSearchParams({ workspace_id: ws, ...extra });
    if (project && !p.has("project_id")) p.set("project_id", project);
    for (const [key, value] of [...p]) if (!value) p.delete(key);
    return "?" + p;
  }
  function selectOptions(select, items, value, empty) {
    select.replaceChildren();
    if (empty) {
      const o = el("option", empty);
      o.value = "";
      select.append(o);
    }
    for (const item of items) {
      const o = el("option", item.name || item.title);
      o.value = item.id;
      select.append(o);
    }
    select.value = value;
  }
  function input(form, name, label, value = "", type = "text") {
    const l = el("label", label),
      n = el(type === "textarea" ? "textarea" : "input");
    n.name = name;
    if (type !== "textarea") n.type = type;
    n.value = value ?? "";
    l.append(n);
    form.append(l);
    return n;
  }
  function picker(form, name, label, values, value) {
    const l = el("label", label),
      s = el("select");
    s.name = name;
    s.setAttribute("aria-label", label);
    for (const option of values) {
      const o = el("option", option);
      o.value = option;
      s.append(o);
    }
    s.value = value;
    l.append(s);
    form.append(l);
    return s;
  }
  function editor(title, key, save) {
    const f = el("form", undefined, "editor");
    f.dataset.draftKey = key;
    f.setAttribute("aria-label", title);
    f.append(el("h2", title));
    f.oninput = () => drafts.set(key, Object.fromEntries(new FormData(f)));
    f.onsubmit = async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(f));
      drafts.set(key, data);
      try {
        await save(data);
        drafts.delete(key);
        message("Saved");
      } catch (err) {
        message(err.message, true);
      }
    };
    const submit = el("button", "Save");
    submit.type = "submit";
    submit.dataset.mutation = "true";
    submit.disabled = !online;
    return {
      form: f,
      finish: () => {
        f.append(submit);
        const draft = drafts.get(key);
        if (draft)
          for (const [name, value] of Object.entries(draft)) {
            if (f.elements[name]) f.elements[name].value = value;
          }
        return f;
      },
    };
  }
  let scopeLoading = Promise.resolve(), scopeGeneration = 0;
  function loadScopes() {
    const generation = ++scopeGeneration;
    scopeLoading = (async () => {
    const workspaces = (await api("/workspaces?limit=50")).items;
    if (generation !== scopeGeneration) return;
    if (ws && !workspaces.some(w => w.id === ws)) workspaces.push(await api("/workspaces/" + ws));
    if (!ws)
      ws = workspaces[0]?.id || "";
    selectOptions($("workspace"), workspaces, ws);
    if (!ws) return;
    const projects = (
      await api("/projects" + query({ project_id: "", limit: 50 }))
    ).items;
    if (generation !== scopeGeneration) return;
    if (project && !projects.some(p => p.id === project)) projects.push(await api("/projects/" + project + query({project_id: ""})));
    selectOptions($("project"), projects, project, "All projects");
    })();
    return scopeLoading;
  }
  function scopeSearch(select, path, label) {
    const search = el("input");
    search.setAttribute("aria-label", "Search " + label);
    search.placeholder = "Search " + label;
    const more = button("More " + label, () => load(true));
    select.parentElement.append(search, button("Find " + label, () => load(false)), more);
    let cursor = null, generation = 0;
    async function load(append) {
      const current = ++generation;
      await scopeLoading;
      const selected = select.value, scope = ws, scopes = scopeGeneration;
      const keep = [...select.options].find(o => o.value === selected)?.cloneNode(true);
      const params = {query: search.value, limit: 50};
      if (append && cursor) params.cursor = cursor;
      if (path === "/projects") params.workspace_id = ws;
      const result = await api(path + "?" + new URLSearchParams(params));
      if (current !== generation || scopes !== scopeGeneration || scope !== ws) return;
      if (!append) selectOptions(select, result.items, selected, path === "/projects" ? "All projects" : null);
      else for (const item of result.items) if (![...select.options].some(o => o.value === item.id)) {
        const option = el("option", item.name); option.value = item.id; select.append(option);
      }
      if (keep && ![...select.options].some(o => o.value === selected)) select.append(keep);
      select.value = selected;
      cursor = result.next_cursor; more.hidden = !result.has_more;
    }
  }
  async function sync() {
    if (stream) stream.close();
    if (!ws) return;
    const snapshot = await api("/snapshots", {
      workspace_id: ws,
      filters: { kinds: ["project"] },
      limit: 50,
    });
    sequence = snapshot.sequence;
    while (snapshot.next_cursor) {
      const next = await api(
        "/snapshots/" +
          snapshot.snapshot_id +
          "?cursor=" +
          encodeURIComponent(snapshot.next_cursor),
      );
      snapshot.next_cursor = next.next_cursor;
    }
    connectEvents();
  }
  function connectEvents() {
    if (stream) stream.close();
    stream = new EventSource(
      base + "/events" + query({ after: sequence, project_id: "" }),
    );
    stream.onopen = () => {
      connected(true);
      reconnectDelay = 1000;
    };
    stream.addEventListener("workbench", (e) => {
      let event;
      try {
        event = JSON.parse(e.data);
      } catch {
        return resetSync();
      }
      if (event.schema_version !== 1) return resetSync();
      if (event.sequence <= sequence) return;
      sequence = event.sequence;
      connected(true);
      if ((page === "Board" || page === "Work Items") && refreshBoard) {
        if (event.entity_type !== "work_item" || (project && event.payload.project_id !== project)) return;
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => refreshBoard?.().catch(e => message(e.message, true)), 100);
        return;
      }
      if (
        page === "Board" ||
        page === "Work Items" ||
        page === "Overview" ||
        page === "Activity"
      ) {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(
          () => render().catch((e) => message(e.message, true)),
          100,
        );
      }
    });
    stream.addEventListener("resync", () => resetSync());
    stream.onerror = () => {
      connected(false);
      stream.close();
      setTimeout(
        () => {
          connectEvents();
          reconnectDelay = Math.min(30000, reconnectDelay * 2);
        },
        reconnectDelay + Math.random() * 300,
      );
    };
  }
  function resetSync() {
    connected(false);
    setTimeout(async () => {
      try {
        connected(true);
        await sync();
        await render();
      } catch (e) {
        message(e.message, true);
      }
    }, 1000);
  }
  async function render() {
    clearTimeout(refreshTimer);
    refreshBoard = null;
    const content = $("content").cloneNode(false);
    $("content").replaceWith(content);
    $("page-title").textContent = page;
    $("project-context").textContent =
      ($("workspace").selectedOptions[0]?.textContent || "Workspace") +
      " / " +
      ($("project").selectedOptions[0]?.textContent || "All projects");
    for (const b of $("navigation").children)
      b.setAttribute("aria-current", b.textContent === page ? "page" : "false");
    if (page === "Overview") {
      const status = await api("/status" + query({ project_id: "" }));
      content.append(el("p", "Execution dispatcher: " + status.dispatcher));
      for (const statusName of states)
        content.append(
          button(
            statusName + ": " + (status.counts[statusName] || 0),
            async () => {
              page = "Board";
              await render();
            },
          ),
        );
      return;
    }
    if (page === "Board" || page === "Work Items") {
      await board(content);
      return;
    }
    if (page === "Projects") {
      await projects(content);
      return;
    }
    if (page === "PRDs") {
      await prds(content);
      return;
    }
    if (page === "Agents") {
      await agents(content);
      return;
    }
    if (page === "Runs") {
      await runs(content);
      return;
    }
    if (page === "Activity") {
      const events = await api(
        "/activity" +
          query({ project_id: "", after: Math.max(0, sequence - 200) }),
      );
      for (const event of events.items)
        content.append(
          el(
            "p",
            `${event.sequence} · ${event.event_type} · ${event.actor_id} · ${event.entity_type}`,
          ),
        );
      return;
    }
    if (page === "Settings") {
      await settings(content);
      return;
    }
    content.append(
      el(
        "p",
        "Execution features are unavailable until their implementation gate passes.",
      ),
    );
  }
  async function projects(content) {
    const e = editor("Create project", "new-project", async (data) => {
      await api("/projects", {
        command_id: crypto.randomUUID(),
        workspace_id: ws,
        key: data.key,
        name: data.name,
        repository_ref: data.repository_ref || null,
      });
      await loadScopes();
      await render();
    });
    input(e.form, "name", "Project name");
    input(e.form, "key", "Ticket prefix (e.g. ACC)");
    input(e.form, "repository_ref", "Registered repository ID (optional)");
    content.append(e.finish());
    const repositories = await api("/repositories");
    const help = el("details");
    help.append(el("summary", "Registered repositories"));
    for (const repo of repositories.items)
      help.append(el("p", repo.name + " · " + repo.id));
    content.append(help);
    await paged(
      "/projects",
      content,
      (p) => {
        const record = el("article", undefined, "record");
        record.append(button(p.key + " · " + p.name, () => projectDetail(p)));
        content.append(record);
      },
      { project_id: "" },
    );
  }
  async function paged(path, content, draw, extra = {}) {
    const rows = await api(path + query({ limit: 50, ...extra }));
    for (const row of rows.items) draw(row);
    if (rows.next_cursor) {
      const more = button("Load more", async () => {
        more.remove();
        await paged(path, content, draw, {
          ...extra,
          cursor: rows.next_cursor,
        });
      });
      content.append(more);
    }
  }
  async function projectDetail(p) {
    const target = $("detail");
    target.replaceChildren();
    const e = editor("Edit project", p.id, async (data) => {
      await api(
        "/projects/" + p.id + query({ project_id: "" }),
        {
          command_id: crypto.randomUUID(),
          expected_revision: p.revision,
          name: data.name,
          description: data.description,
          default_privacy: data.default_privacy,
        },
        "PATCH",
      );
      await loadScopes();
      await render();
    });
    input(e.form, "name", "Name", p.name);
    input(e.form, "description", "Description", p.description, "textarea");
    picker(
      e.form,
      "default_privacy",
      "Inference policy",
      ["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"],
      p.default_privacy,
    );
    target.append(
      e.finish(),
      button(
        "Archive project",
        async () => {
          await api(
            "/projects/" + p.id + "/archive" + query({ project_id: "" }),
            { command_id: crypto.randomUUID(), expected_revision: p.revision },
          );
          target.replaceChildren();
          await loadScopes();
          await render();
        },
        true,
      ),
    );
  }
  const prdText = ["problem_statement", "background", "architecture_notes"],
    prdLists = [
      "objectives",
      "scope",
      "out_of_scope",
      "constraints",
      "assumptions",
      "dependencies",
    ],
    prdObjects = [
      "functional_requirements",
      "nonfunctional_requirements",
      "risks",
      "acceptance_criteria",
      "milestones",
    ];
  function specForm(f, spec, type) {
    const text =
        type === "prd"
          ? prdText
          : ["objective", "description", "rationale", "context_notes"],
      lists =
        type === "prd"
          ? prdLists
          : [
              "acceptance_criteria",
              "definition_of_done",
              "allowed_paths",
              "expected_artifacts",
              "validation_checks",
              "constraints",
              "relevant_files",
              "required_capabilities",
              "required_tools",
              "required_skills",
              "verifier_requirements",
              "requirement_ids",
            ];
    for (const field of text)
      input(
        f,
        "spec." + field,
        field.replaceAll("_", " "),
        spec[field] || "",
        "textarea",
      );
    for (const field of lists)
      input(
        f,
        "spec." + field,
        field.replaceAll("_", " ") + " (one per line)",
        (spec[field] || []).join("\n"),
        "textarea",
      );
    if (type === "prd") {
      const marker = el("input"); marker.type="hidden";marker.name="prd_entries";marker.value="true";f.append(marker);
      const saved = drafts.get(f.dataset.draftKey);
      if (saved?.prd_entries) spec = {...spec, ...readSpec(saved)};
      for (const field of prdObjects) {
      const section = el("fieldset"), rows = el("div");
      section.append(el("legend", field.replaceAll("_", " ")), rows); f.append(section);
      let next = 0;
      function add(value = {}) {
        if (rows.children.length >= 100) throw Error("At most 100 entries per section");
        const index = next++, row = el("fieldset"), prefix = "entry." + field + "." + index + ".";
        row.append(el("legend", field.replaceAll("_", " ") + " entry " + (index + 1)));
        const names = ["risks", "milestones"].includes(field) ? field === "risks" ? ["description", "impact", "mitigation"] : ["title", "requirement_ids"] : ["id", "text"];
        for (const name of names) {
          const fallback = name === "id" ? (field === "functional_requirements" ? "FR" : field === "nonfunctional_requirements" ? "NFR" : "AC") + "-" + (index + 1) : "";
          const inputNode = input(row, prefix + name, field.replaceAll("_", " ") + " " + name.replaceAll("_", " ") + " " + (index + 1), Array.isArray(value[name]) ? value[name].join("\n") : value[name] || fallback, name === "id" || name === "title" ? "text" : "textarea");
          inputNode.maxLength = name === "id" ? 128 : 4000;
        }
        row.append(button("Remove entry " + (index + 1), () => { row.remove(); f.dispatchEvent(new Event("input")); })); rows.append(row);
      }
      for (const value of spec[field] || []) add(value);
      section.append(button("Add " + field.replaceAll("_", " "), () => { add(); f.dispatchEvent(new Event("input")); }));
      }
    }
  }
  function readSpec(data) {
    const spec = {}, entries = {};
    for (const [key, value] of Object.entries(data)) {
      if (key.startsWith("spec.")) {
        const field = key.slice(5);
        spec[field] = [
          ...prdText,
          "objective",
          "description",
          "rationale",
          "context_notes",
        ].includes(field)
          ? value
          : value
              .split("\n")
              .map((v) => v.trim())
              .filter(Boolean);
      }
      if (key.startsWith("json.")) spec[key.slice(5)] = JSON.parse(value);
      if (key.startsWith("entry.")) {
        const [, field, index, name] = key.split(".");
        entries[field] ||= {}; entries[field][index] ||= {};
        entries[field][index][name] = name === "requirement_ids" ? value.split("\n").map(v => v.trim()).filter(Boolean) : value;
      }
    }
    if (data.prd_entries) for (const field of prdObjects) spec[field] = Object.values(entries[field] || {});
    return spec;
  }
  async function prds(content) {
    let deliveryCursor=null;
    const batches=el("section"), moreDeliveries=button("More feature deliveries",()=>loadDeliveries());
    content.append(batches,moreDeliveries);
    async function loadDeliveries() {
      const result=await api("/delivery-batches"+query({limit:20,...(deliveryCursor?{cursor:deliveryCursor}:{})}));
      if(!batches.isConnected)return;
      for(const batch of result.items)batches.append(button("Feature delivery · "+batch.state,()=>deliveryDetail(batch.id)));
      deliveryCursor=result.next_cursor;moreDeliveries.hidden=!result.has_more;
    }
    await loadDeliveries();
    content.append(button("Create PRD", () => prdDetail(null), true));
    content.append(button("Generate PRD", () => planningForm(null), true));
    const recent = await api("/operations" + query({limit:10}));
    for (const entry of recent.items.filter(entry => ["generate", "decompose"].includes(entry.kind))) content.append(button("Operation · " + entry.kind + " · " + entry.state, () => {
      $("detail").replaceChildren(); return operation(entry, $("detail"));
    }));
    await paged("/prds", content, (p) =>
      content.append(
        button(p.title + " · " + p.status + " · revision " + p.revision, () =>
          prdDetail(p),
        ),
      ),
    );
  }
  async function operation(result, target, finished) {
    const display = el("section"); display.setAttribute("aria-live", "polite");
    const scope = ws;
    target.append(display);
    let timer, watching = true;
    target.append(button("Stop watching", () => { watching=false; clearTimeout(timer); display.append(el("p", "Observation stopped. Inference may continue; no cancellation or refund is implied.")); }));
    async function refresh() {
      if (!watching || !display.isConnected || scope !== ws) return;
      const current = await api("/operations/" + result.id + query());
      if (!watching || !display.isConnected || scope !== ws) return;
      display.replaceChildren(el("h3", "Operation " + current.state), el("p", "Request " + current.id + " · budget estimate USD " + current.cost.estimated_usd + " · uncertain calls " + current.cost.uncertain_calls));
      display.append(el("p", "Model policy: " + (current.selected_models?.primary_model_id || "Automatic routing") + " · actual calls: " + (current.actual_models?.map(call => call.model + " (" + call.role + ", " + call.usage_source + ")").join("; ") || "No calls observed")));
      if (current.error_code) display.append(el("p", current.error_code + ". Review the outcome before making another request."));
      if (current.state === "COMPLETED") {
        watching=false;
        if (finished) await finished({...current.result, operation_id:current.id});
        else if (current.result?.prd_id) await prdDetail(await api("/prds/" + current.result.prd_id + query()));
      } else if (["PENDING", "RUNNING"].includes(current.state)) timer=setTimeout(() => refresh().catch(error => message(error.message,true)), 2000);
    }
    target.append(button("Refresh operation", refresh));
    await refresh();
  }
  async function planningForm(prd) {
    const target = $("detail");
    target.replaceChildren();
    if (!project) throw Error("Select a project first");
    if (prd) target.append(el("p", "Approved PRD revision " + prd.revision + " · " + prd.title + ". Breakdown creates a proposal; import and execution require separate review."));
    let quote, quoted;
    const preview = el("pre");
    const e = editor(
      prd ? "Decompose approved PRD" : "Generate PRD",
      "planning-" + (prd?.id || project),
      async (data) => {
        const body = planningInput(data);
        if (!quote || JSON.stringify(body) !== quoted)
          throw Error("Quote this input before requesting inference");
        const result = await api(
          prd ? "/prds/" + prd.id + "/decompose" : "/prds/generate",
          { ...body, command_id: crypto.randomUUID(), quote: quote.quote },
        );
        await operation(
          result,
          target,
          prd ? async (result) => importPreview(result, target) : async result => prdDetail(await api("/prds/" + result.prd_id + query())),
        );
      },
    );
    function planningInput(data) {
      const body = {
        workspace_id: ws,
        project_id: project,
        kind: prd ? "decompose" : "generate",
        budget: data.budget,
        template_id: "prd-default",
        template_version: 1,
        title: data.title || "Generated PRD",
        constraints: data.constraints.split("\n").map(value => value.trim()).filter(Boolean),
        model_selection: data.planner_model ? {mode:"pinned", primary_model_id:data.planner_model, fallback_model_ids:[]} : {mode:"auto"},
      };
      if (prd)
        Object.assign(body, {
          prd_id: prd.id,
          expected_revision: prd.revision,
          allowed_paths: data.allowed_paths
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
        });
      else body.requirement = data.requirement;
      return body;
    }
    input(e.form, "budget", "Planning budget USD", "0.10");
    input(e.form, "title", "Generated title (optional)", prd?.title || "");
    input(e.form, "constraints", "Constraints (one per line)", "", "textarea");
    const models = await api("/model-options" + query({purpose:"planner", limit:100}));
    const planner = picker(e.form, "planner_model", "Planner model", [], "");
    selectOptions(planner, models.items.map(model => ({id:model.id, name:model.provider + " · " + model.deployment + " · " + model.locality + (!model.eligible ? " · " + model.reason_codes.join(", ") : "")})), "", "Automatic routing");
    for (const option of planner.options) if (option.value) option.disabled=!models.items.find(model => model.id === option.value).eligible;
    input(
      e.form,
      prd ? "allowed_paths" : "requirement",
      prd ? "Allowed paths, one per line" : "Requirement text",
      "",
      "textarea",
    );
    target.append(
      e.finish(),
      button(
        "Quote inference",
        async () => {
          const body = planningInput(Object.fromEntries(new FormData(e.form)));
          quote = await api("/planning/quote", body);
          quoted = JSON.stringify(body);
          preview.textContent = "Privacy " + quote.privacy + " · maximum USD " + quote.budget + " · eligible planners " + quote.eligible_models.join(", ") + ". No inference has run. Save confirms generation only.";
        },
        true,
      ),
      preview,
    );
  }
  async function importPreview(proposal, target) {
    const body = {
      command_id: crypto.randomUUID(),
      workspace_id: ws,
      project_id: proposal.project_id,
      prd_id: proposal.prd_id,
      prd_revision: proposal.prd_revision,
      operation_id: proposal.operation_id,
      plans: proposal.batches.map((b) => b.plan),
      requirement_groups: proposal.batches.map(batch => batch.requirement_ids),
      allowed_paths: [
        ...new Set(proposal.items.flatMap((i) => i.spec.allowed_paths)),
      ],
    };
    const staged = await api("/plan-imports", body);
    const selected = new Map();
    target.append(el("h3", "Import preview · PRD revision " + staged.prd_revision), el("p", "Imported tasks remain BACKLOG. No execution is started."));
    for (const task of staged.proposal.items) {
      const row=el("article", undefined, "record"), label=el("label", "Import " + task.title), check=el("input"); check.type="checkbox"; check.checked=true; label.append(check); selected.set(task.proposal_id, check);
      row.append(label, el("p", task.spec.objective), el("p", "Done: " + task.spec.definition_of_done.join("; ")),
        el("p", "Requirement coverage: " + task.spec.requirement_ids.join(", ")), el("p", "Registered validation: " + task.spec.validation_checks.join(", ")), el("p", "Allowed paths: " + task.spec.allowed_paths.join(", ")));
      target.append(row);
    }
    const graph=el("table"); graph.append(el("caption", "Dependency graph: upstream task must complete first"));
    for (const [upstream, downstream] of staged.proposal.edges) {const row=el("tr"); row.append(el("td", upstream), el("td", "→"), el("td", downstream)); graph.append(row);} target.append(graph);
    target.append(button(
        "Import selected tasks",
        async () => {
          const actions = Object.fromEntries(
            staged.proposal.items.map((i) => [i.proposal_id, selected.get(i.proposal_id).checked ? "create" : "skip"]),
          );
          const imported=await api("/plan-imports/" + staged.id + "/commit" + query(), {
            command_id: crypto.randomUUID(),
            expected_revision: staged.revision,
            source_hash: staged.source_hash,
            actions,
          });
          await render();
          await deliveryReview(staged, Object.values(imported.items));
        },
        true,
      ));
  }
  async function deliveryReview(imported,itemIds) {
    const target=$("detail"), scope=ws;
    target.replaceChildren();
    const panel=el("section"), choices=new Map();
    panel.append(el("h2","Review feature execution"),el("p","Review tasks, agents, model policy, paths, checks and spending. Human completion review remains a separate decision."));
    const agents=(await api("/agents"+query({limit:50}))).items;
    for (const id of itemIds) {
      const item=await api("/work-items/"+id+query());
      const row=el("article",undefined,"record"),label=el("label","Execute "+item.display_key+" · "+item.title),selected=el("input"); selected.type="checkbox";selected.checked=true;label.append(selected);
      const agent=picker(row,"agent-"+id,"Agent for "+item.display_key,agents.map(a=>a.id),agents[0]?.id||"");
      for (const option of agent.options) {const definition=agents.find(a=>a.id===option.value);option.textContent=definition.name+" · "+definition.role;}
      row.append(label,el("p","Allowed paths: "+item.spec.allowed_paths.join(", ")),el("p","Registered checks: "+item.spec.validation_checks.join(", ")));
      choices.set(id,{selected,agent,item});panel.append(row);
    }
    const budget=input(panel,"delivery-budget","Feature maximum USD","0.10","number");budget.step="0.01";budget.min="0";
    const results=el("section"),start=button("Start approved feature",async()=>{
      if (!reviewed) throw Error("Preview the current selection before starting.");
      const result=await api("/delivery-batches",{command_id:crypto.randomUUID(),workspace_id:scope,preview:reviewed.preview,selection_hash:reviewed.selection_hash});
      await deliveryDetail(result.id);
    },true);
    let reviewed=null;start.hidden=true;
    panel.addEventListener("input",()=>{reviewed=null;start.hidden=true;results.replaceChildren(el("p","Selection changed. Preview again before approval."));});
    panel.addEventListener("change",()=>{reviewed=null;start.hidden=true;});
    panel.append(button("Preview feature execution",async()=>{
      const selected=[...choices].filter(([,value])=>value.selected.checked);
      reviewed=await api("/delivery-batches/preview",{workspace_id:scope,project_id:imported.project_id,import_id:imported.id,item_ids:selected.map(([id])=>id),contract_revisions:Object.fromEntries(selected.map(([id,value])=>[id,value.item.contract_revision])),agents:Object.fromEntries(selected.map(([id,value])=>[id,value.agent.value])),budget:budget.value,expires_at:Date.now()+3600000});
      results.replaceChildren(el("h3","Execution approval preview"),el("p","Maximum USD "+budget.value+" · approval expires in one hour · no inference yet"));
      for (const member of reviewed.scope.members) results.append(el("p",member.item_id+" · agent revision "+member.agent_revision+" · models "+Object.entries(member.model_bindings).map(([role,binding])=>role+": "+(binding.mode==="auto"?"automatic":binding.primary_model_id+(binding.fallback_model_ids.length?" then "+binding.fallback_model_ids.join(" then "):""))).join("; ")));
      for (const blocker of reviewed.blockers) results.append(el("p",blocker.reason+" · complete upstream ticket "+blocker.dependency_id,"error"));
      start.hidden=reviewed.blockers.length>0;
    }),results,start);
    target.append(panel);
  }
  async function deliveryDetail(id) {
    const target=$("detail"),scope=ws,panel=el("section"),heading=el("h2","Feature delivery"),status=el("p"),summary=el("p"),controls=el("div"),members=el("section");
    target.replaceChildren(panel);panel.append(heading,status,summary,controls,members);
    let current=null,cursor=null,generation=0;
    const more=button("More delivery members",()=>load(true));panel.append(more);
    async function load(append=false) {
      const revision=++generation;
      const result=await api("/delivery-batches/"+id+query({workspace_id:scope,limit:50,...(append&&cursor?{cursor}:{})}));
      if(revision!==generation||!panel.isConnected||ws!==scope)return;
      current=result;status.textContent="Delivery "+result.state+" · "+result.total_members+" selected tasks";
      summary.textContent="Settled ledger debit USD "+(result.budget.charged_microusd/1000000).toFixed(6)+" · held allowance USD "+(result.budget.held_microusd/1000000).toFixed(6)+" · uncertain reservations "+(result.budget.uncertain_reservations||0)+". Human review must finish before delivery is completed.";
      controls.replaceChildren();
      for(const action of result.state==="PAUSED"?["resume","cancel"]:["APPROVED","RUNNING"].includes(result.state)?["pause","cancel"]:result.state==="NEEDS_REVIEW"?["cancel"]:[]) controls.append(button(action+" delivery",async()=>{await api("/delivery-batches/"+id+"/commands"+query({workspace_id:scope}),{command_id:crypto.randomUUID(),expected_revision:current.revision,action,reason:"Operator "+action+" from reviewed delivery"});await load();},true));
      if(!append)members.replaceChildren();
      for(const member of result.members) {
        const row=el("article",undefined,"record");row.append(button(member.display_key+" · "+member.title,async()=>itemDetail(await api("/work-items/"+member.item_id+query({workspace_id:scope})))),el("p",member.status+" · coordination "+member.member_state+" · "+(member.agent_name||"No attempt yet")),el("p",member.blocked_reason?"Next action: "+member.blocked_reason:member.status==="REVIEW"?"Next action: review current checks and approve completion":"Current evidence remains authoritative"));
        if(member.assigned_agent_id)row.append(button("Agent evidence",()=>agentHistory({id:member.assigned_agent_id,name:member.agent_name||"Agent"})));
        members.append(row);
      }
      cursor=result.next_cursor;more.hidden=!result.has_more;
    }
    await load();
    const timer=setInterval(()=>{if(!panel.isConnected||ws!==scope){clearInterval(timer);return;} if(!cursor)load().catch(e=>message(e.message,true));},2000);
    panel.append(el("p","Paged member lists stay in place. Use Refresh delivery members for current status."));
    panel.append(button("Refresh delivery members",()=>load()));
  }
  async function agents(content) {
    const search = input(content, "agent-search", "Search agents");
    const rows = el("section"); rows.setAttribute("aria-label", "Agents");
    let cursor = null, generation = 0;
    const more = button("More agents", () => load(false));
    content.append(button("Find agents", () => load(true)), button("Create agent", () => agentForm()), rows, more);
    async function load(reset) {
      const current = ++generation;
      if (reset) cursor = null;
      const result = await api("/agents" + query({limit: 50, query: search.value, ...(cursor ? {cursor} : {})}));
      if (current !== generation) return;
      if (reset) rows.replaceChildren();
      for (const agent of result.items) {
        const record = el("article", undefined, "record");
        const primary = agent.model_bindings.executor;
        record.append(button(agent.name, () => agentProfile(agent)),
          el("p", agent.role + " · " + agent.adapter_kind + " · Worker " + agent.state + " · " +
            (primary.mode === "auto" ? "Automatic model routing" : primary.primary_model_id)),
          el("p", "Model eligibility checked for each ticket; worker availability does not prove provider readiness."),
          button("History: " + agent.name, () => agentHistory(agent)));
        rows.append(record);
      }
      cursor = result.next_cursor; more.hidden = !result.has_more;
      if (!result.items.length && reset) rows.append(el("p", "No agents match."));
    }
    await load(true);
  }
  async function agentProfile(agent) {
    const target = $("detail"), scope = ws;
    const result = await api("/agents/" + agent.id + query());
    if (scope !== ws) return;
    target.replaceChildren(el("h2", result.definition.name), el("p", "Load " + result.load + "/" + result.capacity + " · " + result.model_eligibility));
    target.append(button("History: " + agent.name, () => agentHistory(agent)), button("Performance", () => agentPerformance(agent)));
    for(const connector of result.connectors||[]) target.append(el("p","Connector "+connector.name+" · reachability "+connector.status+" · "+(connector.observed_at?new Date(connector.observed_at).toLocaleString():"no remote observation yet")+". Host worker capacity is separate."));
    if (result.can_configure) {
      target.append(button("Edit agent", () => agentForm(result.definition)), button(result.definition.enabled ? "Disable agent" : "Enable agent", async () => {
        const {id, revision, created_at, updated_at, created_by, updated_by, ...fields} = result.definition;
        await api("/agents/" + id, {...fields, enabled: !fields.enabled, expected_revision: revision, command_id: crypto.randomUUID()}, "PATCH");
        await render();
      }, true));
    }
    const current = el("section"); current.append(el("h3", "Current work"));
    for (const work of result.current_work) current.append(button(work.display_key + " · " + work.title, () => itemDetail({id: work.item_id})), el("p", work.state || work.status));
    if (!result.current_work.length) current.append(el("p", "No current work."));
    const config = el("details"); config.append(el("summary", "Configuration inspector"), el("pre", JSON.stringify(result.definition, null, 2)));
    target.append(current, config);
  }
  async function agentForm(agent = null) {
    const scope = ws, target = $("detail");
    const [registered, repositories, workspaces, projects] = await Promise.all([
      api("/agent-options"), api("/repositories"), api("/workspaces?limit=100"), api("/projects" + query({limit: 100}))]);
    if (scope !== ws) return;
    target.replaceChildren();
    const defaults = agent || {name: "Engine", role: "engineer", enabled: true, privacy_policy: "LOCAL_ONLY", concurrency_limit: 1,
      allowed_workspaces: [ws], allowed_projects: [], repository_permissions: [], capabilities: ["code"], tools: ["propose_file_changes"], skills: [], model_bindings: {}};
    const choices = {};
    const e = editor(agent ? "Edit agent" : "Create agent", "agent-" + (agent?.id || "new"), async data => {
      const fields = {name: data.name, role: data.role, enabled: data.enabled === "true", privacy_policy: data.privacy_policy,
        concurrency_limit: Number(data.concurrency_limit), adapter_kind: "engine", supported_model_roles: ["executor", "repairer", "reviewer"], model_bindings: {}};
      for (const [name, control] of Object.entries(choices)) fields[name] = [...control.selectedOptions].map(option => option.value);
      for (const role of ["executor", "repairer", "reviewer"]) {
        fields.model_bindings[role] = data[role + "_model"] ? {mode: "pinned", primary_model_id: data[role + "_model"],
          fallback_model_ids: [1,2,3,4].map(n => data[role + "_fallback_" + n]).filter(Boolean)} : {mode: "auto"};
      }
      await api("/agents" + (agent ? "/" + agent.id : ""), {...fields, command_id: crypto.randomUUID(), ...(agent ? {expected_revision: agent.revision} : {})}, agent ? "PATCH" : "POST");
      target.replaceChildren(el("p", "Agent saved.")); await render();
    });
    input(e.form, "name", "Agent name", defaults.name);
    input(e.form, "role", "Agent role", defaults.role);
    picker(e.form, "enabled", "Enabled", ["true", "false"], String(defaults.enabled));
    picker(e.form, "privacy_policy", "Agent privacy", ["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"], defaults.privacy_policy);
    const capacity = input(e.form, "concurrency_limit", "Worker capacity", defaults.concurrency_limit, "number"); capacity.min=1; capacity.max=16;
    for (const [name, label, options] of [
      ["allowed_workspaces", "Allowed workspaces", workspaces.items], ["allowed_projects", "Allowed projects (empty permits all in granted workspaces)", projects.items],
      ["repository_permissions", "Registered repositories", repositories.items], ["tools", "Trusted tools", registered.tools.map(id => ({id, name:id}))],
      ["skills", "Trusted skills", registered.skills], ["capabilities", "Capabilities", registered.capabilities.map(id => ({id, name:id}))]]) {
      const control = picker(e.form, name, label, [], ""); control.multiple = true; control.size = Math.min(5, Math.max(2, options.length));
      selectOptions(control, options, "");
      for (const option of control.options) option.selected = (defaults[name] || []).includes(option.value);
      choices[name] = control;
    }
    for (const role of ["executor", "repairer", "reviewer"]) {
      const result = await api("/model-options" + query({purpose: role, limit: 100}));
      const binding = defaults.model_bindings[role] || {mode: "auto"};
      const options = result.items.map(model => ({id:model.id, name:model.provider + " · " + model.deployment + " · " + model.locality + (!model.eligible ? " · " + model.reason_codes.join(", ") : ""), eligible:model.eligible}));
      for (const [name, label, value] of [[role + "_model", role + " model", binding.primary_model_id || ""],
        ...[1,2,3,4].map(n => [role + "_fallback_" + n, role + " fallback " + n, binding.fallback_model_ids?.[n-1] || ""])]) {
        const control = picker(e.form, name, label, [], ""); selectOptions(control, options, value, name.endsWith("_model") ? "Automatic routing" : "No fallback");
        for (const option of control.options) if (option.value) option.disabled = !options.find(model => model.id === option.value).eligible;
      }
    }
    e.form.append(el("p", "Fallbacks run in numbered order. Automatic routing has no explicit fallback list. Review uses a different model from the implementer when required."));
    target.append(e.finish());
  }
  async function agentHistory(agent, window={}) {
    const target = $("detail"),scope=ws; target.replaceChildren(el("h2", agent.name + " · History"));
    let cursor = null;
    const more = button("More agent activity", load); target.append(more);
    async function load() {
      const result = await api("/agents/" + agent.id + "/activity" + query({limit: 25, ...window, ...(cursor ? {cursor} : {})}));
      if(scope!==ws || !more.isConnected)return;
      for (const entry of result.items) {
        const row = el("article", undefined, "record");
        row.append(el("p", new Date(entry.occurred_at).toLocaleString() + " · " + entry.actor_type + " " + entry.actor_id),
          el("p", entry.event_type.replaceAll("_", " ") + " · " + (entry.metadata.agent_name || "Historical agent") +
            (entry.metadata.model_id ? " · " + entry.metadata.model_id : "")));
        if (entry.item_id) row.append(button("Open ticket", () => itemDetail({id: entry.item_id})));
        if (entry.attempt_id) row.append(button("Open attempt", () => attemptDetail(entry.attempt_id, target)));
        target.append(row);
      }
      cursor = result.next_cursor; more.hidden = !result.has_more;
      if (!result.items.length) target.append(el("p", "No activity in this workspace."));
    }
    await load();
  }
  async function agentPerformance(agent) {
    const target = $("detail"), scope = ws;
    target.replaceChildren(el("h2", agent.name + " · Performance"));
    const days = picker(target, "metrics-days", "Period", ["7", "30", "90"], "30");
    const charts = el("section"); charts.setAttribute("aria-live", "polite"); target.append(charts);
    let generation = 0;
    days.onchange = () => load().catch(error => message(error.message, true));
    async function load() {
      const current = ++generation, end = Date.now(), start = end - Number(days.value)*86400000;
      const result = await api("/agents/" + agent.id + "/metrics" + query({from:start, to:end, bucket:"day"}));
      if (scope !== ws || generation !== current || !charts.isConnected) return;
      charts.replaceChildren(el("p", result.cost_label), el("p", "Metric version " + result.metric_version + " · " + result.totals.duration_samples + " duration samples · " + result.totals.unknown_calls + " unknown calls · " + result.totals.uncertain_calls + " uncertain calls"));
      if (!result.totals.attempts) charts.append(el("p", "No completed work in this period."));
      const totals = result.totals;
      charts.append(el("p", "Success: " + (totals.success_rate === null ? "Unknown" : Math.round(totals.success_rate*100) + "%") + " · denominator " + totals.success_denominator + " · p50 " + (totals.p50_duration_ms ?? "Unknown") + " ms · p95 " + (totals.p95_duration_ms ?? "Unknown") + " ms"));
      charts.append(el("p","Current load "+result.current_load.load+"/"+(result.current_load.capacity??"Unknown")+" · queued "+result.current_load.queued+" · running "+result.current_load.running+" · observed "+new Date(result.current_load.observed_at).toLocaleTimeString()),el("p","Queue p50 "+(totals.p50_queue_ms??"Unknown")+" ms · p95 "+(totals.p95_queue_ms??"Unknown")+" ms · "+totals.queue_samples+" samples"));
      for (const [field, label, unit] of [["succeeded", "Successful attempts", "attempts"], ["failed", "Failed attempts", "attempts"], ["mean_duration_ms", "Execution duration", "ms"], ["priced_usage_cost_microusd", "Priced usage estimate", "micro-USD"]]) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 600 120"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label + " in " + unit + " over " + days.value + " days");
        const maximum = Math.max(1, ...result.buckets.map(row => row[field] || 0));
        result.buckets.forEach((row, index) => {
          if (row[field] === null) return;
          const rect = document.createElementNS(svg.namespaceURI, "rect");
          rect.setAttribute("x", index*600/result.buckets.length); rect.setAttribute("y", 110-row[field]/maximum*100);
          rect.setAttribute("width", Math.max(1, 600/result.buckets.length-2)); rect.setAttribute("height", row[field]/maximum*100); rect.setAttribute("fill", "currentColor");
          const title = document.createElementNS(svg.namespaceURI, "title"); title.textContent=new Date(row.bucket_start).toISOString().slice(0,10) + ": " + row[field] + " " + unit; rect.append(title); svg.append(rect);
        });
        charts.append(el("h3", label + " (" + unit + ")"), svg);
      }
      const table = el("table"), heading = el("tr"); table.append(el("caption", "Daily values in UTC; unknown values are not zero"));
      for (const label of ["Day", "Succeeded", "Failed", "Duration ms", "Priced estimate micro-USD"]) {const th=el("th", label); th.scope="col"; heading.append(th);} table.append(heading);
      for (const row of result.buckets) {
        const tr=el("tr"), day=el("td");day.append(button(new Date(row.bucket_start).toISOString().slice(0,10),()=>agentHistory(agent,{from:Math.max(start,row.bucket_start),to:Math.min(end,row.bucket_start+86400000)})));tr.append(day); for (const value of [row.succeeded, row.failed, row.mean_duration_ms, row.priced_usage_cost_microusd]) tr.append(el("td", value ?? "Unknown")); table.append(tr);
      }
      charts.append(table, el("p", result.exclusions.join("; ")));
    }
    await load();
  }
  function reconcileForm(prd) {
    const target = $("detail");
    target.replaceChildren();
    let staged;
    const preview = el("pre");
    const e = editor(
      "Reconcile approved PRD",
      "reconcile-" + prd.id,
      async (data) => {
        if (!staged) throw Error("Preview reconciliation first");
        await api(
          "/plan-imports/" + staged.id + "/reconcile-commit" + query(),
          {
            command_id: crypto.randomUUID(),
            expected_revision: staged.revision,
            source_hash: staged.source_hash,
            actions: JSON.parse(data.actions),
            item_revisions: JSON.parse(data.item_revisions),
          },
        );
        await render();
      },
    );
    const source = input(
      e.form,
      "input",
      "Reconciliation input JSON (plans or completed operation)",
      "{}",
      "textarea",
    );
    const actions = input(
      e.form,
      "actions",
      "Selected actions JSON",
      "{}",
      "textarea",
    );
    const revisions = input(
      e.form,
      "item_revisions",
      "Compared item revisions JSON",
      "{}",
      "textarea",
    );
    target.append(
      e.finish(),
      button(
        "Preview reconciliation",
        async () => {
          staged = await api("/prds/" + prd.id + "/reconcile", {
            ...JSON.parse(source.value),
            command_id: crypto.randomUUID(),
            workspace_id: ws,
            project_id: prd.project_id,
            prd_id: prd.id,
            prd_revision: prd.revision,
          });
          preview.textContent = JSON.stringify(staged, null, 2);
          actions.value = JSON.stringify(
            Object.fromEntries(
              staged.proposal.items.map((i) => [
                i.proposal_id,
                staged.proposal.comparison[i.proposal_id] ? "keep" : "create",
              ]),
            ),
            null,
            2,
          );
          revisions.value = JSON.stringify(
            Object.fromEntries(
              Object.values(staged.proposal.comparison)
                .filter((v) => v.item_id)
                .map((v) => [v.item_id, v.revision]),
            ),
            null,
            2,
          );
        },
        true,
      ),
      preview,
    );
  }
  async function runs(content) {
    const items = await api("/work-items" + query({ limit: 50 }));
    for (const item of items.items) {
      const attempts = await api(
        "/work-items/" + item.id + "/attempts" + query({ limit: 5 }),
      );
      for (const attempt of attempts.items)
        content.append(
          button(
            item.display_key +
              " · attempt " +
              attempt.ordinal +
              " · " +
              attempt.state,
            () => attemptDetail(attempt.id, content),
          ),
        );
    }
  }
  async function attemptDetail(id, target) {
    const attempt = await api("/attempts/" + id + query());
    target.append(
      el("h3", "Attempt " + attempt.ordinal),
      el("pre", JSON.stringify(attempt, null, 2)),
    );
    for (const section of ["verification", "artifacts", "receipt"])
      target.append(
        button("Show " + section, async () => {
          const result = await api("/attempts/" + id + "/" + section + query());
          target.append(el("pre", JSON.stringify(result, null, 2)));
          if (section === "artifacts")
            for (const artifact of result.items)
              target.append(
                button(
                  "Read " + artifact.relative_path_or_external_reference,
                  async () => {
                    const content = await api(
                      "/artifacts/" + artifact.id + "/content" + query(),
                    );
                    target.append(el("pre", content.content));
                  },
                ),
              );
        }),
      );
  }
  async function executionDetail(item, target) {
    if (item.execution_kind === "ENGINE" && item.status === "READY")
      target.append(
        button(
          "Preview assignment",
          async () => {
            const preview = await api(
              "/work-items/" + item.id + "/assignment-preview" + query(),
              {},
            );
            target.append(el("pre", JSON.stringify(preview, null, 2)));
            for (const candidate of preview.eligible)
              target.append(
                button(
                  "Assign " + candidate.agent_id,
                  async () => {
                    await api("/work-items/" + item.id + "/assign" + query(), {
                      command_id: crypto.randomUUID(),
                      expected_revision: preview.item_revision,
                      agent_id: candidate.agent_id,
                      policy_revision_hash: preview.policy_revision_hash,
                    });
                    await itemDetail(item);
                    await render();
                  },
                  true,
                ),
              );
          },
          true,
        ),
      );
    if (
      item.execution_kind === "ENGINE" &&
      ["ASSIGNED", "WAITING_FOR_HUMAN"].includes(item.status) &&
      !item.active_attempt_id
    )
      target.append(
        button(
          "Review execution contract",
          async () => {
            const preflight = await api(
              "/work-items/" + item.id + "/execution-preflight" + query(),
              {},
            );
            target.append(
              el("pre", JSON.stringify(preflight, null, 2)),
              button(
                "Approve execution",
                async () => {
                  await api("/work-items/" + item.id + "/commands" + query(), {
                    command_id: crypto.randomUUID(),
                    expected_revision: item.revision,
                    command: "approve_execution",
                    contract_revision: item.contract_revision,
                    contract_hash: preflight.contract_hash,
                    policy_revision_hash: preflight.policy_revision_hash,
                    reason: "Human reviewed displayed execution contract",
                  });
                  await itemDetail(item);
                },
                true,
              ),
              button(
                "Run approved item",
                async () => {
                  await api("/work-items/" + item.id + "/commands" + query(), {
                    command_id: crypto.randomUUID(),
                    expected_revision: item.revision,
                    command: "request_execution",
                  });
                  await itemDetail(item);
                  await render();
                },
                true,
              ),
            );
          },
          true,
        ),
      );
    if (["REVIEW", "WAITING_FOR_HUMAN", "DONE"].includes(item.status))
      target.append(
        button("Show current evidence", async () => {
          const evidence = await api(
            "/work-items/" + item.id + "/evidence" + query(),
          );
          target.append(el("pre", JSON.stringify(evidence, null, 2)));
        }),
      );
    const attempts = await api(
      "/work-items/" + item.id + "/attempts" + query({ limit: 10 }),
    );
    for (const attempt of attempts.items) {
      target.append(
        button("Attempt " + attempt.ordinal + " · " + attempt.state, () =>
          attemptDetail(attempt.id, target),
        ),
      );
      if (attempt.state === "RECONCILING")
        for (const action of ["inspect", "resume", "rollback", "discard"])
          target.append(
            button(
              "Recovery " + action,
              async () => {
                const result = await api(
                  "/attempts/" + attempt.id + "/recovery" + query(),
                  {
                    command_id: crypto.randomUUID(),
                    expected_revision: item.revision,
                    action,
                  },
                );
                await operation(result, target);
              },
              true,
            ),
          );
      if (attempt.cancel_requested_at)
        target.append(el("p", "Cancellation requested; waiting for cleanup"));
    }
  }
  async function prdDetail(p) {
    const target = $("detail");
    target.replaceChildren();
    if (!p && !project) {
      message("Select a project before creating a PRD", true);
      return;
    }
    const e = editor(
      p ? "Edit PRD" : "Create PRD",
      p?.id || "new-prd",
      async (data) => {
        const body = {
          command_id: crypto.randomUUID(),
          title: data.title,
          spec: readSpec(data),
          template_id: data.template_id || "prd-default",
          template_version: Number(data.template_version || 1),
        };
        if (p) {
          body.expected_revision = p.revision;
          await api("/prds/" + p.id + query(), body, "PATCH");
        } else {
          body.workspace_id = ws;
          body.project_id = project;
          await api("/prds", body);
        }
        await render();
      },
    );
    input(e.form, "title", "PRD title", p?.title);
    input(
      e.form,
      "template_id",
      "Template ID",
      p?.template_id || "prd-default",
    );
    input(
      e.form,
      "template_version",
      "Template version",
      p?.template_version || 1,
      "number",
    );
    specForm(e.form, p?.spec || {}, "prd");
    target.append(e.finish());
    if (p) {
      if (p.status === "APPROVED")
        target.append(
          button("Decompose approved PRD", () => planningForm(p), true),
        );
      if (p.status === "APPROVED")
        target.append(
          button("Reconcile work items", () => reconcileForm(p), true),
        );
      for (const command of p.status === "DRAFT"
        ? ["submit_review", "archive"]
        : p.status === "IN_REVIEW"
          ? ["approve", "archive"]
          : ["archive"]) {
        target.append(
          button(
            command.replaceAll("_", " "),
            async () => {
              const value = await api("/prds/" + p.id + "/commands" + query(), {
                command_id: crypto.randomUUID(),
                expected_revision: p.revision,
                command,
              });
              await prdDetail(value);
              await render();
            },
            true,
          ),
        );
      }
      target.append(
        button("Show revisions", async () => {
          const values = await api("/prds/" + p.id + "/revisions" + query());
          const history = el("pre", JSON.stringify(values.items, null, 2));
          target.append(history);
        }),
      );
      const templates = await api("/templates" + query());
      if(p.status==="APPROVED") target.append(button("Review imported feature",async()=>{
        const imports=await api("/prds/"+p.id+"/imports"+query());
        const imported=imports.items[0];
        if(!imported)throw Error("Import reviewed task proposals before execution review.");
        await deliveryReview(imported,imported.item_ids);
      }));
      target.append(
        el(
          "p",
          "Pinned template: " +
            p.template_id +
            " v" +
            p.template_version +
            " · " +
            p.template_hash,
        ),
      );
      target.append(
        el(
          "p",
          "Available templates: " +
            templates.items.map((t) => t.id + " v" + t.version).join(", "),
        ),
      );
    }
  }
  async function board(content) {
    const scope = ws + "/" + project + "/tickets";
    if (!boardViews.has(scope)) boardViews.set(scope, {search: "", appliedSearch: "", mobile: "BACKLOG", agent: "", priority: "", type: "", columns: new Map()});
    const view = boardViews.get(scope);
    const workspaceId = ws, projectId = project;
    let generation = 0;
    const toolbar = el("div", undefined, "actions board-toolbar");
    toolbar.append(button("Create work item", () => itemDetail(null), true));
    const search = el("input");
    search.placeholder = "Search titles";
    search.setAttribute("aria-label", "Search titles");
    search.value = view.search;
    search.oninput = () => { view.search = search.value; };
    toolbar.append(
      search,
      button("Search", () => { view.appliedSearch = search.value; view.columns.clear(); return loadColumns(); }),
    );
    content.append(toolbar);
    for (const [name, label, options] of [["priority", "Filter priority", ["0", "1", "2", "3", "4"]],
      ["type", "Filter work type", ["FEATURE", "BUG", "TASK", "RESEARCH", "DOCUMENTATION", "TEST", "REVIEW"]]]) {
      const control = picker(toolbar, name, label, ["", ...options], view[name]);
      control.onchange = () => { view[name] = control.value; view.columns.clear(); loadColumns().catch(e => message(e.message, true)); };
    }
    const agents = await api("/agents?workspace_id=" + workspaceId);
    if (!content.isConnected) return;
    const agentLabel = el("label", "Filter agent"), agent = el("select");
    agent.setAttribute("aria-label", "Filter agent");
    selectOptions(agent, agents.items, view.agent, "All agents"); agentLabel.append(agent); toolbar.append(agentLabel);
    agent.onchange = () => {view.agent = agent.value; view.columns.clear(); loadColumns().catch(e => message(e.message, true));};
    toolbar.append(button("Clear filters", () => {
      view.search = view.appliedSearch = view.agent = view.priority = view.type = ""; view.columns.clear(); return render();
    }));
    const mobile = el("select", undefined, "mobile-state");
    mobile.setAttribute("aria-label", "Visible board state");
    for (const state of states) {
      const o = el("option", state);
      o.value = state;
      mobile.append(o);
    }
    content.append(mobile);
    mobile.value = view.mobile;
    const main = el("div", undefined, "board"),
      exceptional = el("div", undefined, "exceptional");
    main.setAttribute("role", "region");
    main.setAttribute("aria-label", "Work item board columns");
    main.tabIndex = 0;
    content.append(main, el("h3", "Exceptional states"), exceptional);
    if (page === "Work Items") {main.classList.add("item-list"); exceptional.classList.add("item-list");}
    const containers = new Map();
    for (const state of states) {
      const c = el("section", undefined, "column");
      c.dataset.state = state;
      c.append(el("h3", state));
      const list = el("div");
      c.append(list);
      containers.set(state, list);
      (states.indexOf(state) < 7 ? main : exceptional).append(c);
    }
    mobile.onchange = () => {
      view.mobile = mobile.value;
      for (const c of content.querySelectorAll(".column"))
        c.classList.toggle("mobile-hidden", c.dataset.state !== mobile.value);
      if (matchMedia("(max-width: 480px)").matches) loadColumns().catch(e => message(e.message, true));
    };
    for (const c of content.querySelectorAll(".column"))
      c.classList.toggle("mobile-hidden", c.dataset.state !== mobile.value);
    function boardQuery(extra = {}) {
      const params = new URLSearchParams({workspace_id: workspaceId, query: view.appliedSearch, limit: 20, ...extra});
      if (projectId) params.set("project_id", projectId);
      if (view.agent) params.set("agent_id", view.agent);
      if (view.priority) params.set("priority", view.priority);
      if (view.type) params.set("type", view.type);
      return "?" + params;
    }
    async function loadColumns() {
      const current = ++generation;
      const selected = matchMedia("(max-width: 480px)").matches ? view.mobile : "";
      const result = await api("/board" + boardQuery(selected ? {states: selected} : {}));
      if (!content.isConnected || current !== generation) return;
      for (const column of result.columns) {
        const previous = view.columns.get(column.state);
        if (previous?.loadedMore && column.count > 0) {
          const known = new Set(column.items.map(item => item.id));
          const moved = new Set(result.columns.flatMap(value => value.items.filter(item => item.status !== column.state).map(item => item.id)));
          column.items.push(...previous.items.filter(item => !known.has(item.id) && !moved.has(item.id)));
          column.loadedMore = true;
          column.needsRefresh = true;
        }
        view.columns.set(column.state, column);
        drawColumn(column.state, column);
      }
    }
    async function column(state, list, cursor) {
      const current = generation;
      const values = await api(
        "/board/columns/" + state + boardQuery({cursor}),
      );
      if (!content.isConnected || current !== generation) return;
      const value = values.columns[0];
      const previous = view.columns.get(state);
      const seen = new Set(previous?.items.map(item => item.id) || []);
      value.items = [...(previous?.items || []), ...value.items.filter(item => !seen.has(item.id))];
      value.loadedMore = true;
      view.columns.set(state, value);
      drawColumn(state, value);
    }
    function drawColumn(state, values) {
      const list = containers.get(state);
      const scroll = list.scrollTop;
      const focus = list.contains(document.activeElement) ? document.activeElement.dataset.itemId : null;
      list.replaceChildren();
      list.parentElement.querySelector("h3").textContent = state + " · " + values.count;
      for (const item of values.items) {
        const c = el("article", undefined, "card");
        c.append(
          el("span", item.display_key, "meta"),
          button(item.title, () => itemDetail(item)),
          el("p", item.type + " · priority " + item.priority, "meta"),
        );
        c.querySelector("button").dataset.itemId = item.id;
        if (item.reason_code) c.append(el("p", item.reason_code, "meta"));
        c.append(el("p", (item.assigned_agent_name || "Unassigned") + " · " + (item.observed_stage || "Stage unknown"), "meta"));
        if (item.review_needed) c.append(el("p", "Review needed", "meta"));
        list.append(c);
      }
      if (values.has_more) {
        const more = button("Load more " + state, async () => {
          more.remove();
          await column(state, list, values.next_cursor);
        });
        list.append(more);
      }
      if (!values.items.length) list.append(el("p", "No work items match these filters.", "meta"));
      if (values.needsRefresh) list.append(el("p", "Older loaded cards may have changed.", "meta"), button("Refresh " + state, () => {
        view.columns.delete(state); return loadColumns();
      }));
      list.scrollTop = scroll;
      if (focus) list.querySelector('[data-item-id="' + focus + '"]')?.focus({preventScroll: true});
    }
    refreshBoard = loadColumns;
    await loadColumns();
  }
  async function itemDetail(card) {
    const target = $("detail");
    if (!card && !project) {
      message("Select a project before creating work", true);
      return;
    }
    if (card) target.replaceChildren(el("p", "Loading item"));
    const item = card ? await api("/work-items/" + card.id + query()) : null;
    target.replaceChildren();
    const e = editor(
      item ? item.display_key + " · " + item.status : "Create work item",
      item?.id || "new-item",
      async (data) => {
        const body = {
          command_id: crypto.randomUUID(),
          title: data.title,
          execution_kind: data.execution_kind,
          budget_limit: data.budget_limit,
          review_policy: data.review_policy,
          spec: readSpec(data),
          parent_item_id: data.parent_item_id || null,
          prd_id: data.prd_id || null,
          type: data.type,
          priority: Number(data.priority),
          risk: Number(data.risk),
          complexity: Number(data.complexity),
          privacy_policy: data.privacy_policy,
          max_attempts: Number(data.max_attempts),
          engine_max_attempts: Number(data.engine_max_attempts),
          preferred_agent_role: data.preferred_agent_role || null,
          preferred_model_id: data.preferred_model_id || null,
          quality_target: data.quality_target
            ? Number(data.quality_target)
            : null,
        };
        if (item) {
          body.expected_revision = item.revision;
          await api("/work-items/" + item.id + query(), body, "PATCH");
        } else {
          body.workspace_id = ws;
          body.project_id = project;
          await api("/work-items", body);
        }
        await render();
      },
    );
    input(e.form, "title", "Work item title", item?.title);
    picker(
      e.form,
      "type",
      "Work type",
      ["FEATURE", "BUG", "TASK", "RESEARCH", "DOCUMENTATION", "TEST", "REVIEW"],
      item?.type || "TASK",
    );
    for (const [name, value] of Object.entries({
      priority: 2,
      risk: 20,
      complexity: 30,
      max_attempts: 3,
      engine_max_attempts: 3,
    }))
      input(
        e.form,
        name,
        name.replaceAll("_", " "),
        item?.[name] ?? value,
        "number",
      );
    picker(
      e.form,
      "privacy_policy",
      "Item inference policy",
      ["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"],
      item?.privacy_policy || "LOCAL_ONLY",
    );
    input(
      e.form,
      "preferred_agent_role",
      "Preferred agent role",
      item?.preferred_agent_role,
    );
    input(
      e.form,
      "preferred_model_id",
      "Preferred model ID",
      item?.preferred_model_id,
    );
    input(
      e.form,
      "quality_target",
      "Quality target (optional)",
      item?.quality_target,
      "number",
    );
    picker(
      e.form,
      "execution_kind",
      "Execution kind",
      ["MANUAL", "ENGINE"],
      item?.execution_kind || "MANUAL",
    );
    input(
      e.form,
      "budget_limit",
      "Lifetime budget USD",
      item?.budget_limit || "0.10",
    );
    picker(
      e.form,
      "review_policy",
      "Requested review policy",
      ["HUMAN_APPROVAL_REQUIRED", "AI_REVIEW", "AUTO_VERIFY"],
      item?.review_policy || "HUMAN_APPROVAL_REQUIRED",
    );
    input(
      e.form,
      "parent_item_id",
      "Parent item UUID (same project)",
      item?.parent_item_id,
    );
    input(e.form, "prd_id", "PRD UUID", item?.prd_id);
    specForm(e.form, item?.spec || {}, "work_item");
    if (item?.active_attempt_id)
      for (const n of e.form.querySelectorAll("input,textarea,select"))
        n.disabled = true;
    target.append(e.finish());
    if (!item) return;
    const history = el("details"); history.append(el("summary", "Activity")); target.append(history);
    let historyCursor = null;
    async function loadHistory() {
      const rows = await api("/work-items/" + item.id + "/activity" + query({limit: 25, ...(historyCursor ? {cursor: historyCursor} : {})}));
      for (const entry of rows.items) {
        const row = el("article", undefined, "record");
        row.append(el("p", new Date(entry.occurred_at).toLocaleString() + " · " + entry.actor_type + " " + entry.actor_id),
          el("p", ((entry.metadata.entity_type || "") + " " + entry.event_type).replaceAll("_", " ").trim() + " · " + (entry.metadata.title || entry.metadata.stage || "Workspace change")));
        const extra = el("details"); extra.append(el("summary", "Change details"), el("pre", JSON.stringify(entry.metadata, null, 2))); row.append(extra);
        if (entry.attempt_id) row.append(button("Open attempt", () => attemptDetail(entry.attempt_id, target)));
        history.append(row);
      }
      historyCursor = rows.next_cursor; moreHistory.hidden = !rows.has_more;
    }
    const moreHistory = button("More activity", loadHistory); history.append(moreHistory); await loadHistory();
    await executionDetail(item, target);
    let completionEvidence;
    if (item.allowed_actions.includes("approve_completion")) {
      completionEvidence = await api(
        "/work-items/" + item.id + "/evidence" + query(),
      );
      target.append(
        el("h3", "Evidence for completion approval"),
        el("pre", JSON.stringify(completionEvidence, null, 2)),
      );
    }
    const actions = el("div", undefined, "actions");
    for (const command of item.allowed_actions) {
      actions.append(
        button(
          command,
          async () => {
            const body = {
              command_id: crypto.randomUUID(),
              expected_revision: item.revision,
              command,
            };
            if (command === "block" || command === "reopen") {
              body.reason = reason.value;
              if (command === "block") body.reason_code = "MANUAL_BLOCK";
            }
            if (
              command === "approve_completion" ||
              command === "reject_completion"
            ) {
              const evidence = completionEvidence;
              if (!evidence)
                throw Error("Display current evidence before approving");
              body.evidence_hash = evidence.evidence_hash;
              body.contract_revision = item.contract_revision;
              body.reason = reason.value;
            }
            if (command === "manual_submit") {
              body.evidence = {
                checklist: [reason.value],
                references: ["Human submitted from item detail"],
              };
            }
            await api("/work-items/" + item.id + "/commands" + query(), body);
            await itemDetail(item);
            await render();
          },
          true,
        ),
      );
    }
    const reason = el("input");
    reason.placeholder = "Reason or manual checklist";
    reason.setAttribute("aria-label", "Command reason");
    actions.append(reason);
    target.append(actions);
    const edges = await api(
      "/work-items/" + item.id + "/dependencies" + query(),
    );
    for (const edge of edges.items) {
      const row = el("p", edge.blocker_id + " blocks " + edge.dependent_id);
      if (edge.dependent_id === item.id)
        row.append(
          button(
            "Remove dependency",
            async () => {
              await api("/work-items/" + item.id + "/dependencies" + query(), {
                command_id: crypto.randomUUID(),
                expected_revision: item.revision,
                remove: [edge.blocker_id],
              });
              await itemDetail(item);
              await render();
            },
            true,
          ),
        );
      target.append(row);
    }
    const dependency = el("select"), dependencySearch = el("input");
    dependency.setAttribute("aria-label", "Dependency blocker");
    dependencySearch.setAttribute("aria-label", "Search dependency tickets");
    async function findDependencies() {
      const rows = await api("/work-items" + query({query: dependencySearch.value, limit: 50}));
      selectOptions(dependency, rows.items.filter(value => value.id !== item.id).map(value => ({...value, name: value.display_key + " · " + value.title})), "", "Select blocker");
    }
    await findDependencies();
    target.append(
      dependencySearch, button("Find dependency tickets", findDependencies), dependency,
      button(
        "Add dependency",
        async () => {
          await api("/work-items/" + item.id + "/dependencies" + query(), {
            command_id: crypto.randomUUID(),
            expected_revision: item.revision,
            add: [dependency.value],
          });
          await itemDetail(item);
          await render();
        },
        true,
      ),
    );
    const comment = editor(
      "Add comment",
      "comment-" + item.id,
      async (data) => {
        await api("/comments", {
          command_id: crypto.randomUUID(),
          workspace_id: ws,
          item_id: item.id,
          body: data.body,
        });
        await itemDetail(item);
      },
    );
    input(comment.form, "body", "Comment", "", "textarea");
    target.append(comment.finish());
    await paged(
      "/comments",
      target,
      (c) => {
        const edit = editor("Edit comment", c.id, async (data) => {
          await api(
            "/comments/" + c.id + query(),
            {
              command_id: crypto.randomUUID(),
              expected_revision: c.revision,
              body: data.body,
            },
            "PATCH",
          );
          await itemDetail(item);
        });
        input(edit.form, "body", "Comment text", c.body, "textarea");
        target.append(
          edit.finish(),
          button(
            "Delete comment",
            async () => {
              await api("/comments/" + c.id + "/archive" + query(), {
                command_id: crypto.randomUUID(),
                expected_revision: c.revision,
              });
              await itemDetail(item);
            },
            true,
          ),
        );
      },
      { project_id: "", item_id: item.id },
    );
    target.append(
      button("Item revisions", async () => {
        const revisions = await api(
          "/work-items/" + item.id + "/revisions" + query(),
        );
        target.append(el("pre", JSON.stringify(revisions.items, null, 2)));
      }),
    );
  }
  async function settings(content) {
    const setup = await api("/lan/setup");
    content.append(el("h2", "LAN and devices"), el("p", setup.instructions), el("pre", JSON.stringify(setup, null, 2)));
    content.append(button("Create pairing invitation", async () => {
      const issued = await api("/devices/commands", {workspace_id: ws, command_id: crypto.randomUUID(), action: "invite"});
      content.append(el("p", "HTTPS URL: " + issued.urls.join(", ") + " · one-time code: " + issued.code + " · expires in five minutes"));
    }, true), button("Refresh pending devices", async () => { await render(); }));
    const pending = await api("/pairings" + query());
    for (const device of pending.items) {
      const form = editor("Compare client: " + device.client_name, "pair-" + device.id, async data => {
        const capabilities = data.capabilities.split(",").map(value => value.trim()).filter(Boolean);
        await api("/devices/commands", {workspace_id: ws, command_id: crypto.randomUUID(), action: "approve", id: device.id, expected_revision: device.revision, phrase: data.phrase, capabilities});
        await render();
      });
      form.form.append(el("p", "Host verification phrase: " + device.phrase));
      input(form.form, "phrase", "Matching phrase from client", "");
      input(form.form, "capabilities", "Explicit capabilities (comma separated)", "workspace.read,artifact.read");
      content.append(form.finish(), button("Deny " + device.client_name, async () => {
        await api("/devices/commands", {workspace_id: ws, command_id: crypto.randomUUID(), action: "deny", id: device.id, expected_revision: device.revision, phrase: device.phrase, capabilities: ["workspace.read"]}); await render();
      }, true));
    }
    for (const device of (await api("/devices" + query())).items) {
      content.append(el("p", device.name + " · " + device.state));
      if (device.state === "ACTIVE") content.append(button("Revoke " + device.name, async () => {
        await api("/devices/commands", {workspace_id: ws, command_id: crypto.randomUUID(), action: "revoke", id: device.id, expected_revision: device.revision}); await render();
      }, true));
    }
    const workspace = await api("/workspaces/" + ws);
    const e = editor("Workspace policy", ws, async (data) => {
      await api(
        "/workspaces/" + ws,
        {
          command_id: crypto.randomUUID(),
          expected_revision: workspace.revision,
          name: data.name,
          privacy_mode: data.privacy_mode,
          inference_policy: data.inference_policy,
          review_policy: data.review_policy,
        },
        "PATCH",
      );
      await loadScopes();
    });
    input(e.form, "name", "Workspace name", workspace.name);
    picker(
      e.form,
      "privacy_mode",
      "Workspace privacy",
      ["DEVICE_ONLY", "PRIVATE_LAN", "HYBRID"],
      workspace.privacy_mode,
    );
    picker(
      e.form,
      "inference_policy",
      "Inference policy",
      ["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"],
      workspace.inference_policy,
    );
    picker(
      e.form,
      "review_policy",
      "Minimum review policy",
      ["HUMAN_APPROVAL_REQUIRED", "AI_REVIEW", "AUTO_VERIFY"],
      workspace.review_policy,
    );
    content.append(e.finish());
    const management = el("a", "Model, provider, skills and routing settings");
    management.href = "/ui";
    content.append(management);
    const create = editor("Create workspace", "new-workspace", async (data) => {
      await api("/workspaces", {
        command_id: crypto.randomUUID(),
        name: data.name,
      });
      await loadScopes();
    });
    input(create.form, "name", "New workspace name");
    content.append(create.finish());
    const template = editor(
      "Import template",
      "template-import",
      async (data) => {
        await api("/templates/import", {
          command_id: crypto.randomUUID(),
          definition: data.definition,
        });
        await render();
      },
    );
    const definition = input(
      template.form,
      "definition",
      "Template YAML",
      "",
      "textarea",
    );
    const preview = el("pre");
    content.append(
      template.finish(),
      button(
        "Preview template",
        async () => {
          const result = await api("/templates/preview", {
            definition: definition.value,
          });
          preview.textContent = JSON.stringify(result.sections, null, 2);
        },
        true,
      ),
      preview,
    );
    const templates = await api("/templates" + query());
    for (const t of templates.items)
      content.append(
        button("Export " + t.id + " v" + t.version, async () => {
          definition.value = await api(
            "/templates/" +
              encodeURIComponent(t.id) +
              "/export?version=" +
              t.version,
          );
          drafts.set("template-import", { definition: definition.value });
        }),
      );
  }
  for (const name of [
    "Overview",
    "Projects",
    "PRDs",
    "Board",
    "Work Items",
    "Agents",
    "Runs",
    "Activity",
    "Settings",
  ]) {
    $("navigation").append(
      button(name, async () => {
        page = name;
        $("detail").replaceChildren();
        await render();
      }),
    );
  }
  $("workspace").onchange = async () => {
    ws = $("workspace").value;
    project = "";
    $("detail").replaceChildren();
    try {
      await loadScopes();
      await sync();
      await render();
    } catch (e) {
      message(e.message, true);
    }
  };
  scopeSearch($("workspace"), "/workspaces", "workspaces");
  scopeSearch($("project"), "/projects", "projects");
  $("project").onchange = async () => {
    project = $("project").value;
    $("detail").replaceChildren();
    await render();
  };
  $("refresh").onclick = async () => {
    try {
      connected(true);
      await loadScopes();
      await sync();
      await render();
      message("Refreshed. Saved drafts remain available.");
    } catch (e) {
      message(e.message, true);
    }
  };
  window.addEventListener("offline", () => connected(false));
  (async () => {
    try {
      csrf = (
        await (
          await fetch("/ui/session", { credentials: "same-origin" })
        ).json()
      ).csrf;
      connected(true);
      await loadScopes();
      await sync();
      await render();
    } catch (e) {
      connected(false);
      message(e.message, true);
    }
  })();
})();
