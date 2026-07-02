#!/usr/bin/env python3
"""
Builds a REVEILLE or DEFILADE digest, fully in the cloud.

Pipeline:
  1. Gather candidate articles from (a) Gmail via IMAP and (b) RSS feeds.
  2. Clean links, drop anything already published (published.json).
  3. Send candidates + the editorial prompt to the Claude API -> full HTML issue.
  4. Write the issue file, update index.json and published.json.
  5. Email the issue to the reader via Gmail SMTP.

If the Claude call fails, a plain "raw candidates" fallback issue is published
so the morning never comes up empty.

Environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
"""

import argparse
import base64
import datetime as dt
import email
import email.header
import html as htmllib
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc

# ---------------------------------------------------------------- utilities

def log(msg):
    print(f"[digest] {msg}", flush=True)


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ------------------------------------------------------------ URL cleaning

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid",
                   "ref", "referrer", "s", "r", "triedRedirect"}

SKIP_URL_PATTERNS = re.compile(
    r"(unsubscribe|manage.?preferences|email.?settings|privacy.?policy|"
    r"\.(png|jpe?g|gif|webp|svg|ico)(\?|$)|substackcdn\.com|"
    r"open\.substack\.com/pub/[^/]+/p/.*action=|mailto:|"
    r"list-manage\.com|beehiiv\.com/(subscribe|upgrade)|"
    r"accounts\.google|twitter\.com/intent|facebook\.com/sharer)",
    re.IGNORECASE,
)


def try_base64_url(segment):
    """Some briefings (Punchbowl, Foreign Affairs) hide the real URL
    base64-encoded inside the link path. Try to recover it."""
    seg = segment.strip("/").split("?")[0]
    if len(seg) < 16:
        return None
    for pad in ("", "=", "=="):
        try:
            decoded = base64.urlsafe_b64decode(seg + pad).decode("utf-8", "ignore")
        except Exception:
            continue
        m = re.search(r"https?://[^\s\"'<>]+", decoded)
        if m:
            return m.group(0)
    return None


def clean_url(url):
    """Strip tracking params; unwrap base64-wrapped redirect links."""
    url = htmllib.unescape(url).strip().rstrip(").,;\"'")
    if not url.startswith("http"):
        return None
    if SKIP_URL_PATTERNS.search(url):
        return None
    p = urlparse(url)
    # try to unwrap redirector links (real URL base64ed in the path)
    if re.search(r"(link|click|track|url|redirect|e2t|ct\.)", p.netloc + p.path, re.I):
        for segment in p.path.split("/"):
            real = try_base64_url(segment)
            if real:
                return clean_url(real)
    # strip tracking query params
    q = parse_qs(p.query, keep_blank_values=False)
    q = {k: v for k, v in q.items() if k.lower() not in TRACKING_PARAMS}
    query = "&".join(f"{k}={v[0]}" for k, v in q.items())
    return urlunparse((p.scheme, p.netloc, p.path, "", query, ""))


def canonical_key(url):
    """Key used for dedup: host + path, lowercased, no trailing slash."""
    p = urlparse(url)
    return (p.netloc.lower().replace("www.", "") + p.path.rstrip("/")).lower()


# ------------------------------------------------------- Gmail via IMAP

def decode_header(value):
    parts = email.header.decode_header(value or "")
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", "ignore")
        else:
            out += text
    return out


def body_text(msg):
    """Best-effort plain text of an email message."""
    plain, html_part = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part
        elif ctype == "text/html" and html_part is None:
            html_part = part
    part = plain or html_part
    if part is None:
        return ""
    payload = part.get_payload(decode=True) or b""
    text = payload.decode(part.get_content_charset() or "utf-8", "ignore")
    if part is html_part:
        soup = BeautifulSoup(text, "html.parser")
        # keep link destinations visible in the text for extraction
        for a in soup.find_all("a", href=True):
            a.append(f" <{a['href']}> ")
        text = soup.get_text(" ", strip=True)
    return text


