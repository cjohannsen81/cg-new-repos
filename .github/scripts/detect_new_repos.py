#!/usr/bin/env python3
"""Detect newly-added image repositories in the PUBLIC Chainguard catalog.

Watches the public Chainguard registry (cgr.dev/chainguard/*) and reports image
repos that have appeared since a baseline, so you can decide whether to mirror
them into your own org. Reads the catalog with `chainctl images repos list
--public` -- no org / descendants_of plumbing, since the public catalog is not
part of your IAM tree.

Two modes, both backed by a single committed state file (.state/repos.json):

  since_last_run : repos never observed in any prior run (the catalog diff;
                   this is the primary "what's new to adopt" signal)
  since_date     : repos whose createTime is after a persisted baseline date
                   (only meaningful if the public listing returns createTime --
                   verify with `chainctl images repos list --public -o json`)

State file shape:
{
  "since_date": "2026-01-01T00:00:00Z",   # persisted baseline for since_date mode
  "last_run":   "2026-06-24T13:00:00Z",   # informational
  "seen":       { "<repo-name>": "<createTime-or-empty>", ... }   # observation log
}

Note: both modes share the single `seen` log, and every run with findings
absorbs the full current catalog into it. Pick one mode as the steady-state
cron; the other is for ad-hoc queries.
"""
import os
import sys
import json
import datetime
import subprocess

STATE_PATH = os.environ.get("STATE", ".state/repos.json")
MODE = os.environ.get("MODE", "since_last_run")          # since_last_run | since_date
DATE_INPUT = os.environ.get("SINCE_DATE", "").strip()    # optional override for since_date
BODY_PATH = os.environ.get("ISSUE_BODY", "new_images_body.md")

REGISTRY = "cgr.dev/chainguard"
DIRECTORY = "https://images.chainguard.dev/directory/image"   # verify the page suffix once


def parse_ts(s: str) -> datetime.datetime:
    if not s:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def list_repos():
    """Return (repos, meta) for the public Chainguard catalog.

    repos: {name: createTime}
    meta:  {name: {"tier": str, "bundles": list}}
    Field names follow the -o json shape; adjust the .get() keys if a manual
    run shows different names.
    """
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


def render(new: dict, meta: dict, baseline: str) -> str:
    lines = ["## New images in the public Chainguard catalog", ""]
    if not new:
        lines.append(f"_No new images {baseline}._")
        return "\n".join(lines)
    lines.append(f"Detected {len(new)} new image(s) {baseline}.")
    lines.append("")
    lines.append("| Image | Tier | Pull reference | Directory |")
    lines.append("|---|---|---|---|")
    for n in sorted(new):
        tier = meta.get(n, {}).get("tier") or "-"
        lines.append(
            f"| `{n}` | {tier} | `{REGISTRY}/{n}:latest` | [page]({DIRECTORY}/{n}/overview) |"
        )
    lines.append("")
    lines.append(
        "Decide whether to mirror any of these into the org. The "
        "`image-copy-gcp` / `image-copy-ecr` examples in "
        "`chainguard-demo/platform-examples` handle the adoption step."
    )
    return "\n".join(lines)


def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = load_state()
    seen = state.get("seen", {})

    if DATE_INPUT:                       # a one-off override persists going forward
        state["since_date"] = DATE_INPUT
    since_date = state["since_date"]

    current, meta = list_repos()
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

    report = render(new, meta, baseline)
    write_summary(report)

    emit("count", str(len(new)))
    emit("names", ",".join(sorted(new)))
    emit("changed", "true" if new else "false")
    if new:
        shown = sorted(new)
        head = ", ".join(shown[:5]) + ("..." if len(shown) > 5 else "")
        emit("title", f"New Chainguard catalog images: {head} ({len(new)})")
        with open(BODY_PATH, "w") as f:
            f.write(report + "\n")

    # Persist only when there is something new (keeps git history quiet). The
    # observation log absorbs the full current catalog so nothing re-fires.
    if new:
        seen.update(current)
        state["seen"] = seen
        state["last_run"] = now
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
