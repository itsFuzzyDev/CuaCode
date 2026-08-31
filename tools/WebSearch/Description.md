---
name: WebSearch
output:
  results: list
active: True
require_permissions: True
backgroundable: True
---
Search the web for a query and return a list of ranked results, each with a
title, url, and snippet.

Use it when you need to find pages on a topic, then hand a result's url to
WebFetch to actually read the page. This tool only searches - it does not fetch
or summarize pages. Results are capped at `max_results`; ask for what you need,
not the whole internet.
