#!/usr/bin/env python3
"""Drives the real worker over the wire - the check the ?demo harness cannot
be, because the demo paints its lists fake and never folds a real reply.

Boots main.py against a throwaway CUACODE_HOME, sends the terminal envelope,
then walks the project.* and session.pin commands, asserting every reply
shape the bridge frontend folds on (an envelope type with no data.state -
the wire guarantee the boot archive bug taught us to pin). Exits nonzero on
any failure, so a fresh agent can run it as the first thing after touching
the worker or the sidebar.

    python3 lifecheck-projects.py
"""

import json, os, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    home = Path(tempfile.mkdtemp(prefix="lifecheck-"))
    # The store resolves paths on add (macOS /tmp -> /private/var), so the
    # assertions compare against the same normalization.
    workdir = str(Path(home).resolve())
    workname = Path(workdir).name
    env = dict(os.environ, CUACODE_HOME=str(home))
    p = subprocess.Popen(
        [sys.executable, str(REPO / "main.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(REPO),
    )

    def send(eid, type_, data):
        p.stdin.write(json.dumps({"type": type_, "id": eid, "data": data}) + "\n")
        p.stdin.flush()

    def reply_for(eid, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                env = json.loads(line)
            except json.JSONDecodeError:
                continue
            if env.get("id") == eid:
                return env
        return None

    def cmd(eid, action, data=None):
        send(eid, "cmd", {"action": action, **(data or {})})
        return reply_for(eid)

    try:
        # The terminal envelope makes the boot session real (persist with the
        # cwd), the way every frontend starts its worker.
        send("t0", "terminal", {"term_program": "CuaCode", "cwd": str(home)})
        reply_for("t0")

        # The ready line is first on the wire and carries the session id.
        # Whatever its exact shape, the boot session's folder exists now.
        store = home / "sessions"
        sessions = [d for d in store.iterdir() if d.is_dir()] if store.is_dir() else []
        check("boot session born at the terminal envelope", len(sessions) == 1,
              f"found {len(sessions)} session folders")
        sid = sessions[0].name if sessions else ""

        # ---- projects ----
        r = cmd("c1", "project.list")
        check("project.list replies typed envelope",
              r and r.get("type") == "projects" and "state" not in (r.get("data") or {}),
              str(r))
        check("project.list starts empty", (r or {}).get("data", {}).get("projects") == [])

        r = cmd("c2", "project.add", {"dir": str(home)})
        check("project.add replies the fresh list",
              (r or {}).get("data", {}).get("projects") == [workdir], str(r))
        check("projects.json written",
              json.loads((home / "projects.json").read_text()).get("projects") == [workdir])

        r = cmd("c3", "project.add", {"dir": str(home)})
        check("project.add is idempotent", len((r or {}).get("data", {}).get("projects", [])) == 1)

        r = cmd("c4", "project.add", {"dir": "/definitely/not/here"})
        check("project.add rejects a missing dir",
              r and r.get("type") == "status" and r.get("data", {}).get("state") == "error", str(r))

        r = cmd("c5", "project.pin", {"name": "lifecheck-home", "on": True})
        check("project.pin persists and rides the reply",
              (r or {}).get("data", {}).get("pinned") == ["lifecheck-home"], str(r))

        r = cmd("c6", "project.pin", {"name": "lifecheck-home", "on": False})
        check("project.unpin empties the order", (r or {}).get("data", {}).get("pinned") == [], str(r))

        r = cmd("c6", "project.remove", {"dir": workname})
        check("project.remove matches by basename", (r or {}).get("data", {}).get("projects") == [], str(r))

        r = cmd("c7", "project.remove", {"dir": "nope"})
        check("project.remove of an unknown name is a no-op",
              (r or {}).get("data", {}).get("projects") == [] and (r or {}).get("data", {}).get("removed") == "", str(r))

        # ---- project details (custom name + emoji) ----
        r = cmd("c11", "project.add", {"dir": str(home)})
        r = cmd("c12", "project.rename", {"dir": str(home), "name": "Lifecheck Home", "icon": "grad:2"})
        d = (r or {}).get("data") or {}
        check("project.rename replies names + icons",
              d.get("names", {}).get(workdir) == "Lifecheck Home" and d.get("icons", {}).get(workdir) == "grad:2", str(r))
        disk = json.loads((home / "projects.json").read_text())
        check("details are on projects.json",
              disk.get("names", {}).get(workdir) == "Lifecheck Home" and disk.get("icons", {}).get(workdir) == "grad:2",
              str(disk))
        r = cmd("c12b", "project.rename", {"dir": str(home), "icon": "\U0001f680"})
        d = (r or {}).get("data") or {}
        check("a gradient swaps for an emoji", d.get("icons", {}).get(workdir) == "\U0001f680", str(r))
        img = "img:data:image/png;base64,iVBORw0KGgo="
        r = cmd("c12d", "project.rename", {"dir": str(home), "icon": img})
        d = (r or {}).get("data") or {}
        check("an uploaded image is stored as a data url", d.get("icons", {}).get(workdir) == img, str(r))
        r = cmd("c12c", "project.rename", {"dir": str(home), "icon": "this is way too long for an icon"})
        d = (r or {}).get("data") or {}
        check("junk icons are dropped, not stored", d.get("icons", {}).get(workdir) is None, str(r))
        r = cmd("c13", "project.rename", {"dir": str(home), "name": "", "icon": ""})
        d = (r or {}).get("data") or {}
        check("empty rename clears the fields",
              d.get("names", {}).get(workdir) is None and d.get("icons", {}).get(workdir) is None, str(r))
        r = cmd("c14", "project.rename", {"dir": "/not/promoted", "name": "x"})
        check("rename of an unpromoted dir is an error",
              r and r.get("type") == "status" and r.get("data", {}).get("state") == "error", str(r))
        r = cmd("c15", "project.remove", {"dir": workname})
        d = (r or {}).get("data") or {}
        check("remove drops the details too",
              d.get("projects") == [] and d.get("names", {}).get(workdir) is None, str(r))

        # ---- move (explicit reorder, never a drag) ----
        cmd("m0", "project.add", {"dir": str(home)})
        other = str(Path(tempfile.mkdtemp(prefix="lifecheck2-")).resolve())
        cmd("m1", "project.add", {"dir": other})
        r = cmd("m2", "project.move", {"name": Path(other).name, "dir": other, "delta": -1})
        d = (r or {}).get("data") or {}
        check("move within promotion order", d.get("projects") == [other, workdir], str(d.get("projects")))
        cmd("m3", "project.pin", {"name": Path(workdir).name, "on": True})
        cmd("m4", "project.pin", {"name": Path(other).name, "on": True})
        r = cmd("m5", "project.move", {"name": Path(other).name, "delta": -1})
        d = (r or {}).get("data") or {}
        check("move within pin order", d.get("pinned") == [Path(other).name, Path(workdir).name], str(d.get("pinned")))
        r = cmd("m6", "project.move", {"name": "nope", "dir": "/x", "delta": -1})
        check("move of an unknown group is a no-op", r and r.get("type") == "projects", str(r))

        # ---- session pin ----
        r = cmd("c8", "session.pin", {"id": sid, "on": True})
        metas = (r or {}).get("data", {}).get("sessions", [])
        mine = next((m for m in metas if m.get("id") == sid), {})
        check("session.pin replies a fresh sessions list", r and r.get("type") == "sessions", str(r))
        check("the live session's meta carries the pin", bool(mine.get("pinned")), str(mine))
        ts = mine.get("pinned", "")

        # The pin must survive the session's own commit(), which rewrites
        # meta.json from memory: read it back off disk.
        disk = json.loads((store / sid / "meta.json").read_text())
        check("the pin is on disk", disk.get("pinned") == ts, str(disk.get("pinned")))

        r = cmd("c9", "session.pin", {"id": sid, "on": False})
        metas = (r or {}).get("data", {}).get("sessions", [])
        mine = next((m for m in metas if m.get("id") == sid), {})
        check("unpin clears the meta", not mine.get("pinned"), str(mine))

        r = cmd("c10", "session.pin", {"id": "does-not-exist", "on": True})
        check("session.pin of an unknown id is an error, not a crash",
              r and r.get("type") == "status" and r.get("data", {}).get("state") == "error", str(r))

        # The wire shapes the frontend folds, one last time, in one place:
        r = cmd("c16", "project.list")
        d = (r or {}).get("data") or {}
        check("projects reply shape (projects + pinned + names + icons, no state)",
              r.get("type") == "projects" and all(isinstance(d.get(k), list) for k in ("projects", "pinned"))
              and isinstance(d.get("names"), dict) and isinstance(d.get("icons"), dict),
              str(r))

        send("stop", "cmd", {"action": "stop"})
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            check("worker exits on stop", False, "timed out")
    finally:
        if p.poll() is None:
            p.kill()

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        sys.exit(1)
    print("all green")


if __name__ == "__main__":
    main()