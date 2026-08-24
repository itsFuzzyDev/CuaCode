---
name: agent
output:
  output: object | str
  rounds: int
  stopped: str
active: True
require_permissions: False
backgroundable: True
---
Hands a job to a subagent and returns only what it found.

The subagent starts cold. It sees your prompt and nothing else - not this
conversation, not what you already tried, not what the user said. Whatever it
needs has to be in the prompt. Its own context is thrown away when it
finishes, so a search that reads twenty pages costs you the summary, not the
twenty pages.

Use it when the work is bounded and the answer is small: read this, find that,
check whether X is true across these files. Do not use it for anything
interactive, anything you need to watch, or a step you could do in one call
yourself - a subagent is a whole extra model run, so it pays off when it saves
more context than it costs.

It cannot ask you anything and it cannot ask the user anything. An
underspecified prompt comes back as a confident wrong answer, not a question.
Say what "done" means.
