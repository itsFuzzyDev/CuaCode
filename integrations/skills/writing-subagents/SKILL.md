---
name: writing-subagents
description: Write a new subagent the agent tool can run. Use when the user wants a reusable specialist, or when you keep giving the same long instructions to a delegated job.
---
A subagent is one markdown file. Frontmatter is the configuration, the body is
its system prompt.

Write it to `~/.cuacode/subagents/<name>.md` with the file tool. It is loadable
on your next turn - no restart. Never write to the repo's
`integrations/subagents/`; those ship with the app.

## The file

```markdown
---
name: reviewer
description: One line, written for the agent deciding whether to call this. Say what it does and what it returns.
tools: [file, shell]
effort: low
max_rounds: 8
output:
  properties:
    verdict: {type: string, description: What you concluded, in one sentence.}
    issues:
      type: array
      items: {type: string}
      description: One per problem found. Empty when there are none.
    confident: {type: boolean, description: False when you had to guess.}
  required: [verdict, confident]
---
The system prompt goes here. Write it as instructions to the agent, not as a
description of it.
```

## Fields

- `tools` - names from the tools list. `[]` means none, which is right for
  anything that only has to think about text you hand it. `"*"` means all.
  Leaving the key out means none. Give it the fewest that do the job; every
  extra tool is another thing it can waste a round on.
- `effort` - `off`/`low`/`medium`/`high`/`max`. Reading and extracting is
  `low`. Reserve `high` for judgement.
- `max_rounds` - hard stop. It returns `stopped: "max_rounds"` and whatever it
  had. A tool-less agent needs 2-3; a fetching one 8-12.
- `output` - the schema it must fill. Omit it and you get its final text as a
  plain string instead.
- `provider` / `model` - optional, defaults to whatever is active.

## The output schema

This is the part that matters. The agent gets a `submit_result` tool built
from the schema and it is the only way back - everything else it writes is
thrown away. A field it cannot fill honestly is a field it will fill dishonestly,
so:

- Give every field a `description`. It is read as an instruction, not as
  documentation.
- Include a way to say "no": a `found`/`confident` boolean, or an array that is
  allowed to be empty. Without one, a schema demanding an answer gets one
  invented.
- `required` only for fields that always have a truthful value.
- Supported: `type` (string/number/integer/boolean/object/array/null),
  `properties`, `required`, `enum`, `items`, `default`, `minItems`/`maxItems`.
  Nothing else is checked.

## Writing the prompt

The subagent starts cold. No conversation, no screen, no user, no way to ask a
question. Whatever it needs is in the prompt the caller sends and in this
system prompt.

Write what "done" means and what to do when the material does not support an
answer. Most bad subagents are bad because they were never told they were
allowed to come back empty.

## Check it

Run it once through the agent tool on a real input before telling the user it
works. Look at `stopped`: `submitted` is good, `max_rounds` means the budget
is too low, `no_calls` means the prompt is fighting the schema.
