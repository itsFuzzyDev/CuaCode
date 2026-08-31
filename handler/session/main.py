import os, sys
from pathlib import Path

from handler import usage
from handler.agent import effort, providers
from handler.session import blobs, replay, store

class Session:
    """One conversation, persisted as canonical records.

    What lands on disk is not the provider's message shape but the inputs the
    adapters already take: user text, an assistant turn, a raw tool result.
    Loading replays those through the same adapters, so a reload is exact and
    a conversation is not welded to the provider that recorded it.
    """

    def __init__(self, sid: str, meta: dict, records: list[dict]):
        self.id = sid
        self.dir = store.path(sid)
        self.meta = meta
        self._records = records   # committed, on disk, still blob-split
        self._pending = []        # this run, not yet committed
        self._mark = 0
        # Which provider wrote the records that predate per-record stamping.
        # Fixed for the life of the session, unlike meta["provider"], which
        # moves when you switch.
        self._origin = meta.get("origin") or meta.get("provider") or "ollama"

    @classmethod
    def create(cls, provider: str = "ollama", model: str = "", effort_level: str = "",
               frontend: str = "") -> "Session":
        sid = store.new_id()
        now = store.now_iso()
        return cls(sid, {"id": sid, "created": now, "updated": now, "provider": provider,
                         "origin": provider, "model": model, "effort": effort_level,
                         # Which frontend made this session, so a controller can
                         # show only the ones it owns and file the rest under
                         # their own frontend's name. Empty for sessions that
                         # predate the field.
                         "frontend": frontend,
                         # title_source says who chose the name, which is what
                         # decides whether anything is allowed to replace it;
                         # title_attempts caps what the auto-namer may spend.
                         "title": "", "title_source": "", "title_attempts": 0,
                         "cwd": "", "records": 0, "turns": 0, "read_files": []}, [])

    @classmethod
    def open(cls, sid: str) -> "Session":
        d = store.path(sid)
        if not d.is_dir(): raise FileNotFoundError(f"no session {sid}")
        return cls(sid, store.read_json(d / "meta.json"), store.read_jsonl(d / "messages.jsonl"))

    @property
    def provider(self) -> str: return self.meta.get("provider") or "ollama"

    @property
    def effort(self) -> str:
        """How hard the model should think in this conversation.

        Sits beside provider and model rather than in the provider config for
        the same reason those do: it is a property of the conversation, not of
        the account. Reopening a session gets the depth it was run at, and a
        quick session started next to a deep one does not disturb it. Blank
        means the provider's own default, so sessions recorded before this
        existed keep behaving exactly as they did.
        """
        return self.meta.get("effort") or ""

    def set_effort(self, level: str):
        """Validated here, not at request time: a typo silently meaning "no
        thinking" is worse than a rejected setting."""
        level = level or ""
        if level and level not in effort.LADDER:
            raise ValueError(f"unknown effort: {level!r} (have {list(effort.LADDER)})")
        self.meta["effort"] = level
        # Same condition as set_provider: nothing is written for a session that
        # has not committed a round, so an abandoned one leaves no directory.
        if self._records: store.write_json(self.dir / "meta.json", self.meta)

    @property
    def blobs_dir(self) -> Path: return self.dir / "blobs"

    @property
    def notebook(self) -> Path:
        """Session-scoped scratch space: working files that belong to this
        conversation and not to the user's project.

        A path, not a mkdir, for the same reason the session directory itself
        is one -- a session that never writes a scratch file leaves no empty
        directory behind. The file tool creates parents on write, so the first
        write is what brings it into being.
        """
        return self.dir / "notebook"

    # ---- recording ----

    def round_start(self):
        """Mirror of the agent loop's rollback point. A cancel rewinds to
        here; rounds that completed earlier are kept."""
        self._mark = len(self._pending)

    def rewind(self): del self._pending[self._mark:]

    # Every record is stamped when it happens, so a user record is when it was
    # sent and an assistant record is when it landed -- no second field needed.
    # replay only reads the keys it knows, so `ts` never reaches the provider.
    def add_user(self, text: str, images: list = None):
        # Whether this is the opening message has to be asked before the record
        # is appended, and it is asked at all because add_user also carries
        # runtime notices -- a background job finishing must never get to name
        # the conversation just because the real first message was a greeting.
        first = not any(r.get("t") == "user" for r in self._records) and \
                not any(r.get("t") == "user" for r in self._pending)
        rec = {"t": "user", "ts": store.now_iso(), "text": text}
        # Attachments: [{"name": "shot.png", "b64": "..."}]. Recorded on the
        # turn they arrived with, because that is what they are -- part of what
        # the user said, not a tool result that happened to be near it. The
        # base64 is swapped for a blob ref on the way to disk (_split below),
        # so messages.jsonl stays a file you can read.
        if images: rec["images"] = list(images)
        self._pending.append(rec)
        if first and not self.meta.get("title") and self.meta.get("title_source", "") in ("", "stub"):
            from integrations.memory import naming
            # "" for a greeting, deliberately: a frontend showing the session id
            # is telling the truth and a session called "hi" is not. The namer
            # fills it in once there is something to name.
            self.meta["title"], self.meta["title_source"] = naming.provisional(text), "stub"

    def add_recall(self, text: str):
        """A pointer block the runtime put in front of the model.

        Its own record type rather than glued onto the user's text: the records
        are what the conversation *was*, and a line the user never typed must
        not read back as one. replay folds it into the user turn it belongs to,
        which is where the provider needs it -- two user messages in a row is a
        400 on anthropic.
        """
        self._pending.append({"t": "recall", "ts": store.now_iso(), "text": text})

    def set_title(self, title: str, source: str = "auto"):
        self.meta["title"], self.meta["title_source"] = title, source
        if self._records: store.write_json(self.dir / "meta.json", self.meta)

    def set_frontend(self, name: str):
        """Which frontend owns this session. Stamped from the terminal
        envelope's term_program, so a controller can tell its own sessions
        from another frontend's and file the latter under that name."""
        if name and name != self.meta.get("frontend"):
            self.meta["frontend"] = name
            if self._records: store.write_json(self.dir / "meta.json", self.meta)

    def set_cwd(self, path: str):
        """Where this conversation is happening. Recorded for recall: matching
        directories is the strongest signal there is for "what did we do here
        last time", and it costs a string in meta.json rather than a read of
        the conversation itself."""
        if path and path != self.meta.get("cwd"):
            self.meta["cwd"] = str(path)
            if self._records: store.write_json(self.dir / "meta.json", self.meta)

    def add_assistant(self, thinking: str, content: str, native: list, usage: dict = None):
        calls = [{"name": c.name, "args": c.args}
                 for c in providers.get(self.provider).parse_calls(native)]
        # Stamped with the provider that produced it: once you can switch
        # mid-session, `native` is only reusable by the dialect that wrote it.
        rec = {"t": "assistant", "ts": store.now_iso(), "p": self.provider,
               "thinking": thinking, "content": content, "calls": calls, "native": native}
        # What the round cost, on the round: one round, one model, unambiguous,
        # and a rewind takes its cost with it. Absent when the provider reported
        # nothing -- not the same as zero, and never recorded as it.
        if usage: rec["u"] = usage
        self._pending.append(rec)

    def add_tool(self, name: str, result: dict):
        self._pending.append({"t": "tool", "ts": store.now_iso(), "name": name, "result": result})

    def commit(self):
        """Append everything recorded since the last commit. Called only at
        round boundaries, where the conversation is known consistent: an
        assistant turn whose tool_calls have no matching results is rejected
        on the next request, so it must never reach disk."""
        if not self._pending: return
        out = [self._split(r) for r in self._pending]
        store.append_jsonl(self.dir / "messages.jsonl", out)
        self._records.extend(out)
        self._pending.clear()
        self._mark = 0
        self.meta.update(updated=store.now_iso(), records=len(self._records),
                         turns=sum(1 for r in self._records if r.get("t") == "assistant"),
                         read_files=_read_files(),
                         # Rolled up here so a question about every conversation
                         # ever can be answered from the meta files alone.
                         # Recomputed rather than incremented: a rewound round
                         # takes its cost with it.
                         usage=usage.of_records(self._records))
        store.write_json(self.dir / "meta.json", self.meta)

    def _split(self, rec: dict) -> dict:
        if rec.get("t") == "tool":
            return {**rec, "result": blobs.split(rec.get("result", {}), self.blobs_dir)}
        # A user turn carrying attachments is the other record with megabytes in
        # it, and it goes out through the same door for the same reason.
        if rec.get("t") == "user" and rec.get("images"):
            return {**rec, "images": blobs.split(rec["images"], self.blobs_dir)}
        return rec

    # ---- loading ----

    @property
    def system(self) -> str:
        """Read fresh, never persisted, and never part of messages(): ollama
        wants it as a first message but anthropic and gemini take it as a
        request parameter, so placement is the provider's call."""
        return store.system_prompt()

    def records(self) -> list[dict]:
        """The canonical records, committed ones first. Frontends rebuilding a
        conversation on screen read these rather than messages(): they are the
        same shape whichever provider wrote them, and they still carry the
        thinking and the tool results that the provider dialects drop."""
        return list(self._records) + list(self._pending)

    def messages(self) -> list[dict]:
        """Provider-shaped conversation history, system prompt excluded."""
        recs = [blobs.rehydrate(r, self.blobs_dir) for r in self._records]
        return replay.to_messages(recs, self.provider, origin=self._origin)

    def set_model(self, model: str):
        """Which model this conversation is on. Recorded for the same reason
        the provider is: a reopened session should say what wrote it."""
        self.meta["model"] = model
        if self._records: store.write_json(self.dir / "meta.json", self.meta)

    def set_provider(self, name: str):
        """Point the conversation at another provider. Records are canonical,
        so messages() rebuilds the whole history in the new dialect -- images
        and all -- instead of starting over."""
        self.meta["provider"] = name
        if self._records: store.write_json(self.dir / "meta.json", self.meta)

    def restore_tool_state(self):
        """Re-seed the edit gate in tools/file, minus anything that moved on
        disk while the session was closed. A stale entry is far worse than a
        missing one: it lets the agent edit against content it has never seen.
        A dropped entry only costs one re-read."""
        fresh = [e["path"] for e in (self.meta.get("read_files") or []) if _unchanged(e)]
        if not fresh: return
        from tools.loader import load_tools
        load_tools(str(store.tools_dir()))   # importing the tools is what puts _common in sys.modules
        _seed_read_files(fresh)

# tools/file/main.py puts its helpers on sys.path directly, so _common lands
# as a top-level module rather than under a package.
def _read_files() -> list[dict]:
    """Snapshot the edit gate with enough to detect drift later. Paths alone
    would be a lie after a reload -- the transcript says the agent read
    foo.py, but that was three days and an outside edit ago."""
    m = sys.modules.get("_common")
    if not m: return []
    out = []
    for p in sorted(getattr(m, "read_files", ())):
        # mtime+size rather than a hash: commit runs every round, and the
        # agent reads files it has no reason to re-checksum that often.
        try: st = os.stat(p)
        except OSError: continue
        out.append({"path": p, "mtime_ns": st.st_mtime_ns, "size": st.st_size})
    return out

def _unchanged(e: dict) -> bool:
    try: st = os.stat(e["path"])
    except (OSError, KeyError, TypeError): return False
    return st.st_mtime_ns == e.get("mtime_ns") and st.st_size == e.get("size")

def _seed_read_files(paths):
    m = sys.modules.get("_common")
    if m and hasattr(m, "read_files"): m.read_files.update(paths)
