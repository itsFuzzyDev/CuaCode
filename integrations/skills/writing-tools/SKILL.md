---
name: writing-tools
description: Add a new tool to CuaCode. Use only when working on the CuaCode repo itself — a tool is Python that runs in the worker, unlike subagents, workflows and skills, which live in ~/.cuacode.
---
A tool is a folder in `tools/` with exactly three files. It is picked up on the
next worker start — there is no registry to edit.

This one changes the app itself. If the user wants a reusable *job*, they want
a subagent or a workflow, which need no code and take effect immediately. Only
write a tool when the agent needs a capability it does not have.

```
tools/<name>/
    Description.md      frontmatter + what the model reads
    InputSchema.json    arguments
    main.py             run(args, ctx) -> dict
```

## Description.md

```markdown
---
name: mytool
output:
  status: str
  count: int
active: True
require_permissions: False
---
What it does, when to use it, and what it will not do. This is read by the
model on every single turn, so it is worth writing tightly.
```

- `output` — a sketch of the return shape, not a validated schema. One real
  use: any value mentioning `base64` marks the tool as image-returning, and it
  is withheld from models that cannot see.
- `active` — `False` keeps it loaded but unoffered. Right for a stub.
- `require_permissions` — `True` makes the frontend ask the user before every
  call. Set it for anything that changes the machine, spends money, or reaches
  the network.

## InputSchema.json

```json
{
    "properties": {
        "path": {"type": "string", "description": "What this is, in the model's terms."}
    },
    "required": ["path"]
}
```

Arguments are validated against this before `run` is called, so a missing or
mistyped field never reaches the handler. Supported: `type`, `properties`,
`required`, `enum`, `items`, `default`, `minItems`/`maxItems`. Defaults are
*not* filled in — the handler's own `args.get(k, fallback)` stays authoritative.

Every `description` is an instruction to the model. Write them that way.

## main.py

```python
def run(args: dict, ctx) -> dict:
    return {"status": "ok", "count": 1}
```

- Return a dict. An exception is caught and returned as `{"error": ...}`, but a
  handled failure with a sentence in it is far more useful than a traceback.
- `ctx` carries the frontend's terminal info: `ctx.cwd`, `ctx.self_identity`
  (the frontmost app), `ctx.session_dir`. It may be empty. Never require it.
- Keep imports that are slow or optional inside the function. Every `main.py`
  is executed when the registry loads, so a top-level import of something
  heavy is paid by every launch.
- Big results are a problem twice over: they go into the model's context *and*
  across the wire to the frontend. If a tool can return something large, cap it
  and say so in the result, and add a short-form branch for it in `main.py`'s
  `tool_output` handling.

### Runtime description and schema

If what the tool offers depends on the machine — which subagents are installed,
which workflows exist — define either hook in `main.py`:

```python
def describe(body: str) -> str: ...   # body is Description.md's text
def schema() -> dict: ...             # replaces InputSchema.json
```

Both are re-read every turn, so something written mid-conversation shows up
without a restart. `tools/agent/main.py` is the worked example. Keep them cheap
and never let them raise.

## Check it

```bash
python3 -c "
from tools.loader import load_tools, dispatch
reg = load_tools('tools')
print(dispatch(reg, '<name>', {...}))"
```

Then check the frontend renders it: `go/frontends/deck/calls.go` formats
arguments and results per tool, and a tool with no case there falls back to
`key=value`, which is fine but rarely reads well.
