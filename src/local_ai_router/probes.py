"""Inert protocol probes: request native features, inspect observable evidence."""

import base64
import json
import math
import struct
import zlib

from .errors import CapabilityUnsupported
from .schema import Generation, Usage

# One red pixel; no remote URLs or files are read by vision probes.
COLORS = {"red": b"\xff\x00\x00", "blue": b"\x00\x00\xff", "green": b"\x00\xff\x00"}


def pixel_png(color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + COLORS[color]))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


PARAMETERS = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["ok"]}},
    "required": ["value"],
    "additionalProperties": False,
}


def prepare(protocol, probe, path, payload, color="red"):
    if probe in {None, "text", "structured_output", "responses_api", "chat_completions"}:
        return path, payload
    if probe == "embeddings":
        if protocol == "google_gemini":
            return path.replace(":generateContent", ":embedContent"), {"content": payload["contents"][0]}
        if protocol not in {"openai_chat", "openai_responses"}:
            raise CapabilityUnsupported("Embedding probe requires an OpenAI-compatible embedding endpoint")
        return "/embeddings", {
            "model": payload["model"],
            "input": [m["content"] for m in payload.get("messages", payload.get("input", []))],
        }
    if probe == "streaming":
        if protocol == "google_gemini":
            return path.replace(":generateContent", ":streamGenerateContent?alt=sse"), payload
        payload["stream"] = True
        if protocol == "openai_chat":
            payload["stream_options"] = {"include_usage": True}
    elif probe == "tools":
        if protocol == "openai_chat":
            payload.update(
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "echo", "description": "Inert test", "parameters": PARAMETERS},
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "echo"}},
            )
        elif protocol == "openai_responses":
            payload.update(
                tools=[{"type": "function", "name": "echo", "description": "Inert test", "parameters": PARAMETERS}],
                tool_choice={"type": "function", "name": "echo"},
            )
        elif protocol == "anthropic_messages":
            payload.update(
                tools=[{"name": "echo", "description": "Inert test", "input_schema": PARAMETERS}],
                tool_choice={"type": "tool", "name": "echo"},
            )
        elif protocol == "google_gemini":
            payload.update(
                tools=[{"functionDeclarations": [{"name": "echo", "parameters": PARAMETERS}]}],
                toolConfig={"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["echo"]}},
            )
        else:
            raise CapabilityUnsupported("Native tool probe unavailable for this protocol")
    elif probe == "vision":
        question = "What color is this pixel? Reply with exactly one color word."
        png = pixel_png(color)
        if protocol == "openai_chat":
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png, "detail": "low"}},
                    ],
                }
            ]
        elif protocol == "openai_responses":
            payload["input"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": question},
                        {"type": "input_image", "image_url": "data:image/png;base64," + png, "detail": "low"},
                    ],
                }
            ]
        elif protocol == "anthropic_messages":
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
                    ],
                }
            ]
        elif protocol == "google_gemini":
            payload["contents"] = [
                {"role": "user", "parts": [{"text": question}, {"inlineData": {"mimeType": "image/png", "data": png}}]}
            ]
        else:
            raise CapabilityUnsupported("Native image probe unavailable for this protocol")
    elif probe == "reasoning":
        if protocol == "openai_chat":
            payload["reasoning_effort"] = "low"
        elif protocol == "openai_responses":
            payload["reasoning"] = {"effort": "low"}
        elif protocol == "google_gemini":
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 32}
        else:
            raise CapabilityUnsupported("Tiny reasoning probe unavailable for this protocol")
    else:
        raise CapabilityUnsupported("Unknown probe")
    return path, payload


def evidence(protocol, probe, data, text, color="red"):
    if probe == "tools":
        if protocol == "openai_chat":
            calls = [
                c.get("function", {}) for c in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            ]
        elif protocol == "openai_responses":
            calls = [c for c in data.get("output", []) if c.get("type") == "function_call"]
        elif protocol == "anthropic_messages":
            calls = [
                {"name": c.get("name"), "arguments": c.get("input")}
                for c in data.get("content", [])
                if c.get("type") == "tool_use"
            ]
        else:
            calls = [
                {"name": p["functionCall"].get("name"), "arguments": p["functionCall"].get("args")}
                for c in data.get("candidates", [])
                for p in c.get("content", {}).get("parts", [])
                if "functionCall" in p
            ]
        for call in calls:
            try:
                args = call.get("arguments", {})
                args = json.loads(args) if isinstance(args, str) else args
                if call.get("name") == "echo" and args == {"value": "ok"}:
                    return {probe: True}
            except (TypeError, ValueError):
                pass
        return {probe: False}
    if probe == "vision":
        return {probe: text.strip().strip(".").lower() == color}
    if probe == "reasoning":
        usage = data.get("usage", {})
        count = (usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens", 0
        )
        return {probe: bool(count or data.get("usageMetadata", {}).get("thoughtsTokenCount", 0))}
    if probe == "embeddings":
        records = (
            data.get("data", [])
            if protocol != "google_gemini"
            else [{"embedding": data.get("embedding", {}).get("values", [])}]
        )
        valid = bool(records)
        for row in records:
            vector = row.get("embedding", [])
            valid &= bool(vector) and all(type(v) in {float, int} and math.isfinite(v) for v in vector)
        return {probe: valid}
    return {}


async def stream_probe(context, url, payload, timeout):
    from .providers import usage_from

    usage, seen, finished, size = Usage(), False, False, 0
    async with context.client.stream(
        "POST", url, headers=await context.headers(), json=payload, timeout=timeout
    ) as response:
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise CapabilityUnsupported("Provider did not return SSE")
        buffer = ""
        async for chunk in response.aiter_text():
            size += len(chunk)
            buffer += chunk
            if size > 1_000_000:
                raise CapabilityUnsupported("Probe stream exceeded limit")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    finished = True
                    continue
                data = json.loads(raw)
                seen = True
                obj = data.get("response", data.get("message", data))
                new = usage_from(obj)
                for field in type(usage).model_fields:
                    setattr(usage, field, max(getattr(usage, field), getattr(new, field)))
                finished |= data.get("type") in {"response.completed", "message_stop"} or any(
                    c.get("finishReason") for c in data.get("candidates", [])
                )
    return Generation(text="", usage=usage, raw={"probe_evidence": {"streaming": seen and finished}})
