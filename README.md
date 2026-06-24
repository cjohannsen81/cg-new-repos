# chainguard-catalog-watch

A GitHub Actions workflow that watches Chainguard's public [Containers Directory](https://images.chainguard.dev/directory) and opens a triage issue whenever a new image is added to the catalog. It runs on a daily schedule, needs no Chainguard authentication, and keeps its state in the repo.

The point: Chainguard's catalog (~2,600 images and growing) has no "what's new" feed. This diffs the directory against the last run and tells you what appeared, so you can decide whether to provision it into your org.

## How it works

1. A scheduled job scrapes the public Containers Directory over plain HTTP — every `/directory/image/<name>/versions` entry across all pages.
2. It diffs the current set of image names against a committed snapshot (`.state/catalog.json`).
3. Any names not seen before are reported in a GitHub issue labelled `chainguard-catalog`, and the snapshot is updated.

No registry credentials, no `chainctl`, no assumable identity — the directory is public, so the only token in play is the workflow's built-in `GITHUB_TOKEN`.

## Repository layout

```
.github/
├── workflows/
│   └── chainguard-catalog-watch.yml   # schedule + dispatch, issue + commit
└── scripts/
    └── detect_new_repos.py            # the scraper / differ
.state/
└── catalog.json                       # committed snapshot (created on bootstrap)
```

## Setup

1. Copy the two files into your repo at the paths above.
2. Make sure Actions can write back to the repo: **Settings → Actions → General → Workflow permissions → Read and write permissions**. (The workflow also declares `contents: write` and `issues: write`.)
3. Seed the baseline once — see below. Do this before relying on the schedule, or the first scheduled run will report the entire catalog as "new".

That's the whole setup. There are no secrets to configure.

## Bootstrap (run once)

From the **Actions** tab, open **chainguard-catalog-watch → Run workflow**, set **bootstrap** to `true`, and run it.

This records the current catalog into `.state/catalog.json` and commits it **without** opening an issue. It finishes in about a minute and the run summary shows the image count (~2,600). From then on, every run reports only additions relative to that baseline.

## Usage

Once bootstrapped, the workflow runs itself daily (`0 13 * * *`, ~13:00 UTC — adjust the cron in the workflow if you want a different time). When new images appear, you get a single issue listing them with their `cgr.dev/chainguard/<name>` reference and a link to the directory page.

You can also trigger it manually any time via **Run workflow** (leave `bootstrap` off for a normal diff).

To act on a hit: the listed images are mostly Production images that require an entitlement to pull. Provision one into your org with `chainctl starter add-images <name>` or the Console's **Add to org** button.

## Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| Schedule | `cron` in the workflow | `0 13 * * *` | Daily. Catalog additions are infrequent; daily is plenty. |
| `category` | Run-workflow input / `DIR_CATEGORY` env | _(empty = full catalog)_ | Scope to one directory category (`application`, `base`, `ai`, `fips`, `free`, ...) to skip Helm charts / FIPS variants. |
| `FETCH_WORKERS` | constant in the script | `12` | Concurrent page fetches. Lower it to be gentler on the site. |

## State file

`.state/catalog.json` is the durable snapshot, committed back to the repo each time new images are found:

```json
{
  "last_run": "2026-06-24T18:44:15.612412+00:00",
  "seen": { "actions-runner": "", "adc": "", "adminer": "" }
}
```

`seen` is the set of all image names observed so far (values are empty — the directory exposes no timestamps). To reset the baseline, delete this file and bootstrap again.

## Design notes

- **Name-diff only.** The directory has no per-image timestamps, so detection is purely "name not seen before." There's no date-range mode, because there's no date to compare against.
- **Concurrent scrape.** Page 1 reports the total count; the page count is computed up front and pages are fetched in parallel (~1 minute for the full catalog) rather than walked sequentially.
- **Fetch-failure guard.** If a normal run scrapes fewer than 100 entries, it's treated as a transient failure and skipped — the snapshot is left untouched so a blip can't flag the whole catalog as removed or re-fire it as new next time.
- **Conditional commit.** State is committed only when there are new images, keeping git history quiet.
- **Bounded issue body.** New-image lists are capped (100 rows / 60 KB) so a large batch can't exceed GitHub's issue-body limit; the full list always lands in the run summary.

## Limitations

- Watches **container images only**. Agent Skills, Libraries, VMs, and OS Packages are separate Chainguard product surfaces and are not tracked here.
- Reports catalog *presence*, not pullability. Most listed images need an entitlement before you can actually pull them.
- Scrapes a public web page, so a directory redesign could change the markup and require updating the link/pagination patterns in the script. The fetch-failure guard limits the blast radius if that happens.

## Disclaimer

Unofficial tooling. It reads Chainguard's public directory; it is not affiliated with or supported by Chainguard.
