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
    def create(cls, provider: str = "ollama", model: str = "", effort_level: str = "") -> "Session":
        sid = store.new_id()
        now = store.now_iso()
        return cls(sid, {"id": sid, "created": now, "updated": now, "provider": provider,
                         "origin": provider, "model": model, "effort": effort_level,
                         "title": "", "records": 0, "turns": 0, "read_files": []}, [])

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
        """Session-scoped scratch space, created on demand."""
        d = self.dir / "notebook"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- recording ----

    def round_start(self):
        """Mirror of the agent loop's rollback point. A cancel rewinds to
        here; rounds that completed earlier are kept."""
        self._mark = len(self._pending)

    def rewind(self): del self._pending[self._mark:]

    # Every record is stamped when it happens, so a user record is when it was
    # sent and an assistant record is when it landed -- no second field needed.
    # replay only reads the keys it knows, so `ts` never reaches the provider.
    def add_user(self, text: str):
        self._pending.append({"t": "user", "ts": store.now_iso(), "text": text})
        if not self.meta.get("title"): self.meta["title"] = text[:60]

    def add_assistant(self, thinking: str, content: str, native: list, usage: dict = None):
        calls = [{"name": c.name, "args": c.args}
                 for c in providers.get(self.provider).parse_calls(native)]
        # Stamped with the provider that produced it: once you can switch
        # mid-session, `native` is only reusable by the dialect that wrote it.
        rec = {"t": "assistant", "ts": store.now_iso(), "p": self.provider,
               "thinking": thinking, "content": content, "calls": calls, "native": native}
        # What the round cost, on the round rather than on a counter somewhere
        # else. It is the only place the numbers are unambiguous -- one round,
        # one model, one set of counts -- and it means a rewind takes the cost
        # away with the turn instead of leaving the session charged for it.
        # Absent when the provider reported nothing, which is not the same as
        # zero and must not be recorded as it.
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
        if rec.get("t") != "tool": return rec
        return {**rec, "result": blobs.split(rec.get("result", {}), self.blobs_dir)}

    # ---- loading ----

    @property
    def system(self) -> str:
        """Read fresh, never persisted, and never part of messages(): ollama
        wants it as a first message but anthropic and gemini take it as a
        request parameter, so placement is the provider's call."""
        return store.system_prompt("v1")

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
