---
name: screenshot
output:
  image_base64: base64,
  width: int,
  height: int
active: True
require_permissions: False
---
Takes a screenshot with a labeled pixel-coordinate grid overlaid.
Grid lines are labeled with their (x,y) logical pixel position - use
these labels to determine precise coordinates for click/type actions.
Coordinates are in logical (not retina/physical) pixels, the same unit
click and mouse_move take. Captures every monitor as one image; the
labels are desktop coordinates, so they aim at any screen.

Pass region=[x, y] to zoom into that point before clicking a small
target: the tool crops around it and magnifies by `zoom` (default 2x)
with a finer grid. Grid labels remain true screen coordinates, so read
them straight off the zoomed image for click/mouse_move. Recommended
flow for small targets: full screenshot -> pick rough spot -> zoomed
screenshot at that spot -> click exact coordinate.
