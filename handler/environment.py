"""What the agent knows about the machine before it looks at anything.

Without this the agent has no idea what it is running on, where it stores
anything, or what "~/.cuacode" means -- so it guesses, and a guess about a path
costs a shell call to disprove at best.

Everything here is stable for the life of the process. That is a hard
requirement, not a stylistic one: this block is appended to the system prompt,
anthropic caches the system prompt, and a value that changes every turn would
invalidate that cache on every turn. Volatile facts -- what is on screen, what
is frontmost, what time it is -- do not belong here. The screen is answered by
a screenshot and the rest by a tool call, both of which are current, which this
can never be.
"""
import platform
from datetime import date, datetime

from handler.session import store

def _vision(settings: dict) -> list:
    """What this model can and cannot see, and what to do about it."""
    from handler import config
    from handler.agent import providers

    name = settings.get("provider") or ""
    if not name: return []
    default = getattr(providers.get(name), "vision", True)
    if settings.get("vision", default): return []

    # The pair, not just the provider: the model is the part that matters to
    # someone deciding whether to send their screen to it.
    found = config.lookers()
    helper = f"{found[0][0]}/{found[0][1] or config.model_for(found[0][0])}" if found else ""
    out = ["",
           "You cannot see images. This model does not accept them, so the screenshot and",
           "photos tools are not offered to you — not missing, withheld, because sending an",
           "image to this model fails the whole turn."]
    if helper:
        out += ["The screen is still readable to you, through describe_image. That tool",
                "captures the screen itself — you do not need a screenshot tool and you never",
                f"handle the image. It sends the picture to {helper}, which can see, and returns",
                "a written answer to the one question you asked. Never tell the user you have",
                "no way to look at something; use it, or say what it could not tell you.",
                "It is a description rather than a look: slower, an extra model call, and only",
                "as good as the question. Coordinates in it are guesses and cannot be clicked."]
    else:
        out += ["No other configured provider can see either, so nothing here can look at an",
                "image at all. The user names one under \"vision\" in config.json, or leaves it",
                "empty to let any sighted provider take the job."]
    out += ["",
            "click, mouse_move and scroll are withheld for the same reason. Their coordinates",
            "are read off a zoomed screenshot grid, and without one every call is a guess that",
            "lands somewhere real and cannot be undone. Nothing would report that as an error,",
            "which is what makes it worse than a refused call, not better.",
            "",
            "key and type_text you do still have. They take no coordinates — they go wherever",
            "focus already is — so they are the one way you can drive anything. Confirm focus",
            "with describe_image before typing something that matters, and confirm the result",
            "after. Keyboard-first paths (an app's own shortcuts, a launcher, tab order) are",
            "what work here; anything needing a pointer does not.",
            "",
            "So: you can read the screen and you can type. You cannot point at things. If the",
            "user wants real computer use, say plainly that it needs a vision-capable model —",
            "but do not overstate it into being unable to see or do anything at all."]
    return out

def block(ctx=None, settings: dict = None, session=None) -> str:
    settings = settings or {}
    home = store.home()
    repo = store.tools_dir().parent
    cwd = (ctx or {}).get("cwd") or ""

    lines = [
        "<environment>",
        "You are CuaCode, a computer-use agent running on the user's machine.",
        "",
        f"Platform: {platform.system().lower()} {platform.release()} ({platform.machine()})",
        f"Date: {date.today().isoformat()}",
    ]
    if cwd: lines.append(f"Working directory: {cwd}")
    model = settings.get("model") or ""
    if settings.get("provider"):
        lines.append(f"Model: {settings['provider']}"
                     + (f" / {model}" if model else "")
                     + (f"  (effort: {session.effort})" if getattr(session, "effort", "") else ""))
    if getattr(session, "id", ""): lines.append(f"Session: {session.id}   (timestamp is UTC)")
    # The session id's timestamp is UTC and reads like local time at a glance;
    # say the local time out loud so the two are never ambiguous. Rebuilt per turn
    # (see main.py), so it is current, not the time the session started.
    now = datetime.now().astimezone()
    lines.append(f"Local time: {now.strftime('%Y-%m-%d %H:%M:%S %Z (%z)')}")

    # Said out loud, because the alternative is a model that notices it has no
    # screenshot tool and concludes the machine is broken. The tools are
    # withheld deliberately -- an image sent to a model that cannot take one is
    # a 400 that costs the whole turn -- and knowing that is the difference
    # between explaining the limit to the user and flailing at it.
    lines += _vision(settings)

    lines += [
        "",
        f"Your own state lives in {home}:",
        "  config.json      providers, API keys, model settings",
        "  sessions/        one directory per conversation",
        "  subagents/*.md   subagents the agent tool can run -- write new ones here",
        "  workflows/*.py   scripts the workflow tool runs -- write new ones here",
        "  skills/<name>/   skills, each a folder with a SKILL.md -- write new ones here",
        "  memory/          what you remember, one fact per file -- through the memory tool,",
        "                   which lists what is in scope and loads one on request",
        "",
        f"CuaCode itself is installed at {repo}. Its integrations/ directory holds the",
        "bundled subagents, workflows and skills; a file of the same name in the user's",
        "directory wins over a bundled one. Only edit the installation itself when the",
        "user is working on CuaCode.",
        "",
        "Load the `cuacode` skill before answering questions about how you work or",
        "changing anything in that directory. Do not describe your own architecture",
        "from memory.",
        "</environment>",
    ]
    return "\n".join(lines)
