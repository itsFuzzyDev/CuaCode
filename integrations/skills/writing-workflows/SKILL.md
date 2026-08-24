---
name: writing-workflows
description: Write a workflow - a script that runs several subagents in a fixed order, concurrently. Use when a job is the same shape every time and one agent call is not enough.
---
A workflow is a Python file with a `run(args)`. Write it to
`~/.cuacode/workflows/<name>.py` with the file tool; it is runnable on your
next turn. Never write to the repo's `integrations/workflows/`.

Reach for one when the ordering is worth writing down: the same fan-out, the
same stages, every time. If the job is one agent call, it is one agent call.

## The file

```python
NAME = "audit"
DESCRIPTION = "What it does and the args it wants. This is what the model reads to decide to run it."

def run(args):
    files = args.get("files") or []
    if not files:
        return {"error": "audit needs {files: [str]}"}
    log(f"reviewing {len(files)} file(s)")
    found = pipeline(files, lambda f, _item, i: agent("reviewer", f"Review {f}"))
    return {"issues": [x for r in found if r for x in r.get("issues", [])]}
```

No imports. `agent`, `parallel`, `pipeline`, `log` and `AgentSpec` are already
in scope. Standard Python otherwise.

## The verbs

- `agent(name_or_spec, prompt, **overrides)` - runs a subagent, returns its
  output alone, or `None` if it failed. Overrides are spec fields:
  `effort="high"`, `max_rounds=20`. Pass an `AgentSpec(...)` built inline for
  an agent that only exists inside this workflow.
- `pipeline(items, *stages)` - every item through every stage independently.
  No barrier: item A can be in stage three while B is in stage one. Stages are
  called `(previous_result, original_item, index)`. **This is the default.**
- `parallel(thunks)` - runs zero-argument callables and waits for all of them.
  A barrier. Correct only when a stage genuinely needs every earlier result at
  once: dedup across the whole set, an early exit on a total, a synthesis that
  compares findings to each other.
- `log(message)` - a progress line the user sees.

If you wrote `results = parallel(...)`, then a `flatten`/`map`/`filter`, then
another `parallel`, the barrier is not doing anything: put the transform inside
a pipeline stage instead.

## Rules that bite

- A failed agent is `None` in the results, never an exception. Filter before
  you index: `[r for r in results if r]`.
- Say what you dropped. If you cap at the first ten items, `log` it - a
  silently truncated run reads as a complete one.
- Loops need a real exit condition. `MAX_AGENTS` (60) is a backstop against a
  runaway, not a budget to spend.
- Skip work that has become pointless. One read needs no reconciling; zero
  results need no synthesis stage.
- Concurrency is per call, and nesting `parallel` inside a `pipeline` stage
  multiplies threads. Fine for tens of items, not for thousands.

## Check it

Run it once with real args through the workflow tool. The result carries `log`
and `agents` - if `agents` is far higher than you expected, a loop is not
exiting.

`integrations/workflows/research.py` is a complete worked example; read it with
the file tool.
