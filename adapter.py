#!/usr/bin/env python3
"""
AgentRouter OpenAI Adapter
Author: 三百斤的窜天猴 & Community
License: MIT
Purpose: Translates OpenAI Chat Completions requests to AgentRouter.org /v1/responses API,
         bridging Tools / Function Calling bidirectionally, injecting claude-cli User-Agent,
         and providing True SSE Streaming + Azure EastUS2 Token Rate Limit (429) backoff buffering.
"""

import os
import sys
import json
import time
import asyncio
from aiohttp import web, ClientSession

PORT = int(os.environ.get("PORT", 8045))
HOST = os.environ.get("HOST", "0.0.0.0")
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://agentrouter.org")
DEFAULT_API_KEY = os.environ.get("AGENTROUTER_API_KEY", "")

HEADERS_TO_UPSTREAM = {
    "User-Agent": "claude-cli/1.0.108 (external, cli)",
    "Content-Type": "application/json",
}

# Cross-request pacing state (guarding against rapid burst WAF triggers)
_last_request_time = 0.0
_pacing_lock = asyncio.Lock()

async def handle_models(request):
    models_data = [
        {"id": "gpt-6.1-sol", "object": "model", "owned_by": "agentrouter"},
        {"id": "gpt-6-astra", "object": "model", "owned_by": "agentrouter"},
        {"id": "gpt-5.6-sol", "object": "model", "owned_by": "agentrouter"},
    ]
    return web.json_response({"object": "list", "data": models_data})

def convert_tools_to_upstream(tools):
    if not tools:
        return None
    upstream_tools = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            name = fn.get("name") or t.get("name")
            desc = fn.get("description") or t.get("description", "")
            params = fn.get("parameters") or t.get("parameters", {})
            upstream_tools.append({
                "type": "function",
                "name": name,
                "description": desc,
                "parameters": params
            })
    return upstream_tools if upstream_tools else None

def convert_messages_to_upstream(messages):
    prompt_input = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")

        if role == "tool":
            call_id = msg.get("tool_call_id") or "call_unknown"
            tool_output_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            prompt_input.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": tool_output_str
            })
            continue

        if role == "assistant":
            if content:
                prompt_input.append({
                    "role": "assistant",
                    "content": content
                })
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    tc_fn = tc.get("function", {})
                    fn_name = tc_fn.get("name", "")
                    fn_args = tc_fn.get("arguments", "{}")
                    call_id = tc.get("id") or f"call_{int(time.time()*1000)}"
                    prompt_input.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": fn_name,
                        "arguments": fn_args
                    })
            continue

        target_role = "system" if role in ("system", "developer") else "user"
        if isinstance(content, str):
            conv_content = content
        elif isinstance(content, list):
            conv_blocks = []
            for item in content:
                if isinstance(item, str):
                    conv_blocks.append({"type": "input_text", "text": item})
                elif isinstance(item, dict):
                    itype = item.get("type", "")
                    if itype == "text":
                        conv_blocks.append({"type": "input_text", "text": item.get("text", "")})
                    elif itype == "image_url":
                        img_val = item.get("image_url", "")
                        url = img_val.get("url", "") if isinstance(img_val, dict) else str(img_val)
                        conv_blocks.append({"type": "input_image", "image_url": str(url)})
                    elif itype in ("input_text", "input_image", "input_audio"):
                        conv_blocks.append(item)
                    else:
                        conv_blocks.append({"type": "input_text", "text": json.dumps(item, ensure_ascii=False)})
                else:
                    conv_blocks.append({"type": "input_text", "text": str(item)})
            conv_content = conv_blocks
        else:
            conv_content = str(content or "")

        prompt_input.append({
            "role": target_role,
            "content": conv_content
        })

    return prompt_input

