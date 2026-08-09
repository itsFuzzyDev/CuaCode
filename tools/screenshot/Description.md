---
name: screenshot
input:
  grid_size: int = 100
  region: "list[int] = None"
  zoom: float = 2
output:
  image_base64: base64,
  width: int,
  height: int
active: True
require_permissions: False
---
Takes a screenshot with a labeled pixel-coordinate grid overlaid.
Grid lines are labeled with their (x,y) logical pixel position — use
these labels to determine precise coordinates for click/type actions.
Coordinates are in logical (not retina/physical) pixels.

Pass region=[x, y] to zoom into that point before clicking a small
target: the tool crops around it and magnifies by `zoom` (default 2x)
with a finer grid. Grid labels remain true screen coordinates, so read
them straight off the zoomed image for click/mouse_move. Recommended
flow for small targets: full screenshot -> pick rough spot -> zoomed
screenshot at that spot -> click exact coordinate.
