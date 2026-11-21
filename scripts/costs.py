#!/usr/bin/env python3
"""What CuaCode has spent, in dollars, read from the sessions' own records.

    python3 scripts/costs.py [--days N] [--json] [--html PATH] [--check] [--all]

Not a bill. The ollama plan is prepaid (old Pro, flat monthly, capped by
requests), so nothing here is charged - it is what the same tokens would cost
at ollama's published pay-as-you-go rates, the only per-token number that
exists for this usage. Rates from ollama.com/pricing, read 2026-10-09.

Two things this file is careful about, because each has been got wrong and each
is worth about a factor of eight:

  * Cached input is PART of `in`, billed at its own much lower rate. It is not a
discount off it, and it is not additional to it. Billing every input token at
the fresh rate is the eightfold overstatement.

  * A round is priced for WHEN IT RAN. deepseek's rate is half outside
12:00-18:00 UTC on weekdays and all weekend, and a session carried across that
boundary is billed both ways. That is what the timestamps in the records are
for; meta.json's per-day buckets threw the hour away.

And one thing it refuses to guess. **A session is only priced if its provider
reported a cache reading.** Cache reporting began 2026-10-08 and the sessions
before it have real tokens and no cache figure, and a missing reading is not a
zero. Pricing them as uncached overstates them about eightfold; inventing a
cache rate for them would be fiction. So they are listed with their tokens and
no dollars, and `--all` prices them anyway as the ceiling that number is.

Sessions from before 2026-08-11 carry no usage at all; they are counted and
named, never priced at zero.
"""
import os, sys
# The repo root, one level up: these scripts import the worker's own package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# how to run it
#
#   python3 scripts/costs.py                        the terminal report
#   python3 scripts/costs.py --html usage.html      the report as a page, and open it:
#                                           open -a firefox file://$PWD/usage.html
#   python3 scripts/costs.py --json                 the whole thing, machine-readable
#   python3 scripts/costs.py --check                its own arithmetic, 18 checks
#
#   --days N     only the last N days, counted by the UTC date each round was
#                billed on. A round is stamped in UTC, so N counts UTC days and
#                late in the evening a local day may hold more than one.
#   --only IDS   just these sessions. Ids, space-separated, or the path to a
#                file with them in it (one per line, or all on one). This is how
#                you report on one job rather than one window: --days would drag
#                in every other conversation that happened to run that day.
#                Anything that is not id-shaped is ignored, so a wrong file gives
#                an empty report rather than a wrong one.
#   --all        also price the sessions recorded before cache was reported,
#                treating every input token as uncached. That is a ceiling and
#                not a measurement, and the report says so where it shows it.
#   --html PATH  write the page. With no PATH, usage.html in the current dir.
#
# They compose: `--days 7 --html week.html`, `--only <ids> --json`, and so on.
#
# The page is generated, never edited. To change a number, re-run it; to change
# how it looks, edit the CSS inside html() below and every later run picks it up.
#
# ---------------------------------------------------------------------------
import json, sys
from pathlib import Path

from handler import usage as U

PLAN_USD = 20.0  # ollama old Pro, flat $/month. The value below is what it bought.

# $ per 1M tokens: (input, cached input, output). ollama.com/pricing, 2026-10-09.
# These are the PEAK rates; `rates()` halves the half-price models by their
# measured off-peak share, so nothing here needs a second table.
RATES = {
    "deepseek-v4.1-flash": (0.30, 0.006, 1.20),
    "glm-5.3-flash": (0.15, 0.03, 0.50),
    "glm-5.3": (1.40, 0.26, 4.40),
    "glm-5.2": (1.40, 0.26, 4.40),
    "kimi-k2.7-code": (0.95, 0.19, 4.00),
    "kimi-k3": (3.00, 0.30, 15.00),
    "mistral-large-4": (0.68, 0.07, 2.09),
    "gpt-oss:120b": (0.15, 0.014, 0.60),
    "gemma4:31b": (0.14, 0.05, 0.40),
    # Retired from ollama's table. Priced at deepseek's own PAYG rate so 218M
    # tokens of history do not silently count as free.
    "deepseek-v4-flash:0731": (0.0046, 0.0046, 1.28),
}

# Models whose published rate halves off-peak (outside 12:00-18:00 UTC on weekdays,
# all weekend). Both tiers are exactly half, so the discount scales the whole row.
HALF_PRICE = {"deepseek-v4.1-flash", "deepseek-v4-flash:0731"}

# What a model caches on a repeated prompt, and the prompt size below which it does not.
# Measured against the live API 2026-10-09 on a re-sent prefix; glm-5.3-flash cached
# nothing at 4k and 94% at 44k, which is why the threshold is here at all. Used only
# for rounds that reported nothing: a round that said 0 is indistinguishable from a
# round nobody asked, and both get the low end.
CACHE = {
    "deepseek-v4.1-flash": (0.97, 0),
    "deepseek-v4-flash:0731": (0.97, 0),
    "glm-5.3-flash": (0.94, 20000),
    "glm-5.3": (0.97, 0),
    "glm-5.2": (1.00, 0),
    "kimi-k2.7-code": (1.00, 0),
    "kimi-k3": (0.97, 0),
    "mistral-large-4": (0.97, 0),
    "gemma4:31b": (0.57, 0),
    "gpt-oss:120b": (0.00, 0),
}


def floor_ratio(model: str, c: dict) -> float:
    """What this model's bucket would cost if its silent input had cached, over what it
    costs now. A ratio rather than a second rollup, so both figures on the line are the
    same money answering the same question."""
    share, thresh = CACHE.get(model, (0.0, 0))
    r = RATES.get(model)
    if not share or not r: return 1.0
    rate_in, rate_cache, rate_out = r
    in_, cache = c.get("in") or 0, min(c.get("cache") or 0, c.get("in") or 0)
    out = c.get("out") or 0
    silent = in_ - cache
    cached_if = silent * share if in_ >= thresh else 0
    now = silent * rate_in + cache * rate_cache + out * rate_out
    low = (silent - cached_if) * rate_in + (cache + cached_if) * rate_cache + out * rate_out
    return (low / now) if now else 1.0


def _unique(records: list[dict]) -> list[dict]:
    """Drop the duplicates a buggy commit left in a transcript.

    A tree with `_pending.clear()` missing from session.commit() re-appends the whole
    history on every commit, so one round lands on disk once per later commit and
    meta.json counts it every time. Two real rounds can only collide here on the same
    second with identical counts, which would understate a total - the safe direction.
    """
    seen, out = set(), []
    for r in records:
        if r.get("t") not in ("assistant", "usage"):
            continue
        x = r.get("u") or {}
        key = (r.get("ts"), r.get("t"), x.get("provider"), x.get("model"),
               x.get("in"), x.get("out"), x.get("cache"), x.get("think"), x.get("src"))
        if key in seen: continue
        seen.add(key)
        out.append(r)
    return out


def sessions(discount: dict, only: list = None, cutoff: str = "") -> list[dict]:
    """Every session's meta, with usage rebuilt from its own transcript, and each
    half-price model's off-peak share of rounds folded into `discount`.

    Read from the records rather than trusting meta.json: a session in a tree whose
    commit() stopped clearing pending counts each round once per later commit, and
    the duplicate is invisible in the meta that the bug itself wrote. The same pass
    reads the timestamps, which is the only place the off-peak split exists - meta's
    per-day buckets threw the hour away.

    `only` limits it to a set of ids, and does so *inside* this pass rather than
    after it: the off-peak share is measured from the timestamps, so a report on
    four sessions has to measure those four's share. Filtering afterwards would
    price them with a discount earned by hundreds of other conversations.

    `cutoff` (a YYYY-MM-DD date) does the same for time: the usage is rebuilt from
    the rounds billed on or after it, and the off-peak share measured from those
    rounds alone. A window is about rounds, not sessions, so a session that has not
    run since the cutoff comes back with no usage at all rather than its all-time
    total -- showing last week's conversation under tonight's date would be the
    wrong number wearing the right one's name.
    """
    from handler.session import store
    want = set(only or ())
    metas = []
    for meta in store.list_sessions():
        sid = meta.get("id") or ""
        if want and sid not in want: continue
        try:
            records = store.read_jsonl(store.path(sid) / "messages.jsonl")
        except ValueError:
            metas.append(meta); continue
        if records:
            uniq = _unique(records)
            if cutoff:
                uniq = [r for r in uniq if (r.get("ts") or "")[:10] >= cutoff]
            for r in uniq:
                m = (r.get("u") or {}).get("model") or ""
                if m in HALF_PRICE:
                    off, n = discount.get(m, (0, 0))
                    discount[m] = (off + (1 if offpeak(r.get("ts")) else 0), n + 1)
            fresh = U.of_records(uniq)
            # With a window, nothing in it is the answer too: an empty usage is this
            # session's spend inside the window, not a reason to fall back to meta.json.
            if fresh or cutoff: meta = {**meta, "usage": fresh}
        metas.append(meta)
    return metas


