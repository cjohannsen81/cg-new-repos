#!/usr/bin/env python3
"""Detect newly-added image repositories in the PUBLIC Chainguard catalog.

Watches the public Chainguard registry (cgr.dev/chainguard/*) and reports image
repos that have appeared since a baseline, so you can decide whether to mirror
them into your own org. Reads the catalog with `chainctl images repos list
--public` -- no org / descendants_of plumbing.

Modes (the comparison baseline), both backed by a single committed state file:
  since_last_run : repos never observed in any prior run (the catalog diff)
  since_date     : repos whose createTime is after a persisted baseline date
                   (only meaningful if the listing returns createTime)

BOOTSTRAP=true seeds `seen` from the current catalog WITHOUT opening an issue.
Run it once on a fresh repo so the first real run reports deltas, not the whole
catalog.

Emitted GitHub outputs:
  count   : number of new images
  names   : comma-separated names
  changed : "true" if new images were found
  notify  : "true" -> open an issue (new images AND not bootstrap)
  persist : "true" -> commit the state file (new images OR bootstrap)
  title   : issue title (only when notify)

State file (.state/repos.json):
  { "since_date": "...", "last_run": "...", "seen": { name: createTime } }
"""
import os
import sys
import json
import datetime
import subprocess

STATE_PATH = os.environ.get("STATE", ".state/repos.json")
MODE = os.environ.get("MODE", "since_last_run")
DATE_INPUT = os.environ.get("SINCE_DATE", "").strip()
BODY_PATH = os.environ.get("ISSUE_BODY", "new_images_body.md")
BOOTSTRAP = os.environ.get("BOOTSTRAP", "").strip().lower() in ("1", "true", "yes")

REGISTRY = "cgr.dev/chainguard"
DIRECTORY = "https://images.chainguard.dev/directory/image"  # verify the page suffix once

MAX_ISSUE_ROWS = 100        # cap rows in the issue (GitHub body limit is 65536 chars)
MAX_ISSUE_CHARS = 60000     # hard ceiling, well under GitHub's limit


def parse_ts(s: str) -> datetime.datetime:
    if not s:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def list_repos():
    """Return (repos, meta): {name: createTime}, {name: {tier, bundles}}."""
    out = subprocess.check_output(
        ["chainctl", "images", "repos", "list", "--public", "-o", "json"]
    )
    data = json.loads(out)
    items = data.get("items", data) if isinstance(data, dict) else data
    repos, meta = {}, {}
    for r in items:
        name = r.get("name") or r.get("repo")
        if not name:
            continue
        repos[name] = r.get("createTime", "")
        meta[name] = {"tier": r.get("tier", ""), "bundles": r.get("bundles", [])}
    return repos, meta


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


def write_summary(text: str):
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")


def _row(n: str, meta: dict) -> str:
    tier = meta.get(n, {}).get("tier") or "-"
    return f"| `{n}` | {tier} | `{REGISTRY}/{n}:latest` | [page]({DIRECTORY}/{n}/overview) |"


def render(new: dict, meta: dict, baseline: str, limit: int = None) -> str:
    """Full report (limit=None) for the step summary, or capped for an issue."""
    if not new:
        return f"## New images in the public Chainguard catalog\n\n_No new images {baseline}._"
    names = sorted(new)
    head = [
        "## New images in the public Chainguard catalog", "",
        f"Detected {len(names)} new image(s) {baseline}.", "",
        "| Image | Tier | Pull reference | Directory |", "|---|---|---|---|",
    ]
    footer = (
        "\n\nDecide whether to mirror any of these into the org. The "
        "`image-copy-gcp` / `image-copy-ecr` examples in "
        "`chainguard-demo/platform-examples` handle the adoption step."
    )
    shown = names if limit is None else names[:limit]
    while True:
        rows = [_row(n, meta) for n in shown]
        more = len(names) - len(shown)
        extra = (
            f"\n\n...and {more} more -- see the workflow run summary for the full list."
            if more > 0 else ""
        )
        body = "\n".join(head + rows) + extra + footer
        if limit is None or len(body) <= MAX_ISSUE_CHARS or len(shown) <= 1:
            return body
        shown = shown[: max(1, len(shown) // 2)]   # shrink until under the ceiling


def persist(state: dict, seen: dict, current: dict, now: str):
    seen.update(current)
    state["seen"] = seen
    state["last_run"] = now
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = load_state()
    seen = state.get("seen", {})

    if DATE_INPUT:
        state["since_date"] = DATE_INPUT
    since_date = state["since_date"]

    current, meta = list_repos()

    if BOOTSTRAP:
        write_summary(
            f"## Bootstrap\n\nRecorded a baseline of {len(current)} image(s). "
            "No issue opened. Future runs will report only new additions."
        )
        emit("count", str(len(current)))
        emit("changed", "false")
        emit("notify", "false")
        emit("persist", "true")
        persist(state, seen, current, now)
        return 0

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

    write_summary(render(new, meta, baseline))          # full table -> summary

    emit("count", str(len(new)))
    emit("names", ",".join(sorted(new)))
    emit("changed", "true" if new else "false")
    emit("notify", "true" if new else "false")
    emit("persist", "true" if new else "false")

    if new:
        shown = sorted(new)
        head = ", ".join(shown[:5]) + ("..." if len(shown) > 5 else "")
        emit("title", f"New Chainguard catalog images: {head} ({len(new)})")
        with open(BODY_PATH, "w") as f:
            f.write(render(new, meta, baseline, limit=MAX_ISSUE_ROWS) + "\n")
        persist(state, seen, current, now)

    return 0


if __name__ == "__main__":
    sys.exit(main())
