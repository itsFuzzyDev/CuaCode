"""The call-level permission screen for auto mode.

When permission mode is auto, a tool call that would normally prompt the user
is handed to a small model first, which returns a strict allow:true/false
verdict plus one line of reason. A yes runs. A no is not a verdict -- the call
goes to the user through the ordinary prompt, reason attached, and they
decide; only where nobody can be asked does the refusal stand. The screen is
a gate, not a peer: no tools of its own, low effort, one structured answer --
the same shape as the auto-namer, so it inherits that proven path rather than
inventing a second agent loop.

Fails closed in every direction. No verdict, an unparseable one, an
exception, a provider that cannot be reached -- all of them refuse, which
means the person sees the call and decides. Where nobody can be asked, the
refusal stands, because the cost of a needless prompt is one less tool call
and the cost of a missed one is whatever the call did.
"""
import json
from handler.agent.subagent import AgentSpec, run as run_agent
from handler import config


SYSTEM = """You are a safety screen. Decide whether one tool call may run without a
person looking at it.

You are given the tool, its exact arguments, what the call says it would do,
the line or two of conversation around it, and the working directory.

Return allow: true only when running it is clearly safe. Refuse when it could:
destroy or overwrite data, write anywhere outside the working directory or the
session scratch, send anything (email, message, post), delete files, move or
read credentials, or change system or account settings. Refuse when unsure.

A refusal is not the end of the call -- a person is shown it, with your reason
beside it, and they decide. A false alarm costs them a prompt; something
dangerous let through costs what the call did. When in doubt, refuse.

reason is one short sentence for that person: the call and why it was refused."""

SCHEMA = {"type": "object",
          "properties": {
              "allow": {"type": "boolean",
                        "description": "true only if the call is clearly safe"},
              "reason": {"type": "string",
                         "description": "one short sentence, for the user"}},
          "required": ["allow", "reason"]}


def _short(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "..."


def _rules():
    """The user's configured allow/deny/sensitive lists, for the prompt."""
    from tools._safety import rules
    r = rules.rules()
    return {k: v for k, v in r.items() if v}


def decide(ctx, tool, name, args, preview, tail):
    """(allow, reason) for one call. Refuses on anything that goes wrong.

    Called by generate() with everything the gate needs already in scope -- the
    tool's own description, the exact arguments, what the call says it would
    do, and the conversation tail the decision should be anchored to.
    """
    try:
        provider, model = config.permission_decider()
        lines = [
            "Decide this tool call.",
            "",
            "Tool: %s" % name,
            "Description: %s" % getattr(tool, "description", ""),
            "Arguments: %s" % _short(json.dumps(args or {}, default=str), 1500),
        ]
        if preview:
            summary = preview.get("summary")
            diff = preview.get("diff")
            if summary:
                lines.append("It would: %s" % _short(summary, 600))
            if diff:
                lines.append("Change:\n%s" % _short(diff, 1500))
        lines.append("Working directory: %s" % (ctx.get("cwd") or ""))
        if tail:
            lines.append("Context:\n%s" % tail)
        if r := _rules():
            lines.append("User safety rules: %s" % _short(json.dumps(r, default=str), 800))
        lines.append("")
        lines.append("allow: true or false, with a reason.")
        prompt = "\n".join(lines)

        r = run_agent(AgentSpec(name="decider", tools=[], effort="low", max_rounds=2,
                                system=SYSTEM, schema=SCHEMA,
                                provider=provider or None, model=model or None),
                      prompt, ctx=ctx)
        if r.get("error"):
            return False, "permission gate failed (%s)" % r["error"]
        got = r.get("output") or {}
        allow = bool(got.get("allow"))
        reason = str(got.get("reason") or ("allowed" if allow else "denied"))
        return allow, _short(reason, 200)
    except Exception as e:
        return False, "permission gate failed: %s" % e
