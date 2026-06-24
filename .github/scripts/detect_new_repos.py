#!/usr/bin/env python3
"""Detect newly-added repositories in the PUBLIC Chainguard catalog.

The public registry mixes two products under one namespace: container images
and Agent Skills. `chainctl images repos list --public` returns both. We
partition them on the `catalogTier` / `bundles` fields (images carry a tier and
bundles; skills carry neither) and track each in its own state file, so a
skills bulk-drop never buries a new image and vice versa.

Selected by the CATALOG env var (images | skills). Run once per catalog -- the
companion workflow does this with a 2-way matrix.

Modes (the comparison baseline), per-catalog state file:
  since_last_run : repos never observed in any prior run (the catalog diff)
  since_date     : repos whose createTime is after a persisted baseline date

BOOTSTRAP=true seeds the state file from the current catalog WITHOUT opening an
issue. Run once on a fresh repo so the first real run reports deltas.

Emitted GitHub outputs: count, names, changed, notify, persist, title.

State file (.state/<catalog>.json):
  { "since_date": "...", "last_run": "...", "seen": { name: createTime } }
"""
import os
import sys
import json
import datetime
import subprocess

CATALOG = os.environ.get("CATALOG", "images").strip().lower()   # images | skills
STATE_PATH = os.environ.get("STATE", f".state/{CATALOG}.json")
MODE = os.environ.get("MODE", "since_last_run")
DATE_INPUT = os.environ.get("SINCE_DATE", "").strip()
BODY_PATH = os.environ.get("ISSUE_BODY", f"{CATALOG}_body.md")
BOOTSTRAP = os.environ.get("BOOTSTRAP", "").strip().lower() in ("1", "true", "yes")

REGISTRY = "cgr.dev/chainguard"
DIRECTORY = "https://images.chainguard.dev/directory/image"   # <name>/versions
NOUN = "container image" if CATALOG == "images" else "skill"

MAX_ISSUE_ROWS = 100        # cap rows in the issue (GitHub body limit is 65536 chars)
MAX_ISSUE_CHARS = 60000     # hard ceiling, well under GitHub's limit


def parse_ts(s: str) -> datetime.datetime:
    if not s:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_image(rec: dict) -> bool:
    """Container images carry a catalogTier and/or bundles; skills carry neither."""
    tier = (rec.get("catalogTier") or "").strip()
    bundles = rec.get("bundles") or []
    return bool(tier) or bool(bundles)


def list_repos():
    """List the public catalog and keep only the records for CATALOG.

    Returns (repos, meta): {name: createTime}, {name: {tier, bundles}}.
    """
    out = subprocess.check_output(
        ["chainctl", "images", "repos", "list", "--public", "-o", "json"]
    )
    data = json.loads(out)
    items = data.get("items", data) if isinstance(data, dict) else data
    want_image = CATALOG == "images"
    repos, meta = {}, {}
    for r in items:
        name = r.get("name") or r.get("repo")
        if not name or is_image(r) != want_image:
            continue
        repos[name] = r.get("createTime", "")
        meta[name] = {"tier": r.get("catalogTier", ""), "bundles": r.get("bundles", [])}
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


def render(new: dict, meta: dict, baseline: str, limit: int = None) -> str:
    title = f"## New {NOUN}s in the public Chainguard catalog"
    if not new:
        return f"{title}\n\n_No new {NOUN}s {baseline}._"
    names = sorted(new)
    head = [title, "", f"Detected {len(names)} new {NOUN}(s) {baseline}.", ""]

    if CATALOG == "images":
        head += ["| Image | Tier | Pull reference | Directory |", "|---|---|---|---|"]
        def row(n):
            tier = meta.get(n, {}).get("tier") or "-"
            return (f"| `{n}` | {tier} | `{REGISTRY}/{n}:latest` "
                    f"| [page]({DIRECTORY}/{n}/versions) |")
        footer = (
            "\n\nDecide whether to mirror any of these into the org. The "
            "`image-copy-gcp` / `image-copy-ecr` examples in "
            "`chainguard-demo/platform-examples` handle the adoption step."
        )
    else:
        head += ["| Skill | OCI reference |", "|---|---|"]
        def row(n):
            return f"| `{n}` | `{REGISTRY}/{n}` |"
        footer = (
            "\n\nNote: these are Agent Skills, not container images. "
            "`chainctl skills pull` retrieves them (requires a chainctl build "
            "with the `skills` command -- `chainctl update` if yours lacks it)."
        )

    shown = names if limit is None else names[:limit]
    while True:
        rows = [row(n) for n in shown]
        more = len(names) - len(shown)
        extra = (
            f"\n\n...and {more} more -- see the workflow run summary for the full list."
            if more > 0 else ""
        )
        body = "\n".join(head + rows) + extra + footer
        if limit is None or len(body) <= MAX_ISSUE_CHARS or len(shown) <= 1:
            return body
        shown = shown[: max(1, len(shown) // 2)]


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
            f"## Bootstrap ({CATALOG})\n\nRecorded a baseline of {len(current)} "
            f"{NOUN}(s). No issue opened. Future runs will report only new additions."
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
        emit("title", f"New Chainguard {CATALOG}: {head} ({len(new)})")
        with open(BODY_PATH, "w") as f:
            f.write(render(new, meta, baseline, limit=MAX_ISSUE_ROWS) + "\n")
        persist(state, seen, current, now)

    return 0


if __name__ == "__main__":
    sys.exit(main())
