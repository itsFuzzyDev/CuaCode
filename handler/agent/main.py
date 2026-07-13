import os, shutil
import ollama, time
from tools.loader import load_tools, dispatch
from tools._parser.ToProvider import to_provider, format_tool_result
from tools._parser.FromProvider import parse_tool_calls

_tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools')

def generate(API_KEY: str = None, ctx=None, messages: list[dict] = [{"role": "user", "content": "hey"}], settings: dict = {"model": "kimi-k2.6:cloud", "provider": "ollama"}):
    provider = settings.get("provider", "ollama")
    registry = load_tools(tools_dir=_tools_dir)
    _tools = to_provider(tools=registry, provider=provider)
    if provider == "ollama":
        while True:
            if API_KEY:
                os.environ['OLLAMA_API_KEY'] = API_KEY
                if not shutil.which("ollama"):
                    os.environ['OLLAMA_HOST'] = 'https://ollama.com/'
            stream = ollama.chat(
                model=settings.get("model", "kimi-k2.6:cloud"),
                messages=messages,
                tools=_tools,
                think=True,
                stream=True
            )
            thinking, content, tools = "", "", []
            for chunk in stream:
                t = getattr(chunk.message, 'thinking', None) or ""
                c = getattr(chunk.message, 'content', None) or ""
                tcs = getattr(chunk.message, 'tool_calls', None) or []
                if t:
                    yield {"type": "thinking", "text": t}
                    thinking += t
                if c:
                    yield {"type": "content", "text": c}
                    content += c
                if tcs:
                    ser = [tc.model_dump() if hasattr(tc, 'model_dump') else tc.dict() if hasattr(tc, 'dict') else tc for tc in tcs]
                    yield {"type": "tool_calls", "text": ser}
                    tools.extend(ser)
            messages.append({"role": "assistant", "thinking": thinking, "content": content, "tool_calls": tools})
            
            if tools:
                calls = parse_tool_calls({"message": {"tool_calls": tools}}, provider)
                for call in calls:
                    result = dispatch(registry, call.name, call.args, ctx=ctx)
                    fmt = format_tool_result(call, result, provider)
                    if isinstance(fmt, list): messages.extend(fmt)
                    else: messages.append(fmt)
                    if call is not calls[-1]: time.sleep(0.15)
                continue
            yield {"type": "done", "messages": messages}
            break