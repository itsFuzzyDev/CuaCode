# test.py
import ollama, time
from ui import console, print_thinking, print_thinking_start, print_thinking_end, print_tool_calls, prompt_user
from tools.loader import load_tools, dispatch
from tools._parser.ToProvider import to_provider, format_tool_result; from tools._parser.FromProvider import parse_tool_calls

def _capture_self_identity():
    import subprocess, platform
    system = platform.system()

    if system == "Darwin":
        r = subprocess.run(["osascript", "-e",
            'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True)
        return r.stdout.strip() or None

    elif system == "Windows":
        try:
            import win32gui, win32process, psutil
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name().replace(".exe", "")
        except Exception:
            return None

    else:  # Linux
        try:
            r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                                capture_output=True, text=True)
            return r.stdout.strip() or None
        except Exception:
            return None
class Ctx:
    def __init__(self, self_identity):
        self.self_identity = self_identity

ctx = Ctx(self_identity=_capture_self_identity())

registry = load_tools()
messages=[
    {"role": "system", "content": open('system_prompt.txt', 'r').read()}, 
    {"role": "assistant", "content": "Ready. I read the instructions. Key rules I'll follow: batch all independent tool calls in one response, ask only when genuinely blocked."}, 
    {"role": "user", "content": input("> ")}
]
settings = {"provider": "ollama", "model": "kimi-k2.6:cloud"}

while True:
    stream = ollama.chat(model=settings.get("model"), tools=to_provider(registry, settings.get("provider")), messages=messages, stream=True)
    thinking, content, tools = "", "", []
    mode = None 
    for chunk in stream:
        if chunk.message.thinking:
            if mode != "thinking": print_thinking_start(); mode = "thinking"
            print_thinking(chunk.message.thinking); thinking += chunk.message.thinking
        if chunk.message.content:
            if mode == "thinking": print_thinking_end()
            mode = "content"; print(chunk.message.content, end="", flush=True); content += chunk.message.content
        if chunk.message.tool_calls: tools.extend(chunk.message.tool_calls)
    if mode == "thinking": print_thinking_end()
    if tools: print_tool_calls(tools)
    print()


    messages.append({"role": "assistant", "thinking": thinking, "content": content, "tool_calls": tools})
    if not tools: messages.append({"role": "user", "content": input("> ")}); continue
    
    calls = parse_tool_calls({"message": {"tool_calls": tools}}, settings.get("provider"))
    for call in calls:
        result = dispatch(registry, call.name, call.args, ctx=ctx)
        messages.extend(format_tool_result(call, result, settings.get("provider")))
        if call is not calls[-1]: time.sleep(0.15)