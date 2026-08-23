---
name: writing-skills
description: Write a new skill — instructions, and optionally scripts and templates, for a job that has a right way to do it. Use when the user teaches you a procedure worth keeping, or when you keep re-deriving the same steps.
---
A skill is a folder with a `SKILL.md` in it, plus whatever files that skill
needs. Write it to `~/.cuacode/skills/<name>/` with the file tool. It is
loadable on your next turn. Never write to the repo's `integrations/skills/`.

The economics are the whole design: only the name and description sit in
context all the time. The body is loaded when it is needed, and the files
next to it are read only if the body says to. So the description is the part
that has to be right — it is all a future you gets to decide on.

## SKILL.md

```markdown
---
name: deploying-web
description: One line. When does this apply? Written for an agent deciding whether to load it, not for a human browsing a list.
---
The instructions. Imperative, addressed to the agent doing the work.
```

A description that says "helps with deployment" never gets loaded at the right
moment. One that says "Use before pushing to production; covers the staging
gate and the rollback command" does.

## Who may load it

Two things can: you, through the skill tool, and the user, by typing `/<name>`
— the palette lists every skill under the built-in commands, and the body is
attached to the message they send. Both are allowed unless the frontmatter says
otherwise:

```markdown
disable-model-invocation: true   # the user's to invoke; never offered to you
disable-user-invocation: true    # yours to reach for; never in the palette
```

A third flag says the skill is not loaded at all — it is simply on:

```markdown
always: true                     # body goes into the system prompt at startup
```

Use it only for a rule that has to be in force before the first decision of a
turn: a house style, a safety rule, a format every answer takes. It is charged
on every single request for the rest of time, and it is dropped from both the
tool list and the palette because it is already in the prompt. Everything that
is only right *sometimes* stays on demand — that is the whole economy. The user
can also force a skill on without editing it, by naming it in `always_skills` in
`~/.cuacode/config.json`.

Write neither of the invocation flags unless there is a reason. Model-only fits a skill that is only
ever right in the middle of a job the user is not thinking about; user-only
fits one that is a command in all but name — a report, a mode, a format the
user asks for by name. Setting both leaves a skill nothing can load, and the
loader drops such a folder rather than listing it.

## Files next to it

Anything else in the folder is listed when the skill is loaded, but not read.
Point at what you want read, and say when:

```markdown
The rollback runbook is in `rollback.md` — read it only if the deploy fails.
`check.py` verifies the staging gate: run it with
`python3 <dir>/check.py --env staging`.
```

The skill tool returns the folder's absolute path as `dir`, so shell commands
and file reads can be written against it. Use it rather than assuming where the
skill lives.

What belongs in a file rather than in the body:
- Long reference material — a table of error codes, an API surface. The body
  says when to consult it.
- Templates to copy.
- Scripts. A deterministic 40-line script beats 40 lines of instructions
  telling an agent to be careful, every time.

Keep the body itself short. It is loaded whole; if it is over a few hundred
lines, most of it should be a file it links to instead.

## Writing the body

- Instructions, not description. "Run the tests before committing", not "this
  skill is about testing".
- Say what goes wrong. The reason a skill exists is usually a mistake someone
  made once — write down the mistake.
- Do not restate what the tools already say. Skills are for procedure and
  judgement, not for documenting the file tool.
- Say when *not* to apply it. A skill that claims every job costs a load every
  time it is wrong.

## Check it

Load it with the skill tool and read what came back. If the body does not tell
you what to do first, it is not finished.
