---
name: click
output:
  clicked_at: [int, int]
  button: str
  clicks: int
active: True
---
Clicks at the given (x, y) screen coordinate. Coordinates should match
the logical pixel grid shown in the most recent screenshot. Set
clicks to 2 for a double-click.