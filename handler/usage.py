"""What every conversation has cost, added up.

Two halves, and the split is the whole design. A round's cost is stamped onto
the assistant record it belongs to, because that is the only place it is
unambiguous -- one round, one model, one set of counts, and it travels with the
conversation when the conversation is moved or reopened. Then commit() rolls the
records up into meta.json, so a question about a hundred sessions reads a
hundred small json files rather than a hundred transcripts full of screenshots.

That rollup is what makes `--usage` answerable at all. A listing that had to
open messages.jsonl would be reading megabytes of base64 to count tokens that
were already counted months ago.

Nothing here estimates anything. Every figure comes from what a provider
actually reported for a round; a provider that reports no usage contributes
nothing, and the counts say so rather than filling the gap with a guess. The one
exception is marked wherever it is shown: thinking tokens are estimated from the
text on providers that bill them without itemising them.
"""
import json
from collections import defaultdict

from handler.session import store

# What a stamped round counts. Short keys, because they are written once per
# assistant record and a session can hold hundreds of them.
COUNTS = ("in", "out", "think", "secs")


def stamp(fields: dict, provider: str, model: str) -> dict:
    """One round's cost, in the shape it is stored in.

    Empty when the provider reported nothing, and an empty dict is not written
    at all -- a record with no usage means "not measured", which is a different
    thing from a round that cost zero.
    """
    out = {}
    for src, dst in (("in_tokens", "in"), ("out_tokens", "out"), ("thinking_tokens", "think")):
        if n := fields.get(src): out[dst] = n
    if not out: return {}
    if secs := fields.get("gen_secs"): out["secs"] = round(secs, 3)
    if fields.get("thinking_est"): out["est"] = True
    if model: out["model"] = model
    if provider: out["provider"] = provider
    return out


def _add(into: dict, u: dict):
    """Fold one stamped round into a bucket."""
    for k in COUNTS:
        if n := u.get(k): into[k] = round(into.get(k, 0) + n, 3)
    into["rounds"] = into.get("rounds", 0) + 1
    # The largest single prompt, kept beside the running total because the two
    # answer different questions and the total answers neither on its own: a
    # prompt is re-sent in full every round, so a 10k conversation that took two
    # rounds was charged 20k of input while never holding more than 10k. Summing
    # is what the provider bills; the peak is how big the thing actually got.
    into["peak"] = max(into.get("peak", 0), u.get("in") or 0)
    # One estimated round makes the bucket's thinking figure an estimate. It
    # travels with the number so the tilde is still there at the far end.
    if u.get("est"): into["est"] = True


def of_records(records: list[dict]) -> dict:
    """A session's own total, rebuilt from its records.

    Recomputed on every commit rather than incremented: a rewound round takes
    its cost with it when it goes, and a counter that only ever went up would
    keep charging for turns the conversation no longer contains.
    """
    total, models, days = {}, defaultdict(dict), defaultdict(dict)
    for rec in records:
        if rec.get("t") != "assistant": continue
        u = rec.get("u") or {}
        if not u: continue
        _add(total, u)
        key = " ".join(x for x in (u.get("provider", ""), u.get("model", "")) if x) or "unknown"
        _add(models[key], u)
        # The date the round happened, not the date the session was last
        # touched: a conversation carried across three days spent its tokens on
        # three days, and a rollup that filed them all under the last one would
        # make every long session look like a spike.
        if ts := rec.get("ts"): _add(days[ts[:10]], u)
    if not total: return {}
    return {**total, "models": dict(models), "days": dict(days)}


def _blank() -> dict:
    return {"in": 0, "out": 0, "think": 0, "secs": 0.0, "rounds": 0, "peak": 0}


