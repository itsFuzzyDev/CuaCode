import os, re, socket, ssl, time
from tools.loader import load_tools, dispatch, refresh_dynamic
from tools._parser.ToProvider import to_provider
from handler.agent import compact, effort, images, interrupt, providers
from handler.agent.background import JOBS
from handler import peers

_tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools')
_registry = None

def registry():
    """Loaded once, since load_tools re-execs every tool's main.py. Refreshed
    each turn for the self-describing tools only, so an agent can use a subagent
    or skill file it just wrote. Unchanged entries come back byte-identical, so
    the prompt cache survives."""
    global _registry
    if _registry is None: _registry = load_tools(tools_dir=_tools_dir)
    return refresh_dynamic(_registry)

def _needs_vision(t) -> bool:
    """Read off the tool's declared output, not a name list, so a new
    image-returning tool is covered the day it lands."""
    return any("base64" in str(v) for v in (t.output_schema or {}).values())

def _visible(reg: dict, vision: bool) -> dict:
    """The toolbox this model can actually use. Offered a camera it will call
    it, and a rejected image costs the whole turn."""
    if vision: return {k: t for k, t in reg.items() if not t.blind_only}
    # Withheld for two different reasons: an image-returning tool fails the request outright,
    # a coordinate-taking one silently succeeds somewhere the model never looked, which is worse.
    return {k: t for k, t in reg.items() if not _needs_vision(t) and not t.needs_sight}

def _open(p, provider: str, model: str, messages: list, tools: list, system: str, params: dict):
    """The stream, plus its first Delta, with one retry for a rejected knob. stream() is a generator
    function, so priming with one Delta catches a bad parameter here rather than halfway down the loop.
    The retry drops only keys this process added, so an unrelated 400 still reaches the caller.
    """
    from handler import config           # imported late: config imports providers
    try:
        s = p.stream(model, messages, tools, system, params)
        return s, next(s, None)
    except Exception as e:
        # An image rejection is recorded, not retried: the image is already in the
        # history, so a retry sends it again. One failed turn ever, then vision off.
        if config.is_image_rejection(str(e)):
            config.learn_blind(provider, model)
            raise RuntimeError(
                f"{model} cannot accept images. Recorded, so the next turn will offer "
                f"describe_image instead of the screenshot tools.") from e
        learned = effort.learn(str(e), params)
        if learned is None: raise
        fixed, dropped = learned
        config.learn_quirk(provider, model, dropped)
        s = p.stream(model, messages, tools, system, fixed)
        return s, next(s, None)

# How many idle polls pass with nothing from the provider before the stream looks up from
# it and asks whether a peer has spoken. A long round is frame after frame, so this is what
# makes the mailbox noticed while a model is thinking, on the wait it was already sitting
# in. Sixteen polls of 20ms is well under the tenth of a second it takes to notice.
PEEK_EVERY = 16

def _opened(*a):
    """_open with its exception carried back as a value instead of raised. It has
    to run on another thread to be interruptible, and a thread cannot raise into
    its caller."""
    try: return "ok", _open(*a)
    except BaseException as e: return "err", e

# Retries for a request that never arrived: 1, 2, 4, 8 seconds, long enough for
# a wifi handover. Four, because a fifth is sixteen more seconds of spinner.
RETRIES = 4
RETRY_BASE = 1.0
RETRY_CAP = 30.0

# Matched as a substring of the exception class name: the providers share no
# hierarchy, so the name is all they have in common ("Timeout" covers several).
_TRANSIENT_TYPES = ("APIConnectionError", "Timeout", "ConnectError", "ReadError", "WriteError",
                    "RemoteProtocolError", "ProtocolError", "ChunkedEncodingError",
                    "IncompleteRead", "InternalServerError", "ServiceUnavailable",
                    "OverloadedError", "RateLimitError", "ResponseError")

# ...and by message text, for providers raising a bare Exception. Deliberately
# narrow: a false match costs fifteen seconds before the user is told.
_TRANSIENT_TEXT = ("connection", "connect error", "disconnected", "timed out",
                   "temporarily unavailable", "overloaded",
                   "network is unreachable", "name or service not known",
                   "nodename nor servname", "eof occurred", "bad gateway",
                   "service unavailable", "reset by peer", "broken pipe")

# Codes that mean "later" against ones that mean "never". A 401 is not going to
# fix itself, and four retries only delay saying so.
_TRANSIENT_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529}
_FINAL_CODES = {400, 401, 402, 403, 404, 405, 413, 422}

# The OSError subclasses that mean the network, not the disk: a FileNotFoundError
# from a provider is a bug, and retrying it hides the traceback saying so.
_NET_ERRORS = (ConnectionError, TimeoutError, socket.gaierror, socket.timeout, ssl.SSLError)


def _status_of(e) -> int:
    """The HTTP code behind an exception, or 0. Every SDK spells it differently
    and some only have it in the text, hence the regex."""
    for attr in ("status_code", "status", "http_status"):
        v = getattr(e, attr, None)
        if isinstance(v, bool): continue
        if isinstance(v, int) and 100 <= v < 600: return v
        if isinstance(v, str) and v.isdigit() and 100 <= int(v) < 600: return int(v)
    m = re.search(r"\b([45]\d\d)\b", str(e))
    return int(m.group(1)) if m else 0


