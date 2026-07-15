---
name: file
output:
  content: string
  matches: list
active: True
require_permissions: True
---
Read, write, edit, and search text files. Actions: read, write, edit, glob,
grep, mkdir.
read returns line-numbered content, optionally a start-end line range (50k
char cap, truncated flag). write replaces the whole file, creating it and
parent directories if needed. edit applies exact old->new string replacements
in order; each old string must be unique in the file unless all is set, and
the file must have been read (or written) first this session. glob finds
files by pattern (e.g. **/*.py) under a base directory. grep regex-searches a
file or recursively a directory (optional include file filter), returning
file/line/text matches. mkdir creates a directory including parents. Paths
support ~ expansion.
