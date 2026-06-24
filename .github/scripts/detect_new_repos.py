#!/usr/bin/env python3
"""Detect newly-added image repositories in a Chainguard registry.

Two modes, both backed by a single committed state file (.state/repos.json):

  since_last_run : report repos never observed in any prior run
  since_date     : report repos whose createTime is after a persisted baseline
                   date, that we haven't already reported

State file shape:
{
  "since_date": "2026-01-01T00:00:00Z",   # persisted baseline for since_date mode
  "last_run":   "2026-06-24T15:00:00Z",   # informational
  "seen":       { "<repo-name>": "<createTime>", ... }   # observation log
}
"""
import os
import sys
import json
import datetime
import subprocess
import urllib.request

API = os.environ["API"]
ORG_ID = os.environ["ORG_ID"]
STATE_PATH = os.environ.get("STATE", ".state/repos.json")
MODE = os.environ.get("MODE", "since_last_run")        # since_last_run | since_date
DATE_INPUT = os.environ.get("SINCE_DATE", "").strip()  # optional override for since_date


def parse_ts(s: str) -> datetime.datetime:
    if not s:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def token() -> str:
    return subprocess.check_output(["chainctl", "auth", "token"]).decode().strip()


def list_repos(tok: str) -> dict:
    """Return {repo_name: createTime}, following pagination."""
    repos, page = {}, ""
    while True:
        url = (
            f"{API}/registry/v2beta1/repos"
            f"?uidp.descendants_of={ORG_ID}&page_size=1000&page_token={page}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
        for r in data.get("repos", []):
            repos[r["name"]] = r.get("createTime", "")
        page = data.get("next_page_token") or ""
        if not page:
            break
    return repos


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"since_date": "1970-01-01T00:00:00Z", "last_run": None, "seen": {}}


def emit(key: str, value: str):
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"{key}={value}\n")


def report(new: dict, baseline: str):
    lines = [f"### New images in registry ({baseline})", ""]
    if new:
        lines += [f"- `{n}`  created {new[n]}" for n in sorted(new)]
    else:
        lines.append("_No new images._")
    out = "\n".join(lines)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(out + "\n")


def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = load_state()
    seen = state.get("seen", {})

    if DATE_INPUT:                       # one-off override persists going forward
        state["since_date"] = DATE_INPUT
    since_date = state["since_date"]

    current = list_repos(token())
    unseen = {n: ct for n, ct in current.items() if n not in seen}

    if MODE == "since_date":
        cutoff = parse_ts(since_date)
        new = {n: ct for n, ct in unseen.items() if parse_ts(ct) > cutoff}
        baseline = f"since {since_date}"
    else:
        new = unseen
        baseline = (
            f"since last run ({state['last_run']})"
            if state.get("last_run") else "since last run (first run)"
        )

    report(new, baseline)
    emit("count", str(len(new)))
    emit("names", ",".join(sorted(new)))
    emit("changed", "true" if new else "false")

    # Persist: the observation log always absorbs the full current set, and we
    # only write/commit when there is something new (avoids hourly churn).
    if new:
        seen.update(current)
        state["seen"] = seen
        state["last_run"] = now
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
