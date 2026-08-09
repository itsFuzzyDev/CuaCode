"""Search the web and hand back ranked results (title, url, snippet).

Skeleton: the search call is real, the rest is the shape to fill in. Keep
imports lazy so launching the worker never pays for ddgs unless this tool is
actually called.
"""

# Keep a couple of results' worth of raw body so a short-form / summary branch
# can decide later without re-searching. This is what caps the model's context.


def _search(query: str, max_results: int) -> list[dict]:
    """Run the query. Extracted so a fake backend can slot in for tests."""
    from ddgs import DDGS

    with DDGS() as ddgs:
        raw = ddgs.text(query, max_results=max_results, region="us-en")

    results = []
    for r in raw:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", "")[:1000],
        })
    return results


def run(args: dict, ctx) -> dict:
    query = (args.get("query") or "").strip()
    if not query: return {"error": "query must be non-empty"}

    max_results = min(int(args.get("max_results", 5)), 35)

    try: results = _search(query, max_results)
    except Exception as exc: return {"error": f"search failed: {exc}"} # if exception then go figure it out or some shit

    if not results:return {"results": [], "note": "no results for query"}

    return {
        "results": results,
        "count": len(results),
        "truncated": len(results) >= 35, # not even required cause agent cant call more than 35 items max but whatever :shrug:
    }
