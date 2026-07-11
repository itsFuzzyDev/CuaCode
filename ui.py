from rich.console import Console
from rich.theme import Theme
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live

mocha = Theme({
    "thinking": "#6c7086 italic",
    "tool": "#89b4fa bold",
    "tool.args": "#a6adc8",
    "prompt": "#a6e3a1 bold",
    "error": "#f38ba8 bold",
})
console = Console(theme=mocha)

def print_thinking_start(): console.print("<[thinking]thinking…[/thinking]>")
def print_thinking(text): console.print(text, style="thinking", end="")
def print_thinking_end(): console.print("\n</[thinking]thinking[/thinking]>\n")

def print_tool_calls(tools):
    for t in tools:
        args = t.function.arguments or {}
        console.print(Panel(f"\n[tool]{t.function.name}[/tool]([tool.args]{args}[/tool.args])",
                             border_style="#89b4fa", title="tool call", title_align="left"))

def prompt_user() -> str:
    return console.input("[prompt]›[/prompt] ")

def print_error(msg): console.print(f"[error]{msg}[/error]")
from rich.live import Live
from rich.markdown import Markdown

def stream_content(chunks):
    """chunks: generator yielding text pieces. Returns full accumulated text."""
    full = ""
    with Live(console=console, refresh_per_second=12) as live:
        for piece in chunks:
            full += piece
            live.update(Markdown(full))
    return full