-- Synthetic legacy snapshot: Accension source commit f13d3b80b8a882d15da172e0c148b9b1224988b6.
-- Built by constructing pre-edit Store and SkillRegistry; deterministic synthetic rows contain no live data.
-- Initial v0 subset comes from 48ed15e; model and plan payloads use unchanged audited V1 contracts.
-- Canonical table digests (sorted JSON rows, SHA-256):
-- benchmarks: 9ed01d3c41c556e5f7436d69c3083027368956a2bee5b13a0182e6ab44adcf5b
-- cache: 03add0b59d4d7feba8cffb20694862e925493e959fd5409c101abc30ac26a734
-- calls: 2a6fd53c713e9598b0ac3a1252b027ac2e6d36e4ee140c541002a44c24b3c848
-- dna: 756652bb56b0143f1a12bd0229b594795b5016625fac9dcd9cc4c68b66e1bae6
-- egress: 684e187a77c7c9c51af7e26adbe12edc60b4118f37d163bb28c79fce6128966d
-- health: c3e462f9b0ef7a7beced3edd339e2bb72c841086dcd1b7b75bf3a8dca080d375
-- metadata: 4d7d66f0736aab7451377cbcb3b0cea55f9811488bea0e04a1005119693416ba
-- model_registry: 7c2d08913c5541a6a2e186dc76748ff80df6f41f669272a6df3e9b84bf1bfd97
-- outcomes: cb2a9cfb07a6147638e73bab3a9d576f99eeeda12147737750a56aebd352e351
-- profiles: 93cc19212e56cafdf82bd6aee1c8a1019e97aa5f05d15957bfec35a93c7c3cc0
-- provider_health: 5c4145e49cf6d010b67b2e67a3e185a7c0acf59ac5ee86c1d8e561ae5be78ab5
-- receipts: 89b51e1fbd7986d8f1e0eeeb2bd45b4c2e70e3c556b70ad9a7692d06290fadb7
-- runs: 0f61fee2c30497dd33ae9633070b1b40edef7b21e697dc8a811aa1047ed78e45
-- savings_factors: 17174847f4cd382fa1181820d9e7540fec4814e1fd5fc2e9e496fd423d715383
-- savings_gaps: f72495990fffe81261665576644b8a1eb3f33103c85ddc35e53c0d46a3c3ef2a
-- savings_revision: 5aaf1e183f1faf12c327dc2fb8c223022f04684c61fc34533bff62e7e572c776
-- savings_runs: c18d190419ffdf9c3a6038d1cc241e623cdcae30ccd96acc9629538d1e32ba83
-- savings_totals: f172f42f1f999772ed833017b57bb67149baf1c423556ff49140e855a0fd1962
-- skill_defaults: a5095e1fa38db982476f9c40e90d431a4f450538cb68d991d6cadd2fb3f6b995
-- skill_presets: 1b8ba999ea70dfebe35f1c85904382e6c865e75061520388da9a842807ffb63c
-- skill_recipe_runs: a950bb3031f56f9d1b4f09cdf6022ba4d4d19fff80cf054af147bfe5c11d3425
-- skill_removed_legacy: 64d475e2df6aff1de70234a697db2dbae7f481fae7363b9f8793c6a571746a6e
-- skills: 6141fe2b350e25c6965bd4841312e645385ef18f54cf431c27001a7c0fd7aef3
-- task_calls: 47ecfa1d0c535d88d292e303b3be587cdb7bac7f909985d5c13456109b53c142
-- traces: 2d066999781263ebaff89adaa75fa410917d859f67da7fe98b62873c15e57109
-- usage_records: 293b08c3fa076faff848e4a3aa1361237ab3999176c36e7a590e9f55c2ab643e

BEGIN TRANSACTION;
CREATE TABLE benchmarks(model TEXT, category TEXT, stamp REAL, passed INTEGER, evidence TEXT, latency REAL,
          PRIMARY KEY(model,category));
INSERT INTO "benchmarks" VALUES('fixture-model','coding',1.0,1,'{"source":"fixture"}',1.0);
CREATE TABLE cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);
INSERT INTO "cache" VALUES('fixture-cache','cached value',2000000000.0);
CREATE TABLE calls(id TEXT PRIMARY KEY, request TEXT, session TEXT, day TEXT, role TEXT,
          model TEXT, frontier INTEGER, cost REAL, state TEXT, usage TEXT, latency REAL DEFAULT 0);
INSERT INTO "calls" VALUES('fixture-call','fixture-request','fixture-session','2026-10-12','coder','fixture-model',0,0.01,'complete','{"input":10}',1.0);
CREATE TABLE dna(model TEXT, dimension TEXT, specialization TEXT, samples INTEGER, value REAL, sources TEXT, stamp REAL,
          PRIMARY KEY(model,dimension,specialization));
