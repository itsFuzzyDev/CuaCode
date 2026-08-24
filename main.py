import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))

# Before anything starts talking. --usage is a question about the app rather
# than a conversation with it: it prints what every session has cost and exits,
# without opening a session of its own to ask.
if "--usage" in sys.argv:
    from handler import usage as _usage
    sys.exit(_usage.cli(sys.argv[1:]))

from handler.protocol import IPC
from handler.agent.main import generate
from handler.agent import providers
from handler.agent.background import JOBS
from handler.session import store
from handler.session.main import Session
from handler import config, context, environment, usage
from integrations.memory import loader as memory, naming, recall
from integrations.instructions import loader as instructions
from integrations.skills import loader as skills

SETTINGS = config.settings()

ipc = IPC()
ipc.send("status", {"state": "ready"})
Ctx = type('Ctx', (dict,), {'self_identity': property(lambda s: s.get("frontmost_app")), 'session_dir': property(lambda s: s.get("session_dir")),
                            'cwd': property(lambda s: s.get("cwd"))})
ctx = Ctx(ipc.terminal_info)

# A session exists from boot but nothing touches disk until a round commits,
# so launching the app and closing it leaves no empty session dirs behind. The
# effort comes off the config rather than starting blank: the level is a
# property of the conversation, but "which level a new conversation starts on"
# is a property of the account, and without it every restart silently went back
# to whatever the provider thinks is enough thinking.
sess = Session.create(provider=SETTINGS["provider"], model=SETTINGS.get("model", ""),
                      effort_level=config.default_effort())
messages = sess.messages()
# Tools run in this process, so the memory tool retitles the object the loop is
# holding rather than the file underneath it -- a write to meta.json would be
# undone by the next commit, which persists the whole dict from memory.
naming.set_live(sess)

# Which tools need asking is declared per tool (require_permissions); this only
# records whether the frontend on the other end answers when asked. It has to be
# recorded, because the ask blocks forever by design -- a question the user only
# gets to tomorrow is still answered tomorrow -- and a frontend that does not
# implement the reply would otherwise hang the run instead of waiting for it.
ASK_PERMISSION = False

# What the last round actually cost, kept so /context can report it long after
# the event that carried it went past. One round, not a running total: the
# question it answers is "how is this model behaving right now", and a session
# average would smooth away exactly the turn worth asking about.
LAST_TURN = {}

def turn_fields(chunk: dict) -> dict:
    """What the round that just generated cost, and remember it for /context."""
    fields = context.turn_fields(chunk)
    LAST_TURN.clear()
    LAST_TURN.update({"input": (chunk.get("usage") or {}).get("input", 0), **fields})
    return fields

def context_fields(usage: dict) -> dict:
    """The context readout, as far as this turn can tell it.

    Always what was spent, because that is measured. The window and what is
    left of it only when the model's size is actually known -- an unknown
    window is reported as no window at all rather than as a default, so a
    frontend draws a token count instead of a meter reading against a number
    somebody made up.
    """
    used = (usage or {}).get("input", 0) + (usage or {}).get("output", 0)
    if not used: return {}
    out = {"context_used": used}
    if window := config.context_window(SETTINGS["provider"], SETTINGS.get("model", "")):
        out["context_max"] = window
        out["context_left"] = max(window - used, 0)
    return out

def ask_permission(name: str, args: dict, preview: dict = None) -> bool:
    # timeout=None and stop are not in tension: the question stays open for as
    # long as the user needs, but a cancel arriving while it is up ends the run
    # it belonged to instead of leaving it waiting on an answer nobody is going
    # to give. Abandoned reads as refused, and the loop's own cancel check on
    # the next line is what actually stops the turn.
    # preview is what the tool says the call would do -- a diff, a line of prose --
    # and is absent for most of them. Sent as its own field rather than folded into
    # args, so a frontend that has never heard of it draws what it always drew.
    payload = {"name": name, "args": args}
    if preview: payload["preview"] = preview
    reply = ipc.call("permission", payload, timeout=None, stop=ipc.cancelled)
    return bool(reply and (reply.data or {}).get("allow"))

def tool_ordinal(session) -> int:
    """How many tool results the conversation already holds -- the index the
    next one is filed under. Sent with every tool_output so a frontend can ask
    for the whole call later instead of counting rows and hoping it agrees."""
    return sum(1 for r in session.records() if r.get("t") == "tool")

