---
name: WebFetch
output:
  website: str
  output: str | object
  truncated: bool
active: True
require_permissions: True
backgroundable: True
---
Fetches a single web page and returns its content. Requires an absolute URL
including scheme (https://...). Cannot access pages behind auth or paywalls.

One URL per call. Fetching is expensive - use websearch snippets to decide
which page is worth fetching, then read the result before fetching another.

Modes:
digest -
  Hands the page to a temporary subagent along with your goal. Its context is
  discarded; you never see the raw page. Costs one extra model call, returns a
  fraction of the tokens. Use this first, almost always.
  Comes back as {answer, found, quotes, next_urls}: found is false when the
  page never addressed your goal, quotes are verbatim, and next_urls are links
  the subagent thinks look more promising than this page was. Anything you
  need word-for-word beyond the quotes wants full instead.

full -
  Returns the page as markdown, nav/ads/scripts stripped. Images are replaced
  with [image: alt text] placeholders - this tool returns text only.
  Use when you need the page exactly as written - code samples, config
  snippets, exact numbers, anything you'll copy. Costs the full page in
  tokens. Long pages are truncated with a note; re-fetch in digest mode
  instead of paging through.