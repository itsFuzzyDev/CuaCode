import itertools, os, time
from tools.loader import load_tools, dispatch, refresh_dynamic
from tools._parser.ToProvider import to_provider
from handler.agent import effort, images, interrupt, providers
from handler.agent.background import JOBS

_tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools')
_registry = None

def registry():
    """Loaded once. load_tools re-execs every tool's main.py, which is real
    work to repeat on every turn.

    Refreshed once per turn all the same, but only the self-describing tools:
    an agent that writes a subagent, workflow or skill file has to be able to
    use it in the same conversation, and everything else on disk is fixed for
    the life of the process. When nothing has changed the strings come back
    byte-identical, so the provider's prompt cache is untouched.
    """
    global _registry
    if _registry is None: _registry = load_tools(tools_dir=_tools_dir)
    return refresh_dynamic(_registry)

def _needs_vision(t) -> bool:
    """Read off the tool's declared output rather than a hardcoded name list,
    so a new image-returning tool is covered the day it lands."""
    return any("base64" in str(v) for v in (t.output_schema or {}).values())

def _visible(reg: dict, vision: bool) -> dict:
    """The toolbox this model can actually use.

    Never hand a camera to a model that cannot see: offered the tool it will
    call it, and the endpoint rejects the image with a 400 that costs the whole
    turn. The reverse holds too -- a tool that exists to describe an image to a
    model with no eyes is noise in front of a model that has them, and an
    invitation to delegate something it could just look at.
    """
    if vision: return {k: t for k, t in reg.items() if not t.blind_only}
    # Two reasons to withhold, and they are not the same reason. A tool that
    # returns an image would fail the request outright. A tool that takes a
    # coordinate would succeed -- at whatever it happened to land on, chosen by
    # a model that could not look first. The second is the more dangerous of
    # the two, because nothing reports it as an error.
    return {k: t for k, t in reg.items() if not _needs_vision(t) and not t.needs_sight}

def _open(p, provider: str, model: str, messages: list, tools: list, system: str, params: dict):
    """The stream, plus its first Delta, with one retry for a rejected knob.

    Priming matters: stream() is a generator function, so calling it runs none
    of the body and the request -- along with any 400 it earns -- only happens
    once something pulls. Pull one Delta here and a bad parameter is catchable
    rather than exploding halfway down the consuming loop.

    The retry is what makes the effort setting safe to point at a model nobody
    has mapped: the rejected key is dropped, remembered on disk, and the turn
    goes through anyway. Only keys this process added are ever stripped, so an
    unrelated 400 still reaches the caller.
    """
    from handler import config           # imported late: config imports providers
    try:
        s = p.stream(model, messages, tools, system, params)
        return s, next(s, None)
    except Exception as e:
        # A model refusing images is not a knob to drop and retry -- the image
        # is already in the history and would be sent again. Record it instead,
        # so this costs one failed turn ever: the next turn is built with vision
        # off, which withholds the cameras and offers describe_image in their
        # place. The turn still fails, with a sentence that says what happened.
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

def _opened(*a):
    """_open with its exception carried back as a value instead of raised.

    It has to run on another thread to be interruptible, and a thread cannot
    raise into its caller. The distinction _open draws between errors -- a
    dropped knob it retries, an image rejection it records, anything else it
    lets through -- all happens inside, so what escapes here is already final
    and only needs putting back where the caller can see it.
    """
    try: return "ok", _open(*a)
    except BaseException as e: return "err", e

def _preview(tool, args: dict, ctx) -> dict | None:
    """What the call about to be asked about would do, when the tool can say.

    Never allowed to matter: a preview that raises would turn a question into a
    failed call, and the dialog it feeds is perfectly readable without one. So
    anything that goes wrong here comes back as nothing to show.
    """
    fn = getattr(tool, "preview", None)
    if not fn: return None
    try: return fn(args, ctx)
    except Exception: return None

