# HANDOFF — Dispatches (REVEILLE / DEFILADE digest system)

Last updated: 2026-07-05. Written for any model/assistant picking up this project cold.

## What this is

A fully cloud-based personal news digest system for **Agustin** (agustin.tornabene19@gmail.com), an active-duty U.S. Army Field Artillery officer, currently stationed in Poland (will eventually return stateside). He has **no coding experience** — explain everything in plain English, never assume he can debug code, and walk him through any manual step click by click.

Two publications, built automatically by GitHub Actions in the repo **github.com/agustintornabene19-sys/Dispatches**:

- **REVEILLE** — daily brief, Mon–Fri. ~20-minute read: quick punches grouped by desk (Washington, Middle East, Europe & NATO–Russia, The Military, Back Page), 2–3 Long Reads, one "From the Branch" military item.
- **DEFILADE** — Saturday long-read companion. ~2–2.5 hours of underlying reading: 10–12 essay-length pieces in flexible thematic sections, with a Deep Dive cluster and a Continental (French/Spanish, in the original language) section.

Editorial identity: curation, not summary — the writers' own words, excerpted with attribution and a link. Taste: highbrow, avant-garde right center of gravity, ideologically varied, no clickbait or cable-news-tier material. He reads French and Spanish and wants both in the original.

Delivery per issue: (1) an email to his own inbox (sent from his own Gmail via SMTP), and (2) publication to his phone app — a PWA served by GitHub Pages from this same repo.

## History (how we got here)

Originally these were two Claude Cowork **scheduled tasks** running on his desktop (`reveille-daily-digest`, `defilade-weekly-digest` — both now **disabled**, do not re-enable without disabling the cloud version, or he gets duplicates). They read Gmail via the Cowork connector, created Gmail drafts, and pushed issues to the repo with a fine-grained PAT. Problems: required his computer to be on and consumed his Claude subscription tokens. In July 2026 the whole pipeline was rebuilt to run on GitHub Actions with the Anthropic API.

## Architecture

```
GitHub Actions (cron, UTC)
  └─ scripts/build_digest.py --type reveille|defilade
       1. GATHER    Gmail via IMAP (app password) — newsletters from sender
                    domains in sources.yml, last 24h (reveille) / 7d (defilade)
                    + RSS feeds in sources.yml (feedparser; failures tolerated)
       2. FEEDBACK  reads "DISPATCH FEEDBACK" emails (see feedback loop below),
                    merges into feedback.json; NEVER-tagged URLs are excluded
       3. DEDUPE    against published.json "used" list (canonical host+path)
       4. CURATE    one Claude API call (model env CLAUDE_MODEL, default
                    claude-sonnet-5): system = prompts/<type>.md, user = JSON
                    of candidates (+ leftovers pool + feedback). Tolerant HTML
                    extraction + one corrective retry. If it still fails →
                    fallback raw-list issue is published (never an empty morning).
       5. PUBLISH   writes <type>-YYYY-MM-DD.html at repo root, prepends entry
                    to index.json, updates published.json; injects per-article
                    [more · less · never again] mailto links
       6. EMAIL     SMTP (smtp.gmail.com:465, same app password), self-to-self
  └─ workflow commits & pushes the new files (built-in GITHUB_TOKEN)
GitHub Pages serves the repo root → the "Dispatches" PWA on his phone reads index.json
```

## Repo layout

| Path | Role |
|---|---|
| `.github/workflows/digests.yml` | Schedule + build + commit. **See token limitation below.** |
| `scripts/build_digest.py` | The entire pipeline (single file, stdlib + 5 deps). |
| `prompts/reveille.md`, `prompts/defilade.md` | Editorial instructions (plain English — Agustin can edit these himself). |
| `sources.yml` | Email sender-domain fragments + RSS feed list. Designed for hand-editing. |
| `published.json` | Ledger: `used` (all published URLs, dedup), `recent_candidates` (7-day pool of unused REVEILLE candidates that DEFILADE mines). |
| `feedback.json` | Accumulated reader feedback entries `{verb, source, url, date}`. |
| `index.json` | Issue manifest the PWA reads. Newest first. `{id, type, title, date, file}`. |
| `index.html`, `app.js`, `styles.css`, `sw.js`, `manifest.webmanifest`, icons | The PWA shell (predates the cloud rebuild). Bump `VERSION` in `sw.js` when changing the shell, or phones serve stale cache. |
| `reveille-*.html`, `defilade-*.html` | Published issues. |
| `SETUP-GUIDE.md`, `HANDOFF.md` | Docs. |

## Schedule

Single cron `0 3 * * 1-6` (UTC) = 05:00 in Poland during summer time (04:00 in winter — accepted quirk). The workflow decides the digest type by day-of-week: Saturday (`date -u +%u` = 6) → DEFILADE, else REVEILLE. Manual runs: Actions tab → Build digests → Run workflow → **the digest dropdown matters**. When he moves stateside, only the cron line needs changing — but see the token limitation.