def rollup(days: int = 0, metas: list[dict] = None) -> dict:
    """Every session's total, added together.

    days limits it to the last N days by the date the tokens were actually
    spent, which is why the per-day buckets exist: filtering whole sessions by
    their last-touched date would drop the older half of a conversation that is
    still going.
    """
    metas = store.list_sessions() if metas is None else metas
    cutoff = _cutoff(days)

    total, models, per_day, sessions = _blank(), defaultdict(_blank), {}, []
    counted, unmeasured = 0, 0
    for meta in metas:
        u = _repaired(meta)
        if not u:
            # A session from before this existed, or one whose provider never
            # reported usage. Counted separately rather than silently: "nothing
            # was measured here" is information, and a total that quietly
            # omitted it would look like the sessions cost nothing.
            unmeasured += 1
            continue
        kept, kept_days = _blank(), (u.get("days") or {})
        if cutoff:
            for day, spent in kept_days.items():
                if day >= cutoff: _merge(kept, spent)
        else:
            _merge(kept, u)
        if not kept["rounds"]:
            continue

        counted += 1
        _merge(total, kept)
        # Per-model and per-day are stamped per round but kept as two separate
        # breakdowns -- the crossing lives in the records, and a rollup is not
        # worth reopening transcripts. A day filter counts a session only when
        # all of it falls inside; half a session overstates its model.
        whole = kept["rounds"] == u.get("rounds")
        for name, spent in (u.get("models") or {}).items():
            if not cutoff or whole: _merge(models[name], spent)
        for day, spent in kept_days.items():
            if cutoff and day < cutoff: continue
            _merge(per_day.setdefault(day, _blank()), spent)
        sessions.append({"id": meta.get("id", ""), "title": meta.get("title", ""),
                         "updated": meta.get("updated", ""), **kept})

    # Ranked by context, not by billed volume: the sum of re-sent prompts is what
    # a provider charged, and it reads as "how much I used" when it is really
    # "how many times the same window went up". The peak is how big the
    # conversation actually got, which is the figure worth putting first.
    sessions.sort(key=lambda s: s.get("peak") or 0, reverse=True)
    return {
        "sessions": counted, "unmeasured": unmeasured, "since": cutoff or _first_day(per_day),
        "total": total,
        "models": [{"name": k, **v} for k, v in sorted(models.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))],
        "days": [{"day": d, **v} for d, v in sorted(per_day.items())],
        "top": sessions[:8],
    }


def _repaired(meta: dict) -> dict:
    """A session's rollup, brought up to date if it predates part of the shape.

    A rollup written before the peak was recorded has no peak, and no amount of
    adding those rollups together will produce one -- the number only exists in
    the records. So a stale meta is recomputed from its own transcript once and
    written back, and every rollup after that reads the meta like any other.
    Bounded work: it happens to the sessions that predate the field, once each,
    and never to a session that has been committed since.
    """
    u = meta.get("usage") or {}
    if not u or not u.get("rounds") or u.get("peak"): return u
    sid = meta.get("id") or ""
    try: d = store.path(sid)
    except ValueError: return u
    records = store.read_jsonl(d / "messages.jsonl")
    if not records: return u
    fresh = of_records(records)
    # Only when the recompute agrees about what it is recomputing. A transcript
    # that has been moved or trimmed under its meta is left alone rather than
    # quietly rewritten to disagree with the number the session reported.
    if fresh.get("rounds") != u.get("rounds") or not fresh.get("peak"): return u
    store.write_json(d / "meta.json", {**meta, "usage": fresh})
    return fresh


def _merge(into: dict, u: dict):
    for k in ("in", "out", "think", "rounds"):
        into[k] = into.get(k, 0) + (u.get(k) or 0)
    into["secs"] = round(into.get("secs", 0.0) + (u.get("secs") or 0), 3)
    into["peak"] = max(into.get("peak", 0), u.get("peak") or 0)
    if u.get("est"): into["est"] = True


def _cutoff(days: int) -> str:
    if not days or days <= 0: return ""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days - 1)).isoformat()


def _first_day(per_day: dict) -> str:
    return min(per_day) if per_day else ""


def tps(u: dict) -> float:
    secs = u.get("secs") or 0
    return round((u.get("out") or 0) / secs, 1) if secs >= 1 else 0.0


# ---------------------------------------------------------------------------
# the terminal report


