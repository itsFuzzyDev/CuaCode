import sqlite3, json
from pathlib import Path

DB = Path(__file__).parent / "tasks.db"

def _db():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL, done INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))")
    conn.commit()
    return conn

def run(args: dict, ctx) -> dict:
    conn = _db()
    action = args["action"]
    try:
        if action == "add":
            desc = args.get("task")
            if not desc: return {"error": "task description required"}
            cur = conn.execute("INSERT INTO tasks (description) VALUES (?)", (desc,))
            conn.commit()
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
            return {"task": dict(row)}

        elif action == "done":
            tid = args.get("id")
            if not tid: return {"error": "task id required"}
            conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (tid,))
            conn.commit()
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            if not row: return {"error": f"task {tid} not found"}
            return {"task": dict(row)}

        elif action == "delete":
            tid = args.get("id")
            if not tid: return {"error": "task id required"}
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            conn.commit()
            return {"deleted": tid}

        elif action == "clear":
            status = args.get("status", "all")
            if status == "done": conn.execute("DELETE FROM tasks WHERE done = 1")
            elif status == "pending": conn.execute("DELETE FROM tasks WHERE done = 0")
            else: conn.execute("DELETE FROM tasks")
            conn.commit()
            return {"cleared": status}

        elif action == "list":
            status = args.get("status", "all")
            if status == "done": rows = conn.execute("SELECT * FROM tasks WHERE done = 1 ORDER BY id").fetchall()
            elif status == "pending": rows = conn.execute("SELECT * FROM tasks WHERE done = 0 ORDER BY id").fetchall()
            else: rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            return {"tasks": [dict(r) for r in rows]}

        return {"error": f"unknown action: {action}"}
    finally:
        conn.close()
