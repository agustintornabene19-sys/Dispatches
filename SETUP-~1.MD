# Moving REVEILLE & DEFILADE to the cloud — setup guide

No coding needed. You'll click through a few websites, copy-paste three secrets, and upload this folder. Budget ~30 minutes. After this, the digests build themselves every morning on GitHub's computers — your PC can be off, and your Claude subscription is not involved.

**What changes for you:** instead of a Gmail *draft*, the finished issue arrives as a normal **email in your inbox** (sent from your own address to yourself), and still publishes to the Dispatches phone app.

---

## Step 1 — Get an Anthropic API key (~$5–15/month)

This is a *separate* pay-per-use account from your Claude subscription — it's what lets the cloud version "think" without your tokens.

1. Go to <https://console.anthropic.com> and sign in (create an account if asked).
2. Add a payment method under **Settings → Billing**. Tip: you can buy a fixed amount of credits (e.g. $20) instead of open-ended billing — the digests simply pause if credits run out.
3. Go to **API Keys → Create Key**. Name it `dispatches`.
4. **Copy the key** (starts with `sk-ant-`) somewhere temporary, like Notepad. You'll paste it in Step 3 and can then delete it.

## Step 2 — Create a Gmail "app password"

This is a special 16-character password that lets the workflow read your newsletters and send you the finished digest. It does NOT give access to your Google account settings.

1. Go to <https://myaccount.google.com/security> and make sure **2-Step Verification** is ON (turn it on if not — you need it for app passwords).
2. Go to <https://myaccount.google.com/apppasswords>.
3. App name: `dispatches` → **Create**.
4. **Copy the 16-character password** (ignore the spaces Google shows — paste it without spaces).

## Step 3 — Add the three secrets to your GitHub repo

Secrets are stored encrypted by GitHub; they never appear in the repo files.

1. Open <https://github.com/agustintornabene19-sys/Dispatches>.
2. **Settings** (tab at the top of the repo) → in the left sidebar: **Secrets and variables → Actions**.
3. Click **New repository secret** three times, creating exactly these (names must match, all caps):

   | Name | Value |
   |------|-------|
   | `ANTHROPIC_API_KEY` | the `sk-ant-...` key from Step 1 |
   | `GMAIL_ADDRESS` | `agustin.tornabene19@gmail.com` |
   | `GMAIL_APP_PASSWORD` | the 16-character password from Step 2, no spaces |

## Step 4 — Upload this folder to the repo

1. On the repo's main page, click **Add file → Upload files**.
2. On your computer, open the `dispatches-upload` folder, **select everything inside it** (including the `.github` folder — press Ctrl+A), and drag it all into the GitHub upload box. The folder structure is preserved automatically.
3. Commit message: `Add cloud digest pipeline` → **Commit changes**.
4. Check that the repo now shows: `.github/`, `scripts/`, `prompts/`, `sources.yml`, `requirements.txt`, `SETUP-GUIDE.md` alongside your existing app files.

> If dragging the `.github` folder doesn't work in your browser, tell me and I'll walk you through the one-file alternative (GitHub's "Create new file" box accepts folder paths).

## Step 5 — Test it

1. Go to the repo's **Actions** tab. (If GitHub shows a button to enable workflows, click it.)
2. In the left list, click **Build digests** → **Run workflow** → choose `reveille` → green **Run workflow** button.
3. Wait 3–6 minutes. A green check = success: you should have an email in your inbox and a new issue in the phone app. A red X = failure: click into the run, screenshot what you see, and show me — I'll fix it.

## Step 6 — Once it works, retire the old setup

Tell me the test worked and I'll disable the two Cowork scheduled tasks (`reveille-daily-digest`, `defilade-weekly-digest`) so you don't get duplicates. You can also delete the GitHub token file (`token.txt`) from the old task folders — the cloud version doesn't need it.

---

## How it runs from then on

- **REVEILLE:** weekday mornings (~5:00 AM Eastern in summer; GitHub's scheduler can be 5–15 min late).
- **DEFILADE:** Saturday mornings (~6:00 AM Eastern in summer).
- Each run: reads your last 24h/7d of newsletters + checks ~17 RSS feeds → Claude curates the issue → emails it to you → publishes it to the app → records every used link in `published.json` so nothing repeats. DEFILADE also mines the week's good-but-unused pieces.
- If curation ever fails, you still get a plain "raw list" issue rather than nothing.

## Everyday tweaks (no coding)

- **Add/remove a source:** open `sources.yml` on GitHub → pencil icon → edit → Commit. Emails are filtered by sender address fragments; RSS feeds are name + URL pairs.
- **Change the editorial voice/structure:** edit `prompts/reveille.md` or `prompts/defilade.md` the same way — they're plain English.
- **Run an extra issue anytime:** Actions tab → Build digests → Run workflow.
- Some RSS feed URLs may turn out to be wrong/dead — harmless (they're skipped and noted in the run log). Show me a run log anytime and I'll clean the list.
