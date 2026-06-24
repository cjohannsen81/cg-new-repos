#!/usr/bin/env python3
"""Detect newly-added images in Chainguard's public Containers Directory.

Scrapes the full catalog (~2,656 images) from images.chainguard.dev/directory
and reports image names that have appeared since the last run, so you can
decide whether to provision them into your org. Pure HTTP -- no chainctl, no
auth, no entitlement.

The directory exposes names only (no per-row timestamp), so this is a name-diff
tracker against a committed state file (.state/catalog.json):
  { "last_run": "...", "seen": { name: "" } }

BOOTSTRAP=true seeds the baseline without opening an issue. Run once.
Emitted GitHub outputs: count, names, changed, notify, persist, title.
"""
import os
import re
import sys
import json
import math
import datetime
import urllib.request
from concurrent.futures import ThreadPoolExecutor

STATE_PATH = os.environ.get("STATE", ".state/catalog.json")
BODY_PATH = os.environ.get("ISSUE_BODY", "catalog_body.md")
BOOTSTRAP = os.environ.get("BOOTSTRAP", "").strip().lower() in ("1", "true", "yes")
DIR_CATEGORY = os.environ.get("DIR_CATEGORY", "").strip()        # blank = full directory

REGISTRY = "cgr.dev/chainguard"
DIR_BASE = "https://images.chainguard.dev/directory"
DIR_PAGE_URL = (f"{DIR_BASE}/category/{DIR_CATEGORY}" if DIR_CATEGORY else DIR_BASE)
SLUG_RE = re.compile(r"/directory/image/([a-z0-9][a-z0-9._-]*)/(?:versions|overview)")
UA = "chainguard-catalog-watch/1.0 (+github-actions)"

MAX_DIR_PAGES = 600
FETCH_WORKERS = 12
MAX_ISSUE_ROWS = 100
MAX_ISSUE_CHARS = 60000


def _fetch(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def list_directory() -> dict:
    """Scrape the directory, fetching pages concurrently. Returns {name: ""}.

    Page 1 gives the total count and a representative page size, so we compute
    the page count up front and fetch pages 2..N in parallel. A short
    sequential tail-sweep covers any shortfall.
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
    last = min(MAX_DIR_PAGES, max(est, max(visible)) + 5)

    urls = [f"{DIR_PAGE_URL}/{p}" for p in range(2, last + 1)]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        for html in ex.map(_fetch, urls):
            names |= set(SLUG_RE.findall(html))

    page, misses = last + 1, 0
    while total and len(names) < total and page <= MAX_DIR_PAGES and misses < 2:
        before = len(names)
        names |= set(SLUG_RE.findall(_fetch(f"{DIR_PAGE_URL}/{page}")))
        misses = misses + 1 if len(names) == before else 0
        page += 1

    return {n: "" for n in sorted(names)}


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_run": None, "seen": {}}


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


def render(new, baseline: str, limit: int = None) -> str:
    scope = f" ({DIR_CATEGORY})" if DIR_CATEGORY else ""
    title = f"## New images in the public Chainguard catalog{scope}"
    if not new:
        return f"{title}\n\n_No new images {baseline}._"
    names = sorted(new)
    head = [
        title, "", f"Detected {len(names)} new image(s) {baseline}.", "",
        "| Image | Reference | Directory |", "|---|---|---|",
    ]
    footer = (
        "\n\nThese are catalog entries (mostly Production images requiring an "
        "entitlement to pull). To bring one into the org, provision it "
        "(`chainctl starter add-images <name>` or the Console 'Add to org')."
    )

    def row(n):
        return f"| `{n}` | `{REGISTRY}/{n}` | [page]({DIR_BASE}/image/{n}/versions) |"

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

    current = list_directory()

    # A near-empty scrape is a fetch failure, not a shrunken catalog. Skip
    # rather than flag the whole catalog as removed / later re-fire it as new.
    if not BOOTSTRAP and len(current) < 100:
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
            f"## Bootstrap\n\nRecorded a baseline of {len(current)} image(s). "
            "No issue opened. Future runs report only new additions."
        )
        emit("count", str(len(current)))
        emit("changed", "false")
        emit("notify", "false")
        emit("persist", "true")
        persist(state, seen, current, now)
        return 0

    new = {n: ct for n, ct in current.items() if n not in seen}
    baseline = (
        f"since last run ({state['last_run']})"
        if state.get("last_run") else "since last run (first run)"
    )

    write_summary(render(new, baseline))

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
            f.write(render(new, baseline, limit=MAX_ISSUE_ROWS) + "\n")
        persist(state, seen, current, now)

    return 0


if __name__ == "__main__":
    sys.exit(main())
