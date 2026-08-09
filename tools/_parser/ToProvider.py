import json
from tools.loader import Tool
from tools._parser.FromProvider import ToolCall

BACKGROUND_PROP = {
    "type": "boolean",
    "description": ("Start the call and return a job id immediately instead of waiting for it. "
                    "The result does not come back from this call at all -- collect it later with "
                    "the background tool. Only worth it when the call is slow and you have "
                    "something else to do meanwhile; a job whose result you need in order to take "
                    "the next step should just be waited for."),
}

def _base(t: Tool) -> dict:
    """The schema as written, plus the one property no tool declares itself.

    Backgrounding is the agent loop's behaviour, not the handler's -- the
    handler is called identically either way and never learns which it was. So
    it is added here rather than copied into twenty InputSchema.json files that
    would then have to agree with each other, and taken back out in the loop
    before the handler sees it.
    """
    props = t.input_schema.get("properties", {})
    if t.backgroundable: props = {**props, "background": BACKGROUND_PROP}
    return {"properties": props, "required": t.input_schema.get("required", [])}

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
    images = data.get("images", [])
    if img and not images:
        images = [img]
    if images:
        meta = {k: v for k, v in data.items() if k not in ("image_base64", "images")}
        return [
            {"role": "tool", "content": json.dumps({
                "note": "images are attached as the next user message, not included here",
                **meta
            })},
            {"role": "user", "content": f"here are the {len(images)} photo(s) you requested", "images": images},
        ]
    return [{"role": "tool", "content": json.dumps(result)}]

RESULT_FORMATTERS = {"anthropic": _result_anthropic, "openai": _result_openai,
                      "gemini": _result_gemini, "ollama": _result_ollama}

def format_tool_result(call: ToolCall, result: dict, provider: str) -> dict:
    return RESULT_FORMATTERS[provider](call, result)
