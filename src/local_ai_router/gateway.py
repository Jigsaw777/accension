"""Protocol-native forwarding preserves tool calls and streaming semantics."""
import asyncio, json, time
from uuid import uuid4
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from .providers import estimate_cost, token_upper_bound, usage_from
from .routing import deterministic

async def forward(engine, protocol, body, session="gateway"):
    if body.get("previous_response_id") or body.get("conversation"):
        raise HTTPException(400, "Server-side conversation references are unsupported; send explicit history so cost can be bounded")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise HTTPException(400, "Only one completion per request is supported")
    if body.get("background"):
        raise HTTPException(400, "Background inference cannot be metered by this gateway")
    allowed_tool_types = {"function", "custom", None} if protocol == "messages" else {"function", "custom"}
    if any(not isinstance(t, dict) or t.get("type") not in allowed_tool_types for t in body.get("tools", [])):
        raise HTTPException(400, "Only client-executed tools are supported; hosted tool costs are not configured")
    if body.get("model", "router-auto") not in {"router-auto", "router-local", "router-cheap", "router-balanced", "router-quality", "router-planner"}:
        raise HTTPException(400, "Unknown virtual model")
    rid = uuid4().hex
    c = deterministic(json.dumps(body.get("input", body.get("messages", "")))[-6000:])
    caps = ["tools"] if body.get("tools") else []
    if body.get("stream"):
        caps.append("streaming")
    def has_unmetered_content(value):
        if isinstance(value, list):
            return any(has_unmetered_content(v) for v in value)
        if isinstance(value, dict):
            return value.get("type") in {"input_image", "image_url", "image", "input_audio", "audio", "input_file", "file", "video"} or any(has_unmetered_content(v) for v in value.values())
        return False
    if has_unmetered_content(body.get("input", body.get("messages", []))):
        raise HTTPException(400, "Multimodal and remote-file input accounting is not supported in V1")
    if '"cache_control"' in json.dumps(body):
        caps.append("prompt_cache")
    candidates = engine.router.candidates(c, "gateway", caps, allow_cloud=body.get("model") != "router-local", max_tier=2 if body.get("model") == "router-cheap" else 4)
    native = [m for m in candidates if engine.settings.providers[m.provider].kind == "mock" or (engine.settings.providers[m.provider].kind == "anthropic" if protocol == "messages" else m.supports_responses_api if protocol == "responses" else m.supports_chat_completions)]
    if not native:
        raise HTTPException(503, "No healthy configured provider supports this protocol and quality policy")
    error = None
    for model in native:
        provider = engine.settings.providers[model.provider]
        payload = dict(body)
        payload["model"] = model.deployment_name
        field = "max_output_tokens" if protocol == "responses" else "max_completion_tokens" if protocol == "chat" and "max_completion_tokens" in payload else "max_tokens"
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
        sem = engine.providers.semaphores[model.id]
        await sem.acquire()
        call = None
        start = time.monotonic()
        try:
            engine.providers._rate(model)
            call = engine.store.reserve(rid, session, "gateway", model, estimate, engine.settings.budgets.default_request_budget)
            if provider.kind == "mock":
                sem.release()
                return mock_response(engine, protocol, payload, rid, call, model)
            headers = await engine.providers.headers(provider)
            response = await engine.providers.client.send(engine.providers.client.build_request("POST", provider.endpoint.rstrip("/")+"/"+("chat/completions" if protocol == "chat" else protocol), headers=headers, json=payload, timeout=engine.settings.routing.timeouts["cloud"]), stream=bool(body.get("stream")))
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
                                                    for field in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"):
                                                        setattr(usage, field, max(getattr(usage, field), getattr(new, field)))
                                            if item.get("type") in ("response.completed", "message_stop"):
                                                finished = True
                                        except json.JSONDecodeError:
                                            pass
                            yield chunk
                    finally:
                        await response.aclose()
                        cost = estimate_cost(model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens) if usage and finished else None
                        engine.store.settle(call, cost, usage.model_dump() if usage else {}, time.monotonic()-start, failed=not finished)
                        engine.store.health_result(model.id, finished)
                        engine.store.trace(rid, "gateway", model=model.id, protocol=protocol, complete=finished)
                        sem.release()
                return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Router-Request-Id": rid, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            data = response.json()
            usage = usage_from(data)
            cost = estimate_cost(model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens) if usage.input_tokens or usage.output_tokens else estimate
            engine.store.settle(call, cost, usage.model_dump(), time.monotonic()-start)
            engine.store.health_result(model.id, True)
            sem.release()
            return JSONResponse(data, headers={"X-Router-Request-Id": rid})
        except BaseException as exc:
            sem.release()
            if call:
                engine.store.settle(call, None, {}, time.monotonic()-start, failed=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            from .store import BudgetExceeded
            if isinstance(exc, BudgetExceeded):
                raise HTTPException(429, str(exc)) from None
            engine.store.health_result(model.id, False)
            error = type(exc).__name__
            # No retry of a streamed response after bytes have been sent.
    raise HTTPException(503, "All compatible providers unavailable: "+str(error))

def mock_response(engine, protocol, payload, rid, call, model):
    text = "Local router mock response."
    usage = {"input_tokens": 5, "output_tokens": 6}
    engine.store.settle(call, 0, usage, 0)
    if protocol == "responses":
        item = {"id": "msg_"+rid, "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
        data = {"id": "resp_"+rid, "object": "response", "created_at": int(time.time()), "status": "completed", "model": "router-auto", "output": [item], "usage": {**usage, "total_tokens": 11}}
        frames = [("response.created", {"response": {**data, "status": "in_progress", "output": []}}),
                  ("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
                  ("response.content_part.added", {"item_id": item["id"], "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}),
                  ("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text}),
                  ("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": text}),
                  ("response.content_part.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "part": item["content"][0]}),
                  ("response.output_item.done", {"output_index": 0, "item": item}), ("response.completed", {"response": data})]
    elif protocol == "messages":
        data = {"id": "msg_"+rid, "type": "message", "role": "assistant", "model": "router-auto", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}
        frames = [("message_start", {"message": {**data, "content": [], "stop_reason": None}}),
                  ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                  ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
                  ("content_block_stop", {"index": 0}), ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 6}}), ("message_stop", {})]
    else:
        data = {"id": "chatcmpl-"+rid, "object": "chat.completion", "created": int(time.time()), "model": "router-auto", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}}
        frames = [("", {**data, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}),
                  ("", {**data, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})]
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