# What a provider says when the prompt did not fit, beyond the 413 that spells it
# unambiguously. The openai dialect answers 400 with a context_length_exceeded body
# at least as often as it answers 413, and a fold is the only thing that recovers
# from either.
_OVERFLOW_WORDS = ("context length", "context_length", "maximum context", "context window",
                   "context size", "too many tokens", "prompt is too long",
                   "input is too long", "request too large", "exceeds the maximum")


def _overflow(e) -> bool:
    """Whether a refusal means the prompt did not fit the window.

    Was `_status_of(e) == 413`, which is the obvious spelling and not the common
    one: a provider that answers 400 here lands in _FINAL_CODES and is raised, so
    the fold that answers it never runs and a long conversation simply ends with
    an out-of-context error and nothing else.

    Text-matched on a 400/422 rather than on the code alone, and deliberately not
    429: several endpoints spend 429 on quota rather than on the request, and
    folding a conversation for a quota refusal throws away context to answer the
    wrong question.
    """
    code = _status_of(e)
    if code == 413: return True
    if code not in (400, 422): return False
    text = str(e).lower()
    return any(w in text for w in _OVERFLOW_WORDS)


def _transient(e) -> bool:
    """Whether the network is at fault rather than the request. A request never
    delivered costs nothing to send again; one the provider read and refused will
    be refused identically four more times."""
    code = _status_of(e)
    if code in _FINAL_CODES: return False
    if code in _TRANSIENT_CODES: return True
    names = [c.__name__ for c in type(e).__mro__]
    if any(t in n for n in names for t in _TRANSIENT_TYPES): return True
    if isinstance(e, _NET_ERRORS): return True
    text = str(e).lower()
    return any(t in text for t in _TRANSIENT_TEXT)


def _retry_after(e) -> float:
    """What the provider asked to be waited, when it asked: honouring a 429's own
    number beats guessing over it."""
    for attr in ("retry_after", "retry_delay"):
        v = getattr(e, attr, None)
        try:
            if v is not None: return max(0.0, min(float(v), RETRY_CAP))
        except (TypeError, ValueError): pass
    headers = getattr(getattr(e, "response", None), "headers", None)
    try:
        if headers is not None and (v := headers.get("retry-after")):
            return max(0.0, min(float(v), RETRY_CAP))
    except (TypeError, ValueError, AttributeError): pass
    return 0.0


def _backoff(attempt: int, e) -> float:
    """How long before try number attempt+1: doubling, capped, overridden by
    whatever the provider asked for."""
    return _retry_after(e) or min(RETRY_BASE * (2 ** attempt), RETRY_CAP)


def _wait(secs: float, stop) -> bool:
    """Sleep, watching for a cancel. False if the wait was called off: a backoff
    that cannot be stopped makes the app look hung for as long as it waits."""
    end = time.monotonic() + secs
    while True:
        if stop(): return False
        left = end - time.monotonic()
        if left <= 0: return True
        time.sleep(min(0.05, left))

def _preview(tool, args: dict, ctx) -> dict | None:
    """What the call about to be asked about would do, when the tool can say.
    Nothing that goes wrong here may matter: a preview that raises would turn a
    question into a failed call."""
    fn = getattr(tool, "preview", None)
    if not fn: return None
    try: return fn(args, ctx)
    except Exception: return None

def _needs_ask(tool, args: dict, ctx) -> bool:
    """Whether this call has to be put to the user, as opposed to this tool. require_permissions is
    per tool, which is too coarse: `file` reads far more often than it writes. safe(args, ctx) may
    narrow it, only downward. Fails closed: no hook, no answer and an exception all mean ask.
    """
    if not getattr(tool, "require_permissions", False): return False
    fn = getattr(tool, "safe", None)
    if not fn: return True
    try: return not fn(args, ctx)
    except Exception: return True

def _tail(messages, limit: int = 6) -> str:
    """The recent turns, rendered for a permission gate. Text is a string on some
    providers and a list of blocks on others, so extract rather than assume; a
    tool call collapses to its name."""
    def text_of(c):
        if isinstance(c, str): return c or ""
        if isinstance(c, list):
            out = []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text": out.append(b.get("text") or "")
                    elif b.get("type") == "tool_use":
                        out.append("[tool: %s]" % (b.get("name") or "?"))
                elif isinstance(b, str): out.append(b)
            return " ".join(x for x in out if x)
        return ""

    parts = []
    for m in messages[-limit:]:
        role = m.get("role")
        if role not in ("user", "assistant"): continue
        t = text_of(m.get("content"))
        if t: parts.append("%s: %s" % (role, t[:400]))
    return "\n\n".join(parts)[:3000]

def _call_fn(reg: dict, name: str, args: dict, ctx, token: interrupt.Token):
    """The dispatch, with everything bound now rather than when the thread gets
    to it: a detached call outlives the loop iteration that started it, so free
    variables read at run time would be the next call's."""
    cctx = interrupt.ctx_with(ctx, token)
    return lambda: dispatch(reg, name, args, ctx=cctx)

