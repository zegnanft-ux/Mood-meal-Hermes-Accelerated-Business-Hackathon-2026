#!/usr/bin/env python3
"""MoodMeal dashboard server.

Serves console.html + the state files, but with one bit of smarts: every time the
dashboard polls state/schedule.json, we reconcile it against the LIVE cron jobs
(~/.hermes/cron/jobs.json). A one-shot cron self-removes from jobs.json the moment
it fires, so any schedule entry whose job_id is no longer a live cron is dropped.

Result: a scheduled order shows while it's pending and disappears once it has run.
Recurring crons stay in jobs.json, so recurring standing orders keep showing.
No dependence on the agent cleaning up after itself.
"""
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS = Path.home() / ".hermes" / "cron" / "jobs.json"
SCHEDULE = ROOT / "state" / "schedule.json"


def _live_job_ids() -> set:
    """IDs of crons that still exist (pending or recurring). Fired one-shots are gone."""
    try:
        data = json.loads(JOBS.read_text(encoding="utf-8"))
        return {j.get("id") for j in data.get("jobs", []) if j.get("id")}
    except Exception:
        # If we can't read jobs.json, don't nuke the panel — show what we have.
        return None


def _reconcile_schedule() -> list:
    """Drop schedule entries whose cron no longer exists; persist the cleaned list."""
    try:
        items = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    except Exception:
        return []
    live = _live_job_ids()
    if live is None:
        return items
    kept = [it for it in items
            # keep if its cron is still live, or if it has no job_id to verify against
            if (it.get("job_id") in live) or (it.get("job_id") is None)]
    if len(kept) != len(items):
        try:
            SCHEDULE.write_text(json.dumps(kept, indent=2), encoding="utf-8")
        except Exception:
            pass
    return kept


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/state/schedule.json", "state/schedule.json"):
            body = json.dumps(_reconcile_schedule()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass  # quiet


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"MoodMeal dashboard on http://localhost:{port}/console.html (reconciling schedule.json)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
