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
  async function loadScopes() {
    const workspaces = (await api("/workspaces")).items;
    if (!ws || !workspaces.some((w) => w.id === ws))
      ws = workspaces[0]?.id || "";
    selectOptions($("workspace"), workspaces, ws);
    if (!ws) return;
    const projects = (
      await api("/projects" + query({ project_id: "", limit: 200 }))
    ).items;
    if (project && !projects.some((p) => p.id === project)) project = "";
    selectOptions($("project"), projects, project, "All projects");
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
    const content = $("content");
    content.replaceChildren();
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
    if (type === "prd")
      for (const field of prdObjects)
        input(
          f,
          "json." + field,
          field.replaceAll("_", " ") + " (JSON entries with stable IDs)",
          JSON.stringify(spec[field] || []),
          "textarea",
        );
  }
  function readSpec(data) {
    const spec = {};
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
    }
    return spec;
  }
  async function prds(content) {
    content.append(button("Create PRD", () => prdDetail(null), true));
    content.append(button("Generate PRD", () => planningForm(null), true));
    await paged("/prds", content, (p) =>
      content.append(
        button(p.title + " · " + p.status + " · revision " + p.revision, () =>
          prdDetail(p),
        ),
      ),
    );
  }
  async function operation(result, target, finished) {
    const display = el("pre");
    target.append(display);
    async function refresh() {
      const current = await api("/operations/" + result.id + query());
      display.textContent = JSON.stringify(current, null, 2);
      if (current.state === "COMPLETED" && finished)
        await finished(current.result);
    }
    target.append(button("Refresh operation", refresh));
    await refresh();
  }
  async function planningForm(prd) {
    const target = $("detail");
    target.replaceChildren();
    if (!project) throw Error("Select a project first");
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
          prd ? async (result) => importPreview(result, target) : undefined,
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
    input(e.form, "budget", "Planning budget USD", "1.00");
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
          preview.textContent = JSON.stringify(quote, null, 2);
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
      plans: proposal.batches.map((b) => b.plan),
      allowed_paths: [
        ...new Set(proposal.items.flatMap((i) => i.spec.allowed_paths)),
      ],
    };
    const staged = await api("/plan-imports", body);
    target.append(
      el("h3", "Import preview"),
      el("pre", JSON.stringify(staged, null, 2)),
      button(
        "Commit all proposed items",
        async () => {
          const actions = Object.fromEntries(
            staged.proposal.items.map((i) => [i.proposal_id, "create"]),
          );
          await api("/plan-imports/" + staged.id + "/commit" + query(), {
            command_id: crypto.randomUUID(),
            expected_revision: staged.revision,
            source_hash: staged.source_hash,
            actions,
          });
          await render();
        },
        true,
      ),
    );
  }
  async function agents(content) {
    const result = await api("/agents" + query());
    for (const agent of result.items)
      content.append(
        el("h3", agent.name + " · " + agent.state),
        el("pre", JSON.stringify(agent, null, 2)),
      );
    const e = editor("Configure engine agent", "agent-config", async (data) => {
      const definition = JSON.parse(data.definition);
      await api("/agents", { ...definition, command_id: crypto.randomUUID() });
      await render();
    });
    input(
      e.form,
      "definition",
      "Agent configuration JSON",
      JSON.stringify(
        {
          name: "Engine",
          enabled: true,
          allowed_workspaces: [ws],
          allowed_projects: [],
          repository_permissions: [],
          capabilities: ["code"],
          tools: ["propose_file_changes"],
          skills: [],
          supported_model_roles: ["executor", "repairer", "reviewer"],
          privacy_policy: "LOCAL_ONLY",
          concurrency_limit: 1,
        },
        null,
        2,
      ),
      "textarea",
    );
    content.append(e.finish());
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
    const toolbar = el("div", undefined, "actions board-toolbar");
    toolbar.append(button("Create work item", () => itemDetail(null), true));
    const search = el("input");
    search.placeholder = "Search titles";
    search.setAttribute("aria-label", "Search titles");
    toolbar.append(
      search,
      button("Search", () => loadColumns()),
    );
    content.append(toolbar);
    const mobile = el("select", undefined, "mobile-state");
    mobile.setAttribute("aria-label", "Visible board state");
    for (const state of states) {
      const o = el("option", state);
      o.value = state;
      mobile.append(o);
    }
    content.append(mobile);
    const main = el("div", undefined, "board"),
      exceptional = el("div", undefined, "exceptional");
    main.setAttribute("role", "region");
    main.setAttribute("aria-label", "Work item board columns");
    main.tabIndex = 0;
    content.append(main, el("h3", "Exceptional states"), exceptional);
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
      for (const c of content.querySelectorAll(".column"))
        c.classList.toggle("mobile-hidden", c.dataset.state !== mobile.value);
    };
    mobile.onchange();
    async function loadColumns() {
      await Promise.all(
        states.map(async (state) => {
          const list = containers.get(state);
          list.replaceChildren();
          await column(state, list, null);
        }),
      );
    }
    async function column(state, list, cursor) {
      const values = await api(
        "/work-items" +
          query({
            status: state,
            limit: 20,
            query: search.value,
            ...(cursor ? { cursor } : {}),
          }),
      );
      for (const item of values.items) {
        const c = el("article", undefined, "card");
        c.append(
          el("span", item.display_key, "meta"),
          button(item.title, () => itemDetail(item)),
          el("p", item.type + " · priority " + item.priority, "meta"),
        );
        if (item.reason_code) c.append(el("p", item.reason_code, "meta"));
        list.append(c);
      }
      if (values.has_more) {
        const more = button("Load more " + state, async () => {
          more.remove();
          await column(state, list, values.next_cursor);
        });
        list.append(more);
      }
    }
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
    const dependency = el("input");
    dependency.placeholder = "Blocker item UUID";
    dependency.setAttribute("aria-label", "Dependency blocker UUID");
    target.append(
      dependency,
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
