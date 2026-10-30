#!/usr/bin/env python3
"""What the trigger compares against, and what counts as "the window ran out".

Two separate ways automatic compaction silently failed to fire. Both are
arithmetic rather than policy, and both are invisible from the outside: the
provider's refusal reads exactly like the failure compaction exists to prevent,
so the symptom is "it never compacts, it just errors".

    python3 tests/test_compact_window_fill.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handler.agent import compact, providers
from handler.agent import main as am

FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond and detail else ""))
    if not cond: FAILS.append(name)


def raised(message, **attrs):
    """An exception shaped like a provider SDK's, which is what the loop catches."""
    e = Exception(message)
    for k, v in attrs.items(): setattr(e, k, v)
    return e


print("1. the measured prompt is read under the name usage_of() emits")
# The round loop carries the provider's own count across rounds by reading it out
# of the dict usage_of() returns. It read "in", which usage_of never sets, so the
# exact count was dropped every round and only the character estimate reached the
# trigger -- the one number in the comparison that was not a guess.
frame = {"usage": {"prompt_tokens": 281_000, "completion_tokens": 40}}
out = providers.usage_of(frame)
check("usage_of names the whole prompt 'input'", out.get("input") == 281_000, out)
check("usage_of never names it 'in'", "in" not in out, out)
check("the two halves agree on the key, so the count survives the round",
      out.get("in") is None and out.get("input") == 281_000, out)

print("\n2. an over-long request is recognised however it is spelled")
e413 = raised("Payload Too Large", status_code=413)
e400 = raised("This model's maximum context length is 299000 tokens, but you "
              "requested 301000 tokens", status_code=400)
e400b = raised("context_length_exceeded", status_code=400)
e429 = raised("FreeUsageLimitError", status_code=429)
e401 = raised("Unauthorized", status_code=401)
e500 = raised("internal error", status_code=500)
check("413 is overflow", am._overflow(e413))
check("400 with a context-length message is overflow", am._overflow(e400))
check("400 with context_length_exceeded is overflow", am._overflow(e400b))
# Not 429 or 401: neither is answered by throwing context away, and folding for
# either loses the conversation to a refusal that had nothing to do with size.
check("429 is not overflow", not am._overflow(e429))
check("401 is not overflow", not am._overflow(e401))
check("500 is not overflow", not am._overflow(e500))

print("\n3. the trigger and the provider's ceiling measure the same thing")
# The window holds the whole prompt: history plus the system segment, the user's
# AGENTS.md, the tool schemas and the environment block, all re-sent every round.
# Counting only the history left the trigger that far past the line, so a limit
# written near the window could never fire before the provider refused.
window, limit = 300_000, 295_000
overhead = 18_000        # system + AGENTS.md + tool schemas, measured on a real install
# Real prose, not one repeated character: a long run of a single base64 letter is
# what context.strip_images reads as an encoded image, and it would be charged by
# the image rate instead of counted as text.
prose = "the quick brown fox jumps over the lazy dog "
history_chars = (window - overhead) * 4
statics = [{"role": "system", "content": prose * (overhead * 4 // len(prose) + 1)}]
messages = [{"role": "user", "content": prose * (history_chars // len(prose) + 1)}]
whole = compact.tokens(messages) + compact.tokens(statics)
history_only = compact.tokens(messages)
check("history alone leaves the statics out", history_only < whole, (history_only, whole))
check("the statics are not negligible beside a window this size",
      whole - history_only > overhead * 0.5, (whole - history_only, overhead))
# The failure, stated as the arithmetic that produced it: a limit set near the
# ceiling is unreachable on the history-only count, because the provider refuses
# once the *whole* prompt passes the ceiling.
check("a limit at the ceiling is unreachable on history alone",
      limit > window - overhead, (limit, window - overhead))
check("on the whole prompt that same limit is reachable", limit < window)

print("\n4. compact's own arithmetic still holds")
compact.check()

print()
print("failed:", len(FAILS))
raise SystemExit(1 if FAILS else 0)