def _needs_ask(tool, args: dict, ctx) -> bool:
    """Whether this call has to be put to the user, as opposed to this tool.

    require_permissions is declared per tool, and per tool is too coarse for
    most of the tools that declare it: `file` reads far more often than it
    writes, and the overwhelming majority of shell commands only look at the
    machine. Asking about every one of them teaches the user to approve without
    reading, which costs more than it protects -- the prompt that matters is
    the one that arrives rarely enough to still be read.

    So a tool may define safe(args, ctx) and answer for the specific call. It
    is a narrowing only: a tool without require_permissions is never asked
    about either way, and safe() can only take away a prompt the tool itself
    asked for.

    Fails closed in every direction. No hook, an unreadable answer, an
    exception -- all of them mean ask, because the cost of a needless prompt is
    a click and the cost of a missed one is whatever the call did.
    """
    if not getattr(tool, "require_permissions", False): return False
    fn = getattr(tool, "safe", None)
    if not fn: return True
    try: return not fn(args, ctx)
    except Exception: return True

def _call_fn(reg: dict, name: str, args: dict, ctx, token: interrupt.Token):
    """The dispatch, with everything bound now rather than when the thread gets
    around to it.

    A detached call outlives the loop iteration that started it, so free
    variables read at run time would be the *next* call's name and arguments.
    """
    cctx = interrupt.ctx_with(ctx, token)
    return lambda: dispatch(reg, name, args, ctx=cctx)

# How often a round reports the rate it is generating at. Often enough to read
# as live, rarely enough that the readout is not itself a stream: the number is
# derived from characters, and a figure recomputed every token would jitter far
# more than the thing it is measuring.
RATE_EVERY = 0.35

_BG_NOTE = ("running in the background -- this is not the result. Read it with "
            "background(action=\"output\", job=\"{job}\") once it finishes, or check "
            "background(action=\"list\") to see whether it has.")

def _bg_result(job) -> dict:
    b = job.brief()
    return {"result": {**b, "note": _BG_NOTE.format(job=job.id)}}

def _finished_note() -> str:
    """One line per job that ended since anyone last looked, or "".

    Injected as a user message so the model finds out at all: it has no reason
    to poll a registry it cannot see, and a job whose result is never read was
    never worth backgrounding. The result itself is deliberately not inlined --
    a finished build dropped into the middle of a round is the interruption
    backgrounding was meant to avoid.
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

# How many rounds a plan may sit untouched before the loop mentions it. Low
# enough that a forgotten list is caught inside a single detour, high enough that
# an agent working steadily through one step never sees it: every `todo` call
# resets the count, so the note only ever reaches an agent that has stopped
# talking to its own plan.
TODO_STALE = 4

def _todo_note(ctx) -> str:
    """One line about a plan nobody has touched in a while, or "".

    The list is worth nothing if the agent forgets it exists, and it has no way
    to be reminded on its own -- the file is not in the prompt and the tool
    result that last held it has scrolled past. So the runtime says so, in the
    same place and the same voice it reports a finished background job: a note
    about state, not an instruction, and never a reason to fail a round.
    """
    try:
        from tools.todo import state
        s = state.snapshot(ctx)
    except Exception:
        return ""
    if not s: return ""
    where = f"in progress: {s['current']}" if s["current"] else f"next: {s['next']}"
    return (f"todo: {s['done']}/{s['total']} done, {s['open']} open, {where}\n\n"
            "<todo_status>\n"
            f"{s['done']}/{s['total']} done, {s['open']} open, {where}.\n"
            "You have not touched the list in a while. Update it with the todo tool if you have\n"
            "moved on, or clear it if it no longer describes what you are doing.\n"
            "</todo_status>")

# What a cancel leaves behind, and how the model is told about it.
#
# The round used to be deleted -- every message from the start of it, thinking,
# reply and all -- because an assistant turn whose tool_calls have no matching
# results is rejected on the next request, and deleting was the cheap way to be
# sure none were left dangling. The cost was that the model came back with no
# idea it had ever been stopped, and no sight of the half-finished plan the user
# interrupted it to correct: the very thing they were about to talk about.
#
# So the round is closed off instead of removed. Every call that never got a
# result gets one saying why, which is what makes keeping it legal, and the note
# below is folded into the user's next message -- never sent as one of its own,
# because two user messages in a row is a 400 on anthropic.
_INTERRUPTED = "the user interrupted the turn before this call ran"
_STOPPED = "the user interrupted the turn while this call was running"

# Tagged like everything else the runtime puts in front of the model --
# <recall>, <environment>, <user_instructions> -- rather than prefixed with a
# bracket. The tag is the boundary: it says where the runtime stops talking and
# the conversation starts again, which a label at the head of a paragraph cannot.
# First line is a sentence for the human, because frontends draw a notice's
# opening paragraph and drop the rest.
INTERRUPT_NOTE = ("stopped -- the partial turn above was kept\n\n"
                  "<interrupted>\n"
                  "The user stopped you here. Everything above is what you had produced when they\n"
                  "did: a reply may break off mid-sentence, and any tool call answered with\n"
                  "\"interrupted\" never ran. Carry on from it rather than starting the turn over,\n"
                  "and read what they say next as a correction to it.\n"
                  "</interrupted>")

def _steer_note(typed: list[dict]) -> str:
    """The user's own words, arriving mid-turn.

    Tagged for the same reason the others are, and tagged *differently* for one
    that matters more: this is the only injected block that is not the runtime
    speaking. Untagged it reads as an answer to the tool result above it, which
    is exactly what it is not.

    An attachment is named in the text and carried beside it: the pictures ride
    on the same message (see the caller), and a model reading a bare tag with
    two images under it has no way to tell which of them the words are about.
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
    """The round's (call, result) pairs, with one invented for every call that
    never got a real one.

    Results are appended in call order, so everything from len(results) on is
    unanswered: the call that was running when the key was pressed, if there was
    one, and behind it the calls that had not been reached yet. They are told
    apart because they are different facts -- one may have changed something on
    the machine before it was cut off, and the others certainly did not.
    """
    return [(c, {"error": _STOPPED if c is running else _INTERRUPTED})
            for c in parsed[len(results):]]