DETAIL_CHARS = 20000

def _shrink(value, limit: int = DETAIL_CHARS):
    """Cap the strings in a stored payload. The detail view exists because the
    wire strips output, but a 5MB build log is still not something to put
    through line-delimited JSON as one line."""
    if isinstance(value, str):
        if len(value) <= limit: return value
        return value[:limit] + f"\n... [{len(value) - limit} more characters]"
    if isinstance(value, dict): return {k: _shrink(v, limit) for k, v in value.items()}
    if isinstance(value, list): return [_shrink(v, limit) for v in value]
    return value

def tool_detail(session, index: int) -> dict:
    """One call in full: the arguments the model sent and the result that came
    back. Read from the records rather than remembered by the frontend -- the
    wire carries a summary of the result and nothing else, on purpose, and this
    is the other half of that bargain. Images stay as blob refs: the point is
    to read what happened, not to move a megabyte of base64.
    """
    calls, results = [], []
    for r in session.records():
        if r.get("t") == "assistant": calls.extend(r.get("calls") or [])
        elif r.get("t") == "tool": results.append(r)
    if not 0 <= index < len(results):
        # Not "error": that key means a failed envelope everywhere else on this
        # wire, and a frontend asking for a call that has since been rewound
        # away has not hit an error, it has asked for nothing.
        return {"index": index, "total": len(results), "unavailable": "no such call"}
    rec = results[index]
    # Positional, because every call yields exactly one tool record in order --
    # checked by name all the same, so a history that ever drifts shows no
    # arguments rather than another call's.
    call = calls[index] if index < len(calls) and calls[index].get("name") == rec.get("name") else {}
    return {"index": index, "total": len(results), "name": rec.get("name", ""), "ts": rec.get("ts", ""),
            "args": _shrink(call.get("args") or {}), "result": _shrink(rec.get("result") or {})}

def _detail_now(env):
    """Answer tool.detail off the reader thread, so a call can be read while the
    run that made it is still going.

    The main loop is inside generate() for a whole turn: an envelope queued for
    it is answered when the turn ends, which for "what did that call return" is
    an hour after it was useful. This only reads -- records(), and a copy of a
    payload -- so it is safe to run beside the run appending to them.
    """
    try: want = int((env.data or {}).get("index", -1))
    except (TypeError, ValueError): want = -1
    ipc.reply(env, "detail", tool_detail(sess, want))

ipc.direct["tool.detail"] = _detail_now

def replay(session, env):
    """Re-send a reopened conversation as the same events the live run sent.

    A frontend then redraws it with the code it already has, and there is no
    second rendering path to drift from the first. Records are canonical, so
    this reads the same whichever provider wrote them; the calls are put back
    into the current provider's dialect on the way out, exactly as the history
    itself is."""
    p = providers.get(session.provider)
    tools_seen = 0
    for r in session.records():
        t = r.get("t")
        if t == "user":
            # Names, never payloads. A frontend redrawing a conversation wants
            # to show that a picture was attached and what it was called;
            # shipping the base64 back would put megabytes on the wire for
            # every reopened session, to draw a thumbnail nobody asked for.
            data = {"state": "user", "token": r.get("text", ""), "status": "running"}
            if r.get("images"):
                data["images"] = [{"name": i.get("name") or "image"} for i in r["images"]]
            ipc.reply(env, "token", data)
        elif t == "recall":
            # Drawn, not hidden. Something the runtime put in front of the model
            # on the user's behalf is exactly the kind of thing they are
            # entitled to see, and a notice is the row that already means that.
            ipc.reply(env, "token", {"state": "notice", "token": r.get("text", ""), "status": "running"})
        elif t == "assistant":
            if r.get("thinking"):
                ipc.reply(env, "token", {"state": "thinking", "token": r["thinking"], "status": "running"})
            if r.get("content"):
                ipc.reply(env, "token", {"state": "content", "token": r["content"], "status": "running"})
            if r.get("calls"):
                ipc.reply(env, "token", {"state": "tool_calls", "token": p.native_calls(r["calls"]), "status": "tooling"})
        elif t == "tool":
            ipc.reply(env, "token", {"state": "tool_output", "token": r.get("name", ""),
                                     "result": r.get("result", {}), "index": tools_seen, "status": "tooling"})
            tools_seen += 1

