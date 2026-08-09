# This is more or less an example workflow, this workflow works perfectly, but is more of a 'test' workflow or an example workflow.
"""Answer one question from several pages at once, then reconcile the answers.

Also the reference for what a workflow file looks like. There is no import
block: agent, parallel, pipeline, log and AgentSpec are already in scope, and
run(args) is the entry point.
"""
NAME = "research"
DESCRIPTION = ("Reads several URLs concurrently to answer one question, then reconciles "
               "what they said into a single answer. args: {question: str, urls: [str]}")

RECONCILE = AgentSpec(
    name="reconcile", tools=[], effort="medium", max_rounds=3,
    system="""You are handed several independent reads of the same question and you
produce the one answer.

You cannot fetch anything. The reads are all you have.

- Where they agree, say it once.
- Where they disagree, say so explicitly and name what each claimed. Do not
  average them and do not quietly pick the one that sounds better.
- A read marked not-confident is a lead, not a fact.
- If none of them actually answered the question, say that. It is a useful
  result and a fabricated answer is not.""",
    schema={"properties": {
        "answer": {"type": "string", "description": "The reconciled answer, written for someone about to act on it."},
        "disagreements": {"type": "array", "items": {"type": "string"},
                          "description": "Each point the sources conflicted on, and what each said."},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs the answer actually rests on."},
        "confident": {"type": "boolean", "description": "False if the sources conflicted or none of them answered."}},
     "required": ["answer", "confident"]})

def run(args):
    question = (args.get("question") or "").strip()
    urls = args.get("urls") or []
    if not question or not urls:
        return {"error": "research needs {question: str, urls: [str]}"}

    log(f"reading {len(urls)} page(s) for: {question}")
    # pipeline, not parallel: one stage here, but each url is independent and
    # nothing waits on the slowest read to start working on its own result.
    reads = pipeline(urls, lambda url, _item, i: agent(
        "researcher",
        f"Question: {question}\n\nStart from this page: {url}\n"
        f"Follow a link only if that page points somewhere clearly better."))

    got = [r for r in reads if r]
    log(f"{len(got)}/{len(urls)} page(s) came back")
    if not got:
        return {"error": "every read failed", "urls": urls}
    if len(got) == 1:
        # Nothing to reconcile against. Paying for a second model call to
        # rephrase one answer is the kind of thing a workflow should not do.
        return {**got[0], "sources": got[0].get("sources") or urls}

    bundle = "\n\n".join(
        f"<read source=\"{urls[i] if i < len(urls) else '?'}\" confident=\"{r.get('confident')}\">\n"
        f"{r.get('findings', '')}\n</read>" for i, r in enumerate(got))
    out = agent(RECONCILE, f"Question: {question}\n\n{bundle}")
    return out or {"error": "reconcile failed", "reads": got}