def _span(start: float, end: float) -> float:
    """How long a phase lasted, or 0 for one that never happened."""
    return max(end - start, 0.0) if start and end else 0.0

def _rate(phase: str, think_chars: int, reply_chars: int,
          began: float, think_end: float, reply_end: float) -> dict:
    """The rate of the phase currently running, estimated from its characters.

    A live figure cannot be anything else: the provider bills the round when the
    round is over, and a spinner that says nothing until then is exactly the
    minute nobody can account for. It is flagged as an estimate the whole way to
    the screen, and the measured number takes its place as soon as it lands.
    """
    from handler import context
    if phase == "thinking":
        chars, secs = think_chars, _span(began, think_end)
    else:
        chars, secs = reply_chars, _span(think_end or began, reply_end)
    tokens = context.tokens_for_chars(chars)
    return {"type": "rate", "phase": phase, "tokens": tokens, "secs": round(secs, 3),
            "tps": round(tokens / secs, 1) if secs >= 0.2 else 0.0}

def generate(API_KEY: str = None, ctx=None, messages: list[dict] = None, settings: dict = None,
             system="", cancelled=None, ask=None, allow: list = None, extra: dict = None,
             provider_obj=None, detach=None, steer=None):
    """The agent loop. Also the subagent loop -- the last three arguments are
    the whole difference.

    system is one string or a list of them. A list stays a list all the way to
    the provider, which decides what a segment is: a separate system message on
    ollama and openai, a separate system block on anthropic, which has no
    system role to put a message in.

    allow  names the tools this run may call, or None for everything the model
           can see. An empty list is a real answer, not a missing one: it means
           no tools at all, which turns the loop into a single completion.
    extra  is tools built at call time rather than loaded from disk, merged in
           after the filter so they cannot be filtered out. A schema-forcing
           submit_result is one; nothing about it needs to be special-cased,
           because a Tool assembled in memory is a Tool.
    provider_obj takes a provider instance instead of the shared singleton.
           Those singletons carry per-turn state -- Anthropic._thinking is
           handed from stream() to assistant_message() on the instance -- so
           two runs sharing one would trade thinking blocks. Nothing runs
           concurrently yet; the seam is here so that when it does, this is
           already the place it plugs into.
    detach is a threading.Event the frontend sets to push the call currently
           running into the background. An Event rather than a predicate
           because it is consumed: interrupt.run clears it, so one press moves
           one call. Subagents pass None -- there is no user watching a nested
           run to press anything.
    steer  is asked for whatever the user typed while this run was going, and
           is asked at one place only: after a round's tool results, which is
           the single point a user message is legal for every provider. It
           returns a list and clears itself, so a message is spoken once. None
           for subagents, for the same reason detach is.

    cancelled is read everywhere something is in flight, not only between
    steps: the request while it is opening, each streamed chunk, between tool
    calls, and -- through a thread -- during one. A stop that only lands at
    boundaries is not a stop, because the thing anyone wants to stop is the
    ninety-second call, and that is precisely the part with no boundaries in
    it.
    """
    from handler import config           # imported late: config imports providers
    settings = settings or {}
    # Defaults live here, not in the signature: this list is appended to and
    # truncated in place, and a mutable default is shared across every call.
    if messages is None: messages = [{"role": "user", "content": "hey"}]
    provider = settings.get("provider", "ollama")
    p = provider_obj or providers.get(provider)
    model = settings.get("model") or p.default_model
    p.setup(API_KEY)
    # One canonical rung in, whatever this model actually understands out. Read
    # per turn rather than per run so a knob learned to be unsupported mid-run
    # stays dropped, and so hand-written params keep winning over the table.
    params = effort.resolve(provider, p.name, model, settings.get("effort", ""),
                            settings.get("params"), drop=config.quirks(provider, model),
                            override=settings.get("effort_map"),
                            # What the endpoint says about this model, where it
                            # says anything: a model that has told us it cannot
                            # think is sent no thinking parameter at all, rather
                            # than the one the table guessed its family takes.
                            thinks=config.can_think(provider, model))
    # Default sighted when a provider does not declare it: an unexpected 400
    # is obvious, whereas silently withholding the cameras from a capable
    # model just looks like the agent got stupid.
    vision = settings.get("vision", getattr(p, "vision", True))
    reg = _visible(registry(), vision)
    if allow is not None: reg = {k: t for k, t in reg.items() if k in allow}
    if extra: reg = {**reg, **extra}
    tools = to_provider(tools=reg, provider=p.name)
    stop = cancelled or (lambda: False)
    # Rounds since the plan was last spoken to. Counted here rather than in the
    # tool because the tool only hears about the calls it receives, and the
    # interesting number is how many rounds went by without one.
    since_todo = 0

    while True:
        # Rollback point for this round. A cancel has to rewind to here: an
        # assistant message whose tool_calls have no matching results is
        # rejected on the next request. Completed earlier rounds are kept.
        mark = len(messages)
        yield {"type": "round"}
        thinking, content, calls, last = "", "", [], None
        # Per round, not per run: the prompt count reported on the next request
        # already includes everything this one added, so keeping the old numbers
        # around would only ever mean reporting a stale round's.
        usage = {}
        # From asking to the last chunk, tool calls excluded. That is the span a
        # rate belongs to: a round that spent four minutes in a shell call did
        # not generate slowly, and dividing by the whole round would say it did.
        # The wait before the first token is inside it on purpose -- it is time
        # the user spent watching a spinner, and a rate that hid it would flatter
        # a reasoning model that thought for a minute and then typed fast.
        began = time.monotonic()
        # Old screenshots out before the request is built, not after the reply.
        # This is the single biggest thing between a tool finishing and the next
        # token arriving: the whole transcript goes back up every round, and a
        # computer-use transcript is mostly base64. See handler/agent/images.py.
        images.evict(messages, keep=int(settings.get("keep_images", images.DEFAULT_KEEP)))
        # Opening the request is itself a wait -- a reasoning model can sit on
        # the connection for a minute before the first token, and that minute
        # used to be unstoppable. Cancelling here abandons the socket to the
        # daemon thread rather than waiting on a read nobody is reading.
        state, opened = interrupt.run(lambda: _opened(p, provider, model, messages, tools, system, params),
                                      stop=stop)
        if state != "done":
            # Nothing was produced -- the request had not even been answered --
            # so there is nothing to keep and nothing to tell the model about.
            # The delete is a no-op at this point and stands as the guarantee.
            del messages[mark:]
            yield {"type": "cancelled", "messages": messages}
            return
        kind, payload = opened
        if kind == "err": raise payload
        stream, first = payload
        # The two halves of a reply, clocked apart. Thinking's clock starts when
        # the request was opened -- the silence before the first thought is the
        # model thinking about thinking, and charging it to the reply would
        # flatter a model that stalls and then types fast -- and the reply's
        # starts where the thinking stopped. Between them they account for the
        # whole round, which is the point: "where did the ninety seconds go" is
        # the question, and one number for the round cannot answer it.
        think_chars, reply_chars = 0, 0
        think_end, reply_end = 0.0, 0.0
        phase, tick = "", began + RATE_EVERY

        # The primed Delta is put back in front. stream itself stays the
        # generator, so a cancel below still has something to close().
        for d in itertools.chain([first] if first is not None else [], stream):
            if stop():
                stream.close()
                break
            last = d.raw
            # Merged rather than replaced: the two halves of the count arrive on
            # different frames, and on anthropic they arrive at opposite ends of
            # the stream.
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
                # Complete calls by contract: providers whose arguments arrive
                # as JSON fragments assemble them before emitting a Delta.
                yield {"type": "tool_calls", "text": d.tool_calls}
                calls.extend(d.tool_calls)
                reply_end, phase = now, "content"
            # A rate while it is still happening, estimated from characters
            # because that is all anyone has until the provider bills the round.
            # Marked as an estimate all the way out, and replaced by the measured
            # figure the moment there is one.
            if phase and now >= tick:
                tick = now + RATE_EVERY
                yield _rate(phase, think_chars, reply_chars, began, think_end, reply_end)
        # How long the generating took, split the same way the live rate splits
        # it, and reported with the counts rather than derived from them later:
        # the text is in hand here, and nowhere downstream sees the clock.
        # Measured before the cancel is acted on, because a stopped round still
        # cost what it cost and the numbers are only in hand here.
        spent = {"secs": round(time.monotonic() - began, 3),
                 "thinking_chars": think_chars, "reply_chars": reply_chars,
                 "thinking_secs": round(_span(began, think_end), 3),
                 "reply_secs": round(_span(think_end or began, reply_end), 3)}

        if stop():
            # Stopped part-way through the reply. What streamed is kept: half a
            # plan is still the plan the user is about to talk about, and
            # throwing it away is what used to make the correction start from
            # nothing.
            #
            # The calls are dropped, though, and they are the one thing that
            # cannot be kept. None of them ran, so nothing is lost by asking for
            # them again -- and on anthropic a tool_use turn has to carry the
            # signed thinking blocks that produced it, which a cut-off stream
            # never finished handing over. An assistant turn holding calls it
            # cannot prove it thought about is rejected outright.
            if thinking or content:
                if usage: yield {"type": "usage", "usage": dict(usage), "model": model, **spent}
                messages.append(p.assistant_message(thinking, content, []))
                yield {"type": "assistant", "thinking": thinking, "content": content, "tool_calls": []}
                yield {"type": "cancelled", "messages": messages, "note": INTERRUPT_NOTE}
            else:
                # Not a word out of it yet. Nothing worth keeping, and a note
                # saying "you were stopped here" with nothing above it to point
                # at is worse than silence.
                del messages[mark:]
                yield {"type": "cancelled", "messages": messages}
            return

        yield {"type": "model", "text": str(last)}
        # What the round cost, as soon as it is known rather than at the end of
        # the run: a tool loop can go on for minutes, and a context readout that
        # only moves when the agent stops answering is not a gauge.
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
            """End the turn here without leaving the round unusable.

            The assistant message holding these tool_calls is already in the
            list and cannot be taken back -- it carries the signed thinking that
            produced the calls, and on anthropic that is not reconstructible.
            What makes keeping it legal is that every tool_use gets an answer,
            so the invented results go in beside the real ones and the round is
            as complete as any other. The calls that already ran were reported
            as they finished; only the invented ones are announced here.
            """
            extra = _closing_pairs(parsed, results, running)
            messages.extend(p.result_messages(results + extra))
            for c, r in extra:
                yield {"type": "tool_output", "name": c.name, "result": r}
            yield {"type": "cancelled", "messages": messages, "note": INTERRUPT_NOTE}

        for call in parsed:
            # Cheap pre-check. The call itself is watched too, from the thread
            # below, but a cancel that arrived between calls should not pay for
            # starting one first.
            if stop():
                yield from close_out()
                return
            # Which calls need asking is the tool's own decision, declared as
            # require_permissions in its Description.md and narrowed per call by
            # its own safe() -- see _needs_ask. Asked here for the same
            # reason cancel is checked here: between calls is the last moment
            # anything can be stopped, because a click that has already fired
            # cannot be taken back. A refusal is reported like any other failed
            # call rather than by skipping the yield -- the assistant message
            # already holds these tool_calls, and a call left without a result
            # is rejected on the next request.
            # reg, not registry(): what was offered is what may be dispatched.
            # A name outside it comes back as an unknown tool rather than being
            # executed, which is what makes `allow` a restriction and not a
            # suggestion, and is also how a synthetic tool from `extra` is
            # reachable at all.
            tool = reg.get(call.name)
            # `background` is injected into the schema of any tool declaring
            # itself backgroundable, so it arrives as an ordinary argument and
            # has to come back out before the handler -- which knows nothing
            # about any of this -- is handed the rest. Asked for on a tool that
            # did not declare it, it is dropped: a model guessing the flag onto
            # a click should get the click, not a job id.
            args = dict(call.args or {})
            wants_bg = bool(args.pop("background", False)) and getattr(tool, "backgroundable", False)
            token = interrupt.Token()
            fn = _call_fn(reg, call.name, args, ctx, token)
            if ask is not None and tool is not None and _needs_ask(tool, args, ctx) \
                    and not ask(call.name, args, _preview(tool, args, ctx)):
                result = {"error": "denied by the user"}
            elif wants_bg:
                # Never asked, never waited on: the job starts and the round
                # moves on. Permission is still asked above first, because a
                # call the user would have refused is no more acceptable for
                # being out of sight.
                job = JOBS.start(call.name, args, fn)
                yield {"type": "background", "job": job.brief()}
                result = _bg_result(job)
            else:
                state, value = interrupt.run(fn, stop=stop, detach=detach, token=token)
                if state == "done":
                    result = value
                elif state == "cancelled":
                    # This call gets a result saying it was stopped mid-flight,
                    # which is the truth and is not the same as the ones behind
                    # it that never started: a half-finished click cannot be
                    # taken back, and the model has to know it may have landed.
                    # The token was set on the way here, so a tool that watches
                    # for it stops rather than finishing into nothing.
                    yield from close_out(running=call)
                    return
                else:
                    # Detached. The same thread keeps running under a job id,
                    # and the model is told what it got instead of the answer.
                    job = JOBS.adopt(call.name, args, value)
                    yield {"type": "background", "job": job.brief()}
                    result = _bg_result(job)
            yield {"type": "tool_output", "name": call.name, "result": result}
            results.append((call, result))
            if call is not parsed[-1]: time.sleep(0.15)
        # Handed over as a round, not per call: anthropic and gemini require
        # every result from one round batched into a single message.
        messages.extend(p.result_messages(results))
        # After the results, never before them: this lands as a user message,
        # and the only place one is unambiguously legal for every provider is
        # straight after a tool-result message. Recorded through the same yield
        # the loop records everything else with, so a reload replays it.
        # Both runtime notes go in one message, not two. They land as user
        # messages, and two of those in a row is a 400 on anthropic -- but the
        # deeper reason is that they are the same kind of thing to the model:
        # the runtime saying what changed while it was busy.
        stale = _todo_note(ctx) if since_todo >= TODO_STALE else ""
        if stale: since_todo = 0
        # Anything typed while this round was running goes in first and in the
        # same message. First because it is the only part of it a person wrote
        # and the runtime's housekeeping should not be read before them; same
        # message because it is a user message either way, and two in a row is
        # the 400 this seam exists to avoid.
        typed = (steer() or []) if steer else []
        said = _steer_note(typed) if typed else ""
        runtime = "\n\n".join(n for n in (_finished_note(), stale) if n)
        note = "\n\n".join(n for n in (said, runtime) if n)
        # Anything attached to those messages travels on this one, in the order
        # it was sent. Through the adapter rather than by hand for the same
        # reason the opening turn goes through it: three dialects, three shapes.
        # Dropped for a model that cannot see -- the names stay in the note, so
        # it can say it was sent something it cannot look at, which is better
        # than a 400 that ends the run.
        steer_images = [a["b64"] for m in typed for a in (m.get("images") or [])] if vision else []
        if note:
            messages.append(p.user_message(note, steer_images) if steer_images
                            else {"role": "user", "content": note})
            # text is what the conversation gets and what the record keeps.
            # show is what the frontend draws, and it is deliberately only the
            # runtime half: the frontend printed the user's own line the moment
            # they typed it, and sending it back would print it twice.
            yield {"type": "notice", "text": note, "show": runtime, "from_user": bool(said),
                   # Named on the record for the same reason they are named in
                   # the note: a reopened conversation has to say a picture was
                   # sent here, and the record is the only thing that can.
                   "images": [a for m in typed for a in (m.get("images") or [])] if steer_images else []}
