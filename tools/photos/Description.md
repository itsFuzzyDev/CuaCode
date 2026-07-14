---
name: photos
input:
  sources: list[str]
  max_size: int | null
output:
  images: list[base64]
  count: int
  errors: list[dict]
active: True
require_permissions: False
---
Load photos from URLs or local file paths and attach them to the conversation.
The model can then see and reason about the images directly.
Useful when the user references images they want analyzed, or when screenshots
are insufficient (e.g., looking at a specific photo, document scan, reference image).