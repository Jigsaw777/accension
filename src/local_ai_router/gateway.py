"""Protocol-native forwarding preserves tool calls and streaming semantics."""

import asyncio
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .engine import active_operation
from .privacy import fully_local, guard_provider, preflight, repository_scope
from .providers import estimate_cost, token_upper_bound, usage_from
from .routing import deterministic


@active_operation
async def forward(engine, protocol, body, session="gateway", scoped=False):
    metadata = body.get("metadata") or {}
    if not scoped and isinstance(metadata, dict) and metadata.get("accension_repository"):
        repository = engine.settings.repository(metadata["accension_repository"])
        payload = {**body, "metadata": {k: v for k, v in metadata.items() if k != "accension_repository"}}
        with repository_scope(repository):
            return await forward(engine, protocol, payload, session, scoped=True)
    if body.get("previous_response_id") or body.get("conversation"):
        raise HTTPException(
            400, "Server-side conversation references are unsupported; send explicit history so cost can be bounded"
        )
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise HTTPException(400, "Only one completion per request is supported")
    if body.get("background"):
        raise HTTPException(400, "Background inference cannot be metered by this gateway")
    allowed_tool_types = {"function", "custom", None} if protocol == "messages" else {"function", "custom"}
    if any(not isinstance(t, dict) or t.get("type") not in allowed_tool_types for t in body.get("tools", [])):
        raise HTTPException(400, "Only client-executed tools are supported; hosted tool costs are not configured")
    virtual = body.get("model", "accension-auto").replace("router-", "accension-", 1)
    if virtual not in {
        "accension-auto",
        "accension-local",
        "accension-cheap",
        "accension-balanced",
        "accension-quality",
        "accension-planner",
    }:
        raise HTTPException(400, "Unknown virtual model")
    from .observability import correlation

    rid = (correlation.get() or {}).get("request_id") or uuid4().hex
    c = deterministic(json.dumps(body.get("input", body.get("messages", "")))[-6000:])
    caps = ["tools"] if body.get("tools") else []
    if body.get("stream"):
        caps.append("streaming")

    def has_unmetered_content(value):
        if isinstance(value, list):
            return any(has_unmetered_content(v) for v in value)
        if isinstance(value, dict):
            return value.get("type") in {
                "input_image",
                "image_url",
                "image",
                "input_audio",
                "audio",
                "input_file",
                "file",
                "video",
            } or any(has_unmetered_content(v) for v in value.values())
        return False

    if has_unmetered_content(body.get("input", body.get("messages", []))):
        raise HTTPException(400, "Multimodal and remote-file input accounting is not supported by the native gateway")
    if '"cache_control"' in json.dumps(body):
        caps.append("prompt_cache")
    from .roles import RoleResolver

    selection_settings = engine.settings
    presets = {
        "accension-cheap": "maximum-savings",
        "accension-balanced": "balanced",
        "accension-quality": "quality-first",
    }
    if virtual in presets:
        from .policy import preset

        routing, control = preset(engine.settings, presets[virtual])
        control.fully_local = fully_local(engine.settings)
        selection_settings = engine.settings.model_copy(update={"routing": routing, "control_plane": control})
    role = "planner" if virtual == "accension-planner" else "gateway"
    candidates, explanation = RoleResolver(selection_settings, engine.store, engine.providers).select(
        c, role, caps, allow_cloud=virtual != "accension-local", budget=engine.settings.budgets.default_request_budget
    )
    target_protocol = {"messages": "anthropic_messages", "responses": "openai_responses", "chat": "openai_chat"}[
        protocol
    ]

    def compatible(model):
        provider = engine.settings.providers[model.provider]
        if provider.kind == "mock":
            return True
        from .errors import ProviderError

        try:
            if target_protocol not in engine.providers.plugins.get(provider.kind).manifest().protocols:
                return False
        except ProviderError:
            return False
        return (
            model.supports_responses_api
            if protocol == "responses"
            else model.supports_chat_completions
            if protocol == "chat"
            else True
        )

    native = [model for model in candidates if compatible(model)]
    engine.store.trace(
        rid,
        "route",
        virtual_model=virtual,
        candidates=[m.id for m in native],
        reason_codes=["LOCAL_CAPABILITY_AND_POLICY_GATES"],
    )
    if not native:
        raise HTTPException(503, "No healthy configured provider supports this protocol and quality policy")
    host_model = metadata.get("accension_host_model") if isinstance(metadata, dict) else None
    engine.store.economics("begin", rid, session, "gateway", host_model=host_model)
    if isinstance(body.get("metadata"), dict):
        body = {**body, "metadata": {k: v for k, v in body["metadata"].items() if k != "accension_host_model"}}
    error = None
    for attempt, model in enumerate(native, 1):
        if attempt > 1:
            engine.store.log.emit(
                "gateway",
                "retry",
                request_id=rid,
                session_id=session,
                model=model.id,
                provider=model.provider,
                attempt=attempt,
            )
        provider = engine.settings.providers[model.provider]
        guard_provider(engine.settings, provider, "gateway")
        payload = preflight(dict(body), provider)
        payload["model"] = model.deployment_name
        field = (
            "max_output_tokens"
            if protocol == "responses"
            else "max_completion_tokens"
            if protocol == "chat" and "max_completion_tokens" in payload
            else "max_tokens"
        )
        maximum = min(int(payload.get(field) or model.max_output), model.max_output)
        if maximum < 1:
            raise HTTPException(400, "Output token limit must be positive")
        payload[field] = maximum
        if field == "max_completion_tokens":
            payload.pop("max_tokens", None)
        inputs = token_upper_bound(payload)
        if inputs + maximum > model.context_window:
            continue
        estimate = estimate_cost(model, inputs, maximum, reserve=True)
        sem = await engine.providers.acquire_slot(model)
        call = None
        start = time.monotonic()
        try:
            engine.providers._rate(model)
            call = engine.store.reserve(
                rid, session, "gateway", model, estimate, engine.settings.budgets.default_request_budget
            )
            try:
                from .contracts import reserve_egress

                reserve_egress(engine.store, call, rid, model, "gateway", inputs)
            except BaseException:
                engine.store.settle(call, 0, {}, 0)
                call = None
                raise
            if provider.kind == "mock":
                sem.release()
                return mock_response(engine, protocol, payload, rid, call, model)
            headers = await engine.providers.headers(provider)
            from .transports import HTTPTransport

            base = HTTPTransport().base(engine.providers.context(provider, model.provider))
            response = await engine.providers.client.send(
                engine.providers.client.build_request(
                    "POST",
                    base + "/" + ("chat/completions" if protocol == "chat" else protocol),
                    headers=headers,
                    json=payload,
                    timeout=engine.settings.routing.timeouts["local" if provider.local else "cloud"],
                ),
                stream=bool(body.get("stream")),
            )
            response.raise_for_status()
            if body.get("stream"):
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    await response.aclose()
                    raise ValueError("Upstream did not return SSE")

                async def events(response=response, model=model, call=call, sem=sem, start=start):
                    usage = None
                    finished = False
                    buffer = ""
                    try:
                        async for chunk in response.aiter_text():
                            buffer += chunk
                            if len(buffer) > 2_000_000:
                                raise ValueError("Oversized SSE event")
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                if line.startswith("data:"):
                                    raw = line[5:].strip()
                                    if raw == "[DONE]":
                                        finished = True
                                    else:
                                        try:
                                            item = json.loads(raw)
                                            obj = item.get("response", item.get("message", item))
                                            if obj.get("usage"):
                                                new = usage_from(obj)
                                                if usage is None:
                                                    usage = new
                                                else:
                                                    for field in (
                                                        "input_tokens",
                                                        "output_tokens",
                                                        "cached_tokens",
                                                        "cache_write_tokens",
                                                    ):
                                                        setattr(
                                                            usage,
                                                            field,
                                                            max(getattr(usage, field), getattr(new, field)),
                                                        )
                                            if item.get("type") in ("response.completed", "message_stop"):
                                                finished = True
                                        except json.JSONDecodeError:
                                            pass
                            yield chunk
                    finally:
                        await response.aclose()
                        cost = (
                            estimate_cost(
                                model,
                                usage.input_tokens,
                                usage.output_tokens,
                                usage.cached_tokens,
                                usage.cache_write_tokens,
                            )
                            if usage and finished
                            else None
                        )
                        engine.store.settle(
                            call,
                            cost,
                            usage.model_dump() if usage else {},
                            time.monotonic() - start,
                            failed=not finished,
                        )
                        engine.store.health_result(model.id, finished)
                        engine.store.trace(rid, "gateway", model=model.id, protocol=protocol, complete=finished)
                        engine.store.economics("finish", rid, "complete" if finished else "uncertain")
                        sem.release()

                return StreamingResponse(
                    events(),
                    media_type="text/event-stream",
                    headers={"X-Router-Request-Id": rid, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            data = response.json()
            usage = usage_from(data)
            cost = (
                estimate_cost(
                    model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens
                )
                if usage.input_tokens or usage.output_tokens
                else estimate
            )
            engine.store.settle(call, cost, usage.model_dump(), time.monotonic() - start)
            engine.store.health_result(model.id, True)
            engine.store.economics("finish", rid)
            sem.release()
            return JSONResponse(data, headers={"X-Router-Request-Id": rid})
        except BaseException as exc:
            sem.release()
            if call:
                engine.store.settle(call, None, {}, time.monotonic() - start, failed=True)
            if isinstance(exc, asyncio.CancelledError):
                engine.store.economics("pause", rid, "interrupted")
                raise
            from .store import BudgetExceeded

            if isinstance(exc, BudgetExceeded):
                engine.store.economics("finish", rid, "failed")
                raise HTTPException(429, str(exc)) from None
            from .errors import PrivacyViolation

            if isinstance(exc, PrivacyViolation):
                engine.store.economics("finish", rid, "failed")
                raise HTTPException(403, str(exc)) from None
            from .errors import normalize_error

            normalized = normalize_error(exc)
            engine.providers.record_failure(model, normalized)
            error = normalized.code
            # No retry of a streamed response after bytes have been sent.
    engine.store.economics("finish", rid, "failed")
    raise HTTPException(503, "All compatible providers unavailable: " + str(error))


def mock_response(engine, protocol, payload, rid, call, model):
    text = "Local router mock response."
    usage = {"input_tokens": 5, "output_tokens": 6}
    engine.store.settle(call, 0, usage, 0)
    engine.store.economics("finish", rid)
    if protocol == "responses":
        item = {
            "id": "msg_" + rid,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        data = {
            "id": "resp_" + rid,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": "router-auto",
            "output": [item],
            "usage": {**usage, "total_tokens": 11},
        }
        frames = [
            ("response.created", {"response": {**data, "status": "in_progress", "output": []}}),
            (
                "response.output_item.added",
                {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
            ),
            (
                "response.content_part.added",
                {
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
            (
                "response.output_text.delta",
                {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text},
            ),
            ("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": text}),
            (
                "response.content_part.done",
                {"item_id": item["id"], "output_index": 0, "content_index": 0, "part": item["content"][0]},
            ),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": data}),
        ]
    elif protocol == "messages":
        data = {
            "id": "msg_" + rid,
            "type": "message",
            "role": "assistant",
            "model": "router-auto",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        }
        frames = [
            ("message_start", {"message": {**data, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 6}},
            ),
            ("message_stop", {}),
        ]
    else:
        data = {
            "id": "chatcmpl-" + rid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "router-auto",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        }
        frames = [
            (
                "",
                {
                    **data,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
                },
            ),
            (
                "",
                {
                    **data,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ),
        ]
    if not payload.get("stream"):
        return JSONResponse(data)

    async def stream():
        for index, (event, value) in enumerate(frames):
            if event:
                value = {"type": event, **value}
                if protocol == "responses":
                    value["sequence_number"] = index
            yield (f"event: {event}\n" if event else "") + "data: " + json.dumps(value) + "\n\n"
        if protocol == "chat":
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