# How often a round reports the rate it is generating at: often enough to read
# as live, rarely enough that the readout is not itself a stream.
RATE_EVERY = 0.35

_BG_NOTE = ("running in the background -- this is not the result. Read it with "
            "background(action=\"output\", job=\"{job}\") once it finishes, or check "
            "background(action=\"list\") to see whether it has.")

def _bg_result(job) -> dict:
    b = job.brief()
    return {"result": {**b, "note": _BG_NOTE.format(job=job.id)}}

def _finished_note() -> str:
    """One line per job that ended since anyone last looked, or ""; injected as a
    user message so the model finds out at all. The result is not inlined: a
    finished build dropped into a round is the interruption backgrounding avoids.
    """
    done = JOBS.newly_finished()
    if not done: return ""
    lines = [f"- {j.id} ({j.name}) {j.state} after {j.elapsed}s" for j in done]
    return (f"{len(done)} background job{'s' if len(done) > 1 else ''} finished\n\n"
            "<background_jobs>\n"
            "These finished while you were working. Read one with\n"
            "background(action=\"output\", job=\"<id>\") when it is relevant to what you are\n"
            "doing; ignore it if it is not.\n"
            + "\n".join(lines) + "\n</background_jobs>")

# How many rounds a plan may sit untouched before the loop mentions it. Every
# `todo` call resets the count, so only a stalled plan is mentioned.
TODO_STALE = 4


def _compact_now(messages: list, used: int, limit: int, ctx=None) -> str:
    """Fold this conversation down to its target, in place. "" when nothing was folded.

    Two lists have to move together and only one of them is here. `messages` is the
    provider-shaped history generate() is about to send, and the caller holds a
    reference to it -- rebinding it would leave main.py sending the old one next
    turn. So it is rewritten in place with a slice assignment, and the records the
    session keeps are told separately, because the records are what the next reload
    is built from and they still hold the full original.

    The session is reached through ctx, not imported: this module is also the
    subagent loop, which runs for a caller with no session at all, and a subagent
    folding a conversation nobody owns would be writing to disk on behalf of a
    parent it cannot see.
    """
    from handler.agent import compact as c
    # Read off ctx, where the worker puts it, and fall back to the live session for
    # a call made outside a turn. Not the other way round: a test, a subagent or a
    # future frontend that starts its own loop has no global to inherit, and a fold
    # that quietly did nothing because of that would look like a fold that failed.
    sess = ctx.get("session") if hasattr(ctx, "get") else None
    if sess is None:
        try:
            from handler.session import naming
            sess = naming.live()
        except Exception:
            pass
    if sess is None: return ""
    records = sess.records()
    steps = c.plan(records, used=used, limit=limit)
    if not steps: return ""
    # A fold is a model call, and one takes as long as it takes: seconds on a long
    # middle. The frontend is told before the wait starts, because a screen that
    # just stops for no stated reason reads as a hang, and this is the one pause in
    # a turn where the worker is blocked on something the person cannot see.
    emit = ctx.get("emit") if hasattr(ctx, "get") else None
    if callable(emit):
        try: emit({"type": "compacting", "stage": "start", "folded": steps["folded"],
                   "freed": steps["freed"]})
        except Exception: pass
    try:
        got = c.summarize(records[steps["start"]:steps["end"]], steps["words"], ctx=ctx)
    finally:
        # Whatever happened, and especially if it raised: the frontend's state is not
        # allowed to be left saying "compacting" because a provider went away.
        if callable(emit):
            try: emit({"type": "compacting", "stage": "done"})
            except Exception: pass
    if "error" in got:
        # Reported, not raised: the conversation still works, it is just still long,
        # and the next round will try again with a slightly different figure.
        return f"could not compress: {got['error']}"
    steps["summary"] = got["summary"]
    # Rebuild the provider messages from the folded records rather than editing the
    # list: the fold removes assistant/tool groups, and the only thing that knows
    # how to turn a record list back into this dialect is replay.
    # Read before the fold: fold() slices `records` in place, so a length taken
    # after it is the count that went out, not the one that came in.
    before = len(records)
    sess.rewrite(c.fold(records, steps), fold=steps)
    messages[:] = sess.messages()
    # The transcript's mark. Says what it did, how much it saved, and -- because it
    # is the thing a person will want when a summary reads wrong -- that the
    # original is still on disk.
    saved = f"{steps['freed']:,} chars freed"
    after = len(records)
    return (f"compacted {before} records to {after} \u00b7 {saved} \u00b7 "
            f"the original is in folds/ on this session")

