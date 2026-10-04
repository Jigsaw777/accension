"use strict";
const pages = ["Setup", "Providers", "Models", "Roles", "Routing", "Repositories", "Integrations", "Playground", "Traces", "Costs", "Cache", "System"];
const $ = id => document.getElementById(id);
let csrf = null, state = null, pulseState = null, pulseStream = null;
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function notice(text, error = false) { $("notice").textContent = text; $("notice").className = error ? "error" : ""; }
function result(value) {
  const section=$("output-section"); section.hidden=false; section.replaceChildren(el("h2","Result"));
  if(value.classification && value.selection){
    const c=value.classification;
    section.append(el("h3",value.selection.selected ? "Selected: "+value.selection.selected : "Connect or calibrate an eligible model"),el("p",`${c.task_family} · Complexity ${c.complexity}/100 · Risk ${c.risk}/100 · ${c.planning_depth.replaceAll("_"," ")}`),el("p",`Privacy: ${value.privacy}. Configured maximum: $${value.cost_simulation.configured_maximum}. Inference calls: 0.`));
    table(section,["Model","Eligible","Quality / target","Maximum USD","Reasons"],value.selection.candidates.map(m=>[m.model,m.eligible?"Yes":"No",m.quality_estimate+" / "+m.quality_target,m.estimated_max_cost??"Unknown",m.reason_codes.join(", ")||"Meets policy"]));
    if(value.selection.notice)section.append(el("p",value.selection.notice));
    details(section,"Full routing explanation",value);
  }else if(value.results){
    section.append(el("p",value.limitations||"Calibration results"));table(section,["Model","Fixture","Outcome","Latency"],value.results.map(r=>[r.model,r.case,r.passed?"Passed":r.error||"Failed",r.latency.toFixed(2)+"s"]));details(section,"Cost accounting",value.costs);
  }else if(Array.isArray(value) && value.some(v=>v.stage)){
    table(section,["Stage","Model / result","Time"],value.map(r=>[r.stage,r.model||r.reason_code||(r.passed===true?"Passed":""),new Date(r.timestamp*1000).toLocaleTimeString()]));details(section,"Structured trace details",value);
  }else section.append(el("pre",JSON.stringify(value,null,2)));
  section.scrollIntoView({block:"start"});
}
async function api(path, body) {
  const r = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: body === undefined ? {} : {"Content-Type": "application/json", "X-CSRF-Token": csrf || ""}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data; try { data = await r.json(); } catch { throw Error("The local server returned an unreadable response."); }
  if (!r.ok) { throw Error(r.status === 401 ? "Local session expired. Reload this page to reconnect." : typeof data.error === "string" ? data.error : typeof data.detail === "string" ? data.detail : "Check the supplied fields."); }
  return data;
}
async function action(name, data = {}, refresh = true) {
  const value = await api("/ui/action/" + name, data);
  result(value); notice("Done.");
  if (refresh) await reload();
  return value;
}
function button(text, fn, cls = "") {
  const b = el("button", text, cls); b.type = "button";
  b.onclick = async () => { b.disabled = true; notice(""); try { await fn(); } catch (e) { notice(e.message, true); } finally { b.disabled = false; } };
  return b;
}
function card(title, text, parent = $("content"), cls = "") {
  const c = el("section", undefined, "card " + cls); c.append(el("h2", title)); if (text) c.append(el("p", text)); parent.append(c); return c;
}
function field(form, name, label, value = "", type = "text", choices = null) {
  const l = el("label", label); let input;
  if (choices) {
    input = el("select"); choices.forEach(choice => { const [v, label] = Array.isArray(choice) ? choice : [choice, choice]; const opt = el("option", label); opt.value = v; input.append(opt); });
  } else input = el(type === "textarea" ? "textarea" : "input");
  if (!choices && type !== "textarea") input.type = type;
  input.name = name; if (type === "checkbox") input.checked = Boolean(value); else input.value = value === null ? "" : value;
  if (type === "number") { input.step = "any"; input.min = "0"; }
  if (type === "password") input.autocomplete = "new-password";
  l.append(input); form.append(l); return input;
}
function submit(form, text, fn) {
  const b = el("button", text); b.type = "submit"; form.append(b);
  form.onsubmit = async e => { e.preventDefault(); b.disabled = true; notice(""); try { await fn(new FormData(form)); } catch (err) { notice(err.message, true); } finally { b.disabled = false; } };
}
function table(parent, headers, rows) {
  const wrap = el("div", undefined, "table-wrap"), t = el("table"), tr = el("tr"); headers.forEach(h => tr.append(el("th", h))); t.append(tr);
  rows.forEach(row => { const r = el("tr"); row.forEach(v => { const td = el("td"); if (v instanceof Node) td.append(v); else td.textContent = v ?? "—"; r.append(td); }); t.append(r); }); wrap.append(t); parent.append(wrap);
}
function details(parent, label, value) { const d = el("details"), pre = el("pre", JSON.stringify(value, null, 2)); d.append(el("summary", label), pre); parent.append(d); }
function go(page) { location.hash = page.toLowerCase(); }
function modelChoices(empty = true) { return [...(empty ? [["", "Automatic / no preference"]] : []), ...state.models.map(m => [m.id, m.id])]; }
function current() { const name = location.hash.slice(1).toLowerCase(); return pages.find(p => p.toLowerCase() === name) || "Setup"; }
async function reload() { state = await api("/ui/state"); render(); }
function modelField(form, name, label, value="") {
  const input=field(form,name,label,value), list=el("datalist"); list.id="models-"+Math.random().toString(36).slice(2);input.setAttribute("list",list.id);form.append(list);
  let timer;
  input.oninput=()=>{clearTimeout(timer);timer=setTimeout(async()=>{try{const page=await api("/ui/models?limit=20&search="+encodeURIComponent(input.value));list.replaceChildren(...page.items.map(m=>{const option=el("option");option.value=m.id;return option;}));}catch(e){notice(e.message,true);}},200);};
  input.oninput();return input;
}
function showDNA(value) {
  const section=$("output-section");section.hidden=false;section.replaceChildren(el("h2","Model DNA · "+value.model),el("p","Scores are local observations. Unknown means no measured evidence; limited samples are uncertain."));
  table(section,["Capability","Estimate","Samples","Uncertainty"],Object.entries(value.dimensions).map(([key,d])=>[key.replaceAll("_"," "),d.value===null?"Unknown":(d.value*100).toFixed(1)+"%",d.samples,d.uncertainty]));
  details(section,"Evidence and specializations",value);
  section.scrollIntoView({block:"start"});
}
function render() {
  if (!state) return;
  $("demo-label").hidden=!state.mock;
  const page = current(); $("title").textContent = page === "Setup" ? "Your AI, working together" : page;
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.textContent === page));
  $("content").replaceChildren(); $("status").textContent = state.policy.control_plane.fully_local || state.policy.routing.preset === "fully-local" ? "Fully local" : state.policy.control_plane.routing_location === "hybrid" ? "Hybrid decisions enabled" : "Local control";
  ({Setup: setup, Providers: providers, Models: models, Roles: roles, Routing: routing, Repositories: repositories, Integrations: integrations, Playground: playground, Traces: traces, Costs: costs, Cache: cache, System: system})[page]();
}
function setup() {
  card("One place for every model.", "Connect the AI models you already use. Accension chooses eligible models by capability, quality and cost, then verifies repository changes. Routing, budgets and history stay on this computer.", $("content"), "hero");
  const metrics = el("div", undefined, "metrics");
  [[state.providers.length, "Connected providers"], [state.models_total, "Models discovered"], ["$" + state.dashboard.costs.today_usd.toFixed(4), "Estimated spend today"], [state.policy.routing.preset, "Routing policy"]].forEach(([v,l]) => { const m = el("div", undefined, "metric"); m.append(el("strong", String(v)), el("span", l)); metrics.append(m); }); $("content").append(metrics);
  const grid = el("div", undefined, "grid"); $("content").append(grid);
  const steps = [
    ["01 / DISCOVER", "Find local AI", "Check known loopback endpoints for local models. Nothing is downloaded.", "Discover local AI", () => action("discover-local")],
    ["02 / CONNECT", "Add your providers", "Use local models, cloud providers, or both. Provider accounts are optional.", "Connect a provider", () => go("providers")],
    ["03 / CALIBRATE", "Learn model strengths", "Run small objective checks. Review the estimated maximum before any paid run.", "Review calibration", () => go("models")],
    ["04 / CHOOSE", "Set your policy", "Start with Balanced or choose Fully Local. Privacy always limits selection.", "Choose policy", () => go("routing")],
    ["05 / START", "Connect your tools", "Connect Codex, Claude, or another MCP / OpenAI-compatible client.", "Open integrations", () => go("integrations")]
  ];
  steps.forEach(([n,t,d,b,f]) => {const c = card(t,d,grid); c.prepend(el("div",n,"step")); c.append(button(b,f));});
}
function providers() {
  const list = card("Connected providers", "Connection tests and discovery read provider metadata; they do not generate paid model output.");
  if (!state.providers.length) list.append(el("p", "No providers yet. Connect one below, or discover a local runtime."));
  else table(list, ["Provider", "Location", "Health", "Models", ""], state.providers.map(p => [p.id, p.local ? "This device" : "Cloud / remote", p.health.status, p.models, button("Test connection", () => action("provider-test", {name:p.id}), "quiet")]));
  list.append(button("Discover local AI", () => action("discover-local")), button("Refresh inventory", () => action("discover"), "quiet"));
  const c = card("Connect or reconnect a provider", "Use an existing provider name to reconnect. Saved credentials are never displayed.");
  const form = el("form"), kind = field(form, "kind", "Provider", "openrouter", "text", state.manifests.filter(m => m.id !== "mock").map(m => [m.id,m.name]));
  const name = field(form, "name", "Connection name", "openrouter"); name.required = true;
  const dynamic = el("div"); form.append(dynamic);
  function fields() {
    dynamic.replaceChildren(); const m = state.manifests.find(m => m.id === kind.value); name.value = m.id;
    field(dynamic,"auth","Authentication",m.authentication[0],"text",m.authentication);
    field(dynamic,"protocol","Protocol",m.protocols[0],"text",m.protocols);
    m.fields.forEach(f => {const input=field(dynamic,f.name,f.label,f.default || "",f.type === "password" ? "password" : f.type === "boolean" ? "checkbox" : f.type === "list" ? "textarea" : "text",f.type === "select" ? f.choices : null); input.required = f.required; if (f.help) dynamic.append(el("p",f.help,"hint")); if (f.type === "list") input.placeholder = "One value per line";});
    if (m.id === "mcp") dynamic.append(el("p", "This starts trusted local software. Only use an executable you trust; it can access files and the network.", "hint"));
  }
  kind.onchange = fields; fields();
  submit(form,"Save connection", async data => {
    const m = state.manifests.find(m => m.id === data.get("kind")), values = {auth:data.get("auth"),protocol:data.get("protocol")};
    m.fields.filter(f => f.name !== "api_key").forEach(f => {values[f.name] = f.type === "boolean" ? data.has(f.name) : f.type === "list" ? String(data.get(f.name)||"").split("\n").map(s=>s.trim()).filter(Boolean) : data.get(f.name) || "";});
    const secret = data.get("api_key") || null; const password=form.querySelector('[name="api_key"]'); if(password) password.value="";
    await action("provider-connect",{name:data.get("name"),kind:m.id,fields:values,secret});
  }); c.append(form);
}
function models() {
  const c=card("Model inventory", "Unknown cloud prices are excluded from automatic budgeted routing. Capability and quality evidence remain local.");
  const search=field(c,"search","Search all models"); const inventory=el("div"); c.append(inventory);
  let offset=0,timer,revision=0;
  const draw=async()=>{const generation=++revision;try{const page=await api("/ui/models?limit=50&offset="+offset+"&search="+encodeURIComponent(search.value));if(generation!==revision)return;inventory.replaceChildren();table(inventory,["Model","Status","Location","Input / output per 1M","Evidence"],page.items.map(m=>[m.id,m.status,m.locality,m.input_price==null||m.output_price==null?"Price unknown":"$"+m.input_price+" / $"+m.output_price,button("Model DNA",async()=>showDNA(await api("/ui/models/dna?model="+encodeURIComponent(m.id))),"quiet")]));inventory.append(el("p",`${page.total} models · ${page.total?offset+1:0}–${Math.min(offset+page.limit,page.total)}`));if(offset)inventory.append(button("Previous",()=>{offset=Math.max(0,offset-50);return draw();},"quiet"));if(page.next_offset!==null)inventory.append(button("Next",()=>{offset=page.next_offset;return draw();},"quiet"));}catch(e){notice(e.message,true);}};
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{offset=0;draw();},200);};draw();
  if(!state.models.length){c.append(el("p","Discover models from Providers to get started."));return;}
  const cal=card("Quick calibration", "Choose up to three models. Eleven tiny fixtures cover coding, planning, review and more. Results are estimates, not a quality guarantee.");
  const form=el("form");modelField(form,"models","Model to calibrate (search by ID)",state.models[0].id);
  field(form,"budget","Maximum calibration budget (USD)",.05,"number"); const quoteBox=el("div");
  submit(form,"Preview maximum cost",async data=>{const quote=await action("calibrate-preview",{models:data.getAll("models"),budget:Number(data.get("budget"))},false);quoteBox.replaceChildren();quoteBox.append(el("p",`${quote.models.length} model(s), estimated maximum $${quote.estimated_maximum.toFixed(6)}. Quote expires in 15 minutes.`));if(quote.rejected.length)details(quoteBox,"Models excluded",quote.rejected); if(!quote.models.length)return;const allow=field(quoteBox,"allow","I approve this paid calibration maximum",false,"checkbox");allow.parentElement.hidden=!quote.paid_approval_required;quoteBox.append(button("Run quoted calibration",async()=>{if(quote.paid_approval_required&&!allow.checked)throw Error("Approve the displayed paid maximum first.");await action("calibrate-run",{quote_id:quote.id,allow_paid:allow.checked});}));});cal.append(form,quoteBox);
  const edit=card("Model settings", "Manual capability overrides take precedence over probes. Only enable capabilities you have verified.");const ef=el("form");modelField(ef,"model","Model",state.models[0].id);field(ef,"input","Input price / million (USD)","","number");field(ef,"output","Output price / million (USD)","","number");field(ef,"enabled","Enabled for routing",true,"checkbox");
  submit(ef,"Save model settings",data=>{const fields={enabled:data.has("enabled")};if(data.get("input")!=="")fields.input_price=Number(data.get("input"));if(data.get("output")!=="")fields.output_price=Number(data.get("output"));return action("model-update",{model:data.get("model"),fields});});edit.append(ef);
  const probe=card("Verify a capability", "Each probe is tiny and budgeted. Cloud probes require explicit approval. Successful probes are cached for 24 hours.");const pf=el("form");modelField(pf,"model","Model",state.models[0].id);field(pf,"capability","Capability","text","text",["text","structured_output","tools","streaming","vision","reasoning","responses_api","chat_completions","embeddings"]);field(pf,"budget","Maximum probe budget (USD)",.01,"number");field(pf,"approved","I approve cloud inference up to this maximum",false,"checkbox");submit(pf,"Run capability probe",data=>action("probe",{model:data.get("model"),capability:data.get("capability"),budget:Number(data.get("budget")),approved:data.has("approved")}));probe.append(pf);
}
function roles() {
  card("Choose who does what", "Automatic assignments use capability, price, quality evidence, health and privacy gates. A pinned model falls back when unavailable. Classifier and arbiter models are optional; local policy always works.");
  Object.entries(state.roles).forEach(([name,r])=>{const c=card(name.replaceAll("_"," "),r.selected ? "Auto candidate: "+r.selected : "No eligible model. Local deterministic policy remains available.");if(r.notice)c.append(el("p",r.notice));const f=el("form"),p=state.role_policies[name];field(f,"strategy","Assignment",p.strategy,"text",["auto","preferred","pinned","disabled"]);modelField(f,"model","Preferred model",p.model||"");field(f,"locality","Location policy",p.locality,"text",["local-only","local-preferred","any"]);field(f,"quality","Minimum quality estimate (optional)",p.minimum_quality,"number");submit(f,"Save role",data=>action("role-set",{role:name,policy:{strategy:data.get("strategy"),model:data.get("model")||null,locality:data.get("locality"),minimum_quality:data.get("quality")===""?null:Number(data.get("quality"))}}));c.append(f);details(c,"Selection reasons and fallback graph",r);});
}
function routing() {
  const c=card("Routing policy", "Presets share one policy engine. Fully Local blocks cloud inference and cloud metadata requests. Local Control keeps decisions local while allowing eligible cloud compute.");const f=el("form");field(f,"preset","Preset",state.policy.routing.preset,"text",state.presets);submit(f,"Apply preset",data=>action("policy-set",{preset:data.get("preset")}));c.append(f);
  const b=card("Spending limits", "All amounts are USD. Accension reserves a conservative maximum before inference; uncertain calls retain that reservation.");const bf=el("form");const labels={default_request_budget:"Per request",session_budget:"Per session",daily_soft_budget:"Daily soft warning",daily_hard_budget:"Daily hard limit",planner_budget:"Planning per request",worker_budget:"Execution per request",verification_budget:"Verification per request",max_frontier_calls:"Maximum legacy high-tier calls",max_initial_frontier_plans:"Maximum legacy high-tier plans"};Object.entries(state.policy.budgets).forEach(([key,value])=>field(bf,key,labels[key]||key,value,"number"));submit(bf,"Save budgets",data=>action("policy-set",{budgets:Object.fromEntries([...data].map(([k,v])=>[k,Number(v)]))}));b.append(bf);
  const advanced=card("Advanced policy", "Hybrid routing explicitly permits task information to leave this computer for classifier or arbiter decisions. Repository privacy still applies.");const af=el("form");field(af,"location","Decision location",state.policy.control_plane.routing_location,"text",["local-only","hybrid"]);field(af,"unknown","Allow unknown cloud prices using conservative configured allowances",state.policy.routing.allow_unknown_pricing,"checkbox");submit(af,"Save advanced policy",data=>action("policy-set",{control_plane:{routing_location:data.get("location")},routing:{allow_unknown_pricing:data.has("unknown")}}));advanced.append(af);
  const share=card("Share a routing profile", "Export omits credentials, provider endpoints, model identifiers, executable commands and repository paths.");share.append(button("Export safe profile",async()=>{const value=await api("/ui/config/export");const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"}));const a=el("a");a.href=url;a.download="accension-profile.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},"quiet"));const pf=el("form");field(pf,"profile","Import profile JSON","","textarea");submit(pf,"Validate and import",data=>action("profile-import",JSON.parse(data.get("profile"))));share.append(pf);
}
function repositories() {
  const c=card("Registered repositories", "Source stays local by default. Cloud Redacted sanitizes detectable secrets; it cannot guarantee that every secret is found.");table(c,["Project", "Privacy", "Checks"],state.repositories.map(r=>[r.path,r.privacy?.mode || (r.allow_cloud?"CLOUD_ALLOWED":"LOCAL_ONLY"),Object.keys(r.validation).join(", ")]));
  const edit=card("Register or update a project", "Choose independent checks before running edits. Accension applies only hash-checked, declared file changes.");const f=el("form");field(f,"path","Full project folder").required=true;field(f,"mode","Privacy","LOCAL_ONLY","text",["LOCAL_ONLY","CLOUD_REDACTED","CLOUD_ALLOWED"]);field(f,"check","Validation", "python-unittest","text",[["python-unittest","Python unittest"],["python-pytest","Python pytest"],["npm-test","npm test"],["none","No checks yet (inspection only)"],["custom","Custom registered checks"]]);field(f,"custom","Custom checks JSON (advanced)",'{"tests":["{python}","-m","pytest","-q"]}',"textarea");field(f,"never_send","Never-send paths (one glob per line)","secrets/**\ndeployment/private/**","textarea");submit(f,"Save repository",data=>{const checks={"python-unittest":{tests:["{python}","-m","unittest","discover","-v"]},"python-pytest":{tests:["{python}","-m","pytest","-q"]},"npm-test":{tests:["npm","test"]},none:{}};return action("repository-set",{path:data.get("path"),mode:data.get("mode"),validation:data.get("check")==="custom"?JSON.parse(data.get("custom")):checks[data.get("check")],never_send:data.get("never_send").split("\n").map(s=>s.trim()).filter(Boolean)});});edit.append(f);
}
function integrations() {
  const c=card("Connect developer tools", "MCP supports planning, repository-aware execution and diagnostics. The compatibility gateway forwards native protocol requests to eligible models.");
  c.append(el("h3","OpenAI-compatible clients"),el("p","Base URL"),el("pre",state.integrations.endpoint),el("p","Model: accension-auto. Use your local API token from the terminal; provider keys are never given to clients."));
  c.append(el("h3","MCP clients"),el("pre",state.integrations.mcp_command),el("p","Codex and Claude registrations can be installed with the local CLI. Existing configuration is backed up before changes."));
  c.append(el("pre","router integration install codex\nrouter integration install claude-code\nrouter integration install claude-desktop"));
  const f=el("form");field(f,"client","Client","codex","text",["codex","claude-code","claude-desktop"]);const preview=el("div");submit(f,"Preview integration",async data=>{const value=await action("integration-preview",{client:data.get("client")},false);preview.replaceChildren();details(preview,"Managed registration to install",value);preview.append(button("Install this registration",()=>action("integration-install",{quote_id:value.id}),"quiet"));});c.append(f,preview);
  details(c,"Virtual routing policies",state.integrations.virtual_models);
  card("Compatibility", "Native chat, Responses and Messages preserve client-executed tools and streaming. Bedrock and Gemini serve orchestration calls; native gateway requests require a matching protocol. Multimodal gateway input and hosted provider tools are currently rejected because their costs cannot be bounded.");
}
function playground() {
  const runCard=card("Compile and execute a task", "Use a registered repository with independent checks. Planning and execution can call eligible models within your configured budget.");
  const rf=el("form");field(rf,"task","Goal","","textarea").required=true;field(rf,"repo_path","Repository",state.repositories[0]?.path||"","text",state.repositories.map(r=>[r.path,r.path]));field(rf,"mode","Action","plan-only","text",[["plan-only","Compile portable AXIR"],["execute","Run and verify"]]);field(rf,"budget","Maximum API budget (USD)",state.policy.budgets.default_request_budget,"number");field(rf,"privacy","Task privacy","LOCAL_ONLY","text",["LOCAL_ONLY","CLOUD_REDACTED","CLOUD_ALLOWED"]);submit(rf,"Start task",async data=>{const value=await action(data.get("mode")==="plan-only"?"plan":"run",{task:data.get("task"),repo_path:data.get("repo_path"),budget:Number(data.get("budget")),privacy:data.get("privacy")},false);if(value.status==="complete")showReceipt(await api("/ui/receipts/"+value.plan_id));});runCard.append(rf);
  const planned=state.runs.filter(r=>r.status==="planned");if(planned.length){const pc=card("Compiled plans","Execution checks repository hashes and current privacy, capability and quality gates again.");table(pc,["Plan","Repository",""],planned.map(r=>[r.id,r.repo,button("Execute",async()=>{const value=await action("execute",{plan_id:r.id,repo_path:r.repo},false);showReceipt(await api("/ui/receipts/"+value.plan_id));})]));}
  const c=card("Preview a route", "See classification, eligible models, fallback paths, cost bounds and privacy. This simulator makes no inference calls and changes no repository files.");const f=el("form");field(f,"task","Task","Add a small greeting feature with tests","textarea").required=true;field(f,"repo","Repository context (optional)","","text",[["","No repository context"],...state.repositories.map(r=>[r.path,r.path])]);submit(f,"Simulate route",data=>action("route",{task:data.get("task"),repo_path:data.get("repo")},false));c.append(f);
}
function traces() {
  const receipts=card("Receipts & Routing Lab", "Inspect recorded usage, economics, changed-file hashes and validation. Lab comparisons use local evidence and make no model calls.");table(receipts,["Run","Status","Evidence"],state.runs.map(r=>{const group=el("div",undefined,"row");group.append(button("Receipt",async()=>showReceipt(await api("/ui/receipts/"+r.id)),"quiet"),button("Routing Lab",()=>action("lab-compare",{run_id:r.id},false),"quiet"));return[r.id,r.status,group];}));
  const c=card("Execution history", "Traces show observable decisions, model calls, validation and escalation. Hidden reasoning and provider secrets are not logged.");table(c,["Request", "Last activity", ""],state.dashboard.recent.map(r=>[r.request,new Date(r.stamp*1000).toLocaleString(),button("Inspect trace",async()=>result(await api("/ui/traces/"+encodeURIComponent(r.request))),"quiet")]));if(!state.dashboard.recent.length)c.append(el("p","No requests yet. Preview a route or connect a client to get started."));
}
function costs() {
  savingsSettings();
  const d=state.dashboard,c=card("Local cost dashboard", "Token charges are estimates from configured prices and reported usage. Uncertain calls retain their reserved maximum.");const metrics=el("div",undefined,"metrics");[["$"+d.costs.today_usd.toFixed(4),"Estimated spend today"],[d.costs.calls,"Inference calls"],[d.local_execution_percent+"%","Local execution"],[d.costs.uncertain_calls,"Uncertain calls"]].forEach(([v,l])=>{const m=el("div",undefined,"metric");m.append(el("strong",String(v)),el("span",l));metrics.append(m);});c.append(metrics);table(c,["Model","Calls","Mean latency","Estimated USD"],d.models.map(m=>[m.model,m.calls,m.latency.toFixed(2)+"s",m.estimated_cost.toFixed(6)]));c.append(el("p",d.savings_note,"hint"));details(c,"Learned success profiles",d.profiles);
}
function cache() {const c=card("Local cache", "Exact cache entries expire by policy and are keyed to repository/configuration fingerprints. Code edits are not semantically reused across changed repositories.");details(c,"Cache statistics",state.dashboard.cache);c.append(button("Clear cache",()=>action("cache-clear"),"danger"));}
function system() {
  const appearance=card("Appearance","Choose a theme; your preference stays in this browser."),theme=field(appearance,"theme","Theme",localStorage.getItem("accension-theme")||"system","text",["system","light","dark"]);theme.onchange=()=>{document.documentElement.dataset.theme=theme.value;localStorage.setItem("accension-theme",theme.value);};
  const c=card("System health", "Accension "+state.version+" · No account or remote control service required.");c.append(button("Run doctor",()=>action("doctor",{},false)),button("Reset learned profiles",()=>action("profiles-reset"),"quiet"));
  const recovery=card("Recovery", "Interrupted work is never resumed automatically. Resume requires the exact last completed checkpoint; rollback only touches unchanged router-written files.");table(recovery,["Plan","Status","Actions"],state.runs.map(r=>{const group=el("div",undefined,"row");["inspect","resume","rollback","discard"].forEach(a=>group.append(button(a,()=>action("recovery",{id:r.id,action:a}),a==="discard"?"danger":"quiet")));return[r.id,r.status,group];}));if(!state.runs.length)recovery.append(el("p","No persisted runs."));
  card("Provider extensions", "Third-party Python plugins are trusted executable software, not sandboxed. Install only packages you trust and explicitly enable their entry-point names in local configuration. Shared routing profiles cannot enable executable plugins.");
}
pages.forEach(page=>{const a=el("a",page);a.href="#"+page.toLowerCase();$("nav").append(a);});
document.documentElement.dataset.theme=localStorage.getItem("accension-theme")||"system";
window.addEventListener("hashchange",()=>{ $("output-section").hidden=true; notice(""); render(); window.scrollTo({top:0}); });
(async()=>{try{csrf=(await api("/ui/session")).csrf;await reload();await startPulse();}catch(e){notice(e.message,true);}})();

