import json
from tools.loader import Tool
from tools._parser.FromProvider import ToolCall

def _base(t: Tool) -> dict:
    return {"properties": t.input_schema.get("properties", {}), "required": t.input_schema.get("required", [])}

# ---- schema: registry -> provider tool list ----

def _schema_anthropic(tools: dict) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": {"type": "object", **_base(t)}}
            for t in tools.values()]

def _schema_openai(tools: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
             "parameters": {"type": "object", **_base(t)}}} for t in tools.values()]

def _schema_gemini(tools: dict) -> dict:
    return {"function_declarations": [{"name": t.name, "description": t.description,
            "parameters": {"type": "OBJECT", **_base(t)}} for t in tools.values()]}

def _schema_ollama(tools: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
             "parameters": {"type": "object", **_base(t)}}} for t in tools.values()]

SCHEMA_ADAPTERS = {"anthropic": _schema_anthropic, "openai": _schema_openai,
                    "gemini": _schema_gemini, "ollama": _schema_ollama}

def to_provider(tools: dict, provider: str):
    active = {k: t for k, t in tools.items() if t.active}
    return SCHEMA_ADAPTERS[provider](active)

# ---- result: dispatch output -> provider message ----

def _result_anthropic(call: ToolCall, result: dict) -> dict:
    return {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}

def _result_openai(call: ToolCall, result: dict) -> dict:
    return {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}

def _result_gemini(call: ToolCall, result: dict) -> dict:
    return {"function_response": {"name": call.name, "response": result}}

def _result_ollama(call: ToolCall, result: dict) -> list[dict]:
    data = result.get("result", {})
    img = data.get("image_base64")
    if img:
        meta = {k: v for k, v in data.items() if k != "image_base64"}
        return [
            {"role": "tool", "content": json.dumps({
                "note": "image is attached as the next user message, not included here",
                **meta
            })},
            {"role": "user", "content": "here is the screenshot you requested", "images": [img]},
        ]
    return [{"role": "tool", "content": json.dumps(result)}]

RESULT_FORMATTERS = {"anthropic": _result_anthropic, "openai": _result_openai,
                      "gemini": _result_gemini, "ollama": _result_ollama}

def format_tool_result(call: ToolCall, result: dict, provider: str) -> dict:
    return RESULT_FORMATTERS[provider](call, result)