def _n(v: int) -> str:
    if v >= 1_000_000: return f"{v / 1e6:.2f}M"
    if v >= 10_000: return f"{v // 1000}k"
    if v >= 1_000: return f"{v / 1000:.1f}k"
    return str(v)


def _dur(secs: float) -> str:
    s = int(secs)
    if s >= 3600: return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60: return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def render(rep: dict) -> str:
    total = rep["total"]
    head = f"usage · {rep['sessions']} session" + ("" if rep["sessions"] == 1 else "s")
    if rep.get("since"): head += f" · since {rep['since']}"
    if rep.get("unmeasured"): head += f" · {rep['unmeasured']} with nothing measured"

    est = "~" if total.get("est") else ""
    # The peak is only known for rounds recorded since it was; a session rolled
    # up before that says nothing rather than claiming zero.
    peak = _n(total["peak"]) if total.get("peak") else "\u2013"
    # "context" comes first because it is the number that means anything: the
    # largest window a session ever held. "billed input" is what the provider
    # charged -- the same window re-sent every round -- and a sum that dwarfs
    # the peak invites the same misreading every time it is the headline.
    lines = [head, "",
             f"  {'context':<13} {peak:>10}  largest single window held",
             f"  {'billed input':<13} {_n(total['in']):>10}  re-sent every round",
             f"  {'tokens out':<13} {_n(total['out']):>10}  of which thinking  {est}{_n(total['think'])}",
             f"  {'rounds':<13} {total['rounds']:>10}",
             f"  {'generating':<13} {_dur(total['secs']):>10}  {tps(total)} tok/s average"]

    if rep["models"]:
        lines += ["", "by model"]
        width = max(len(m["name"]) for m in rep["models"])
        for m in rep["models"]:
            rounds = f"{m['rounds']} round" + ("" if m["rounds"] == 1 else "s")
            think = ("~" if m.get("est") else "") + _n(m["think"])
            lines.append(f"  {m['name']:<{width}}   in {_n(m['in']):>7}   out {_n(m['out']):>7}   "
                         f"think {think:>7}   {tps(m):>6} tok/s   {rounds}")

    if rep["days"]:
        recent = rep["days"][-14:]
        peak = max(d["in"] + d["out"] for d in recent) or 1
        lines += ["", "by day"]
        for d in recent:
            spent = d["in"] + d["out"]
            lines.append(f"  {d['day']}  {'█' * max(round(spent / peak * 24), 1):<24}  {_n(spent)}")

    if rep["top"]:
        # Context, rounds and billed volume together. Ranked by context (the
        # peak) because that is how big each conversation actually got; the
        # billed column is every round's prompt added up, and the prompt goes up
        # again in full each round, so a conversation that never held more than
        # 25k can have been charged 472k over 24 of them.
        lines += ["", f"  {'largest context':<21}  {'':<40}  {'billed':>7}  {'rounds':>6}  {'peak':>7}"]
        for s in rep["top"]:
            # A session is named a turn or two in; one showing nothing but its
            # id is telling the truth about itself.
            title = (s["title"] or "")[:40]
            # Unknown for sessions rolled up before the peak was recorded, and an
            # unknown peak says nothing rather than claiming zero.
            peak = _n(s["peak"]) if s.get("peak") else "-"
            lines.append(f"  {s['id']}  {title:<40}  {_n(s['in'] + s['out']):>7}  "
                         f"{s['rounds']:>6}  {peak:>7}")

    return "\n".join(lines)


def cli(argv: list[str]) -> int:
    """`main.py --usage [--days N] [--json]`, printed and gone.

    A flag on the worker rather than a tool or a slash command, because the
    question it answers -- what has all of this cost me -- is one you ask about
    the app rather than inside a conversation with it, and starting a session to
    find out would add to the number you were asking about.
    """
    days = 0
    if "--days" in argv:
        try: days = int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("usage: main.py --usage [--days N] [--json]")
            return 2
    rep = rollup(days)
    print(json.dumps(rep, indent=2) if "--json" in argv else render(rep))
    return 0
