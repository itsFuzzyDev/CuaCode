---
name: todo
output:
  todo: list
active: True
---
Your plan for the task in front of you, and where you are in it. One list per
conversation; it survives a reload and never leaks into another chat.

Use it for anything that takes more than about three steps, anything you will be
in the middle of when the screen changes under you, and anything the user gave
you as a list. Do not use it for a single action -- a plan with one step in it is
noise.

How it is meant to go:

1. `plan` with every step you can see, before doing any of them. Writing the
   steps out is the point of the tool: it is where you notice the ordering, the
   step that depends on something you have not checked, and the two steps that
   are really one. Short imperative lines -- "read handler/context.py", not "I
   will now investigate the context module".
2. `start` the step you are about to do. Exactly one is in progress at a time;
   starting another puts the previous one back to pending and says so.
3. `done` it the moment it is actually done, not in a batch at the end. The
   result hands you the next step, so there is no reason to guess at it.
4. `note` on a step when it turned something up that a later step needs -- a
   path, a number, a decision. The list is what you will re-read in twenty
   rounds, and by then "check the config" will not remind you of anything.
5. `add` when the work reveals steps the plan missed. `drop` with a reason when
   a step turns out to be unnecessary -- that is not the same as done, and a
   dropped step the user disagrees with is worth them seeing.

Every action returns the whole list, so the current state of the plan is always
in your most recent tool result. `list` exists for when it is not -- after a long
detour, before you tell the user you have finished, or when the runtime tells you
the list has gone untouched. Check it before claiming the task is complete:
"all done" while two steps are still open is the failure this tool exists to
prevent.

Keep the list honest. It is the user's view of what you are doing, not a
performance -- do not fill it with steps you have already finished, and do not
leave it describing work you abandoned.