# Set when a turn is cancelled, spent on the next one. The note says the run was
# stopped and what survived of it, and it has to travel between turns because
# there is nowhere legal to put it at the moment it is written: it is a user
# message, and the user is about to send one of their own.
pending_note = ""

while True:
    # The frontend that spawned us is gone: its pipe closed (EOF) or we were
    # reparented to launchd (getppid()==1). Either way there is nobody left to
    # talk to, so exit rather than poll an empty inbox forever -- otherwise
    # every crash or force-quit leaves an orphan worker behind. Mirrors the
    # `stop` path below: background work is asked to stop first, then the
    # session is committed and we leave.
    if ipc.eof() or os.getppid() == 1:
        JOBS.kill_all()
        sess.commit()
        sys.exit(0)
    # Names that finished while the loop was busy. Applied here rather than on
    # the thread that produced them, so meta.json has one writer and stdout has
    # one writer -- a naming call that lands mid-stream must not interleave
    # with it.
    for finished in naming.drain():
        if named := naming.apply(finished, sess):
            ipc.send("status", {"state": "session_title", **named})

    for env in ipc.poll():
        if env.type == "terminal":
            ipc.reply(env, "status", {"terminal": ipc.terminal_info})
            ipc.terminal_info = env.data
            ctx = Ctx(ipc.terminal_info)
            # The boot session was created before this envelope landed, so its
            # frontend is stamped here rather than at create().
            sess.set_frontend(ctx.get("term_program") or "")
            continue
        if env.type != "cmd": continue
        action = env.data.get("action")
        if action == "stop":
            # Background work is asked to stop first. It cannot outlive the
            # process either way -- the threads are daemons -- but a shell job
            # holds a real process group, and that one does outlive us unless
            # something kills it on the way out.
            JOBS.kill_all()
            sess.commit()
            ipc.reply(env, "status", {"state": "stopped"})
            sys.exit(0)
        elif action == "cancel":
            # The run itself was already stopped by the flag IPC set on the
            # reader thread; this is only the acknowledgement.
            ipc.reply(env, "status", {"state": "cancel_ack"})
        elif action == "background":
            # Same shape as cancel, and for the same reason: the flag was set on
            # the reader thread while this loop was still inside generate(), so
            # by the time the envelope is read here the call has already moved.
            # Nothing to do but say so.
            ipc.reply(env, "status", {"state": "background_ack"})
        elif action == "background.list":
            # For a frontend drawing a jobs panel. The model reads the same
            # registry through the background tool; this is the other audience.
            ipc.reply(env, "jobs", {"jobs": [j.brief() for j in JOBS.list(env.data.get("state") or "all")]})
        elif action == "background.kill":
            jid = env.data.get("job", "")
            ipc.reply(env, "jobs", {"killed": JOBS.kill(jid), "job": jid,
                                    "jobs": [j.brief() for j in JOBS.list()]})
        elif action == "permission.mode":
            ASK_PERMISSION = env.data.get("mode") == "ask"
            ipc.reply(env, "status", {"state": "permission", "mode": "ask" if ASK_PERMISSION else "auto"})
        elif action == "tool.detail":
            # The other half of the summarising above: the wire carries a size
            # where a result was, and this is how a frontend gets the thing
            # itself -- asked for, one call at a time, never streamed at
            # everyone by default. Normally answered on the reader thread by
            # _detail_now and never seen here; this is what catches one that
            # arrived before that was registered.
            try: want = int(env.data.get("index", -1))
            except (TypeError, ValueError): want = -1
            ipc.reply(env, "detail", tool_detail(sess, want))
        elif action == "context.report":
            # What the window is full of, read off what is already loaded. It
            # asks the provider nothing -- a readout that spent tokens to say how
            # many tokens you had spent would be a poor joke -- so it is answered
            # from the prompt, the tool schemas and the history in memory, and
            # says plainly which of its numbers were measured and which estimated.
            try:
                ipc.reply(env, "context", context.report(sess, SETTINGS, ctx,
                                                         messages=messages, last=LAST_TURN))
            except Exception as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "usage.report":
            # The same rollup `--usage` prints, for a frontend that would rather
            # draw it. Read from the meta files, so a hundred conversations cost
            # a hundred small json reads and not one transcript.
            try: days = int(env.data.get("days") or 0)
            except (TypeError, ValueError): days = 0
            rep = usage.rollup(days)
            # This conversation's own, including the rounds not yet committed --
            # the session you are in is the one you are asking about.
            rep["session"] = {"id": sess.id, **(usage.of_records(sess.records()) or {})}
            ipc.reply(env, "usage", rep)

        elif action == "skill.list":
            # The palette's half of skills. Only the ones a user may invoke:
            # a skill marked `disable-user-invocation` is the model's to reach
            # for, and never appears as a /command.
            try:
                ipc.reply(env, "skills", {"skills": skills.listing("user")})
            except Exception as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "session.list":
            ipc.reply(env, "sessions", {"sessions": store.list_sessions(), "active": sess.id})
        elif action == "provider.list":
            # listing() never carries a key, only has_key -- this crosses the
            # wire and lands in frontend logs.
            ipc.reply(env, "providers", {"providers": config.listing(),
                                         "vision": config.vision_helper()[0], "vision_model": config.vision_helper()[1],
                                         "sighted": config.sighted()})

        elif action == "provider.use":
            try:
                SETTINGS = config.settings(config.use(env.data.get("name", "")))
                # Records are canonical, so the history rebuilds in the new
                # dialect rather than the conversation starting over.
                sess.set_provider(SETTINGS["provider"])
                messages = sess.messages()
                ipc.reply(env, "status", {"state": "provider", "provider": SETTINGS["provider"],
                                          "model": SETTINGS.get("model", ""), "msg_count": len(messages)})
            except ValueError as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "provider.set":
            try:
                name = env.data.get("name", "")
                config.update(name, model=env.data.get("model"), key=env.data.get("api_key"),
                              vision=env.data.get("vision"), params=env.data.get("params"),
                              effort_map=env.data.get("effort_map"),
                              model_params=env.data.get("model_params"))
                if name == SETTINGS.get("provider"):
                    SETTINGS = config.settings()
                    if env.data.get("model"): sess.set_model(env.data["model"])
                    # A model swap within one provider can turn vision off, and
                    # a history holding screenshots would then fail every
                    # request from here on. Rebuilding drops them to notes --
                    # the records keep the blobs, so switching back restores
                    # the images themselves.
                    messages = sess.messages()
                # Reply from listing(), never from the request: echoing back
                # env.data would put the key straight into the frontend log.
                ipc.reply(env, "providers", {"providers": config.listing(),
                                         "vision": config.vision_helper()[0], "vision_model": config.vision_helper()[1],
                                         "sighted": config.sighted()})
            except ValueError as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "model.list":
            # Asked of the provider rather than kept in a table here: the
            # catalogs rotate, and a hardcoded list is wrong the week after it
            # is written. A provider that will not answer says so, and the
            # frontend keeps showing what is configured.
            try:
                name = env.data.get("name") or SETTINGS["provider"]
                p = providers.new(name)
                p.setup(config.api_key(name))
                found = p.models() if hasattr(p, "models") else None
                known = config.entry(name)
                ipc.reply(env, "models", {
                    "provider": name, "models": found or [],
                    "listed": found is not None,
                    "active": config.model_for(name),
                    # What is already known about each, so the picker can mark a
                    # model that cannot see without asking the provider once per row.
                    "blind": sorted({m for m, caps in (known.get("model_caps") or {}).items()
                                     if "vision" not in caps} | set(known.get("blind_models") or []))})
            except Exception as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "vision.use":
            # Which provider looks at images for a model that cannot. A role,
            # not a provider setting, so it is set here rather than through
            # provider.set -- and validated there, because a helper that cannot
            # see would otherwise fail much later, inside a tool call.
            try:
                config.set_vision_helper(env.data.get("name", ""), env.data.get("model"))
                ipc.reply(env, "providers", {"providers": config.listing(),
                                             "vision": config.vision_helper()[0], "vision_model": config.vision_helper()[1],
                                         "sighted": config.sighted()})
            except ValueError as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "session.effort":
            # Session state, not provider state: the depth you picked belongs
            # to this conversation and travels with it on reload.
            try:
                # Refused rather than approximated. Every other rung lands on
                # the nearest thing this model has, which is coarse but true to
                # what it says; "off" landing on the lowest rung there is would
                # be the box marked "no thinking at all" buying thinking.
                if reason := config.effort_block(env.data.get("effort", "")):
                    ipc.reply(env, "status", {"state": "error", "error": reason})
                else:
                    sess.set_effort(env.data.get("effort", ""))
                    # ...and remembered as the level the next one starts on.
                    # The session still owns what it is running at; this only
                    # stops the choice dying with the process.
                    config.set_default_effort(sess.effort)
                    ipc.reply(env, "status", {"state": "effort", "effort": sess.effort,
                                              "session_id": sess.id})
            except ValueError as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})

        elif action == "session.new":
            # Carried over rather than reset: starting a fresh chat is not a
            # request to go back to whatever the provider does by default.
            recall.forget(sess.id)
            instructions.forget(sess.id)
            # Last round's cost belonged to the conversation that just went.
            LAST_TURN.clear()
            sess = Session.create(provider=SETTINGS["provider"], model=SETTINGS.get("model", ""),
                                  effort_level=sess.effort, frontend=ctx.get("term_program") or "")
            messages = sess.messages()
            naming.set_live(sess)
            ipc.reply(env, "status", {"state": "session", "session_id": sess.id,
                                      "effort": sess.effort, "msg_count": len(messages)})
        elif action == "session.load":
            try:
                loaded = Session.open(env.data.get("id", ""))
                loaded.restore_tool_state()
                LAST_TURN.clear()
                sess, messages = loaded, loaded.messages()
                naming.set_live(sess)
                # The status goes first so a frontend can clear whatever it was
                # showing before the replayed conversation starts arriving.
                ipc.reply(env, "status", {"state": "session", "session_id": sess.id,
                                          "effort": sess.effort, "msg_count": len(messages)})
                replay(sess, env)
                ipc.reply(env, "token", {"state": "done", "token": "done", "status": "done",
                                         "msg_count": len(messages)})
            except (FileNotFoundError, ValueError, KeyError) as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})
        elif action == "session.delete":
            try:
                sid = env.data.get("id", "")
                ipc.reply(env, "status", {"state": "deleted" if store.delete(sid) else "error", "session_id": sid})
            except ValueError as e:
                ipc.reply(env, "status", {"state": "error", "error": str(e)})
        elif action == "chat":
            # Re-read per turn rather than trusting what boot saw. The config
            # is a file the user edits, and until now only two IPC actions
            # refreshed this -- so a model changed by hand left the worker
            # running on the old answer for the rest of its life, most visibly
            # by withholding the cameras from a model that could see perfectly
            # well. Reading a small json file per turn is nothing next to the
            # request that follows it.
            before = (SETTINGS.get("vision", True), SETTINGS.get("model", ""))
            SETTINGS = config.settings()
            now = (SETTINGS.get("vision", True), SETTINGS.get("model", ""))
            if now != before:
                # The model or its eyes changed under us. The history was
                # rendered for the old answer -- images dropped to notes, or
                # notes standing where images could now be -- so it is rebuilt
                # before anything is sent, and the session records what it is
                # actually running on.
                sess.set_model(now[1])
                messages = sess.messages()
            text = env.data.get("text", "")
            # What the user attached to this message, as [{"name", "b64"}].
            # Only the payload reaches the model -- the filename is for the
            # frontends, which show it in place of a picture they have no way
            # to draw -- and it is kept on the record so a reopened session
            # still says what was sent rather than "1 image".
            attached = [a for a in (env.data.get("images") or [])
                        if isinstance(a, dict) and a.get("b64")]
            # A model that cannot see is told rather than sent an image it will
            # 400 on. The attachment is dropped from the request and from the
            # record both: recording it would put it back into the history on
            # the next turn, and the turn after that, forever.
            if attached and not config.can_see(SETTINGS["provider"]):
                names = ", ".join(a.get("name") or "image" for a in attached)
                ipc.reply(env, "token", {"state": "notice", "status": "running",
                                         "token": f"{len(attached)} image(s) not sent ({names}): "
                                                  f"{SETTINGS.get('model') or SETTINGS['provider']} cannot see images"})
                attached = []
            # Asked before the record is written, because "is this the opening
            # message" stops being answerable the moment it is one.
            opening = not any(r.get("t") == "user" for r in sess.records())
            # Built by the provider adapter rather than by hand: a user turn
            # with a picture on it is three different shapes, and this is the
            # same call replay makes when it rebuilds the turn from disk.
            turn = providers.get(sess.provider).user_message(text, [a["b64"] for a in attached])
            # The names, in the message itself. Not recorded as a separate
            # record: it is derived from the attachments on the user record, so
            # replay rebuilds the identical line from the identical source
            # rather than from a note that could drift from it.
            providers.append_user_text(turn, providers.attachment_note(attached))
            messages.append(turn)
            sess.add_user(text, attached)
            # Where the turn is happening, for anything that scores by it. Set
            # before recall runs and before the tool descriptions are rebuilt,
            # which is what makes the memory index the *right* project's.
            sess.set_cwd(ctx.get("cwd") or "")
            memory.set_cwd(ctx.get("cwd") or "")
            # Pointers to things already known that look related. Appended to
            # the user's own message rather than sent as a second one: two user
            # messages in a row is a 400 on anthropic, and this is a note about
            # that message anyway. Never allowed to fail a turn -- a recall
            # block is a convenience, and no convenience gets to eat a message.
            try:
                note = recall.block(text, sid=sess.id, path=ctx.get("cwd") or "",
                                    apps=[ctx.get("frontmost_app") or ""])
            except Exception:
                note = ""
            # The same treatment for documentation sitting in the working
            # directory: names and sizes, never bodies, and at most twice in a
            # conversation. Separate from recall because the two answer different
            # questions -- what do I already know about this, versus what does
            # this repository say about itself -- and a frontend showing them as
            # one notice would make the second look like a memory hit.
            try:
                docs = instructions.docs_block(text, sid=sess.id, path=ctx.get("cwd") or "",
                                               first=opening)
            except Exception:
                docs = ""
            # A message that opens with /<skill> loads that skill's instructions.
            # Same rail again: the user's line stays the line they typed, and
            # the body rides beside it rather than replacing it -- so a skill
            # invoked by hand reads back as what it was.
            try:
                skill_block = skills.invocation(text)
            except Exception:
                skill_block = ""
            # The interrupt note rides the same rail as recall and docs, and for
            # the same reason: it is runtime text about this user message, so it
            # is folded into it rather than sent as another. Spent as it is used
            # -- a turn is only interrupted once, and a note repeated on every
            # turn after would read as a fresh stop each time.
            for extra in (pending_note, note, docs, skill_block):
                if not extra: continue
                sess.add_recall(extra)
                providers.append_user_text(messages[-1], extra)
                ipc.reply(env, "token", {"state": "notice", "token": extra, "status": "running"})
            pending_note = ""
            ipc.reply(env, "status", {"type": "chat_received"})
            # Point ctx at whichever session this turn belongs to. Here rather
            # than beside each `sess =` above, because there are three of those
            # and a tool only ever reads this from inside the run below -- so
            # the one place it has to be right is the moment before the run.
            # The directory is a path, not a mkdir: nothing creates it until a
            # tool actually puts something in it, and an idle launch still
            # leaves no session behind.
            ctx["session_dir"] = str(sess.dir)
            # How hard this conversation is thinking, for anything that starts
            # a model of its own from inside a tool call. A subagent that did
            # not name a level runs at the conversation's rather than at a
            # constant nobody chose -- see handler/agent/subagent.py.
            ctx["effort"] = sess.effort
            ipc.begin_run()
            round_mark = len(messages)
            # What the round in flight has been charged, held only until the
            # assistant record it belongs to is written. Cleared per round, so a
            # round the provider never reported on is recorded as unmeasured
            # rather than inheriting the previous round's numbers.
            round_cost = {}
            try:
                # The provider half of the settings is account-wide and the
                # effort is this conversation's, so they are joined per turn
                # rather than kept as one stale dict.
                for chunk in generate(API_KEY=config.api_key(SETTINGS["provider"]), messages=messages,
                                      ctx=ctx, settings={**SETTINGS, "effort": sess.effort},
                                      # Two segments, not one string: the instructions
                                      # are the same on every machine and the second
                                      # block is what is true about this one. Kept
                                      # apart so each provider puts them where they
                                      # belong -- separate system messages on ollama
                                      # and openai, separate system blocks on
                                      # anthropic. Machine facts are assembled per
                                      # turn rather than stored on the session: the
                                      # prompt has to describe the machine this turn
                                      # runs on, not the one the conversation started
                                      # on.
                                      # Three segments now: the instructions that
                                      # ship with the agent, the user's own
                                      # standing orders from ~/.cuacode/AGENTS.md,
                                      # and what is true about this machine. The
                                      # user's block sits between them because it
                                      # is stable -- a provider caches the prompt
                                      # as a prefix, and the environment block
                                      # carries the clock, so anything placed
                                      # after it is re-sent every turn anyway.
                                      # Empty segments are dropped: a blank system
                                      # block is a 400 on more than one provider.
                                      # Four now: the always-on skills sit with the
                                      # user's standing orders because that is what
                                      # they are -- rules in force before the first
                                      # decision of the turn, not something to go
                                      # and fetch -- and they are as stable as the
                                      # block above them, so the cached prefix
                                      # still runs to the environment block.
                                      system=[s for s in (sess.system, instructions.user_block(),
                                                          skills.always_block(),
                                                          environment.block(ctx, SETTINGS, sess)) if s],
                                      cancelled=ipc.cancelled,
                                      # Whatever was typed while this run was
                                      # going. A callable, not a list: the loop
                                      # asks at the one point in a round where a
                                      # user message is legal, and anything that
                                      # arrives after the last of those is put
                                      # back on the inbox by end_run and becomes
                                      # an ordinary next turn.
                                      steer=ipc.take_steer,
                                      # The event itself, not a reading of it:
                                      # generate() clears it as it consumes it,
                                      # so one press backgrounds one call.
                                      detach=ipc.background,
                                      ask=ask_permission if ASK_PERMISSION else None):
                    typ = chunk.get("type")
                    if typ == "round":
                        # generate() takes its own rollback mark immediately
                        # before yielding this, so the two stay in step and a
                        # rewind here drops exactly the round it drops.
                        round_mark = len(messages)
                        round_cost = {}
                        sess.round_start()
                    elif typ == "assistant":
                        sess.add_assistant(chunk.get("thinking", ""), chunk.get("content", ""),
                                           chunk.get("tool_calls") or [], usage=round_cost)
                    elif typ == "cancelled":
                        # No rewind. The loop closed the round off rather than
                        # abandoning it -- every tool_call it left behind has a
                        # result saying it was interrupted -- so what is in
                        # messages is resumable, and the records that describe it
                        # were yielded on the way here like any other round's.
                        # Deleting them was what used to leave the model with no
                        # idea it had been stopped, and no sight of the half-
                        # finished work the user pressed the key to correct.
                        messages = chunk.get("messages", messages)
                        sess.commit()
                        # Told to the model on its next turn rather than now: the
                        # note is a user message, and one sent on its own here
                        # would sit next to the one the user is about to type.
                        pending_note = chunk.get("note", "")
                        ipc.reply(env, "token", {"state": "cancelled", "token": "cancelled", "status": "cancelled", "msg_count": len(messages)})
                    elif typ == "rate":
                        # No "status" key: this says how fast, not what state
                        # the run is in, and a rate arriving mid-stream must not
                        # move a frontend off tooling or thinking.
                        ipc.reply(env, "status", {"state": "rate", **context.rate_fields(chunk)})
                    elif typ == "usage":
                        # Mid-run, once a round's request has been counted. Its
                        # own status rather than a field on the next token: a
                        # tool loop can run for minutes without one.
                        turn = turn_fields(chunk)
                        # Kept for the assistant record that is about to be
                        # written: the counts arrive one yield before the turn
                        # they describe.
                        round_cost = usage.stamp(turn, SETTINGS["provider"], SETTINGS.get("model", ""))
                        fields = {**context_fields(chunk.get("usage")), **turn}
                        if fields:
                            ipc.reply(env, "status", {"state": "usage", **fields})
                    elif typ == "done":
                        messages = chunk.get("messages", messages)
                        sess.commit()
                        # After the commit, so the namer reads a turn count that
                        # has actually happened. Fires at most three times in a
                        # session, on its own thread: the answer is already
                        # streamed and nobody waits on a label.
                        naming.maybe_start(sess)
                        ipc.reply(env, "token", {"state": "done", "token": "done", "status": "done",
                                                 "msg_count": len(messages),
                                                 **context_fields(chunk.get("usage")), **turn_fields(chunk)})
                    elif typ == "tool_calls":
                        ipc.reply(env, "token", {"state": "tool_calls", "token": chunk.get("text"), "status": "tooling"})
                    elif typ == "background":
                        # A call left running. Sent as its own state rather than
                        # folded into tool_output because the two mean opposite
                        # things to anything drawing a row: one closes it, this
                        # one says it will not close for a while.
                        job = chunk.get("job", {})
                        ipc.reply(env, "token", {"state": "background", "token": job.get("job", ""),
                                                 "result": job, "status": "tooling"})
                    elif typ == "notice":
                        # Runtime text put into the conversation, and -- since
                        # mid-turn messages land here too -- sometimes the user's
                        # own words with it. Recorded as a user record either
                        # way: that is the role it occupies in the history, and a
                        # record that lied about it would replay wrong.
                        sess.add_user(chunk.get("text", ""), chunk.get("images") or None)
                        # Only the runtime half goes to the frontend. It drew the
                        # user's line when they typed it, and a notice repeating
                        # it would put the same sentence on screen twice.
                        shown = chunk.get("show", chunk.get("text", ""))
                        if shown:
                            ipc.reply(env, "token", {"state": "notice", "token": shown, "status": "running"})
                    elif typ == "tool_output":
                        result = chunk.get("result", {})
                        name = chunk.get("name")
                        # Taken before the record is added: this is the index the
                        # call is about to be filed under, and it is what a
                        # frontend sends back to tool.detail to read it in full.
                        idx = tool_ordinal(sess)
                        sess.add_tool(name, result)
                        if name in ("screenshot", "photos"):
                            # Keep IPC payload under ~75 chars — images live in messages, not the wire
                            count = result.get("count", 1)
                            ipc.reply(env, "token", {"state": "tool_output", "token": name, "result": {"n": count}, "index": idx, "status": "tooling"})
                        elif name in ("WebFetch", "agent", "workflow", "skill", "describe_image") and isinstance(result.get("result"), dict):
                            # Same reason as images and shell output: a page in
                            # full mode is tens of thousands of characters, and
                            # a frontend drawing one row should never have to
                            # read a web page to do it. What it needs is the
                            # size and where it came from; the content itself
                            # is already in the messages.
                            r = result["result"]
                            body = r.get("output")
                            brief = {k: v for k, v in r.items() if k not in ("output", "log", "text", "instructions", "description")}
                            if isinstance(r.get("instructions"), str): brief["chars"] = len(r["instructions"])
                            if isinstance(body, str): brief["chars"] = len(body)
                            elif isinstance(body, dict): brief["fields"] = sorted(body)[:8]
                            if isinstance(r.get("log"), list): brief["steps"] = len(r["log"])
                            ipc.reply(env, "token", {"state": "tool_output", "token": name,
                                                     "result": {"result": brief}, "index": idx, "status": "tooling"})
                        elif name == "shell" and isinstance(result.get("result"), dict):
                            # Same reason as images: command output belongs in the
                            # messages, and a frontend reading line-delimited JSON
                            # should never have to swallow a build log to draw one row.
                            r = result["result"]
                            brief = {k: v for k, v in r.items() if k not in ("stdout", "stderr")}
                            brief["bytes"] = len(r.get("stdout", "")) + len(r.get("stderr", ""))
                            ipc.reply(env, "token", {"state": "tool_output", "token": name, "result": {"result": brief}, "index": idx, "status": "tooling"})
                        else:
                            ipc.reply(env, "token", {"state": "tool_output", "token": name, "result": result, "index": idx, "status": "tooling"})
                    else:
                        ipc.reply(env, "token", {"state": typ, "token": chunk.get("text"), "status": "running"})
            except Exception as e:
                # A turn that dies mid-round leaves an assistant message whose
                # tool_calls have no results, which the provider rejects on the
                # next request. Rewind the live list and the records together,
                # or the session is persisted in a state it can never resume from.
                del messages[round_mark:]
                sess.rewind()
                sess.commit()
                ipc.reply(env, "token", {"state": "error", "token": str(e), "status": "error"})
            finally:
                # The turn is over however it ended. Anything typed during it
                # that the loop never reached goes back on the inbox here and is
                # answered as an ordinary next message -- queued, not swallowed.
                ipc.end_run()
    time.sleep(0.001)