INSERT INTO "dna" VALUES('fixture-model','dimension','specialization',1,0.9,'fixture',1.0);
CREATE TABLE egress(call TEXT PRIMARY KEY, request TEXT, node TEXT, provider TEXT, model TEXT, role TEXT,
          tokens INTEGER, files TEXT, state TEXT, stamp REAL, measurement TEXT);
INSERT INTO "egress" VALUES('fixture-call','fixture-request','fixture-node','fixture-provider','fixture-model','coder',10,'[]','complete',1.0,'{"redacted":true}');
CREATE TABLE health(model TEXT PRIMARY KEY, failures INTEGER, until REAL);
INSERT INTO "health" VALUES('fixture-model',0,0.0);
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "metadata" VALUES('fixture-key','fixture-value');
CREATE TABLE model_registry(id TEXT PRIMARY KEY, provider TEXT, data TEXT NOT NULL, updated REAL NOT NULL);
INSERT INTO "model_registry" VALUES('fixture-model','fixture-provider','{"id":"fixture-model","provider":"fixture-provider","deployment_name":"mock-worker","tier":1,"enabled":true,"roles":["executor","repair","reviewer","planner","gateway"],"input_price":0.0,"output_price":0.0,"cached_input_price":null,"cache_write_price":null,"context_window":32768,"max_output":4096,"supports_tools":true,"supports_structured_output":true,"supports_streaming":true,"supports_prompt_cache":false,"supports_vision":false,"supports_code":true,"supports_reasoning":false,"supports_mcp":false,"supports_responses_api":true,"supports_chat_completions":true,"expected_latency":5.0,"concurrency_limit":4,"rate_limit":30,"quality_priors":{"default":0.99},"status":"available","locality":"unknown","protocols":[],"supports_text":true,"supports_embeddings":false,"quality":{},"reliability":1.0,"ttft":null,"tokens_per_second":null,"ram_estimate_mb":null,"vram_estimate_mb":null,"loaded":null,"local_runtime":null,"pricing_status":"user_supplied","currency":"USD","pricing_source":"unknown","pricing_updated_at":null,"capabilities_source":"user_configuration","capability_evidence":{},"discovered_from":"user_configuration","discovered_at":null,"benchmarked_at":null,"inventory_stale":false}',1.0);
CREATE TABLE outcomes(seq INTEGER PRIMARY KEY, model TEXT, family TEXT, stamp REAL, data TEXT NOT NULL);
INSERT INTO "outcomes" VALUES(1,'fixture-model','coding',1.0,'{"success":true}');
CREATE TABLE profiles(model TEXT, family TEXT, n INTEGER, success REAL, latency REAL,
          PRIMARY KEY(model,family));
INSERT INTO "profiles" VALUES('fixture-model','coding',1,1.0,1.0);
CREATE TABLE provider_health(provider TEXT PRIMARY KEY, status TEXT NOT NULL, until REAL, error_code TEXT, stamp REAL);
INSERT INTO "provider_health" VALUES('fixture-provider','healthy',0.0,'',1.0);
CREATE TABLE receipts(run TEXT PRIMARY KEY, request TEXT, stamp REAL, data TEXT NOT NULL);
INSERT INTO "receipts" VALUES('fixture-run','fixture-request',1.0,'{"verified":true}');
CREATE TABLE runs(id TEXT PRIMARY KEY, repo TEXT, fingerprint TEXT, status TEXT, data TEXT);
INSERT INTO "runs" VALUES('fixture-run','fixture-repo','fixture-fingerprint','complete','{"plan":{"plan_id":"fixture-run","request_id":"fixture-request","goal":"Synthetic compatibility fixture","task_type":"coding","assumptions":[],"constraints":[],"architecture_summary":"","success_criteria":["Keep synthetic data readable"],"risk_level":20,"estimated_complexity":30,"allowed_budget":0.5,"artifacts":[],"tasks":[{"id":"fixture-task","jira_key":"","title":"Fixture task","objective":"Exercise stored plan readers","detailed_description":"","rationale":"","task_type":"coding","dependencies":[],"relevant_files":[],"likely_symbols":[],"required_context":[],"acceptance_criteria":["Data round trips"],"unit_tests":[],"integration_tests":[],"validation_commands":[],"skills_required":[],"tools_required":[],"estimated_input_tokens":2000,"estimated_output_tokens":2000,"complexity":30,"risk":20,"preferred_model_tier":1,"minimum_capabilities":["code"],"max_cost":0.1,"max_attempts":3,"parallelizable":true,"expected_artifacts":["fixture.txt"],"verifier_requirements":[],"escalation_conditions":[],"privacy_class":"CLOUD_ALLOWED","cloud_eligible":true,"quality_slo":null,"max_cloud_context":null,"max_cloud_files":null,"fallbacks":[]}],"dependency_edges":[],"global_validation":[],"rollback_strategy":"Restore only router-written files from backups, after checking hashes.","planner_model":"deterministic","planner_confidence":0.8,"privacy_contract":"CLOUD_ALLOWED","egress_budget":{"max_cloud_context_tokens_per_request":null,"max_cloud_files_per_request":null},"quality_contract":null,"skill_contract":{"skills":[],"preset_id":null,"suggestion_source":"explicit","token_budget":null}},"completed":{"fixture-task":{"status":"complete"}}}');
CREATE TABLE savings_factors(request TEXT, kind TEXT, stage TEXT, data TEXT, PRIMARY KEY(request,kind,stage));
INSERT INTO "savings_factors" VALUES('fixture-request','ordinary','complete','{"factor":1}');
CREATE TABLE savings_gaps(call TEXT PRIMARY KEY, session TEXT, day TEXT, stamp REAL);
INSERT INTO "savings_gaps" VALUES('fixture-call','fixture-session','2026-10-12',1.0);
CREATE TABLE savings_revision(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER);
INSERT INTO "savings_revision" VALUES(1,0);
CREATE TABLE savings_runs(request TEXT PRIMARY KEY, run TEXT, session TEXT, day TEXT,
          status TEXT, kind TEXT, stamp REAL, updated REAL, baseline TEXT, data TEXT);
