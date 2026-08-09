---
name: tasks
output:
  tasks: list
  task: object
active: True
---
Manage a persistent task list. Actions: add, done, list, delete, clear.
Tasks persist across sessions via SQLite. Each task has an id, description,
done status, and creation timestamp.
