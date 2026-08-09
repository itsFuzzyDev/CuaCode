---
name: file
output:
  content: string
  matches: list
active: True
require_permissions: True
---
Read, write, edit, search and manage text files. Actions: read, write, edit,
glob, grep, ls, mkdir, move, delete, undo. Paths support ~ and are resolved
against the session's working directory.
read returns line-numbered content, optionally a start-end line range. Long
files come back truncated with next_start to continue from.
write replaces the whole file, creating it and parent directories if needed.
An existing file must have been read first this session.
edit applies exact old->new string replacements in order; each old string must
be unique in the file unless all is set, and the file must have been read (or
written) first this session. If one edit fails none are applied, and a miss
caused by whitespace comes back as did_you_mean with the file's own text for
that block.
Both return a unified diff of what changed, and a syntax_error for a Python,
JSON, YAML or TOML file left unparseable. A file changed on disk since it was
read must be read again before it can be written or edited.
glob finds files by pattern (e.g. **/*.py) under a base directory, newest
first. grep regex-searches a file or recursively a directory, optionally
case-insensitively, with context lines, an include file filter, or files_only
for paths alone. Both skip .git, node_modules, build output and the like unless
no_ignore is set.
ls lists a directory. mkdir creates one including parents. move renames a file
to `to`, which must not already exist. delete removes a file, to the Trash
where there is one, and only removes a directory that is empty.
undo rolls back the last write, edit, move or delete this tool made to that
path, one step per call.
