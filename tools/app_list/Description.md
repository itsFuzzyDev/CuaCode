---
name: app_list
output:
  running: [str]
  installed: [str]
active: True
require_permissions: False
---
Lists applications on the machine: currently running/visible apps,
and all installed applications. Use this to check an app's exact
name before calling app_open.