GitHub cron is best-effort: runs can start 5–15+ min late, and the first occurrence after editing the schedule is sometimes skipped entirely.

## Secrets (repo Settings → Secrets and variables → Actions)

- `ANTHROPIC_API_KEY` — console.anthropic.com key, pay-per-use, separate from his Claude subscription. ~$0.10–0.50/issue.
- `GMAIL_ADDRESS` — agustin.tornabene19@gmail.com
- `GMAIL_APP_PASSWORD` — Google app password (16 chars); grants IMAP read + SMTP send. If Gmail auth ever breaks, first suspect: he rotated his Google password (revokes app passwords).

## Access & the workflow-file limitation (IMPORTANT)

A fine-grained PAT (Contents: read/write, this repo only) exists on his desktop at
`C:\Users\agust\Claude 1\Scheduled\reveille-daily-digest\token.txt` (and a copy in the defilade folder). An assistant with file access + a shell can clone/push with it:
`git clone https://x-access-token:<TOKEN>@github.com/agustintornabene19-sys/Dispatches.git`

- **Never** print, echo, or commit the token; scrub it from any output (`sed 's/github_pat_[A-Za-z0-9_]*/TOKEN/g'`).
- The PAT **cannot create or modify anything under `.github/workflows/`** (no Workflows permission). All workflow changes must be made by Agustin through the GitHub web UI — give him the complete file to paste, not a diff.
- Everything else (script, prompts, sources, docs) can be pushed directly.
- `api.github.com` REST may be unreachable from some sandboxes; plain `git` over HTTPS to github.com works. A public read-only clone (no token) is the easiest way to inspect current state.

## The feedback loop

Every article link in an issue gets small `[more · less · never again]` links (injected by the script, not by the model). Each is a `mailto:` to himself with subject `DISPATCH FEEDBACK <MORE|LESS|NEVER> :: <source> :: <url>`. Next run, the script finds those emails over IMAP, appends to `feedback.json`, hard-excludes NEVER'd URLs, and passes the whole history to the model as an editorial memo (prompts explain how to interpret it). No backend needed.

## Known incidents & lessons (July 2026)

1. **Windows short-name uploads.** Drag-and-drop upload to GitHub from his machine turned `.github` → `GITHUB~1`, `requirements.txt` → `REQUIR~1.TXT`. If he uploads files, verify names afterward. Prefer web-UI "Create new file" or direct pushes.
2. **Fallback issues (Jul 3–4).** The model's reply sometimes isn't a bare HTML doc. Fixed with tolerant extraction (`extract_html`) + one corrective retry + logging of the reply head on failure.
3. **Truncated script (Jul 5).** A pushed copy of `build_digest.py` was cut off mid-file — syntactically valid, so `py_compile` passed, but the `if __name__ == "__main__"` guard was gone: green 19-second runs that did nothing. Lessons: (a) a healthy run takes 3–6 minutes — **a sub-minute green run means the script didn't really run**; (b) verify pushed files end with `main()`; (c) there can be sync lag between the Cowork file-tools view and the sandbox mount of the outputs folder — when editing this project, verify the bytes in the sandbox (`tail` the file) before pushing, or edit via shell directly.

## Diagnostics cheat-sheet

- Script logs every stage to stdout with a `[digest]` prefix (IMAP counts, RSS counts + failed feeds, fresh candidate count, model-reply head on curation failure, publish + email status). Actions run → build job → "Build the digest" step.
- Fallback issues embed their reason: `grep -o 'Automatic fallback issue: [^<]*' <issue>.html`.
- No commit after a green run = the script didn't write files (see incident 3).
- Run duration is the fastest health signal: 3–6 min good, <1 min bad.
- "Nothing gathered — aborting" (exit 1) happens when zero candidates survive; most plausible on quiet days. Open item below.

## Open items / ideas already discussed with Agustin

- "Nothing gathered" currently fails with no email; should send a short notice instead.
- Two-stage curation (cheap-model scoring pass over all candidates before the writing pass) for more breadth.
- Verify links (HEAD request) before publishing; fetch article pages for better excerpts/reading times.
- Enforced per-issue French/Spanish minimum is in the DEFILADE prompt; watch it in practice.
- PWA full-text search over the archive.
- Several RSS feed URLs in `sources.yml` were best guesses; failures are logged per run — prune/fix from a run log.
- When stateside: new cron line (he pastes the workflow file).

## Working with Agustin

Plain English, concise, no jargon without explanation. Walk him through UI steps one click at a time. He prefers being asked clarifying questions over assumptions. Don't make him read code — summarize what it does and why it matters to him.
