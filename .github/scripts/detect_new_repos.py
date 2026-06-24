#!/usr/bin/env python3
"""Detect newly-added entries in Chainguard's public catalog.

Two independent trackers, selected by the CATALOG env var, each with its own
committed state file:

  catalog : the FULL container catalog (~2,656 images) scraped from the public
            Containers Directory at images.chainguard.dev/directory. Names only
            (the directory exposes no per-row timestamp), so it's a name-diff
            tracker. Pages are fetched concurrently.

  skills  : Agent Skills, from `chainctl images repos list --public`, keeping
            repos with NO catalogTier/bundles (images carry them; skills don't).
            These carry createTime.

The companion workflow runs both via a 2-way matrix.

Modes:
  since_last_run : repos never observed in any prior run (both)
  since_date     : createTime after a persisted baseline (skills only)

BOOTSTRAP=true seeds the state file without opening an issue. Run once.
Emitted GitHub outputs: count, names, changed, notify, persist, title.
"""
import os
import re
import sys
import json
import math
import datetime
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CATALOG = os.environ.get("CATALOG", "catalog").strip().lower()   # catalog | skills
STATE_PATH = os.environ.get("STATE", f".state/{CATALOG}.json")
MODE = os.environ.get("MODE", "since_last_run")
DATE_INPUT = os.environ.get("SINCE_DATE", "").strip()
BODY_PATH = os.environ.get("ISSUE_BODY", f"{CATALOG}_body.md")
BOOTSTRAP = os.environ.get("BOOTSTRAP", "").strip().lower() in ("1", "true", "yes")
DIR_CATEGORY = os.environ.get("DIR_CATEGORY", "").strip()        # blank = full directory

REGISTRY = "cgr.dev/chainguard"
DIR_BASE = "https://images.chainguard.dev/directory"
DIR_PAGE_URL = (f"{DIR_BASE}/category/{DIR_CATEGORY}" if DIR_CATEGORY else DIR_BASE)
SLUG_RE = re.compile(r"/directory/image/([a-z0-9][a-z0-9._-]*)/(?:versions|overview)")
UA = "chainguard-catalog-watch/1.0 (+github-actions)"
NOUN = {"catalog": "catalog image", "skills": "skill"}.get(CATALOG, CATALOG)

MAX_DIR_PAGES = 600
FETCH_WORKERS = 12
MAX_ISSUE_ROWS = 100
MAX_ISSUE_CHARS = 60000


def parse_ts(s: str) -> datetime.datetime:
    if not s:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_image(rec: dict) -> bool:
    tier = (rec.get("catalogTier") or "").strip()
    return bool(tier) or bool(rec.get("bundles") or [])


def _fetch(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def list_directory() -> dict:
    """Scrape the public Containers Directory, fetching pages concurrently.

    Page 1 tells us the total count and a representative page size, so we know
    how many pages exist up front instead of walking until empty. Pages 2..N
    are fetched in parallel; a short sequential tail-sweep covers any estimate
    shortfall. Returns {name: ""} (the directory carries no timestamps).
    """
    html1 = _fetch(f"{DIR_PAGE_URL}/1")
    names = set(SLUG_RE.findall(html1))
    if not names:
        return {}

    m = re.search(r"([\d,]{2,})\s+images", html1)
    total = int(m.group(1).replace(",", "")) if m else 0
    page_size = max(len(names), 1)
    link_re = re.compile(re.escape(DIR_PAGE_URL) + r"/(\d+)\b")
    visible = [int(x) for x in link_re.findall(html1)] or [1]
    est = math.ceil(total / page_size) if total else 0
    last = min(MAX_DIR_PAGES, max(est, max(visible)) + 5)   # generous upper bound

    urls = [f"{DIR_PAGE_URL}/{p}" for p in range(2, last + 1)]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        for html in ex.map(_fetch, urls):
            names |= set(SLUG_RE.findall(html))

    # Tail safety: if we're still short of the advertised total, sweep forward
    # until two consecutive pages add nothing. Rarely runs.
    page, misses = last + 1, 0
    while total and len(names) < total and page <= MAX_DIR_PAGES and misses < 2:
        before = len(names)
        names |= set(SLUG_RE.findall(_fetch(f"{DIR_PAGE_URL}/{page}")))
        misses = misses + 1 if len(names) == before else 0
        page += 1

    return {n: "" for n in sorted(names)}


def list_skills():
    out = subprocess.check_output(
        ["chainctl", "images", "repos", "list", "--public", "-o", "json"]
    )
    data = json.loads(out)
    items = data.get("items", data) if isinstance(data, dict) else data
    repos, meta = {}, {}
    for r in items:
        name = r.get("name") or r.get("repo")
        if not name or is_image(r):
            continue
        repos[name] = r.get("createTime", "")
        meta[name] = {"tier": r.get("catalogTier", ""), "bundles": r.get("bundles", [])}
    return repos, meta


def list_repos():
    if CATALOG == "skills":
        return list_skills()
    repos = list_directory()
    return repos, {n: {} for n in repos}


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
    scope = f" ({DIR_CATEGORY})" if (CATALOG == "catalog" and DIR_CATEGORY) else ""
    title = f"## New {NOUN}s in the public Chainguard catalog{scope}"
    if not new:
        return f"{title}\n\n_No new {NOUN}s {baseline}._"
    names = sorted(new)
    head = [title, "", f"Detected {len(names)} new {NOUN}(s) {baseline}.", ""]

    if CATALOG == "catalog":
        head += ["| Image | Reference | Directory |", "|---|---|---|"]
        def row(n):
            return (f"| `{n}` | `{REGISTRY}/{n}` "
                    f"| [page]({DIR_BASE}/image/{n}/versions) |")
        footer = (
            "\n\nThese are catalog entries (mostly Production images requiring "
            "an entitlement to pull). To bring one into the org, provision it "
            "(`chainctl starter add-images <name>` or the Console 'Add to org')."
        )
    else:
        head += ["| Skill | OCI reference |", "|---|---|"]
        def row(n):
            return f"| `{n}` | `{REGISTRY}/{n}` |"
        footer = (
            "\n\nNote: Agent Skills, not container images. `chainctl skills pull` "
            "retrieves them (needs a chainctl build with the `skills` command)."
        )

    shown = names if limit is None else names[:limit]
    while True:
        rows = [row(n) for n in shown]
        more = len(names) - len(shown)
        extra = (f"\n\n...and {more} more -- see the workflow run summary."
                 if more > 0 else "")
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

    # A near-empty scrape is a fetch failure, not a shrunken catalog. Skip
    # rather than flag the whole catalog as removed / later re-fire it as new.
    if CATALOG == "catalog" and not BOOTSTRAP and len(current) < 100:
        write_summary(
            f"## Catalog scrape returned only {len(current)} entries -- "
            "treating as a fetch failure and skipping this run."
        )
        emit("changed", "false")
        emit("notify", "false")
        emit("persist", "false")
        return 0

    if BOOTSTRAP:
        write_summary(
            f"## Bootstrap ({CATALOG})\n\nRecorded a baseline of {len(current)} "
            f"{NOUN}(s). No issue opened. Future runs report only new additions."
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

    write_summary(render(new, meta, baseline))

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