INSERT INTO "savings_runs" VALUES('fixture-request','fixture-run','fixture-session','2026-10-12','complete','ordinary',1.0,1.0,'{}','{"saved":0.0}');
CREATE TABLE savings_totals(bucket TEXT, key TEXT, data TEXT, PRIMARY KEY(bucket,key));
INSERT INTO "savings_totals" VALUES('fixture-bucket','fixture-key','{"total":1}');
CREATE TABLE skill_defaults(scope TEXT PRIMARY KEY, preset TEXT);
INSERT INTO "skill_defaults" VALUES('workspace','fixture-preset');
CREATE TABLE skill_presets(id TEXT PRIMARY KEY, name TEXT UNIQUE, data TEXT);
INSERT INTO "skill_presets" VALUES('fixture-preset','Fixture Preset','{"id":"fixture-preset","name":"Fixture Preset","description":"Synthetic baseline preset.","ordered_skills":["fixture-skill"],"enabled":true,"task_tags":[],"preferred_roles":[],"auto_suggest":false,"token_budget":null,"created_at":1.0,"updated_at":1.0}');
CREATE TABLE skill_recipe_runs(run TEXT PRIMARY KEY, recipe TEXT, family TEXT, data TEXT);
INSERT INTO "skill_recipe_runs" VALUES('fixture-run','fixture-recipe','coding','{"skills":["fixture-skill"]}');
CREATE TABLE skill_removed_legacy(id TEXT PRIMARY KEY);
INSERT INTO "skill_removed_legacy" VALUES('fixture-removed-skill');
CREATE TABLE skills(id TEXT PRIMARY KEY, path TEXT UNIQUE, data TEXT, words TEXT);
INSERT INTO "skills" VALUES('fixture-skill','fixture/SKILL.md','{"id":"fixture-skill","name":"Fixture Skill","description":"Synthetic baseline skill.","source":"local","path":"fixture/SKILL.md","content_hash":"54d2b5e3d3f188ab3a5bdc92147196df1c1bd10051643a9f9566d2c271f34475","enabled":true,"trusted":false,"tags":[],"roles":[],"task_families":[],"requires":[],"conflicts_with":[],"before":[],"after":[],"token_size":2,"last_seen":1.0,"metadata_source":"filename"}','fixture skill');
CREATE TABLE task_calls(call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
INSERT INTO "task_calls" VALUES('fixture-call','fixture-task');
CREATE TABLE traces(seq INTEGER PRIMARY KEY, request TEXT, stage TEXT, stamp REAL, data TEXT);
INSERT INTO "traces" VALUES(1,'fixture-request','plan',1.0,'{"ok":true}');
CREATE TABLE usage_records(call TEXT PRIMARY KEY, request TEXT, task TEXT, role TEXT, stamp REAL, data TEXT);
INSERT INTO "usage_records" VALUES('fixture-call','fixture-request','fixture-task','coder',1.0,'{"input":10}');
CREATE INDEX traces_request ON traces(request);
CREATE INDEX task_calls_task ON task_calls(task_id);
CREATE INDEX calls_request ON calls(request);
CREATE INDEX calls_session ON calls(session);
CREATE INDEX registry_provider_id ON model_registry(provider,id);
CREATE INDEX egress_request_node ON egress(request,node);
CREATE INDEX receipt_request ON receipts(request);
CREATE INDEX savings_status_updated ON savings_runs(status,updated);
CREATE INDEX savings_updated ON savings_runs(updated);
CREATE INDEX usage_request ON usage_records(request,stamp);
CREATE INDEX savings_gaps_day ON savings_gaps(day);
CREATE INDEX savings_gaps_session ON savings_gaps(session);
CREATE INDEX savings_gaps_stamp ON savings_gaps(stamp);
CREATE INDEX skill_recipe_family ON skill_recipe_runs(family,recipe);
COMMIT;
PRAGMA user_version=3;