def measured_from(metas: list[dict]) -> str:
    """The first day any session reported a cache reading.

    Derived from the sessions rather than written down as a date, because the
    date is a property of the data and not a decision: re-record an old session
    from a provider that reports cache and the boundary moves on its own.

    Empty when nothing anywhere has a reading, in which case nothing is priced
    and the report says so rather than showing a confident zero.
    """
    days = sorted((m.get("created") or "")[:10] for m in metas
                  if (m.get("usage") or {}).get("cache"))
    return days[0] if days else ""


def is_priced(meta: dict) -> bool:
    """Whether this session's cost is known rather than merely computable.

    A session counts as priced when its provider reported a cache reading for
    it. Everything recorded before cache was read has real tokens and no cache
    figure, and the two are not the same fact: pricing it uncached says it
    cached nothing, which is false, and says it by a factor of about eight.
    """
    return bool((meta.get("usage") or {}).get("cache"))


def partition(metas: list[dict]) -> tuple[list, list]:
    """The sessions whose cost is known, and the ones whose tokens are all we have."""
    measured, legacy = [], []
    for m in metas:
        u = m.get("usage") or {}
        if u.get("rounds"):
            (measured if is_priced(m) else legacy).append(m)
    return measured, legacy


def roles_of(meta: dict, table: dict, loop: bool = False) -> list[dict]:
    """A session's side calls -- a subagent, the permission gate, a namer.

    Billed whether or not the conversation's own loop ran them, so each is a
    row under the session that paid for it, priced on its own counts.

    `loop` adds the conversation's own rounds as `main`. They are not in `srcs`,
    which is only what ran beside the conversation, so a table claiming to group
    the same tokens by role would otherwise describe the side calls and leave the
    rest of the bill unaccounted for.
    """
    u = meta.get("usage") or {}
    # The model is the session's own: a side call is made by this worker on this
    # conversation's behalf, and its rate is the rate of the hour it ran.
    name = (u.get("models") or {})
    model = next(iter(name), "")
    model = model.split(" ", 1)[-1] if " " in model else model
    out, sides = [], 0.0
    for src, spent in sorted((u.get("srcs") or {}).items(),
                             key=lambda kv: -((kv[1].get("in") or 0) + (kv[1].get("out") or 0))):
        v = usd(model, spent, table)
        if v: sides += v["raw"]
        out.append({"src": src, "tokens": spent, "money": v})
    if loop:
        # The loop is what is left of the session once its side calls are paid for, not
        # its tokens priced at one model's rate: a session that ran two models would
        # have the whole bill put on the first one's, and the rows would sum past 100%.
        whole = money_for(meta, table)
        left = None if whole is None else max(0.0, whole - sides)
        out.insert(0, {"src": "main", "tokens": loop_only(u),
                       "money": None if left is None else {"usd": round(left, 2), "raw": left}})
    return out


def offpeak(ts: str) -> bool:
    """Half price: outside 12:00-18:00 UTC on weekdays, and all weekend.

    deepseek's two tiers differ by exactly a half, so one factor covers the input,
    the cached input and the output together and no split is needed.
    """
    from datetime import datetime
    try: d = datetime.fromisoformat(ts or "")
    except (TypeError, ValueError): return False
    return d.weekday() >= 5 or not (12 <= d.hour < 18)


def rates(discount: dict) -> dict:
    """The table with each half-price model's measured off-peak share applied."""
    out = dict(RATES)
    for m, (off, n) in discount.items():
        if n and m in out:
            out[m] = tuple(r * (1 - 0.5 * off / n) for r in out[m])
    return out


def usd(model: str, c: dict, rates_: dict = None) -> dict | None:
    """One model's money, split by where it went. None when no rate is known."""
    r = (rates_ or RATES).get(model)
    if not r:
        return None
    rate_in, rate_cache, rate_out = r
    cached = min(c.get("cache") or 0, c.get("in") or 0)
    fresh = (c.get("in") or 0) - cached
    cost = fresh * rate_in + cached * rate_cache + (c.get("out") or 0) * rate_out
    return {
        "usd": round(cost / 1e6, 2),
        # Unrounded, for adding up: summing the cents each round displays as turns a
        # pile of small sessions into a total that its own rows cannot reach.
        "raw": cost / 1e6,
        "in_usd": round(fresh * rate_in / 1e6, 2),
        "cache_usd": round(cached * rate_cache / 1e6, 2),
        "out_usd": round((c.get("out") or 0) * rate_out / 1e6, 2),
        "in": c.get("in") or 0,
        "out": c.get("out") or 0,
        "cache": cached,
        "rounds": c.get("rounds") or 0,
    }


def money(rep: dict, rates_: dict = None) -> tuple[list, list, float]:
    """Every model in the rollup, priced. What has no rate comes back separate.

    Also the floor: the same rollup priced as if every round that reported no cache had
    cached at its model's measured rate. Returned as a fraction of the top figure."""
    priced, unpriced = [], []
    top = floor = 0.0
    for m in rep["models"]:
        name = m["name"].split(" ", 1)[-1]  # "ollama x" -> "x"
        v = usd(name, m, rates_)
        if v:
            top += v["raw"]
            floor += v["raw"] * floor_ratio(name, m)
        (priced if v else unpriced).append({"name": m["name"], **(v or {}), "tokens": m})
    priced.sort(key=lambda m: -m["raw"])
    unpriced.sort(key=lambda m: -(m["tokens"]["in"] + m["tokens"]["out"]))
    return priced, unpriced, (floor / top if top else 1.0)


def _n(v: int) -> str:
    if v >= 1e9: return f"{v / 1e9:.2f}B"
    if v >= 1e6: return f"{v / 1e6:.1f}M"
    if v >= 1e3: return f"{v / 1e3:.1f}k"
    return str(v)


def render(rep: dict, priced: list, unpriced: list, days: int, floor: float = 1.0,
           split: tuple = None) -> str:
    total = round(sum(m["raw"] for m in priced), 2)
    head = f"spend · {rep['sessions']} sessions"
    if rep.get("since"):
        head += f" · {rep['since']}"
        if rep.get("days") and len(rep["days"]) > 1: head += f" to {rep['days'][-1]['day']}"
    if days: head += f" · last {days} day{'s' if days != 1 else ''}"
    if rep.get("unmeasured"):
        head += f" · {rep['unmeasured']} with nothing measured"

    # What the number above cannot be, said beside it. Every round that reported no
    # cache is priced as if it were never cached, and these models do cache a repeated
    # prompt (measured 2026-10-09: deepseek 97%, glm-5.3-flash 94% above ~20k context,
    # kimi 100%). So the top figure is a ceiling and the floor is what the same rounds
    # would cost if every one of them had cached. The truth sits between, and which end
    # is nearer depends on context size, which the records do not carry.
    # The split comes before the money, because it is what the money means:
    # every session priced had a cache reading, and every one not priced is a
    # known quantity of tokens whose cost is not known.
    lines = [head, ""]
    if split:
        measured, legacy, boundary = split
        if boundary:
            lines.append(f"  {'cache readings':<14} {'from ' + boundary:>12}  "
                         f"{len(measured)} priced, {len(legacy)} recorded before it")

    lines += [
             f"  at list rates  {'$' + format(total, ',.2f'):>12}  ceiling: every silent round billed uncached",
             f"  if cache hit  {'$' + format(total * floor, ',.2f'):>12}  floor: every silent round cached at its model's measured rate",
             f"  on the plan    {'$' + format(PLAN_USD, ',.2f') + '/mo':>12}  old Pro, flat"
             + (f" - {total / PLAN_USD:.1f}x that at the ceiling" if PLAN_USD else "")]

    width = max((len(m["name"].split(" ", 1)[-1]) for m in priced), default=0)
    if priced:
        lines += ["", "by model"]
        for m in priced:
            name = m["name"].split(" ", 1)[-1]
            cached = f" ({round(m['cache'] * 100 / m['in'])}% cached)" if m["in"] and m["cache"] else ""
            lines.append(f"  {name:<{width}}  {'$' + format(m['usd'], ',.2f'):>10}   "
                         f"in {_n(m['in'])}{cached:<12} {_n(m['out'])} out"
                         f"   [{m['in_usd']:.2f} in / {m['cache_usd']:.2f} cached / {m['out_usd']:.2f} out]")

    if unpriced:
        lines += ["", "no published rate (tokens shown, not priced)"]
        for m in unpriced:
            t = m["tokens"]
            lines.append(f"  {m['name']:<{max(width, 20)}}  {_n(t['in'])} in / {_n(t['out'])} out"
                         f"   {t['rounds']} rounds")

    if rep.get("top"):
        # Ranked on billed tokens, not dollars: a session in a rollup carries no
        # per-model split, so its money cannot be told apart from its neighbour's.
        lines += ["", "biggest conversations (billed tokens)"]
        for s in rep["top"][:5]:
            lines.append(f"  {_n(s['in'] + s['out']):>7}  {s['rounds']:>4} rounds  {s['id']}  {(s['title'] or '')[:38]}")

    return "\n".join(lines)


