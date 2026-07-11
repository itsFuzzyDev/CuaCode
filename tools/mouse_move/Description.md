---
name: mouse_move
input:
  x: int
  y: int
output:
  moved_to: [int, int]
active: True
---
Moves the mouse cursor to the given (x, y) screen coordinate.
Coordinates should match the logical pixel grid shown in the most
recent screenshot — use the labeled gridlines to pick a target.
Does not click; use the click tool for that.