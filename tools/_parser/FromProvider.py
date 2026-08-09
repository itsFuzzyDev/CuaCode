from dataclasses import dataclass

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict

def _ollama(response) -> list[ToolCall]:
    calls = response["message"].get("tool_calls") or []
    return [ToolCall(f"call_{i}", c["function"]["name"], c["function"]["arguments"]) for i, c in enumerate(calls)]

PARSERS = {"ollama": _ollama}

def parse_tool_calls(response, provider: str) -> list[ToolCall]:
    return PARSERS[provider](response)