def check():
    """One model's own money against hand-computed values."""
    m = usd("glm-5.3-flash", {"in": 1_000_000, "cache": 0, "out": 1_000_000})
    assert (m["in_usd"], m["out_usd"], m["usd"]) == (0.15, 0.50, 0.65), m
    m = usd("glm-5.3-flash", {"in": 1_000_000, "cache": 1_000_000, "out": 0})
    assert (m["in_usd"], m["cache_usd"], m["usd"]) == (0.0, 0.03, 0.03), m  # all of it cached
    m = usd("glm-5.3-flash", {"in": 1_000_000, "cache": 2_000_000, "out": 0})
    assert m["cache"] == 1_000_000 and m["usd"] == 0.03, m  # cache never exceeds the prompt it sits in
    assert usd("nvidia/whatever", {"in": 1, "out": 1}) is None
    # All 100 rounds off-peak halves the whole table for that model.
    half = rates({"deepseek-v4.1-flash": (100, 100)})["deepseek-v4.1-flash"]
    assert half == (0.15, 0.003, 0.60), half
    # None off-peak leaves it alone; a plain weekday hour is not off-peak.
    assert rates({})["deepseek-v4.1-flash"] == RATES["deepseek-v4.1-flash"]
    assert offpeak("2026-10-10T12:30:00+00:00") and not offpeak("2026-10-07T12:30:00+00:00")  # Sat vs Wed noon
    assert offpeak("2026-10-07T20:00:00+00:00") and offpeak("2026-10-07T03:00:00+00:00")
    # Peak is the window itself: 12:00 through 17:59 on a weekday. 18:00 is the far edge
    # of it, so it is already off-peak.
    assert not offpeak("2026-10-07T12:00:00+00:00") and not offpeak("2026-10-07T17:59:00+00:00")
    assert offpeak("2026-10-07T18:00:00+00:00") and offpeak("2026-10-07T11:59:00+00:00")
    assert offpeak("") is False
    # And the duplicated records a buggy commit leaves behind are dropped.
    dup = [{"t": "assistant", "ts": "T", "u": {"in": 10, "out": 1, "model": "x"}},
           {"t": "assistant", "ts": "T", "u": {"in": 10, "out": 1, "model": "x"}},
           {"t": "tool", "ts": "T", "result": {}}]
    assert len(_unique(dup)) == 1, _unique(dup)
    # The floor: a bucket whose cache was all reported does not move; one that reported
    # nothing while its model caches comes down steeply. glm-5.3-flash under its
    # threshold stays put, which is the measured 4k behaviour.
    full = {"in": 1_000_000, "cache": 0, "out": 0}
    assert floor_ratio("gpt-oss:120b", full) == 1.0            # no cache rate published
    assert floor_ratio("deepseek-v4.1-flash", full) < 0.05     # 97% of it would be cached
    small = {"in": 10_000, "cache": 0, "out": 0}
    big = {"in": 100_000, "cache": 0, "out": 0}
    assert floor_ratio("glm-5.3-flash", small) == 1.0         # below the 20k threshold
    assert floor_ratio("glm-5.3-flash", big) < 0.25            # above it, 94% caches: (6% x .15 + 94% x .03) / .15
    reported = {"in": 1_000_000, "cache": 1_000_000, "out": 0}
    assert floor_ratio("deepseek-v4.1-flash", reported) == 1.0  # nothing silent left
    # The boundary is a property of the data: it is the first day anything
    # reported a cache reading, and it does not exist when nothing has.
    assert measured_from([{"created": "2026-08-01", "usage": {"rounds": 2}},
                          {"created": "2026-10-08", "usage": {"rounds": 2, "cache": 5}}]) == "2026-10-08"
    assert measured_from([{"created": "2026-08-01", "usage": {"rounds": 2}}]) == ""
    # A missing reading is not a zero: that is the whole distinction.
    assert not is_priced({"usage": {"rounds": 3, "in": 10}})
    assert not is_priced({"usage": {"rounds": 3, "in": 10, "cache": 0}})
    assert is_priced({"usage": {"rounds": 3, "in": 10, "cache": 4}})
    # A session with no usage at all is neither: it is not counted, so it cannot
    # be priced as zero either.
    a, b = partition([{"id": "x", "usage": {"rounds": 1, "cache": 9, "in": 9}},
                      {"id": "y", "usage": {"rounds": 1, "in": 9}},
                      {"id": "z"}])
    assert [m["id"] for m in a] == ["x"] and [m["id"] for m in b] == ["y"], (a, b)
    print("ok - 18 checks")


def ids_from(arg: str) -> list:
    """A `--only` argument: ids, or a file of them, one per line.

    A file is how a long list travels without quoting, and is what the session
    ids were actually written down in. Anything that is not an id-shaped word is
    ignored, so pointing it at the wrong file gives nothing rather than a
    silently empty report.
    """
    from pathlib import Path
    text = arg
    p = Path(arg)
    if p.is_file():
        text = p.read_text(encoding="utf-8")
    out = []
    for word in text.replace(",", " ").split():
        # an id is 8 digits, a dash, 6 digits, a dash, 4 hex
        parts = word.split("-")
        if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) == 8 and parts[1].isdigit():
            out.append(word)
    return out


def main(argv: list[str]) -> int:
    if "--check" in argv:
        check()
        return 0
    days = 0
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("usage: costs.py [--days N] [--only IDS|FILE] [--json] [--html PATH] [--check] [--all]")
            return 2
    only = []
    if "--only" in argv:
        try: only = ids_from(argv[argv.index("--only") + 1])
        except IndexError:
            print("costs: --only wants ids or a file of them")
            return 2
    discount = {}
    cutoff = U._cutoff(days)
    metas = sessions(discount, only=only, cutoff=cutoff)
    # A conversation that spent nothing in the window is not a session of this report.
    if cutoff: metas = [m for m in metas if (m.get("usage") or {}).get("rounds")]
    measured, legacy = partition(metas)
    # The cache boundary is a property of the history, not of the window, so it is read
    # from meta.json as written: --days 1 must not claim cache reporting began today.
    from handler.session import store
    boundary = measured_from(store.list_sessions())

    # Only measured sessions are priced by default. A session recorded before the
    # provider reported cache has real tokens and no cache figure, and a missing
    # reading is not a zero: pricing it uncached overstates it about eightfold,
    # and inventing a reading is fiction. So the default report prices what is
    # known and names what is not. --all prices the rest as the ceiling it is.
    priced_metas = metas if "--all" in argv else measured
    rep = U.rollup(days, metas=priced_metas)
    table = rates(discount)
    priced, unpriced, floor = money(rep, table)
    legacy_tokens = {"in": sum((m.get("usage") or {}).get("in") or 0 for m in legacy),
                     "out": sum((m.get("usage") or {}).get("out") or 0 for m in legacy)}

    if "--html" in argv:
        i = argv.index("--html")
        path = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else "usage.html"
        Path(path).write_text(
            html(rep, priced, table, measured, legacy, boundary, legacy_tokens, discount, days),
            encoding="utf-8")
        print(f"wrote {path}")
    elif "--json" in argv:
        print(json.dumps({"plan_usd": PLAN_USD, "days": days,
                          "total_usd": round(sum(m["raw"] for m in priced), 2),
                          "floor_ratio": round(floor, 4),
                          "cache_readings_from": boundary,
                          "priced_sessions": len(measured),
                          "unpriced_sessions": len(legacy),
                          "only": only,
                          "unpriced_tokens": legacy_tokens,
                          "offpeak_share": {m: round(o / n, 3) for m, (o, n) in discount.items() if n},
                          "models": priced, "unpriced": [m["name"] for m in unpriced],
                          "usage": rep}, indent=2))
    else:
        print(render(rep, priced, unpriced, days, floor,
                     split=(measured, legacy, boundary) if legacy else None))
    return 0


