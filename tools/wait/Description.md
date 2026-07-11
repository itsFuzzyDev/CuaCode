---
name: wait
output:
  waited: number
active: True
---
Pauses for the given number of seconds (max 10). Use this to let a
UI settle after an action — e.g. after app_open, before a page
finishes loading, or after an animation.