const periods=[["current","Current run"],["session","Session"],["today","Today"],["7d","7 days"],["30d","30 days"],["all","All time"]];
function currency(value, code="USD") {return value==null?"Unavailable":new Intl.NumberFormat(undefined,{style:"currency",currency:code,minimumFractionDigits:2,maximumFractionDigits:Math.abs(Number(value))<.01?4:2}).format(Number(value));}
function tokens(value) {return value==null?"Unavailable":new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(value);}
function savingsLabel(data) {const saved=data?.estimated_cost_saved;return saved==null?"API spend "+currency(data?.actual_cost):Number(saved)<0?"Est. "+currency(-Number(saved))+" above baseline":"Est. saved "+currency(saved);}
function renderPulse(value) {
  if(!value?.header){$("pulse-main").textContent="Usage unavailable";return;}
  pulseState=value;const h=value.header, config=value.settings;
  $("pulse-main").textContent=config.enabled?savingsLabel(h):"Savings tracking off";
  const extra=[];
  if(config.enabled&&config.show_tokens&&h.paid_cloud_tokens_avoided!=null)extra.push(tokens(Math.abs(h.paid_cloud_tokens_avoided))+(h.paid_cloud_tokens_avoided<0?" extra cloud tokens":" paid tokens avoided"));
  if(config.enabled&&config.show_percentage&&h.estimated_cost_saved_percent!=null)extra.push(Math.abs(Number(h.estimated_cost_saved_percent)).toFixed(0)+(Number(h.estimated_cost_saved_percent)<0?"% higher":"% reduction"));
  if(config.enabled&&h.estimated_cost_saved==null)extra.push(tokens(h.local_tokens_processed)+" local · "+tokens(h.cloud_tokens_processed)+" cloud");
  $("pulse-extra").textContent=extra.length?" · "+extra.join(" · "):"";
  $("savings-pulse").classList.toggle("running",value.active.length>0);
  $("savings-pulse").setAttribute("aria-label",(periods.find(p=>p[0]===h.period)?.[1]||"Today")+": "+$("pulse-main").textContent+$("pulse-extra").textContent+". Open savings details.");
  $("pulse-live").textContent=value.active.length?"● Active · "+savingsLabel(value.current):periods.find(p=>p[0]===h.period)?.[1]||"Today";
  if(!$("savings-popover").hidden)drawPopover(false);
}
async function startPulse() {
  renderPulse(await api("/ui/savings"));
  pulseStream=new EventSource("/ui/savings/events");
  pulseStream.addEventListener("savings.updated",event=>renderPulse(JSON.parse(event.data)));
  pulseStream.onerror=()=>{$("pulse-live").textContent="Reconnecting…";};
}
function metricRows(parent,data){
  if(!data){parent.append(el("p","No execution yet."));return;}
  const dl=el("dl",undefined,"economics-grid");
  const values=[["API cost",currency(data.actual_cost)],["Baseline · estimated",currency(data.baseline_estimated_cost)],["Savings · estimated",data.estimated_cost_saved==null?"Unavailable":Number(data.estimated_cost_saved)<0?currency(-Number(data.estimated_cost_saved))+" above baseline":currency(data.estimated_cost_saved)],
    ["Reduction · estimated",data.estimated_cost_saved_percent==null?"Unavailable":Number(data.estimated_cost_saved_percent).toFixed(1)+"%"],["Paid cloud tokens avoided · est.",tokens(data.paid_cloud_tokens_avoided)],
    ["Local tokens processed",tokens(data.local_tokens_processed)],["Cloud tokens processed",tokens(data.cloud_tokens_processed)],["Provider cached input · in cloud total",tokens(data.actual_cached_tokens)],
    ["Context tokens avoided · est.",tokens(data.context_tokens_avoided)],["Plan reuse",String(data.plan_reuse||0)]];
  values.forEach(([label,value])=>dl.append(el("dt",label),el("dd",value)));parent.append(dl);
  if(data.partial)parent.append(el("p","Partial coverage: unavailable prices, usage or baselines are excluded from precise savings claims.","hint"));
}
function drawPopover(reset=true){
  const pop=$("savings-popover"),data=pulseState;if(!data)return;
  // Keep the focused period control stable while live numbers change.
  if(reset||!pop.querySelector(".pulse-details")){
    pop.replaceChildren();const top=el("div",undefined,"row");top.append(el("h2","Accension Savings"),button("Close",closePopover,"quiet"));pop.append(top);
    const period=field(pop,"pulse-period","Header period",data.settings.header_period,"text",periods);
    period.onchange=async()=>{try{await api("/ui/action/savings-set",{header_period:period.value});renderPulse(await api("/ui/savings"));}catch(e){notice(e.message,true);}};
    pop.append(el("div",undefined,"pulse-details"));
    const how=el("details");how.append(el("summary","How is this calculated?"),el("p","Estimated savings = the frozen direct-model baseline minus API execution cost. Each logical stage is counted once; retries add actual cost. Local tokens are processed locally. Provider cached tokens stay in the cloud total. Context, routing and cache are contributing factors, not additive savings."),el("p","Token equivalents across models are estimates. Usage × price snapshots estimates API charges, not the final provider invoice. Dates use UTC; local electricity and hardware costs are excluded. Each receipt keeps its original baseline."));pop.append(how,button("Savings & Baseline settings",()=>{closePopover();go("costs");},"quiet"));
  }
  const body=pop.querySelector(".pulse-details");body.replaceChildren();body.append(el("h3",data.current_status==="active"?"Current run · live estimate":"Latest run"));metricRows(body,data.current);
  for(const [name,value] of [["Today · finalized",data.today],["All time · finalized",data.all]])body.append(el("h3",name),el("p",savingsLabel(value)+" · "+tokens(value.paid_cloud_tokens_avoided)+" paid tokens avoided"));
  body.append(el("h3","Baseline for new runs"),el("p",data.baseline.model||"Set a cloud baseline to estimate savings"),el("p",data.baseline.method+" · "+data.baseline.confidence+" confidence","hint"));
}
function closePopover(){$("savings-popover").hidden=true;$("savings-pulse").setAttribute("aria-expanded","false");$("savings-pulse").focus();}
$("savings-pulse").onclick=()=>{const open=$("savings-popover").hidden;$("savings-popover").hidden=!open;$("savings-pulse").setAttribute("aria-expanded",String(open));if(open){drawPopover();$("savings-popover").querySelector("button")?.focus();}};
document.addEventListener("keydown",event=>{if(event.key==="Escape"&&!$("savings-popover").hidden)closePopover();});
document.addEventListener("click",event=>{if(!$("savings-popover").hidden&&!event.target.closest(".pulse-anchor")){$("savings-popover").hidden=true;$("savings-pulse").setAttribute("aria-expanded","false");}});
function savingsSettings(){
  const c=card("Savings & Baseline","Your usage and savings ledger stays on your machine. Changes apply to new runs; historical receipts retain their original estimate.");
  const load=async()=>{const data=await api("/ui/savings"),s=data.settings,f=el("form");field(f,"enabled","Savings tracking",s.enabled,"checkbox");field(f,"baseline_method","Comparison baseline",s.baseline_method,"text",[["DIRECT_MODEL","The model I normally use"],["HOST_MODEL","Client model when explicitly identified"],["USER_SELECTED_MODEL","Another selected cloud model"],["QUALITY_BASELINE","Quality-first eligible cloud model"],["DISABLED","Disable comparison"]]);modelField(f,"baseline_model","Cloud baseline model (search registered IDs)",s.baseline_model||"");field(f,"header_period","Header period",s.header_period,"text",periods);field(f,"show_tokens","Show paid tokens avoided",s.show_tokens,"checkbox");field(f,"show_percentage","Show percentage reduction",s.show_percentage,"checkbox");submit(f,"Save savings settings",async form=>{await action("savings-set",{enabled:form.has("enabled"),baseline_method:form.get("baseline_method"),baseline_model:form.get("baseline_model")||null,header_period:form.get("header_period"),show_tokens:form.has("show_tokens"),show_percentage:form.has("show_percentage")},false);renderPulse(await api("/ui/savings"));});c.append(f);};load().catch(e=>notice(e.message,true));
}
function showReceipt(value){
  const section=$("output-section");section.hidden=false;section.replaceChildren(el("h2","Execution Receipt · "+value.run_id),el("p","Status: "+value.status));metricRows(section,value.economics);
  if(value.economics?.calls)table(section,["Stage","Model","API cost","Tokens"],value.economics.calls.map(c=>[c.role,c.model,currency(c.actual_cost),tokens(c.input_tokens+c.output_tokens)+(c.local?" local":" cloud")]));
  if(value.validation?.length)table(section,["Registered check","Result","Exit"],value.validation.map(c=>[c.check,c.passed?"PASS":"FAIL",c.exit_code??"Unavailable"]));
  if(value.files_changed?.length)table(section,["Changed file","Final SHA-256 (prefix)"],value.files_changed.map(f=>[f.path,f.after?.slice(0,16)||"Absent"]));
  details(section,"Observed evidence and hashes",value);
  section.scrollIntoView({block:"start"});
}
