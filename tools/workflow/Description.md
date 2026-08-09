---
name: workflow
output:
  output: object
  log: list
  agents: int
active: True
require_permissions: True
backgroundable: True
---
Runs a stored workflow: a script that orchestrates several subagents in a
fixed order.

The difference from calling `agent` a few times is that the ordering is
written down rather than decided. A workflow fans work out, runs stages
concurrently, and combines the results the same way every run. Reach for one
when the user names it, or when the job is the exact shape a stored workflow
already covers.

Workflows are Python files in ~/.cuacode/workflows and they run with the
user's privileges. Do not invent one and do not run one whose purpose you
cannot see from its description.

A workflow can spend many model calls before it returns anything. Run one when
the job is worth that; otherwise call the agents yourself.