# ---------------------------------------------------------------------------
# the page

# What ran a round that the session's own loop did not, and which group it lands in.
#
# Two groups, and the split is by WHO, not by size. `decider` and `namer` are the
# agent's own plumbing -- the permission gate fires because the agent asked to run
# a tool, and the title is written for the agent's conversation -- so they fold
# into Agent: "what did this session cost" includes what it cost to run itself.
# `explorer`, `researcher` and `digest` are a different actor doing work the agent
# asked for, which is the one thing worth seeing separately.
#
# They are listed under their group rather than merged away, so nothing is hidden
# by being grouped: permissions and title still appear, named, under Agent.
ROLE = {
    "main": ("Agent", "the conversation's own loop"),
    "decider": ("Agent", "asks whether a tool call needs approval"),
    "namer": ("Agent", "writes the session's short title"),
    "explorer": ("Delegated", "a subagent, named for its job"),
    "researcher": ("Delegated", "a subagent, named for its job"),
    "digest": ("Delegated", "a page digest: reads one and returns the answer"),
}

# The order a group is read in.
GROUPS = ["Agent", "Delegated"]


def role_of(src: str) -> tuple:
    """(group, what it is) for a src, and a name from the word itself when new.

    A `src` this file has not seen before is shown as itself inside Delegated
    rather than guessed at: the records are the authority on what ran, and a
    side call is the group it belongs to by definition.
    """
    return ROLE.get(src, ("Delegated", ""))


def cache_of(meta: dict) -> tuple:
    """(what it cost, what the same rounds cost with no cache) for one session.

    The second figure is the one worth hiding behind the first: the same tokens
    with every input token billed at the fresh rate. Cached input costs a
    fiftieth of fresh input on deepseek, so a well-cached session's no-cache
    price is not a little higher, it is somewhere between five and twenty times
    higher, and reading that ratio is how you see what caching is doing.
    """
    u = meta.get("usage") or {}
    cached = min(u.get("cache") or 0, u.get("in") or 0)
    fresh = (u.get("in") or 0) - cached
    out = u.get("out") or 0
    models = u.get("models") or {}
    key = next(iter(models), "")
    name = key.split(" ", 1)[-1] if " " in key else key
    r = RATES.get(name)
    if not r: return (None, None)
    with_ = (fresh * r[0] + cached * r[1] + out * r[2]) / 1e6
    without = ((fresh + cached) * r[0] + out * r[2]) / 1e6
    return (with_, without)


def _tip(meta: dict, money: float | None) -> str:
    """A session's cost, with the no-cache price on the hover.

    Silent when there is nothing to compare: a session priced at nothing (all
    its rounds cached, or a rounding to zero) has no ratio worth showing, and a
    tooltip reading "0.0x more" is worse than no tooltip.
    """
    if money is None: return _d(None)
    _, without = cache_of(meta)
    if without is None or money <= 0 or without <= money + 1e-9:
        return _d(money)
    factor = without / money
    return (f'<span class="tip">{_d(money)}'
            f'<span class="tip-body">{_d(without)} without caching '
            f'<b>{factor:.1f}&times; more</b></span></span>')


def _tip_total(total: float, tin: int, tcache: int, tout: int) -> str:
    """The whole bill, with the same tokens priced without caching behind it.

    Every input token at the fresh rate is the ceiling a reader gets if they
    price the tokens themselves and skip the cache rate. Said on the hover
    rather than in a column, because it is the same rounds twice.
    """
    name = "deepseek-v4.1-flash"
    r = RATES.get(name)
    if not r or total <= 0: return f"${total:,.2f}"
    # Every input token at the fresh rate: nothing was cached in this telling.
    without = (tin * r[0] + tout * r[2]) / 1e6
    if without <= total + 1e-9: return f"${total:,.2f}"
    return (f'<span class="tip">${total:,.2f}'
            f'<span class="tip-body">${without:,.2f} without caching '
            f'<b>{without / total:.1f}&times; more</b></span></span>')


def by_model(metas: list, table: dict) -> list:
    """Every model, side by side: what it cost, how much it cached, how fast.

    The trap this exists to avoid: a cache rate averaged over every session a
    model ever ran is not a property of the model. `deepseek-v4.1-flash` looks
    like it caches 28% of its input if you average in 129 sessions recorded
    before its provider reported cache, and `glm-5.3-flash` looks like it caches
    nothing at all, because none of its sessions ever said. So a model's cache
    rate is computed ONLY from the sessions that reported one, and the row says
    how many that was. A model with no readings shows no rate rather than zero.

    Tokens per second is the other honest column: it is output over generating
    time, both of which the provider reports, so it needs no interpretation.
    """
    agg = {}
    for meta in metas:
        u = meta.get("usage") or {}
        if not u.get("rounds"): continue
        reported = bool(u.get("cache"))
        for key, v in (u.get("models") or {}).items():
            name = key.split(" ", 1)[-1] if " " in key else key
            a = agg.setdefault(name, {
                "name": name, "sessions": 0, "rounds": 0,
                "in": 0, "out": 0, "think": 0, "secs": 0.0,
                "cache": 0, "reported": 0, "reported_in": 0, "usd": 0.0,
                "days": set(),
            })
            for k in ("in", "out", "think", "rounds"):
                a[k] += v.get(k) or 0
            a["secs"] += v.get("secs") or 0
            a["sessions"] += 1
            if reported:
                a["reported"] += 1
                a["reported_in"] += v.get("in") or 0
                a["cache"] += min(v.get("cache") or 0, v.get("in") or 0)
            # Priced at the model's measured off-peak share, where one exists.
            m = usd(name, v, table)
            if m and reported: a["usd"] += m["usd"]
            for d in (v.get("days") or {}):
                a["days"].add((d, 1 if offpeak(d + "T00:00:00+00:00") else 0))
    out = []
    for a in agg.values():
        a["tps"] = round(a["out"] / a["secs"], 1) if a["secs"] >= 1 else 0.0
        a["share"] = _pct(a["cache"], a["reported_in"]) if a["reported_in"] else None
        a["rate"] = RATES.get(a["name"])
        a["priced"] = a["usd"] > 0 or a["reported"] > 0
        out.append(a)
    # Biggest first: the models that were actually used, which is what the table
    # is for. A model with one round belongs at the bottom, priced or not.
    out.sort(key=lambda a: -a["in"])
    return out


def chart_rows(measured: list, legacy: list, table: dict, cap: int = 40) -> tuple:
    """One bar per session, longest first, priced and unpriced in one picture.

    The bar is drawn from BILLED TOKENS, not from dollars, and that is the one
    decision in this function: a token count is known for every session ever
    recorded, while a cost is known for only some of them. A chart drawn in
    dollars would have to either drop the 388 sessions with no cache reading or
    invent a bar for them, and both are worse than drawing the quantity that
    every session actually has.

    So the bar is a fact and the cost column beside it is honest about which
    costs are known. Sorted by the bar, since that is what the eye is comparing.
    """
    rows = []
    for meta in measured + legacy:
        u = meta.get("usage") or {}
        if not u.get("rounds"): continue
        rows.append({
            "id": meta.get("id") or "",
            "title": (meta.get("title") or meta.get("id") or "")[:44],
            "created": (meta.get("created") or "")[:10],
            "in": u.get("in") or 0,
            "out": u.get("out") or 0,
            "billed": (u.get("in") or 0) + (u.get("out") or 0),
            "cache": min(u.get("cache") or 0, u.get("in") or 0),
            "rounds": u.get("rounds") or 0,
            "usd": money_for(meta, table) if u.get("cache") else None,
            "priced": bool(u.get("cache")),
        })
    rows.sort(key=lambda r: -r["billed"])
    shown = rows[:cap]
    top = max((r["billed"] for r in shown), default=1) or 1
    for r in shown:
        r["w"] = round(r["billed"] / top * 100, 2)
    return shown, len(rows) - len(shown)


