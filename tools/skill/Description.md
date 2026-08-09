---
name: skill
output:
  name: str
  instructions: str
  files: list
active: True
require_permissions: False
---
Loads a skill: instructions for doing one kind of job properly, written down
ahead of time.

The list below is all you have — a name and a line about when it applies. The
actual instructions arrive when you load one. Load it *before* starting the
work, not after you are stuck: a skill exists because that job has a right way
to do it that is not obvious from the outside.

One call per skill. Loading one you do not need costs context for nothing, and
guessing at a job a skill covers usually costs more than that.

If a skill lists files, they are on disk next to it — read the ones it tells
you to read with the file tool. Do not read them all.
