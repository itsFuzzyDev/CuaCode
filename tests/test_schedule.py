#!/usr/bin/env python3
"""The scheduler's arithmetic and its folder rules, without a daemon or a model.

The expensive part of a scheduler is deciding *when* and *what it is allowed to
be*: a wrong cron answer runs at the wrong hour, a job that forgot `off` is a
permission prompt nobody can answer. These are asserts, not a framework, because
that is the smallest thing that fails when the rules break.

    python3 tests/test_schedule.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="cuacode-schedule-test-")
os.environ["CUACODE_HOME"] = TMP
sys.path.insert(0, str(ROOT))

from handler import schedule as s  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


def write(rel: str, text: str) -> Path:
    path = Path(TMP, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---- cron ----

f = s.parse_cron("*/15 * * * *")
check("step field", s.cron_next(f, datetime(2026, 11, 13, 7, 7)) == datetime(2026, 11, 13, 7, 15))

f = s.parse_cron("0 8 * * 1-5")
check("weekday match", s.cron_next(f, datetime(2026, 11, 13, 7, 0)) == datetime(2026, 11, 13, 8, 0))
check("weekday skips the weekend",
      s.cron_next(f, datetime(2026, 11, 13, 9, 0)) == datetime(2026, 11, 16, 8, 0))

# Cron's day rule is OR, not AND: the first of the month OR any Sunday.
f = s.parse_cron("0 0 1 * 0")
check("day-of-month or day-of-week",
      s.cron_next(f, datetime(2026, 11, 13, 12, 0)) == datetime(2026, 11, 15, 0, 0))

check("alias", s.parse_cron("@daily") == s.parse_cron("0 0 * * *"))
check("sunday 7 folds to 0", s.parse_cron("0 0 * * 7")[4] == (0,))

try:
    s.parse_cron("61 * * * *")
    check("out of range is refused", False)
except ValueError:
    check("out of range is refused", True)

try:
    s.parse_cron("*/0 * * * *")
    check("zero step is refused", False)
except ValueError:
    check("zero step is refused", True)

# ---- the three `when` spellings ----

kind, value = s.parse_when("every 90s")
check("every 90s", (kind, value) == ("every", 90))
kind, value = s.parse_when("every 2h")
check("every 2h", (kind, value) == ("every", 7200))
kind, value = s.parse_when("at 2026-11-14 09:00")
check("at", (kind, value) == ("at", datetime(2026, 11, 14, 9, 0)))
check("cron fallback", s.parse_when("0 8 * * *")[0] == "cron")

try:
    s.parse_when("every 1s")
    check("a too-short interval is refused", False)
except ValueError:
    check("a too-short interval is refused", True)

job = {"kind": "every", "every": 60}
check("every next", s.next_run(job, datetime(2026, 11, 13, 7, 0)) == datetime(2026, 11, 13, 7, 1))
job = {"kind": "at", "at": datetime(2026, 11, 13, 7, 0)}
check("at in the past has no next", s.next_run(job, datetime(2026, 11, 13, 8, 0)) is None)

# ---- prompts ----

job = {"name": "hook", "prompt": "got {{json.message}} from {{header.X-Repo}} at {{now}}"}
payload = s.Payload(raw='{"message":"push"}', json={"message": "push"},
                    headers={"X-Repo": "cuacode"})
text = s.render(job["prompt"], job, payload)
check("json and header render", "got push from cuacode at " in text, text)
check("unknown token stays", "{{nope}}" in s.render("{{nope}}", job, None))

job = {"name": "hook", "prompt": "Do the thing."}
out = s.prompt_for(job, s.Payload(raw='{"message":"push"}', json={"message": "push"}))
check("unmentioned payload is appended", "untrusted data" in out and '"message": "push"' in out, out)
check("webhook turn is labelled", "started by a webhook" in out)

job = {"name": "hook", "prompt": "Payload: {{body}}"}
out = s.prompt_for(job, s.Payload(raw="hello", json=None))
check("mentioned payload is not appended twice", out.count("hello") == 1, out)

# ---- folders ----

write("scheduled/toml/job.toml", """
when = "every 30m"
prompt = "Do the thing."
tools = ["file", "shell"]
permissions = "off"
max_rounds = 4
""")
write("scheduled/md/job.md", """---
when: "0 9 * * *"
permissions: off
tools: []
enabled: false
---
Say hello from Markdown.
""")
write("scheduled/at/job.md", """---
at: "2026-12-01 10:00"
permissions: "off"
---
From a file with frontmatter.
""")
write("scheduled/bad/job.toml", 'prompt = "x"\npermissions = "ask"\n')
write("scheduled/named/job.toml", 'name = "other"\nprompt = "x"\npermissions = "off"\n')

jobs, errors = s.discover_jobs()
by_name = {j["name"]: j for j in jobs}
check("discovery finds the good folders", set(by_name) == {"toml", "md", "at"}, set(by_name))
check("wrong permission is refused", any("bad" in e and "off" in e for e in errors), errors)
check("the folder name cannot be renamed", any("named" in e for e in errors), errors)
check("toml prompt", by_name["toml"]["prompt"] == "Do the thing.")
check("toml limits", by_name["toml"]["max_rounds"] == 4)
check("tools", by_name["toml"]["tools"] == ["file", "shell"])
check("markdown body is the prompt", by_name["md"]["prompt"] == "Say hello from Markdown.")
check("yaml off reads as off", by_name["md"]["permissions"] == "off")
check("markdown disabled", by_name["md"]["enabled"] is False)
check("at from frontmatter", by_name["at"]["kind"] == "at")

write("scheduled/badtool/job.toml", 'prompt = "x"\npermissions = "off"\n')
write("scheduled/badtool/tools/broken/main.py", "def run(args, ctx):\n    return {}\n")
jobs, errors = s.discover_jobs()
by_name = {j["name"]: j for j in jobs}
check("a broken tool is refused", "badtool" not in by_name
      and any("badtool" in e and "Description.md" in e for e in errors), errors)

# every: 30 is an integer, so _when_from turns it into `every 30s`.
write("scheduled/every/job.toml", 'every = 30\nprompt = "x"\npermissions = "off"\n')
jobs, errors = s.discover_jobs()
by_name = {j["name"]: j for j in jobs}
check("integer every", by_name["every"]["kind"] == "every" and by_name["every"]["every"] == 30)

# ---- extras and the temporary home ----

write("scheduled/extras/job.toml", """
when = "0 7 * * *"
prompt = "Use the demo skill."
permissions = "off"
provider = "ollama"
model = "test-model"
""")
write("scheduled/extras/providers.toml", """
[ollama]
model = "test-model"
base_url = "http://127.0.0.1:11434/v1"
""")
write("scheduled/extras/mcp/servers.json",
      '{"mcpServers": {"demo": {"command": "echo", "args": ["hi"]}}}')
write("scheduled/extras/skills/demo/SKILL.md",
      "---\nname: demo\ndescription: says demo\n---\nSay demo.\n")
write("scheduled/extras/subagents/reviewer.md",
      "---\nname: reviewer\ndescription: reviews\n---\nReview it.\n")
write("scheduled/extras/workflows/check.py",
      "NAME = 'check'\n\ndef run(args):\n    return {}\n")
write("scheduled/extras/tools/mytool/Description.md",
      "---\nname: mytool\n---\nSay hi.\n")
write("scheduled/extras/tools/mytool/InputSchema.json", '{"type":"object","properties":{}}')
write("scheduled/extras/tools/mytool/main.py", 'def run(args, ctx):\n    return {"ok": True}\n')
write("scheduled/extras/SYSTEM.md", "Job rule: keep it to one line.\n")

write("config.json", json.dumps({
    "active": "ollama",
    "providers": {"ollama": {"model": "user-model", "api_key": "user-key"}},
}))
write("mcp/servers.json", '{"mcpServers": {"user": {"command": "true"}}}')
write("AGENTS.md", "Be careful.\n")
Path(TMP, "sessions").mkdir()
Path(TMP, "memory").mkdir()

jobs, errors = s.discover_jobs()
extras = next(j for j in jobs if j["name"] == "extras")
check("provider extras", list(extras["providers"]) == ["ollama"])
check("mcp extras", list(extras["mcp"]) == ["demo"])
check("skill count", extras["skill_count"] == 1)
check("subagent count", extras["subagent_count"] == 1)
check("workflow count", extras["workflow_count"] == 1)
check("tool count", extras["tool_count"] == 1)
check("system file", extras["system_name"] == "SYSTEM.md")

home = s.build_home(extras, "run-test")
config = json.loads((home / "config.json").read_text())
check("job provider wins", config["providers"]["ollama"]["model"] == "test-model")
check("the user key survives the override", config["providers"]["ollama"]["api_key"] == "user-key")
check("base_url survives", config["providers"]["ollama"]["base_url"] == "http://127.0.0.1:11434/v1")
check("permission mode in the overlay", config["permission"]["mode"] == "off")
check("overlay is private", (home.stat().st_mode & 0o777) == 0o700)
check("config is private", ((home / "config.json").stat().st_mode & 0o777) == 0o600)
check("skill symlinked", (home / "skills" / "demo").is_symlink())
check("subagent symlinked", (home / "subagents" / "reviewer.md").is_symlink())
check("workflow symlinked", (home / "workflows" / "check.py").is_symlink())
check("custom tool symlinked", (home / "tools" / "mytool").is_symlink())
check("built-in tool still there", (home / "tools" / "file").is_dir())
agents_text = (home / "AGENTS.md").read_text()
check("AGENTS.md carries the user block", "Be careful." in agents_text)
check("AGENTS.md carries the job block", "Job rule: keep it to one line." in agents_text)
check("AGENTS.md is a real file", not (home / "AGENTS.md").is_symlink())
check("sessions is the real one", (home / "sessions").resolve() == Path(TMP, "sessions").resolve())
servers = json.loads((home / "mcp/servers.json").read_text())["mcpServers"]
check("mcp merged", set(servers) == {"user", "demo"}, servers)
shutil.rmtree(home)
check("overlay removed", not home.exists())

# The scheduler's overlay config is only half the base_url story; the worker has
# to carry it into the provider instance. settings() is the seam.
from handler import config as config_module  # noqa: E402
settings = config_module.settings({
    "active": "ollama",
    "providers": {"ollama": {"model": "x", "base_url": "http://127.0.0.1:11434/v1"}},
})
check("base_url reaches settings", settings.get("base_url") == "http://127.0.0.1:11434/v1", settings)

s.cleanup_overlays()
check("cleanup empties .run", not s.overlay_root().exists() or not any(s.overlay_root().iterdir()))

# `schedule new` writes the text files, so editing a job does not mean editing
# TOML. The old inline form still works; this is the template going forward.
from types import SimpleNamespace  # noqa: E402
s.cmd_new(SimpleNamespace(name="fresh"))
fresh = Path(TMP, "scheduled/fresh")
check("new writes PROMPT.md", (fresh / "PROMPT.md").is_file())
check("new writes SYSTEM.md", (fresh / "SYSTEM.md").is_file())
check("new points at the prompt file", 'prompt_file = "PROMPT.md"' in (fresh / "job.toml").read_text())
jobs, errors = s.discover_jobs()
fresh_job = next(j for j in jobs if j["name"] == "fresh")
check("fresh prompt is read", "What should this agent do?" in fresh_job["prompt"])
check("fresh system is found", fresh_job["system_name"] == "SYSTEM.md")
check("fresh is disabled", fresh_job["enabled"] is False)

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all ok")
sys.exit(1 if FAILS else 0)