def chart_html(measured: list, legacy: list, table: dict) -> str:
    """The bars, drawn with a table and two divs: no library, and it prints."""
    shown, rest = chart_rows(measured, legacy, table)
    if not shown: return "", ""
    out = []
    for r in shown:
        # An unpriced session's bar is drawn hollow: the tokens are real, the
        # dollars are not known, and a solid bar would claim otherwise.
        cls = "fill" if r["priced"] else "hollow"
        money = (f'<span class="num">${r["usd"]:,.2f}</span>' if r["usd"] is not None
                 else '<span class="never">not measured</span>')
        out.append(f'''        <tr>
          <td class="name">{r["title"]}</td>
          <td class="date">{r["created"]}</td>
          <td class="barcell"><span class="bar"><i class="{cls}" style="width:{r["w"]}%"></i></span></td>
          <td class="num">{r["billed"]:,}</td>
          <td class="num dim">{r["rounds"]:,}</td>
          <td>{money}</td>
        </tr>''')
    note = (f"<p class=\"lede\">{len(shown)} of {len(shown) + rest} sessions, the largest first."
            + (f" The other {rest} are in the table above." if rest else "") + "</p>")
    return "\n".join(out), note


def loop_only(u: dict) -> dict:
    """The session's own loop, with the side calls subtracted out.

    This is the distinction the first version of the plot missed, and it made the
    plot say the wrong thing. A `usage` rollup counts EVERY round the session
    paid for: its own loop, plus every subagent, every page digest, every
    permission check and title. Those side calls are 333 of one session's 687
    rounds here, and each carries a tiny context of its own -- so folding them in
    drops the mean context per round to a third of what the loop's really is, and
    the arithmetic that is supposed to explain the bill stops describing it.

    A subagent's request is not the conversation's request: it does not re-send
    the conversation. So anything about the conversation's own shape is computed
    from these numbers and never from the rollup's.
    """
    srcs = u.get("srcs") or {}
    sub = {k: sum(v.get(k) or 0 for v in srcs.values()) for k in ("rounds", "in", "out", "cache", "secs")}
    return {
        "in": max(0, (u.get("in") or 0) - sub["in"]),
        # Cache travels with it, or the loop's own row is priced as if none of its
        # re-sent prompt had ever been cached -- eight times the whole bill.
        "cache": max(0, (u.get("cache") or 0) - sub["cache"]),
        "out": max(0, (u.get("out") or 0) - sub["out"]),
        "rounds": max(0, (u.get("rounds") or 0) - sub["rounds"]),
        "secs": max(0.0, (u.get("secs") or 0.0) - sub["secs"]),
        "side_rounds": sub["rounds"],
        "side_in": sub["in"],
        "all_rounds": u.get("rounds") or 0,
        "all_in": u.get("in") or 0,
        "peak": u.get("peak") or 0,
    }


