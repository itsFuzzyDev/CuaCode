---
name: app_open
input:
  app: str
output:
  ok: bool
  app: str
  snapped: bool
  self_snapped: bool
active: True
---
Opens an application by name (or path on macOS), waits up to 5s for
its window to appear, then arranges the screen: your own
window (the terminal running this agent) gets snapped to the left
30%, and the newly opened app gets snapped to the right 70%.
Use this instead of manually navigating a dock/taskbar/launcher when
you already know the app's name.