def _todo_note(ctx) -> str:
    """One line about a plan nobody has touched in a while, or "". The file is not in the prompt
    and the result that last held it has scrolled past, so the runtime says so. Never a reason
    to fail a round.
    """
    try:
        from tools.todo import state
        s = state.snapshot(ctx)
    except Exception:
        return ""
    if not s: return ""
    # The head is a row, so it is short enough to draw whole: the counts and the step
    # being worked on, cut at a word. The block below says it in full for the model;
    # a frontend draws only this line, because that is all a person needs from it.
    def cut(text) -> str:
        t = " ".join(str(text or "").split())
        return t if len(t) <= 40 else t[:40].rsplit(" ", 1)[0].rstrip(" ,;:-(") + "\u2026"
    where = (f"in progress: {cut(s['current'])}" if s["current"]
             else f"next: {cut(s['next'])}")
    head = f"todo: {s['done']}/{s['total']} done, {s['open']} open, {where}"
    return (f"{head}\n\n"
            "<todo_status>\n"
            f"{s['done']}/{s['total']} done, {s['open']} open, {where}.\n"
            "You have not touched the list in a while. Update it with the todo tool if you have\n"
            "moved on, or clear it if it no longer describes what you are doing.\n"
            "</todo_status>")

# A cancel closes the round off, not deletes it: every unanswered call gets a
# result saying why, which makes keeping it legal. The note rides the next message.
_INTERRUPTED = "the user interrupted the turn before this call ran"
_STOPPED = "the user interrupted the turn while this call was running"

# <tagged> like every runtime note: the tag is the boundary between runtime and
# conversation, and the first line is the sentence frontends draw.
INTERRUPT_NOTE = ("stopped -- the partial turn above was kept\n\n"
                  "<interrupted>\n"
                  "The user stopped you here. Everything above is what you had produced when they\n"
                  "did: a reply may break off mid-sentence, and any tool call answered with\n"
                  "\"interrupted\" never ran. Carry on from it rather than starting the turn over,\n"
                  "and read what they say next as a correction to it.\n"
                  "</interrupted>")

# Shaped like INTERRUPT_NOTE, round closed off rather than deleted: half a reply
# still carries on. Nobody chose this stop, so the model is told to continue.
LOST_NOTE = ("connection lost -- the partial turn above was kept\n\n"
             "<connection_lost>\n"
             "The connection to the provider dropped part-way through this turn. Everything\n"
             "above is what had arrived when it did, and the reply may break off mid-sentence.\n"
             "Carry on from there rather than starting the turn over.\n"
             "</connection_lost>")

def _peer_note(mail: list[dict]) -> str:
    """Another agent's words, marked as coming from one.

    Its own block rather than folded in with the notes a person wrote: the tag is
    the only thing telling the model these words are a peer's and not the
    operator's, and it has to survive being read back later.
    """
    if not mail: return ""
    return "\n\n".join("agent %s sent this while you were working\n\n%s"
                        % (r.get("from_name", "?"), peers.render(r)) for r in mail)

def _steer_note(typed: list[dict]) -> str:
    """The user's own words, arriving mid-turn. Tagged differently from the runtime notes: this
    is the only injected block a person wrote, and untagged it reads as an answer to the tool
    result above it. An attachment is named in the text and carried beside it.
    """
    parts = []
    for m in typed:
        said = m.get("text") or ""
        if names := [a.get("name") or "image" for a in (m.get("images") or [])]:
            attached = "[attached: " + ", ".join(names) + "]"
            said = said + "\n" + attached if said else attached
        parts.append(said)
    return ("the user sent this while you were working\n\n"
            "<user_message>\n" + "\n\n".join(parts) + "\n</user_message>")

def _closing_pairs(parsed: list, results: list, running=None) -> list:
    """The round's (call, result) pairs, with one invented for every call that never got a real
    one. Results are appended in call order, so from len(results) on is unanswered, and the
    running call is told apart from those never reached: it may have changed the machine.
    """
    return [(c, {"error": _STOPPED if c is running else _INTERRUPTED})
            for c in parsed[len(results):]]

def _span(start: float, end: float) -> float:
    """How long a phase lasted, or 0 for one that never happened."""
    return max(end - start, 0.0) if start and end else 0.0

def _rate(phase: str, think_chars: int, reply_chars: int,
          began: float, think_end: float, reply_end: float) -> dict:
    """The rate of the phase currently running, estimated from its characters.
    A live figure cannot be anything else, since the provider bills the round
    when it is over. Flagged as an estimate until the measured number lands."""
    from handler import context
    if phase == "thinking":
        chars, secs = think_chars, _span(began, think_end)
    else:
        chars, secs = reply_chars, _span(think_end or began, reply_end)
    tokens = context.tokens_for_chars(chars)
    return {"type": "rate", "phase": phase, "tokens": tokens, "secs": round(secs, 3),
            "tps": round(tokens / secs, 1) if secs >= 0.2 else 0.0}