async def handle_chat_completions(request):
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"error": {"message": f"Invalid JSON body: {str(e)}"}}, status=400)

    model = body.get("model", "gpt-6.1-sol")
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    incoming_tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    auth_header = request.headers.get("Authorization", "")
    api_key = auth_header.replace("Bearer ", "").strip() if auth_header else DEFAULT_API_KEY
    if not api_key:
        api_key = DEFAULT_API_KEY

    if not api_key:
        return web.json_response(
            {"error": {"message": "API key required. Provide via Authorization header (Bearer sk-...) or AGENTROUTER_API_KEY env var."}},
            status=401
        )

    prompt_input = convert_messages_to_upstream(messages)

    upstream_payload = {
        "model": model,
        "input": prompt_input,
        "stream": bool(stream),
    }

    converted_tools = convert_tools_to_upstream(incoming_tools)
    if converted_tools:
        upstream_payload["tools"] = converted_tools
        if tool_choice:
            if isinstance(tool_choice, str):
                upstream_payload["tool_choice"] = tool_choice
            elif isinstance(tool_choice, dict) and "function" in tool_choice:
                upstream_payload["tool_choice"] = tool_choice["function"].get("name")

    req_headers = dict(HEADERS_TO_UPSTREAM)
    req_headers["Authorization"] = f"Bearer {api_key}"

    target_url = f"{UPSTREAM_BASE}/v1/responses"

    # Micro-pacing guard (at least 2.0s between requests to prevent upstream burst limits)
    global _last_request_time
    async with _pacing_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < 2.0:
            await asyncio.sleep(2.0 - elapsed)
        _last_request_time = time.monotonic()

    max_retries = 3
    backoff_delays = [15, 20]

    async with ClientSession() as session:
        try:
            # ─────────────────────────────────────────────────────────────
            # PATH 1: True SSE Streaming (stream=True)
            # ─────────────────────────────────────────────────────────────
            if stream:
                upstream_resp = None
                for attempt in range(max_retries):
                    resp = await session.post(target_url, headers=req_headers, json=upstream_payload, timeout=180)
                    if resp.status == 429 and attempt < max_retries - 1:
                        delay = backoff_delays[attempt]
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Upstream 429 Rate Limited (stream). Waiting {delay}s before retry {attempt+1}/{max_retries-1}...", flush=True)
                        await resp.release()
                        await asyncio.sleep(delay)
                        continue
                    if resp.status != 200:
                        err_text = await resp.text()
                        return web.json_response(
                            {"error": {"message": f"Upstream error {resp.status}: {err_text}"}},
                            status=resp.status
                        )
                    upstream_resp = resp
                    break

                if not upstream_resp:
                    return web.json_response({"error": {"message": "Upstream unavailable after retries"}}, status=502)

                sse_response = web.StreamResponse(
                    status=200,
                    reason="OK",
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive"
                    }
                )
                await sse_response.prepare(request)

                created_ts = int(time.time())
                resp_id = f"chatcmpl-{created_ts}"
                first_chunk_sent = False
                tool_calls = {}
                tool_count = 0
                had_tools = False

                try:
                    async for line_bytes in upstream_resp.content:
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except Exception:
                            continue

                        event_type = data.get("type")

                        if event_type == "response.created":
                            resp_id = data.get("response", {}).get("id") or resp_id

                        elif event_type == "response.output_item.added":
                            item = data.get("item", {})
                            if item.get("type") == "function_call":
                                had_tools = True
                                item_id = item.get("id")
                                call_id = item.get("call_id") or item_id or f"call_{int(time.time()*1000)}"
                                name = item.get("name", "")
                                tool_calls[item_id] = {
                                    "index": tool_count,
                                    "id": call_id,
                                    "name": name,
                                    "arguments": ""
                                }
                                delta_obj = {
                                    "tool_calls": [{
                                        "index": tool_count,
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": ""
                                        }
                                    }]
                                }
                                if not first_chunk_sent:
                                    delta_obj["role"] = "assistant"
                                    first_chunk_sent = True
                                tool_count += 1
                                chunk = {
                                    "id": resp_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_ts,
                                    "model": model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": delta_obj,
                                        "finish_reason": None
                                    }]
                                }
                                await sse_response.write((f"data: {json.dumps(chunk)}\n\n").encode("utf-8"))

                        elif event_type == "response.function_call_arguments.delta":
                            item_id = data.get("item_id")
                            arg_delta = data.get("delta", "")
                            if item_id in tool_calls:
                                tool_calls[item_id]["arguments"] += arg_delta
                                chunk = {
                                    "id": resp_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_ts,
                                    "model": model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [{
                                                "index": tool_calls[item_id]["index"],
                                                "function": {"arguments": arg_delta}
                                            }]
                                        },
                                        "finish_reason": None
                                    }]
                                }
                                await sse_response.write((f"data: {json.dumps(chunk)}\n\n").encode("utf-8"))

                        elif event_type == "response.output_text.delta":
                            delta_text = data.get("delta", "")
                            if delta_text:
                                delta_obj = {"content": delta_text}
                                if not first_chunk_sent:
                                    delta_obj["role"] = "assistant"
                                    first_chunk_sent = True
                                chunk = {
                                    "id": resp_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_ts,
                                    "model": model,
                                    "choices": [{
                                        "index": 0,
                                        "delta": delta_obj,
                                        "finish_reason": None
                                    }]
                                }
                                await sse_response.write((f"data: {json.dumps(chunk)}\n\n").encode("utf-8"))

                    # Final stop chunk
                    finish_reason = "tool_calls" if had_tools else "stop"
                    stop_chunk = {
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": finish_reason}
                        ]
                    }
                    await sse_response.write((f"data: {json.dumps(stop_chunk)}\n\n").encode("utf-8"))
                    await sse_response.write(b"data: [DONE]\n\n")
                    await sse_response.write_eof()
                finally:
                    await upstream_resp.release()

                return sse_response

            # ─────────────────────────────────────────────────────────────
            # PATH 2: Non-Streaming JSON (stream=False)
            # ─────────────────────────────────────────────────────────────
            else:
                resp_status = 200
                resp_text = ""
                for attempt in range(max_retries):
                    async with session.post(target_url, headers=req_headers, json=upstream_payload, timeout=120) as resp:
                        resp_status = resp.status
                        resp_text = await resp.text()

                        if resp_status == 429 and attempt < max_retries - 1:
                            delay = backoff_delays[attempt]
                            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Upstream 429 Rate Limited (non-stream). Waiting {delay}s before retry {attempt+1}/{max_retries-1}...", flush=True)
                            await asyncio.sleep(delay)
                            continue
                        break

                if resp_status != 200:
                    return web.json_response(
                        {"error": {"message": f"Upstream error {resp_status}: {resp_text}"}},
                        status=resp_status
                    )

                upstream_data = json.loads(resp_text)
                content_text = ""
                tool_calls_list = []
                output_items = upstream_data.get("output", [])

                for item in output_items:
                    itype = item.get("type")
                    if itype == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                content_text += c.get("text", "")
                    elif itype == "function_call":
                        fn_name = item.get("name", "")
                        fn_args = item.get("arguments", "{}")
                        call_id = item.get("call_id") or item.get("id") or f"call_{int(time.time()*1000)}"
                        tool_calls_list.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": fn_args
                            }
                        })

                created_ts = int(time.time())
                resp_id = upstream_data.get("id", f"chatcmpl-{int(time.time())}")
                finish_reason = "tool_calls" if tool_calls_list else "stop"

                message_obj = {
                    "role": "assistant",
                    "content": content_text if content_text else None
                }
                if tool_calls_list:
                    message_obj["tool_calls"] = tool_calls_list

                openai_response = {
                    "id": resp_id,
                    "object": "chat.completion",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message_obj,
                            "finish_reason": finish_reason
                        }
                    ],
                    "usage": upstream_data.get("usage", {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    })
                }
                return web.json_response(openai_response)

        except Exception as e:
            return web.json_response({"error": {"message": f"Adapter error: {str(e)}"}}, status=500)

async def handle_health(request):
    return web.json_response({"status": "healthy", "service": "agentrouter-adapter"})

def init_app():
    app = web.Application(client_max_size=1_000_000_000)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    return app

if __name__ == "__main__":
    app = init_app()
    print(f"AgentRouter Adapter listening on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)
