"""Workflows: orchestration that is code, not a model's discretion.

An agent deciding to spawn four agents is a judgement call it makes again,
differently, every time. A workflow is the same fan-out written down -- which
agents run, in what order, what is done with their results -- so it runs the
same way twice. That is the whole point of the layer.

The scripts are plain Python modules, because the host is Python. There is no
DSL to learn or sandbox to maintain: a workflow is a file with a run(args)
function, and the orchestration verbs -- agent, parallel, pipeline, log -- are
injected into its namespace before it executes. Anything you can do in Python
you can do in a workflow, which also means a workflow you did not write runs
with your privileges. They come from your own ~/.cuacode/workflows, and they
should stay that way.
"""
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
import threading

from handler.agent import flow
from handler.agent.subagent import AgentSpec, run as run_agent
from handler.session import store

_BUNDLED = Path(__file__).resolve().parents[2] / "integrations" / "workflows"

# A runaway loop in a workflow spends real money, and the loop that does it is
# usually a while() whose exit condition never became true. This is the
# backstop, set far above any workflow anyone means to write.
MAX_AGENTS = 60

@dataclass
class Workflow:
    name: str
    description: str
    path: Path
    fn: Callable

def user_dir() -> Path:
    d = store.home() / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _describe(ns: dict, path: Path) -> str:
    doc = (ns.get("__doc__") or "").strip()
    return ns.get("DESCRIPTION") or (doc.split("\n")[0] if doc else f"workflow from {path.name}")

def _load(path: Path, env: dict) -> Workflow:
    """Execute the file with the orchestration verbs already in scope.

    Injected into globals rather than imported by the script, so a workflow is
    the twelve lines that are actually about the work -- no import block, and
    no way to import a differently-configured copy of the runner.
    """
    ns = {"__name__": f"workflow.{path.stem}", "__file__": str(path), **env}
    code = compile(path.read_text(), str(path), "exec")
    exec(code, ns)
    fn = ns.get("run")
    if not callable(fn): raise RuntimeError(f"{path.name}: no run(args) function")
    return Workflow(name=ns.get("NAME") or path.stem, description=_describe(ns, path),
                    path=path, fn=fn)

def _env(ctx, log_to: list, counter: dict):
    """The verbs a workflow script sees as globals."""
    from integrations.subagents import loader as agents

    lock = threading.Lock()

    def log(message: str):
        line = str(message)
        log_to.append(line)
        emit = ctx.get("emit") if isinstance(ctx, dict) else None
        if callable(emit):
            try: emit("workflow", {"type": "log", "text": line})
            except Exception: pass

    def agent(ref, prompt: str, **over):
        """Run an agent by name (or an AgentSpec) and return its output alone.

        The output, not the wrapper: a workflow composing agents wants the dict
        the agent produced, and a caller that has to unwrap {output, rounds,
        stopped} at every step reads like plumbing. A failure returns None and
        says so in the log, which keeps one dead agent from taking down a
        fan-out that was mostly fine.
        """
        with lock:
            counter["n"] += 1
            if counter["n"] > MAX_AGENTS:
                raise RuntimeError(f"workflow exceeded {MAX_AGENTS} agents -- stopping it")
        spec = ref if isinstance(ref, AgentSpec) else agents.get(ref)
        if over: spec = replace(spec, **over)
        result = run_agent(spec, prompt, ctx=ctx)
        if result.get("error"):
            log(f"agent {spec.name} failed: {result['error']}")
            return None
        return result.get("output")

    return {"agent": agent, "log": log, "parallel": flow.parallel, "pipeline": flow.pipeline,
            "AgentSpec": AgentSpec, "agents": agents}

def _meta(path: Path) -> tuple:
    """(name, description) read without running the file.

    Listing what is installed happens on every turn, and executing a dozen
    scripts to find out what they are called would run their module-level code
    a dozen times an hour for nothing. The parse tree has both fields and no
    side effects.
    """
    import ast
    try: tree = ast.parse(path.read_text(), str(path))
    except SyntaxError: return None
    name, desc = path.stem, ""
    for node in tree.body:
        if not isinstance(node, ast.Assign): continue
        for target in node.targets:
            if not isinstance(target, ast.Name): continue
            try: value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError): continue
            if target.id == "NAME" and isinstance(value, str): name = value
            elif target.id == "DESCRIPTION" and isinstance(value, str): desc = value
    if not desc:
        doc = ast.get_docstring(tree) or ""
        desc = doc.strip().split("\n")[0] if doc else f"workflow from {path.name}"
    return name, desc

def _scan() -> dict:
    """{name: path} for every installed workflow. User directory second, so a
    name written there shadows a bundled one."""
    out = {}
    for d in (_BUNDLED, user_dir()):
        if not d.exists(): continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"): continue
            meta = _meta(f)
            if meta: out[meta[0]] = f
    return out

def listing() -> list:
    """(name, description) for every installed workflow, for a tool that has to
    describe them to a model."""
    return [_meta(p) for p in _scan().values()]

def run(name: str, args=None, ctx=None) -> dict:
    """Run one workflow to completion. Returns {output, log, agents}.

    Only the workflow being run is executed. The verbs it closes over carry
    this run's ctx and agent counter, which is also why nothing here is cached
    -- a reused function would still be logging into the previous run's list.
    """
    sink, counter = [], {"n": 0}
    env = _env(ctx, sink, counter)
    path = _scan().get(name)
    if path is None:
        return {"error": f"unknown workflow: {name!r} (have {sorted(_scan()) or 'none installed'})"}
    try: found = _load(path, env)
    except Exception as e:
        return {"error": f"{path.name} failed to load: {e}"}
    try:
        out = found.fn(args if args is not None else {})
    except Exception as e:
        return {"error": f"{name} failed: {e}", "log": sink, "agents": counter["n"]}
    return {"output": out, "log": sink, "agents": counter["n"]}