def generate(API_KEY: str = None, ctx=None, messages: list[dict] = None, settings: dict = None,
             system="", cancelled=None, ask=None, auto=None, allow: list = None, extra: dict = None,
             provider_obj=None, detach=None, steer=None, peer_sid=None):
    """The agent loop for a whole turn, and the subagent loop. Yields typed chunks: round, thinking,
    content, tool_calls, model, usage, rate, assistant, tool_output, done, background, retry, cancelled,
    failed, notice, peer_waiting. mark is the round's rollback point; steer is asked once per round,
    after the results. peer_sid is the session whose mailbox the round seam drains, so a message from
    another agent is handed over as soon as a round ends rather than when the turn does.
    """
    from handler import config           # imported late: config imports providers
    settings = settings or {}
    # Default here, not in the signature: the list is appended to and truncated
    # in place, and a mutable default is shared across every call.
    if messages is None: messages = [{"role": "user", "content": "hey"}]
    provider = settings.get("provider", "ollama")
    p = provider_obj or providers.get(provider)
    model = settings.get("model") or p.default_model
    p.setup(API_KEY)
    # One canonical rung in, whatever this model understands out. Read per turn, so a
    # knob learned unsupported mid-run stays dropped and hand-written params win.
    params = effort.resolve(provider, p.name, model, settings.get("effort", ""),
                            settings.get("params"), drop=config.quirks(provider, model),
                            override=settings.get("effort_map"),
                            # What the endpoint says about this model: one that said
                            # it cannot think is sent no thinking parameter at all.
                            thinks=config.can_think(provider, model))
    # Default sighted when a provider does not declare it: an unexpected 400 is
    # obvious, whereas withheld cameras silently look like a stupid agent.
    vision = settings.get("vision", getattr(p, "vision", True))
    reg = _visible(registry(), vision)
    if allow is not None: reg = {k: t for k, t in reg.items() if k in allow}
    if extra: reg = {**reg, **extra}
    tools = to_provider(tools=reg, provider=p.name)
    stop = cancelled or (lambda: False)
    # Chunks a helper wanted sent but cannot yield itself: a function called from
    # inside this loop is not this generator, so it appends here and the loop drains
    # it. A fold is the only producer, because it is the only thing in a round that
    # blocks on a model the caller cannot see.
    outbox: list[dict] = []
    ctx = ctx if isinstance(ctx, dict) else {}
    ctx["emit"] = outbox.append
    # Rounds since the plan was last spoken to. Counted here, not in the tool: the
    # interesting number is how many rounds went by without a todo call.
    since_todo = 0
    # Rounds lost to a connection that dropped before producing anything: the one
    # failure that retries invisibly, so a per-round counter would retry forever.
    stream_retries = 0
    # The prompt size the provider last charged for, carried across rounds: `usage` is
    # cleared at the top of each one, which made a trigger read off it always zero.
    usage, measured = {}, 0
    # Whether a peer message is waiting. Asked on the idle poll for the whole round, because
    # a worker sits through a long tool call with nothing on stdout and the main loop that
    # would drain the mailbox is parked in here for all of it. Answered at the round seam
    # below, which is where a message is legal for every provider: waiting here only means the
    # person is told now rather than when the round happened to end.
    waiting = []
    def peek():
        nonlocal waiting
        if peer_sid and not waiting: waiting = peers.drain(peer_sid)

    def full_prompt() -> int:
        """What the window is actually full of: history plus everything re-sent beside it.

        The ceiling the provider enforces counts the system segment, the user's
        AGENTS.md, the tool schemas and the environment block as well as the
        history -- all of it goes up with every round. Measuring only `messages`
        put the trigger that far past the line, so a limit written near the window
        could never fire: the provider refused first, every time.
        """
        statics = system if isinstance(system, list) else ([system] if system else [])
        return (compact.tokens(messages) + compact.tokens(statics)
                + compact.tokens(tools or []))

    while True:
        # Rollback point for this round. A cancel rewinds to here: an assistant
        # message whose tool_calls have no results is rejected. Earlier rounds stay.
        mark = len(messages)
        yield {"type": "round"}
        thinking, content, calls, last = "", "", [], None
        # Carried before the clear: the provider's own count beats any estimate of ours.
        # "input", not "in": usage_of() names the whole prompt that way, and stamp()
        # is what renames it, one layer later. Read as "in" this was always zero, so
        # the provider's own count was discarded every round and the trigger had
        # nothing but the character estimate to decide on.
        measured = int((usage or {}).get("input") or 0) or measured
        # Per round, not per run: the count on the next request already includes
        # everything this one added, so old numbers would only be a stale round's.
        usage = {}
        # From asking to the last chunk, tool calls excluded: a round that spent four
        # minutes in a shell was not generating slowly. Pre-token wait is included.
        began = time.monotonic()
        # Told, not shown: the words go in at the round seam below, where a user message is
        # legal for every provider, and this is only the round that is still running saying
        # there is something to carry into the one after it.
        got_mail = []
        if waiting:
            got_mail, waiting = waiting, []
            for rec in got_mail:
                yield {"type": "peer_waiting", "from_peer": rec.get("from"),
                       "peer_name": rec.get("from_name"), "n": len(got_mail)}
        # Old screenshots out before the request is built: the whole history goes back up
        # every round, and a computer-use one is mostly base64.
        images.evict(messages, keep=int(settings.get("keep_images", images.DEFAULT_KEEP)))
        # Before the request, and only once per round: a conversation over its trigger
        # folds now rather than waiting for the provider to refuse it.
        limit = compact.trigger(settings)
        if limit:
            # The larger of the two: measured is exact but a round old, the estimate
            # covers what has been appended since. Both sides count the whole prompt,
            # so a count in config means the same thing on either side.
            used = max(measured, full_prompt())
            folded = _compact_now(messages, used, limit, ctx=ctx)
            if folded: measured = 0        # it described a history that no longer exists
            # Both halves of the fold go out before the notice: start, then done,
            # then what it bought.
            for ev in outbox: yield ev
            outbox.clear()
            if folded and "could not" not in folded:
                yield {"type": "notice", "text": folded, "compacted": True}
        # Opening the request is itself a wait, and cancelling abandons the socket
        # rather than waiting on a read nobody reads.
        attempt, stream, first = 0, None, None
        # Folds spent answering 413s this round. A conversation refused more times
        # than this is not going to fit, and folding further only destroys context
        # to prove it.
        MAX_FOLDS = 4
        folds = 0
        while True:
            state, opened = interrupt.run(lambda: _opened(p, provider, model, messages, tools, system, params),
                                          stop=stop)
            if state != "done":
                # Nothing was produced, so nothing to keep and nothing to tell the
                # model about. The delete is a no-op here and stands as the guarantee.
                del messages[mark:]
                yield {"type": "cancelled", "messages": messages}
                return
            kind, payload = opened
            if kind != "err":
                stream, first = payload
                break
            # The window ran out. This is the one refusal a fold can answer, so it is
            # answered rather than raised: before this, 413 was final and a long
            # conversation was simply over.
            #
            # Not gated on a configured window. A provider that refuses has just told
            # us the conversation is too long, which is better information than any
            # figure in config, and the point is that a model whose size nobody wrote
            # down still recovers.
            #
            # Repeated, not once. One fold cuts to a target derived from what is in
            # hand, and a provider's real window is not always that big: a conversation
            # refused at 90k that folds to 73k is still refused. So it folds again, as
            # long as each fold is still freeing something. MAX_FOLDS is the backstop
            # against a fold that frees nothing looping forever.
            if _overflow(payload) and folds < MAX_FOLDS:
                before = max(measured, full_prompt())
                if folds == 0:
                    yield {"type": "notice", "text": "over the window: compressing before retrying",
                           "compacted": True}
                # Half of what is in hand: this request was refused at this size, so a
                # fold that halves it is the smallest one that can plausibly clear the
                # window it was refused by.
                said = _compact_now(messages, used=before, limit=max(before // 2, 1), ctx=ctx)
                for ev in outbox: yield ev
                outbox.clear()
                folds += 1
                measured = 0
                after = full_prompt()
                # Progress is the only reason to go round again. A fold that frees
                # nothing would ask the same provider the same question forever.
                if after < before:
                    attempt = 0
                    continue
                # Nothing left to fold: fall through and let it be final.
            # What the provider read and refused is final: identical next time, and
            # the user is owed the reason now rather than after four backoffs.
            if attempt >= RETRIES or not _transient(payload): raise payload
            secs = _backoff(attempt, payload)
            attempt += 1
            # Said out loud: a silent retry and a hung app look identical from the
            # outside, and the watcher wants to know whether anything will happen.
            yield {"type": "retry", "attempt": attempt, "of": RETRIES,
                   "secs": round(secs, 1), "error": str(payload)}
            if not _wait(secs, stop):
                del messages[mark:]
                yield {"type": "cancelled", "messages": messages}
                return
        # Thinking and reply are clocked apart, thinking from request-open, so
        # together they answer where the round's seconds went.
        think_chars, reply_chars = 0, 0
        think_end, reply_end = 0.0, 0.0
        phase, tick = "", began + RATE_EVERY

        # The stream goes on its own thread and the caller polls it, so a cancel lands here
        # mid-read the way it lands on a tool, not only when the next chunk turns up. Stopped
        # is this turn being called off; anything else is still the link's problem, and
        # _transient decides whether it is one that is worth sending again.
        lost = None
        source = interrupt.stream(stream, stop=stop, first=first, peek=peek, every=PEEK_EVERY)
        try:
            for d in source:
                # Not a provider frame: something said while the loop waited, which is a peer
                # having spoken. Passed on and nothing else, since it is not part of the
                # round -- the round is what the model sent, and this is what somebody else
                # did while it was sending it.
                if isinstance(d, interrupt.Notice):
                    yield d.what
                    continue
                last = d.raw
                # Merged, not replaced: the two halves of the count arrive on
                # different frames, on anthropic at opposite ends of the stream.
                usage.update(providers.usage_of(d.raw))
                now = time.monotonic()
                if d.thinking:
                    yield {"type": "thinking", "text": d.thinking}
                    thinking += d.thinking
                    think_chars, think_end, phase = think_chars + len(d.thinking), now, "thinking"
                if d.content:
                    yield {"type": "content", "text": d.content}
                    content += d.content
                    reply_chars, reply_end, phase = reply_chars + len(d.content), now, "content"
                if d.tool_calls:
                    # Complete calls by contract: a provider whose arguments arrive
                    # as fragments assembles them before emitting a Delta.
                    yield {"type": "tool_calls", "text": d.tool_calls}
                    calls.extend(d.tool_calls)
                    reply_end, phase = now, "content"
                # A rate while it is still happening, estimated from characters
                # because that is all anyone has until the provider bills the round.
                if phase and now >= tick:
                    tick = now + RATE_EVERY
                    yield _rate(phase, think_chars, reply_chars, began, think_end, reply_end)
        except interrupt.Stopped:
            # The read was still waiting when the cancel arrived, and the socket shutdown
            # is what ended it. A cancelled round, not a lost one.
            lost = None
        except Exception as e:
            # Only a network failure is caught: a provider or loop bug swallowed into
            # "connection lost" hides behind the one explanation nobody investigates.
            if not _transient(e): raise
            lost = e
            try: stream.close()
            except Exception: pass
        finally:
            # Closed rather than left to the collector: the generator's cleanup is what shuts
            # the socket and reaps the producer thread, and an exception on the way out must
            # not be what decides whether that happens.
            source.close()
        # How long generating took, reported with the counts since the text is in hand
        # here. Measured before the cancel is acted on: a stopped round still cost.
        spent = {"secs": round(time.monotonic() - began, 3),
                 "thinking_chars": think_chars, "reply_chars": reply_chars,
                 "thinking_secs": round(_span(began, think_end), 3),
                 "reply_secs": round(_span(think_end or began, reply_end), 3)}

        if lost is not None:
            if thinking or content:
                # Kept, like an interrupted round: half a reply is still a reply. The calls
                # go, none ran, and anthropic rejects a tool_use turn without its thinking.
                if usage: yield {"type": "usage", "usage": dict(usage), "model": model, **spent}
                messages.append(p.assistant_message(thinking, content, []))
                yield {"type": "assistant", "thinking": thinking, "content": content, "tool_calls": []}
                yield {"type": "failed", "messages": messages, "note": LOST_NOTE,
                       "error": str(lost), "kept": True}
                return
            # Not a word arrived, so there is nothing to preserve and nothing for a
            # note to point at: the same failure as a request that never opened.
            del messages[mark:]
            if stream_retries >= RETRIES: raise lost
            secs = _backoff(stream_retries, lost)
            stream_retries += 1
            yield {"type": "retry", "attempt": stream_retries, "of": RETRIES,
                   "secs": round(secs, 1), "error": str(lost)}
            if not _wait(secs, stop):
                yield {"type": "cancelled", "messages": messages}
                return
            continue
        # The link is proven good, so the next drop starts its own count rather
        # than inheriting one from an outage two rounds ago.
        stream_retries = 0

        if stop():
            # Stopped mid-reply: what streamed is kept, half a plan is still the plan.
            # Calls drop, none ran, and anthropic needs the signed thinking behind them.
            if thinking or content:
                if usage: yield {"type": "usage", "usage": dict(usage), "model": model, **spent}
                messages.append(p.assistant_message(thinking, content, []))
                yield {"type": "assistant", "thinking": thinking, "content": content, "tool_calls": []}
                yield {"type": "cancelled", "messages": messages, "note": INTERRUPT_NOTE}
            else:
                # Not a word out of it yet, so nothing worth keeping: a note saying
                # "you were stopped here" with nothing above it is worse than silence.
                del messages[mark:]
                yield {"type": "cancelled", "messages": messages}
            return

        yield {"type": "model", "text": str(last)}
        # What the round cost, as soon as it is known rather than at the end of the
        # run: a tool loop can go on for minutes, and a stalled gauge is no gauge.
        if usage: yield {"type": "usage", "usage": dict(usage), "model": model, **spent}
        messages.append(p.assistant_message(thinking, content, calls))
        yield {"type": "assistant", "thinking": thinking, "content": content, "tool_calls": calls}

        if not calls:
            yield {"type": "done", "messages": messages, "usage": dict(usage), **spent}
            return

        parsed = p.parse_calls(calls)
        since_todo = 0 if any(c.name == "todo" for c in parsed) else since_todo + 1
        results = []

        def close_out(running=None):
            """End the turn here without leaving the round unusable. The assistant message holding
            these tool_calls cannot be taken back: it carries the signed thinking that produced
            the calls. Every tool_use gets an answer, so invented results go in beside the real ones.
            """
            extra = _closing_pairs(parsed, results, running)
            messages.extend(p.result_messages(results + extra))
            for c, r in extra:
                yield {"type": "tool_output", "name": c.name, "result": r}
            yield {"type": "cancelled", "messages": messages, "note": INTERRUPT_NOTE}

        for call in parsed:
            # Cheap pre-check: a cancel that arrived between calls should not pay for
            # starting one first. The call itself is watched from the thread below.
            if stop():
                yield from close_out()
                return
            # Whether to ask is the tool's own; a refusal reports as a failed call.
            # reg, not registry(): what was offered is what may be dispatched.
            tool = reg.get(call.name)
            # `background` rides in as an ordinary argument and comes back out before
            # the handler. Guessed onto a tool that never declared it, it is dropped.
            args = dict(call.args or {})
            wants_bg = bool(args.pop("background", False)) and getattr(tool, "backgroundable", False)
            token = interrupt.Token()
            fn = _call_fn(reg, call.name, args, ctx, token)
            # Gated three ways: the user (ask), a model that screens first (auto), or not
            # at all. Auto is a net, not a verdict: a flagged call still goes to the user.
            needed = tool is not None and _needs_ask(tool, args, ctx)
            denied = None
            if needed and auto is not None:
                allow, reason = auto(tool, call.name, args, _preview(tool, args, ctx),
                                     _tail(messages), ctx)
                if not allow:
                    if ask is not None and not ask(call.name, args, _preview(tool, args, ctx), reason):
                        denied = "denied by the user"
                    elif ask is None:
                        denied = "denied by the permission model: %s" % reason
            elif needed and ask is not None and not ask(call.name, args, _preview(tool, args, ctx)):
                denied = "denied by the user"
            if denied:
                result = {"error": denied}
            elif wants_bg:
                # Never waited on: the job starts and the round moves on. Permission
                # was still asked above, since out of sight is not out of scope.
                job = JOBS.start(call.name, args, fn)
                yield {"type": "background", "job": job.brief()}
                result = _bg_result(job)
            else:
                state, value = interrupt.run(fn, stop=stop, detach=detach, token=token)
                if state == "done":
                    result = value
                elif state == "cancelled":
                    # Stopped mid-flight: the result says so, it may have landed, and
                    # the token is already set, so watching tools stop.
                    yield from close_out(running=call)
                    return
                else:
                    # Detached: the same thread keeps running under a job id, and the
                    # model is told what it got instead of the answer.
                    job = JOBS.adopt(call.name, args, value)
                    yield {"type": "background", "job": job.brief()}
                    result = _bg_result(job)
            yield {"type": "tool_output", "name": call.name, "result": result}
            # Something sent to a peer is drawn like something received from one: the
            # buddy row, and a head saying who to whom. Without this a sent message
            # leaves no mark in the conversation it came from, and the only evidence it
            # happened is a tool result nobody reads twice.
            if call.name == "roster" and isinstance(result.get("result"), dict):
                for sent in (result["result"].get("sent") or []):
                    # No peer_in: this one went out, so the main loop must not record it
                    # as a message that arrived. The head says which way it went.
                    yield {"type": "peer", "text": "you to %s\n%s" % (sent.get("name", "?"), args.get("body", ""))}
            results.append((call, result))
            if call is not parsed[-1]: time.sleep(0.15)
        # Handed over as a round, not per call: anthropic and gemini require every
        # result from one round batched into a single message.
        messages.extend(p.result_messages(results))
        # Runtime notes land after the results, the only place a user message is legal
        # for every provider, and both in one message: two in a row is a 400.
        stale = _todo_note(ctx) if since_todo >= TODO_STALE else ""
        if stale: since_todo = 0
        # Anything typed while this round ran goes in first and in the same message: a
        # person wrote it, and two user messages in a row is the 400 this seam avoids.
        typed = (steer() or []) if steer else []
        said = _steer_note(typed) if typed else ""
        # Peer mail rides the same seam and the same message, for the same reason. The
        # collect has to happen here and not on the main loop: that loop is parked in
        # this function for the whole turn, so a message that arrives while a tool is
        # running waits for the turn to end unless the seam reads the mailbox itself.
        # Any round that ends hands the model what it got, so the words are not spoken
        # into this turn by force: this is a message read when the round is already over,
        # which is the same point a person's own typed words arrive at. Nothing is cut
        # short for it and nothing is re-run because of it.
        # Acked here and only here, once the mail is in the message above the ack, so a
        # turn that ends before that line hands it back rather than swallowing it. Its
        # own block, so a peer's words never read back as the person's.
        mail = got_mail or (peers.drain(peer_sid) if peer_sid else [])
        if mail: peers.ack([r.get("id") for r in mail])
        told = _peer_note(mail) if mail else ""
        runtime = "\n\n".join(n for n in (_finished_note(), stale) if n)
        note = "\n\n".join(n for n in (said, told, runtime) if n)
        # Steered attachments ride the same message through the adapter, and are
        # dropped for a blind model: the names stay in the note, beating a 400.
        steer_images = [a["b64"] for m in typed for a in (m.get("images") or [])] if vision else []
        if note:
            messages.append(p.user_message(note, steer_images) if steer_images
                            else {"role": "user", "content": note})
            # text is the conversation and the record; show is what the frontend draws,
            # only the runtime half, since it already printed the user's own line.
            # A peer's message is neither: its head goes to the frontend as its own
            # notice, so another agent's words are not drawn as the person's.
            yield {"type": "notice", "text": note,
                   "show": "\n\n".join(n for n in (runtime, told) if n),
                   "from_user": bool(said),
                   # Named on the record for the same reason as in the note: a reopened
                   # conversation has to say a picture was sent here.
                   "images": [a for m in typed for a in (m.get("images") or [])] if steer_images else []}
            for rec in mail:
                # Its own state, not `notice`: the frontend gives it the buddy mark and
                # the peer colour, so a peer's message is not read as a note about this
                # machine. Head first, body after a newline, which is the shape the
                # transcript folds into a row that opens.
                yield {"type": "peer", "text": f"{peers.head(rec)}\n{rec.get('body', '')}",
                       "show": peers.head(rec), "peer_in": True,
                       "from_peer": rec.get("from"), "peer_name": rec.get("from_name")}