def scatter_svg(metas: list, table: dict, w: int = 1028, h: int = 620) -> tuple:
    """Held against sent: how big a conversation got, against what it cost to carry.

    The axes, and why these rather than the obvious ones. Billed input against
    requests is the first thing anyone plots and it is nearly a tautology, because
    billed input IS requests multiplied by context -- so a high correlation there
    is arithmetic, not a finding.

    The question with an answer in it: a session held some amount of context and
    paid for some amount of input, and the ratio is how many times over it paid
    for the same window. On log-log axes that ratio is the distance above the
    diagonal. A dot sitting on the diagonal held its context once and never
    re-sent it, which no real session does; every dot is above it by however many
    times it re-sent. The spread of that distance is what makes one conversation
    cost more than another.

    Requests is carried as the dot's size, because requests drives the distance
    rather than being a quantity worth an axis of its own.

    Log on both axes: held context runs 12k to 608k and billed input runs 14k to
    148M, and linear axes would pile every dot into one corner.
    """
    import math
    pts = []
    for meta in metas:
        u = meta.get("usage") or {}
        L = loop_only(u)
        if L["rounds"] < 1 or L["in"] < 1 or L["peak"] < 1: continue
        pts.append({"held": L["peak"], "sent": L["in"], "reps": L["in"] / L["peak"],
                    "reqs": L["rounds"], "side": L["side_rounds"],
                    "title": (meta.get("title") or meta.get("id") or "")[:60],
                    "id": meta.get("id") or "",
                    "measured": bool(u.get("cache")),
                    "usd": money_for(meta, table)})
    if len(pts) < 2: return "", ""

    pad_l, pad_r, pad_t, pad_b = 70, 26, 22, 50
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    xs = [p["held"] for p in pts]
    ys = [p["sent"] for p in pts]
    x0, x1 = math.floor(math.log10(min(xs))), math.ceil(math.log10(max(xs)))
    y1 = math.ceil(math.log10(max(ys)))
    y0 = y1 - 5
    while min(ys) < 10 ** y0: y0 -= 1
    def sx(v): return pad_l + (math.log10(v) - x0) / (x1 - x0) * pw
    def sy(v): return pad_t + ph - (math.log10(v) - y0) / (y1 - y0) * ph

    def tick(n: float) -> str:
        return (f"{n/1e9:g}B" if n >= 1e9 else f"{n/1e6:g}M" if n >= 1e6
                else f"{n/1e3:g}k" if n >= 1e3 else f"{n:g}")

    grid = []
    for e in range(x0, x1 + 1):
        v = 10 ** e
        grid.append(f'<line x1="{sx(v):.1f}" y1="{pad_t}" x2="{sx(v):.1f}" y2="{pad_t+ph}" class="grid"/>')
        grid.append(f'<text x="{sx(v):.1f}" y="{pad_t+ph+20}" class="tick" text-anchor="middle">{tick(v)}</text>')
    for e in range(y0, y1 + 1):
        v = 10 ** e
        grid.append(f'<line x1="{pad_l}" y1="{sy(v):.1f}" x2="{pad_l+pw}" y2="{sy(v):.1f}" class="grid"/>')
        grid.append(f'<text x="{pad_l-10}" y="{sy(v)+3.5:.1f}" class="tick" text-anchor="end">{tick(v)}</text>')

    # The diagonal: held once, sent once. Everything real sits above it.
    diag = []
    for k in range(0, 161):
        logx = x0 + (x1 - x0) * k / 160
        if y0 <= logx <= y1:
            diag.append((sx(10 ** logx), sy(10 ** logx)))
    diag_line = ""
    if len(diag) >= 2:
        (ax, ay), (bx, by) = diag[0], diag[-1]
        diag_line = (f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" class="ref"/>\n'
                     f'<text x="{bx - 8:.1f}" y="{by + 5:.1f}" class="reflabel" text-anchor="end">'
                     f'held once, sent once</text>')

    dots = []
    for p in pts:
        cls = "dot measured" if p["measured"] else "dot"
        # Size is the request count on a log ramp: a linear radius would make
        # every session past a hundred requests look the same.
        r = 3.0 + 2.4 * math.log10(max(p["reqs"], 1)) / 3.0
        # The tooltip's own payload, as data attributes. An SVG <title> is what
        # the browser offers for free and it is not enough: it waits about a
        # second before appearing, cannot be styled, and shows the same thing to
        # everybody. So the facts ride on the element and a small script paints
        # them, which is instant and reads like the rest of the page.
        attrs = (
            f'data-t="{_attr(p["title"])}" data-held="{p["held"]:,}" data-sent="{p["sent"]:,}" '
            f'data-reps="{p["reps"]:.0f}" data-reqs="{p["reqs"]:,}" data-side="{p["side"]:,}" '
            f'data-usd="{"" if p["usd"] is None else f"{p['usd']:.2f}"}" '
            f'data-measured="{'1' if p['measured'] else '0'}"')
        dots.append(
            f'<circle cx="{sx(p["held"]):.1f}" cy="{sy(p["sent"]):.1f}" r="{r:.1f}" '
            f'class="{cls}" {attrs}></circle>')

    svg = (f'<div class="plotwrap"><svg class="plot" viewBox="0 0 {w} {h}" width="100%" '
           f'role="img" aria-label="Context held against billed input, one point per session, '
           f'point size is the request count">\n'
           f'{"".join(grid)}\n{diag_line}\n{"".join(dots)}\n'
           f'<text x="{pad_l+pw/2:.0f}" y="{pad_t+ph+44:.0f}" class="axtitle" text-anchor="middle">'
           f'context held at its widest</text>\n'
           f'<text x="18" y="{pad_t+ph/2:.0f}" class="axtitle" text-anchor="middle" '
           f'transform="rotate(-90 18 {pad_t+ph/2:.0f})">billed input</text>\n'
           f'</svg>'
           f'<div class="plot-tip" id="plottip" hidden></div></div>')

    reps = sorted(p["reps"] for p in pts)
    med = reps[len(reps) // 2] if reps else 0
    note = (f"{len(pts)} sessions. How big each conversation got against how much it paid to carry "
            f"itself: the distance above the diagonal is how many times over the same window was sent, "
            f"and the median session sent its {med:.0f}x. Hover a point to see which one it is. "
            f"Side calls are excluded -- a subagent's request does not re-send the conversation, so "
            f"counting it would describe a different thing.")
    return svg, note


def _attr(s: str) -> str:
    """Text safe to sit inside a double-quoted attribute."""
    return (s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def why_expensive(meta: dict) -> str:
    """The one line that explains a session's bill, for the session that is worst.

    The question the plot provokes is "why is THAT one so expensive", and the
    answer is in two numbers that are already recorded: how many rounds it ran,
    and how big the context was by the end. A round re-sends the whole prompt, so
    the same window goes up once per round and the bill is rounds x context --
    which is why a long conversation is expensive out of proportion to how long
    it looks.
    """
    u = meta.get("usage") or {}
    rounds = u.get("rounds") or 0
    tin = u.get("in") or 0
    peak = u.get("peak") or 0
    if rounds < 2 or not tin: return ""
    if not peak:
        return (f"{rounds:,} requests re-sent the prompt each time, {tin:,} input tokens "
                f"in total.")
    return (f"{rounds:,} requests, each re-sending the prompt. Its widest context was "
            f"~{peak:,} tokens, and it was sent {tin / peak:.0f} times over: "
            f"{tin:,} input tokens for a conversation that never held more than {peak:,}.")


def _sessions_cell(a: dict) -> str:
    """How many of a model's sessions reported a cache reading.

    Just the count when they all did, because "4 of 4" says nothing the 4 does
    not. The fraction only appears when it is doing work: it is the difference
    between "this model caches 94%" and "the four sessions that told us did".
    """
    if a["reported"] == a["sessions"]:
        return f'{a["sessions"]}'
    return f'{a["reported"]}<span class="never"> of {a["sessions"]}</span>'


def _pct(part: float, whole: float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _d(v: float | None) -> str:
    return "&mdash;" if v is None else f"${v:,.2f}"


def _bar(p: str) -> str:
    return f'<span class="cache"><u>{p}</u><i><b style="width:{p}"></b></i></span>'


def html(rep: dict, priced: list, table: dict, measured: list, legacy: list,
         boundary: str, legacy_tokens: dict, discount: dict, days: int = 0) -> str:
    """The whole thing as one page, generated from what was just computed.

    Every figure here comes from the same `rep` the terminal report uses, so the
    two cannot disagree, and nothing on the page is typed by hand.
    """
    total = round(sum(m["raw"] for m in priced), 2)
    t = rep["total"]
    tin, tout, trounds = t.get("in", 0), t.get("out", 0), t.get("rounds", 0)
    tcache = min(t.get("cache", 0), tin)
    cache_pct = _pct(tcache, tin)

    # The ceiling on the same rounds: every input token billed as fresh. This is
    # the number a reader gets if they price the tokens themselves and skip the
    # cache rate, which is why it is on the page rather than in a footnote.
    name = max(priced, key=lambda m: m["in"], default=None)
    peak_rates = RATES.get(name["name"].split(" ", 1)[-1]) if name else None
    if peak_rates:
        at_peak = ((tin - tcache) * peak_rates[0] + tcache * peak_rates[1] + tout * peak_rates[2]) / 1e6
    else:
        at_peak = total

    # ---- by session, with each one's side calls under it
    rows = []
    for meta in sorted(measured, key=lambda m: -((m.get("usage") or {}).get("in") or 0)):
        u = meta["usage"]
        money = money_for(meta, table)
        cp = _pct(min(u.get("cache") or 0, u.get("in") or 0), u.get("in") or 0)
        sub = ""
        for role in roles_of(meta, table):
            rs = role["tokens"]
            sub += (f'<tr class="sub"><td><span class="tick"></span>{role["src"]}</td>'
                    f'<td class="num dim">{rs.get("in") or 0:,}</td>'
                    f'<td class="num ghost">{_pct(min(rs.get("cache") or 0, rs.get("in") or 0), rs.get("in") or 0)}%</td>'
                    f'<td class="num ghost">{(rs.get("in") or 0) - min(rs.get("cache") or 0, rs.get("in") or 0):,}</td>'
                    f'<td class="num ghost">{rs.get("out") or 0:,}</td>'
                    f'<td class="num ghost">{rs.get("rounds") or 0}</td>'
                    f'<td class="num ghost">{_d(role["money"]["usd"] if role["money"] else None)}</td></tr>')
        rows.append(f'''        <tr class="sess">
          <td class="name">{(meta.get("title") or meta.get("id") or "")[:56]}</td>
          <td class="num">{u.get("in") or 0:,}</td>
          <td>{_bar(f"{cp}%")}</td>
          <td class="num dim">{(u.get("in") or 0) - min(u.get("cache") or 0, u.get("in") or 0):,}</td>
          <td class="num">{u.get("out") or 0:,}</td>
          <td class="num ghost">{u.get("rounds") or 0}</td>
          <td class="num">{_tip(meta, money)}</td>
        </tr>
{sub}''')

    # ---- by role across every session
    agg = {}
    for meta in measured:
        for role in roles_of(meta, table, loop=True):
            a = agg.setdefault(role["src"], {"in": 0, "out": 0, "rounds": 0, "usd": 0.0})
            rs = role["tokens"]
            a["in"] += rs.get("in") or 0
            a["out"] += rs.get("out") or 0
            a["rounds"] += rs.get("rounds") or 0
            if role["money"]: a["usd"] += role["money"]["raw"]
    machine = sum(v["usd"] for k, v in agg.items() if k != "main")

    # Grouped: the general word is the row, and what ran the rounds sits under it.
    # A table of six flat names makes "delegated" and "housekeeping" look like
    # the same kind of thing, which is the mistake the old label made.
    by_group = {}
    for src, v in agg.items():
        group, _ = role_of(src)
        g = by_group.setdefault(group, {"in": 0, "out": 0, "rounds": 0, "usd": 0.0, "members": []})
        for k in ("in", "out", "rounds"):
            g[k] += v[k]
        g["usd"] += v["usd"]
        g["members"].append((src, v))

    role_rows = ""
    for group in GROUPS:
        g = by_group.get(group)
        if not g: continue
        share = g["usd"] / total * 100 if total else 0
        role_rows += (f'''        <tr class="grp"><td>{group}</td><td class="num">{g["rounds"]:,}</td>
              <td class="num">{g["in"]:,}</td><td class="num dim">{g["out"]:,}</td>
              <td class="num">${g["usd"]:,.2f}</td>
              <td class="num ghost">{share:.1f}%</td></tr>''')
        for src, v in sorted(g["members"], key=lambda kv: -kv[1]["usd"]):
            role_rows += (f'''        <tr class="sub"><td><span class="tick"></span>{src}</td>
              <td class="num dim">{v["rounds"]:,}</td><td class="num dim">{v["in"]:,}</td>
              <td class="num ghost">{v["out"]:,}</td><td class="num ghost">${v["usd"]:,.2f}</td>
              <td class="num ghost">{v["usd"] / total * 100 if total else 0:.1f}%</td></tr>''')
        if group == "Delegated":
            role_rows += ('        <tr class="gloss"><td colspan="6">'
                          'A subagent is named for the job it was given, and a digest reads one page '
                          'and returns the answer. Neither runs in this conversation\'s own loop.</td></tr>')
        elif group == "Agent":
            role_rows += ('        <tr class="gloss"><td colspan="6">'
                          'The loop, plus its own plumbing: the permission gate that decides '
                          'whether a tool call is allowed, and the title written for the session.'
                          '</td></tr>')

    offpeak = discount.get("deepseek-v4.1-flash")
    offpeak_note = (f"{round(offpeak[0] / offpeak[1] * 100)}% of rounds ran off-peak" if offpeak else "")

    # ---- every session, as a plot. Both sets, so the picture is the whole
    # history rather than only the part with a cost.
    chart_body, chart_note = chart_html(measured, legacy, table)
    plot, plot_note = scatter_svg(measured + legacy, table)
    # The most expensive priced session, explained in one line: the plot makes you
    # ask why one dot is high up, and this answers it without a second page.
    worst = max(measured, key=lambda m: ((m.get("usage") or {}).get("in") or 0), default=None)
    worst_line = ""
    if worst:
        worst_line = (f'<p class="note"><b>{worst.get("title") or worst.get("id")}</b> &mdash; '
                      f'{why_expensive(worst)}</p>')

    # ---- models side by side. `measured` is the priced set, so a rate here is
    # only ever computed from sessions that actually reported one.
    models = by_model(measured, table)
    model_rows = ""
    for a in models:
        hit = (f'<span class="cache num"><u>{a["share"]}%</u>'
               f'<i><b style="width:{a["share"]}%"></b></i></span>') if a["share"] is not None else \
              '<span class="never">not reported</span>'
        tps = f'{a["tps"]:.0f}' if a["tps"] else '<span class="never">&mdash;</span>'
        cost = (f'<span class="num">{_d(a["usd"])}</span>' if a["usd"]
                else ('<span class="never">no rate</span>' if not a["rate"]
                      else '<span class="never">&mdash;</span>'))
        model_rows += (f'''        <tr>
          <td class="name">{a["name"]}</td>
          <td class="num dim">{_sessions_cell(a)}</td>
          <td class="num">{a["rounds"]:,}</td>
          <td class="num">{a["in"]:,}</td>
          <td>{hit}</td>
          <td class="num">{a["out"]:,}</td>
          <td class="num dim">{tps}</td>
          <td>{cost}</td>
        </tr>\n''')

    role_section = f'''<div class="sec"><h2>By what ran it</h2>
  <p class="lede">The same tokens grouped by role. {machine:,.2f} dollars of it is
  machinery the sessions started on their own, not their own work.</p>
  <table><thead><tr><th>Role</th><th>Requests</th><th>Input</th><th>Output</th><th>Cost</th>
  <th>Share</th></tr></thead><tbody>
{role_rows}
  </tbody></table></div>'''

    legacy_block = ""
    if legacy:
        legacy_block = f'''<div class="sec"><h2>Excluded</h2>
  <p class="lede">{len(legacy)} sessions recorded before the provider reported cache carry
  {legacy_tokens['in']:,} input and {legacy_tokens['out']:,} output tokens with no cache reading.
  A missing reading is not a zero, so they are counted here and not priced.</p></div>'''

    span = rep.get("days") or []
    # What the list is: the whole history, or the window the figure describes.
    span_note = (f" in the last {days} day{'s' if days != 1 else ''}" if days
                 else (', ' + boundary + ' onward' if boundary else ''))
    when = span[0]["day"] + (" to " + span[-1]["day"] if len(span) > 1 else "") if span else ""

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>CuaCode usage</title>
<style>
  :root {{ --bg:#151110; --line:#2a2827; --line2:#3a3837; --fg:#eae8e6; --muted:#a8a5a3;
           --faint:#8a8785; --accent:#e07850; --good:#7ec699; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  html {{ -webkit-font-smoothing:antialiased; }}
  body {{ background:var(--bg); color:var(--fg); padding:54px 56px 50px; width:1140px;
          font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif; }}
  .num {{ font-family:"SF Mono",ui-monospace,Menlo,monospace; font-variant-numeric:tabular-nums; }}
  header {{ border-bottom:1px solid var(--line2); padding-bottom:15px; margin-bottom:38px;
            display:flex; justify-content:space-between; align-items:baseline; }}
  h1 {{ font-size:17px; font-weight:600; }}
  .when {{ color:var(--faint); font-size:12.5px; }}
  .cap {{ color:var(--muted); font-size:13px; margin-bottom:12px; }}
  .figure {{ font-size:72px; font-weight:600; letter-spacing:-3px; line-height:1; color:var(--accent); }}
  .aside {{ margin-top:15px; margin-bottom:42px; color:var(--muted); font-size:13.5px; }}
  .aside b {{ color:var(--fg); font-weight:500; }}
  .aside .rule {{ color:var(--line2); padding:0 10px; }}
  .sec {{ margin-bottom:40px; }}
  .sec h2 {{ font-size:13px; font-weight:600; margin-bottom:4px; }}
  .sec p.lede {{ color:var(--faint); font-size:12.5px; margin-bottom:18px; max-width:76ch; }}
  table {{ width:100%; border-collapse:collapse; }}
  /* A gutter between columns. Monospace digits are their own width, so with no
     horizontal padding a wide number and the next one read as a single figure
     ("1,552,902 1,340"), and two header words run together. */
  th {{ text-align:right; font-size:12px; font-weight:500; color:var(--faint);
        padding:0 0 10px 20px; border-bottom:1px solid var(--line2); white-space:nowrap; }}
  th:first-child {{ text-align:left; padding-left:0; }}
  td {{ padding:13px 0 13px 20px; border-bottom:1px solid var(--line); text-align:right;
        white-space:nowrap; }}
  td:first-child {{ text-align:left; padding-left:0; }}
  /* The first column is the one that gives: a long title truncates rather than
     wrapping, because a wrapped title makes its row twice as tall as the one
     under it and the whole table goes ragged. `max-width:0` inside a table
     layout lets the column shrink as far as it must. */
  td.name {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
             max-width:260px; text-align:left; }}
  .never {{ color:var(--faint); font-size:12px; }}
  /* the chart: a table, a track and a fill. No library, so it prints and it
     cannot drift from the numbers the way a canvas would. */
  td.barcell {{ width:34%; padding-right:20px; }}
  .bar {{ display:block; height:9px; background:var(--line); border-radius:2px; overflow:hidden; }}
  .bar i {{ display:block; height:100%; border-radius:2px; }}
  .bar i.fill {{ background:var(--accent); opacity:.75; }}
  .bar i.hollow {{ background:repeating-linear-gradient(
      135deg, var(--line2), var(--line2) 3px, transparent 3px, transparent 6px); }}
  td.date {{ color:var(--faint); font-size:12px; font-family:"SF Mono",monospace; }}
  tr:hover td {{ background:rgba(234,232,230,.03); }}
  .note {{ color:var(--faint); font-size:12.5px; margin-top:16px; line-height:1.6; }}
  .note b {{ color:var(--muted); font-weight:500; }}
  /* the plot: an SVG, styled from here so it belongs to the page's palette */
  svg.plot {{ display:block; margin:4px 0 0; overflow:visible; }}
  svg.plot .grid {{ stroke:var(--line); stroke-width:1; }}
  svg.plot .tick {{ fill:var(--faint); font-size:11px; font-family:"SF Mono",monospace; }}
  svg.plot .axtitle {{ fill:var(--muted); font-size:11.5px; }}
  svg.plot .ref {{ stroke:var(--line2); stroke-width:1; stroke-dasharray:4 4; }}
  svg.plot .reflabel {{ fill:var(--faint); font-size:11px; font-family:"SF Mono",monospace; }}
  svg.plot .dot {{ fill:var(--bg); stroke:var(--line2); stroke-width:1.2; }}
  svg.plot .dot.measured {{ fill:var(--accent); stroke:var(--accent); opacity:.85; }}
  /* A plot point is small; the pointer wants a bigger target than the dot, and
     the tooltip wants to follow it. */
  svg.plot .dot {{ cursor:crosshair; }}
  svg.plot .dot:hover {{ stroke:var(--fg); stroke-width:1.6; }}
  .plotwrap {{ position:relative; }}
  .plot-tip {{
    position:absolute; pointer-events:none; z-index:20; max-width:330px;
    background:#0f0c0b; border:1px solid var(--line2); border-radius:8px;
    padding:9px 11px; font-size:12.5px; line-height:1.5; color:var(--muted);
    box-shadow:0 8px 22px rgba(0,0,0,.55);
  }}
  .plot-tip[hidden] {{ display:none; }}
  .plot-tip .t {{ color:var(--fg); font-weight:500; display:block; margin-bottom:5px; }}
  .plot-tip .r {{ display:flex; justify-content:space-between; gap:16px; }}
  .plot-tip .r b {{ color:var(--fg); font-weight:500; }}
  .dim {{ color:var(--muted); }} .ghost {{ color:var(--faint); }}
  tr.sub td {{ padding:9px 0 9px 20px; border-bottom:none; font-size:13px; }}
  tr.sub td:first-child {{ padding-left:22px; color:var(--faint); }}
  tr.sess td {{ border-top:1px solid var(--line); }}
  tr.sess:first-child td {{ border-top:none; }}
  .tick {{ display:inline-block; width:9px; height:1px; background:var(--line2);
           vertical-align:middle; margin-right:9px; }}
  tr.total td {{ border-top:1px solid var(--line2); border-bottom:none; padding-top:16px; font-weight:600; }}
  .cache {{ display:inline-flex; align-items:center; gap:9px; justify-content:flex-end; }}
  .cache i {{ display:block; width:42px; height:3px; background:var(--line); border-radius:2px; overflow:hidden; }}
  .cache i > b {{ display:block; height:100%; background:var(--good); }}
  .cache u {{ text-decoration:none; color:var(--good); font-weight:500; }}
  .role-src {{ display:block; font-family:"SF Mono",monospace; font-size:11.5px; color:var(--faint); margin-top:2px; }}
  /* The no-cache price lives on the hover, because it is the same rounds twice
     and a second column of near-doubled dollars reads as a second charge. */
  .tip {{ position:relative; cursor:default; border-bottom:1px dotted var(--line2); }}
  .tip-body {{
    position:absolute; right:0; bottom:calc(100% + 7px); white-space:nowrap;
    background:#0f0c0b; border:1px solid var(--line2); border-radius:7px;
    padding:7px 10px; font-size:12px; color:var(--muted); font-weight:400;
    opacity:0; pointer-events:none; transition:opacity .12s ease; z-index:9;
    box-shadow:0 6px 18px rgba(0,0,0,.5);
  }}
  .tip-body b {{ color:var(--accent); font-weight:600; margin-left:5px; }}
  .tip:hover .tip-body {{ opacity:1; }}
  tr.grp td {{ border-top:1px solid var(--line); font-weight:500; }}
  tr.gloss td {{ border-bottom:none; padding:2px 0 12px 22px; color:var(--faint);
                 font-size:12px; text-align:left; font-weight:400; white-space:normal; }}
  footer {{ margin-top:38px; padding-top:20px; border-top:1px solid var(--line);
            color:var(--faint); font-size:12.5px; line-height:1.7; }}
</style></head><body>
<header><h1>CuaCode usage</h1><div class="when">{when}</div></header>
<div class="cap">Cost</div>
<div class="figure num">${total:,.2f}</div>
<div class="aside">
  At peak rates: <b class="num">${at_peak:,.2f}</b>
  <span class="rule">|</span> Input served from cache: <b class="num">{cache_pct}%</b>
  <span class="rule">|</span> <b class="num">{trounds:,}</b> requests
</div>
<div class="sec"><h2>By session</h2>
  <p class="lede">{len(measured)} sessions with a cache reading{span_note}.
  Each one's own work, with the background calls it triggered underneath it.</p>
  <table><thead><tr><th>Session</th><th>Input</th><th>From cache</th><th>Fresh input</th>
  <th>Output</th><th>Requests</th><th>Cost</th></tr></thead><tbody>
{''.join(rows)}
      <tr class="total"><td>Total</td><td class="num">{tin:,}</td><td>{_bar(f"{cache_pct}%")}</td>
      <td class="num">{tin - tcache:,}</td><td class="num">{tout:,}</td>
      <td class="num ghost">{trounds:,}</td><td class="num">{_tip_total(total, tin, tcache, tout)}</td></tr>
  </tbody></table></div>
<div class="sec"><h2>Every session</h2>
  <p class="lede">{plot_note}</p>
  {plot}
  {worst_line}
</div>
<div class="sec"><h2>Every session, listed</h2>
  <p class="lede">{chart_note}</p>
  <table><thead><tr><th>Session</th><th>Date</th><th></th><th>Billed tokens</th>
  <th>Requests</th><th>Cost</th></tr></thead><tbody>
{chart_body}
  </tbody></table>
  <p class="note">A solid bar is a session whose cost is known. A hatched one has tokens
  and no cache reading, so its cost is not measured.</p></div>
<div class="sec"><h2>Models</h2>
  <p class="lede">Every model these sessions ran, side by side. A cache rate is
  computed only from the sessions that reported one, because a rate averaged over
  sessions that never reported would be a property of the records rather than of
  the model. The sessions column says how many of each said.</p>
  <table><thead><tr><th>Model</th><th>Sessions</th><th>Requests</th><th>Input</th>
  <th>Cache hit</th><th>Output</th><th>tok/s</th><th>Cost</th></tr></thead><tbody>
{model_rows}
  </tbody></table></div>
{role_section}
{legacy_block}
<footer>
  Notional pay-as-you-go value: what these tokens would list at. The account is on a flat plan,
  so nothing is charged per token.<br>
  Priced per round at the rate of the hour it ran: off-peak is outside 12:00 to 18:00 UTC on
  weekdays and all day on weekends{'. ' + offpeak_note + '.' if offpeak_note else '.'}<br>
  A session counts only once its provider reported a cache reading; before {boundary or 'that'} the
  records carry tokens and no cache figure.
</footer>
<script>
/* The plot's tooltip. An SVG <title> waits a second, cannot be styled, and cannot
   show a row of figures, so this reads the facts off the point and paints them.
   No framework: the page is one generated file and should stay openable offline. */
(function () {{
  var wrap = document.querySelector('.plotwrap');
  if (!wrap) return;
  var tip = wrap.querySelector('.plot-tip');
  var dots = wrap.querySelectorAll('.dot');
  function row(k, v) {{ return '<span class="r"><span>' + k + '</span><b>' + v + '</b></span>'; }}
  function show(ev, d) {{
    var usd = d.getAttribute('data-usd');
    var side = d.getAttribute('data-side');
    var reqs = d.getAttribute('data-reqs');
    tip.innerHTML =
      '<span class="t">' + d.getAttribute('data-t') + '</span>' +
      row('held at its widest', d.getAttribute('data-held') + ' tokens') +
      row('billed input', d.getAttribute('data-sent') + ' tokens') +
      row('times over', d.getAttribute('data-reps') + 'x') +
      row('requests', reqs + (side && side !== '0' ? ' (' + side + ' side)' : '')) +
      '<span class="r"><span>cost</span><b>' +
        (usd ? '$' + usd : (d.getAttribute('data-measured') === '1' ? '&mdash;' : 'not measured')) +
      '</b></span>';
    tip.hidden = false;
    place(ev);
  }}
  function place(ev) {{
    var box = wrap.getBoundingClientRect();
    var x = ev.clientX - box.left + 14;
    var y = ev.clientY - box.top + 14;
    /* Flip rather than overflow: near the right or bottom edge the tooltip would
       otherwise be cut off by the page, which reads as the tooltip being broken. */
    if (x + tip.offsetWidth > box.width) x = ev.clientX - box.left - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > box.height) y = ev.clientY - box.top - tip.offsetHeight - 14;
    tip.style.left = Math.max(0, x) + 'px';
    tip.style.top = Math.max(0, y) + 'px';
  }}
  dots.forEach(function (d) {{
    d.addEventListener('mouseenter', function (e) {{ show(e, d); }});
    d.addEventListener('mousemove', place);
    d.addEventListener('mouseleave', function () {{ tip.hidden = true; }});
  }});
}})();
</script></body></html>'''


def money_for(meta: dict, table: dict) -> float | None:
    """One session's cost, across every model it ran. None when no rate is known."""
    u = meta.get("usage") or {}
    total, seen = 0.0, False
    for key, spent in (u.get("models") or {}).items():
        name = key.split(" ", 1)[-1] if " " in key else key
        v = usd(name, spent, table)
        if v:
            total += v["raw"]
            seen = True
    return round(total, 2) if seen else None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
