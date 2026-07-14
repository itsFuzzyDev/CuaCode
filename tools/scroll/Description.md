---
name: scroll
output:
  scrolled_at: [int, int]
  dx: int
  dy: int
active: True
require_permissions: False
---
Scrolls at the given (x, y) position. dy positive scrolls down,
negative scrolls up. dx positive scrolls right, negative scrolls left.

Scroll distance is approximate and varies by app — it does not map
precisely to pixels. Scroll in small steps (e.g. dy=100-200) and take
a screenshot between scrolls to check progress, rather than trying to
scroll the exact right amount in one call.