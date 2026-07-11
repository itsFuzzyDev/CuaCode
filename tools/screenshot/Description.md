---
name: screenshot
input:
  grid_size: int = 100
output:
  image_base64: base64,
  width: int,
  height: int
active: True
---
Takes a screenshot with a labeled pixel-coordinate grid overlaid.
Grid lines are labeled with their (x,y) logical pixel position — use
these labels to determine precise coordinates for click/type actions.
Coordinates are in logical (not retina/physical) pixels.