def gather_email_candidates(cfg, since_days):
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    sender_domains = [d.lower() for d in cfg.get("email_sender_domains", [])]
    since = (dt.datetime.now(UTC) - dt.timedelta(days=since_days))

    candidates = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(address, password)
    except Exception as e:
        log(f"WARNING: could not log in to Gmail IMAP: {e}")
        return candidates

    try:
        imap.select('"[Gmail]/All Mail"', readonly=True)
        date_str = since.strftime("%d-%b-%Y")
        _, data = imap.search(None, f'(SINCE "{date_str}")')
        ids = data[0].split()
        log(f"IMAP: {len(ids)} messages since {date_str}; filtering by sender")
        # newest first, cap the number of full bodies we download
        ids = ids[::-1]
        fetched = 0
        for msg_id in ids:
            if fetched >= 120:
                break
            _, hdr = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw_hdr = b"".join(p[1] for p in hdr if isinstance(p, tuple))
            hmsg = email.message_from_bytes(raw_hdr)
            sender = decode_header(hmsg.get("From", ""))
            sender_addr = email.utils.parseaddr(sender)[1].lower()
            if not any(d in sender_addr for d in sender_domains):
                continue
            _, full = imap.fetch(msg_id, "(BODY.PEEK[])")
            raw = b"".join(p[1] for p in full if isinstance(p, tuple))
            msg = email.message_from_bytes(raw)
            fetched += 1
            text = body_text(msg)
            subject = decode_header(msg.get("Subject", ""))
            source = email.utils.parseaddr(sender)[0] or sender_addr

            # canonical URL: Substack's "View this post on the web at ..."
            url = None
            m = re.search(r"View this post on the web at\s*<?(https?://\S+)", text)
            if m:
                url = clean_url(m.group(1))
            # collect all cleaned links found in the body
            links = []
            for raw_url in re.findall(r"https?://[^\s\"'<>\)]+", text):
                cu = clean_url(raw_url)
                if cu and cu not in links:
                    links.append(cu)
            candidates.append({
                "origin": "email",
                "source": source,
                "title": subject,
                "url": url or (links[0] if links else None),
                "links": links[:25],
                "excerpt": re.sub(r"\s+", " ", text)[:2200],
                "date": decode_header(msg.get("Date", "")),
            })
        log(f"IMAP: kept {len(candidates)} newsletter emails")
    except Exception as e:
        log(f"WARNING: IMAP fetch problem: {e}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return candidates


# ------------------------------------------------------------- RSS feeds

def gather_rss_candidates(cfg, since_days):
    since = dt.datetime.now(UTC) - dt.timedelta(days=since_days)
    candidates, failed = [], []
    for feed in cfg.get("rss_feeds", []):
        name, url = feed["name"], feed["url"]
        try:
            resp = requests.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (DispatchesDigestBot)"})
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                raise ValueError("no entries")
        except Exception as e:
            failed.append(f"{name} ({e.__class__.__name__})")
            continue
        for entry in parsed.entries[:15]:
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            if ts:
                when = dt.datetime(*ts[:6], tzinfo=UTC)
                if when < since:
                    continue
            elif parsed.entries.index(entry) > 2:
                continue  # undated feeds: only take the top few
            summary = BeautifulSoup(
                entry.get("summary", "") or
                (entry.get("content", [{}])[0].get("value", "")),
                "html.parser").get_text(" ", strip=True)
            link = clean_url(entry.get("link", "")) if entry.get("link") else None
            if not link:
                continue
            candidates.append({
                "origin": "rss",
                "source": name,
                "title": entry.get("title", "(untitled)"),
                "url": link,
                "links": [link],
                "excerpt": summary[:2200],
                "date": entry.get("published", entry.get("updated", "")),
            })
    log(f"RSS: {len(candidates)} items; {len(failed)} feeds failed"
        + (f" -> {', '.join(failed)}" if failed else ""))
    return candidates, failed


# ------------------------------------------------------------- Claude call

def call_claude(prompt_text, candidates, digest_type, today_str, leftovers):
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

    payload = {
        "date": today_str,
        "digest_type": digest_type.upper(),
        "candidates": candidates,
    }
    if leftovers:
        payload["earlier_this_week_unused"] = leftovers

    user_msg = (
        "Here are the candidate articles gathered from the inbox and RSS feeds, "
        "as JSON. Build today's issue per your instructions.\n\n"
        "STRICT RULES: Use ONLY URLs that appear in this JSON — never invent or "
        "modify a link. Quote/excerpt only from the excerpt text provided. "
        "Respond with the COMPLETE, self-contained HTML document for the issue "
        "and NOTHING else — no preamble, no markdown fences.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        system=prompt_text,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # strip accidental markdown fences
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if "<html" not in text.lower():
        raise ValueError("model response did not contain an HTML document")
    return text


def fallback_html(digest_type, today_str, candidates, note):
    items = "".join(
        f'<p style="margin:10px 0"><b>{htmllib.escape(c["title"] or "(untitled)")}</b><br>'
        f'<span style="color:#666">{htmllib.escape(c["source"])}</span>'
        + (f' · <a href="{htmllib.escape(c["url"])}">Read ›</a>' if c.get("url") else "")
        + "</p>"
        for c in candidates[:40]
    )
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{digest_type.upper()}</title></head>
<body style="margin:0;background:#f4f1ea;font-family:Georgia,serif;color:#222">
<div style="max-width:660px;margin:0 auto;background:#fffdf8;padding:18px 22px">
<h1 style="color:#0f2a3f">{digest_type.upper()} — {today_str}</h1>
<p style="color:#7a1f1f"><i>Automatic fallback issue: {htmllib.escape(note)}.
Below is the raw list of everything gathered today.</i></p>
{items}
</div></body></html>"""


# ------------------------------------------------------------- publishing

def update_index(issue_id, digest_type, title, date_str, filename):
    index_path = REPO_ROOT / "index.json"
    index = load_json(index_path, {"issues": []})
    index["issues"] = [i for i in index["issues"] if i.get("id") != issue_id]
    index["issues"].insert(0, {
        "id": issue_id, "type": digest_type.upper(), "title": title,
        "date": date_str, "file": filename,
    })
    save_json(index_path, index)


def send_email(subject, html_body, note_lines):
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address
    plain = subject + "\n\n" + "\n".join(note_lines) + \
        "\n\nOpen this email in an HTML-capable client, or read it in the Dispatches app."
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(address, password)
        server.sendmail(address, [address], msg.as_string())
    log("email sent")


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["reveille", "defilade"], required=True)
    args = ap.parse_args()
    digest_type = args.type

    cfg = load_yaml(REPO_ROOT / "sources.yml")
    since_days = 1 if digest_type == "reveille" else 7
    today = dt.datetime.now(UTC).date()
    today_str = today.isoformat()
    pretty_date = today.strftime("%A, %d %B %Y")

    prompt_text = (REPO_ROOT / "prompts" / f"{digest_type}.md").read_text(encoding="utf-8")

    # 1. gather
    email_cands = gather_email_candidates(cfg, since_days)
    rss_cands, failed_feeds = gather_rss_candidates(cfg, since_days)
    candidates = email_cands + rss_cands

    # 2. dedupe against everything already published
    published = load_json(REPO_ROOT / "published.json",
                          {"used": [], "recent_candidates": []})
    used_keys = {canonical_key(u) for u in published["used"]}
    fresh, seen = [], set()
    for c in candidates:
        key = canonical_key(c["url"]) if c.get("url") else c["title"].lower()
        if key in used_keys or key in seen:
            continue
        seen.add(key)
        fresh.append(c)
    fresh = fresh[:140]
    log(f"{len(fresh)} fresh candidates after dedup")
    if not fresh:
        log("nothing gathered — aborting without publishing")
        sys.exit(1)

    # DEFILADE also mines the week's unused REVEILLE candidates
    leftovers = published.get("recent_candidates", []) if digest_type == "defilade" else []

    # 3. curate
    notes = []
    if failed_feeds:
        notes.append(f"[{len(failed_feeds)} RSS feeds unreachable this run]")
    try:
        html_doc = call_claude(prompt_text, fresh, digest_type, pretty_date, leftovers)
    except Exception as e:
        log(f"WARNING: Claude call failed: {e}")
        notes.append(f"[curation failed this run — {e.__class__.__name__}]")
        html_doc = fallback_html(digest_type, pretty_date, fresh, str(e)[:200])

    # 4. write issue + index + ledger
    filename = f"{digest_type}-{today_str}.html"
    (REPO_ROOT / filename).write_text(html_doc, encoding="utf-8")
    title = "Daily Brief" if digest_type == "reveille" else "Weekend Reading"
    update_index(f"{digest_type}-{today_str}", digest_type, title, today_str, filename)

    used_now = re.findall(r'href="(https?://[^"]+)"', html_doc)
    published["used"] = (published["used"] + used_now)[-3000:]
    used_now_keys = {canonical_key(u) for u in used_now}
    # keep a 7-day pool of unused candidates for DEFILADE to mine
    pool = published.get("recent_candidates", []) if digest_type == "reveille" else []
    if digest_type == "reveille":
        stamp = today_str
        for c in fresh:
            if c.get("url") and canonical_key(c["url"]) not in used_now_keys:
                pool.append({"seen": stamp, "source": c["source"],
                             "title": c["title"], "url": c["url"],
                             "excerpt": c["excerpt"][:1200]})
        cutoff = (today - dt.timedelta(days=7)).isoformat()
        pool = [c for c in pool if c["seen"] >= cutoff][-250:]
    published["recent_candidates"] = pool if digest_type == "reveille" else []
    save_json(REPO_ROOT / "published.json", published)
    log(f"wrote {filename}, updated index.json and published.json")

    # 5. email
    weekday = today.strftime("%A")
    day_month = today.strftime("%d %B").lstrip("0")
    subject = (f"REVEILLE — Daily Brief · {weekday}, {day_month}"
               if digest_type == "reveille"
               else f"DEFILADE — Weekend Reading · {weekday}, {day_month}")
    try:
        send_email(subject, html_doc, notes)
    except Exception as e:
        log(f"WARNING: email send failed: {e} (issue is still published to the app)")


if __name__ == "__main__":
    main()
