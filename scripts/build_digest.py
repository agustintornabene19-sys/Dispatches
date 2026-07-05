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
from urllib.parse import parse_qs, quote, urlparse, urlunparse

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


# ---------------------------------------------------------- reader feedback

FEEDBACK_PREFIX = "DISPATCH FEEDBACK"


def gather_feedback(days=90):
    """Read feedback emails the reader sent to himself via the digest's
    'more / less / never again' links. Subject format:
    DISPATCH FEEDBACK <MORE|LESS|NEVER> :: <source> :: <url>"""
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    entries = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(address, password)
        imap.select('"[Gmail]/All Mail"', readonly=True)
        since = (dt.datetime.now(UTC) - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = imap.search(None, f'(SUBJECT "{FEEDBACK_PREFIX}" SINCE "{since}")')
        for msg_id in data[0].split():
            _, hdr = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            raw = b"".join(p[1] for p in hdr if isinstance(p, tuple))
            m = email.message_from_bytes(raw)
            subject = decode_header(m.get("Subject", ""))
            parts = [p.strip() for p in subject.split("::")]
            verb = next((v for v in ("NEVER", "LESS", "MORE")
                         if v in parts[0].upper()), None)
            if not verb or len(parts) < 3 or not parts[2].startswith("http"):
                continue
            entries.append({"verb": verb, "source": parts[1], "url": parts[2],
                            "date": decode_header(m.get("Date", ""))})
        imap.logout()
        log(f"feedback: {len(entries)} entries found in mailbox")
    except Exception as e:
        log(f"WARNING: feedback fetch failed: {e}")
    return entries


def inject_feedback_links(html_doc, address, url_sources):
    """After the first link to each article, add tiny mailto links:
    [more · less · never again]. Tapping one opens a pre-filled email
    to self, which the next run picks up as feedback."""
    seen = set()

    def repl(match):
        url = match.group(2)
        key = canonical_key(url)
        if key in seen or key not in url_sources:
            return match.group(0)
        seen.add(key)
        source = url_sources[key]

        def mk(verb, label):
            subj = quote(f"{FEEDBACK_PREFIX} {verb} :: {source} :: {url}")
            return (f'<a href="mailto:{address}?subject={subj}" '
                    f'style="color:#999;text-decoration:none">{label}</a>')

        return (match.group(0)
                + ' <span style="font-size:11px;color:#aaa;white-space:nowrap">['
                + mk("MORE", "more") + " · " + mk("LESS", "less") + " · "
                + mk("NEVER", "never again") + "]</span>")

    return re.sub(r'(<a href="(https?://[^"]+)"[^>]*>.*?</a>)',
                  repl, html_doc, flags=re.S)


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

def call_claude(prompt_text, candidates, digest_type, today_str, leftovers,
                feedback_entries=None):
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
    if feedback_entries:
        payload["reader_feedback"] = [
            {"verb": e["verb"], "source": e["source"], "url": e["url"]}
            for e in feedback_entries[-150:]
        ]

    user_msg = (
        "Here are the candidate articles gathered from the reader's own email "
        "subscriptions and public RSS feeds, as JSON. Build today's issue per "
        "your instructions — a personal digest of brief, attributed excerpts "
        "that link out to each source.\n\n"
        "STRICT RULES: Use ONLY URLs that appear in this JSON — never invent or "
        "modify a link. Quote/excerpt only from the excerpt text provided. "
        "Respond with the COMPLETE, self-contained HTML document for the issue "
        "and NOTHING else — no preamble, no markdown fences.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    def ask(messages):
        resp = client.messages.create(
            model=model,
            max_tokens=20000,
            system=prompt_text,
            messages=messages,
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    messages = [{"role": "user", "content": user_msg}]
    text = ask(messages)
    html_doc = extract_html(text)
    if html_doc is None:
        log("model reply was not HTML; first 300 chars: "
            + text[:300].replace("\n", " "))
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "That was not an HTML document. Reply now with ONLY the "
                "complete self-contained HTML document for the issue — begin "
                "with <!DOCTYPE html> and include nothing before or after it. "
                "Reminder: the format is brief quoted excerpts with clear "
                "attribution and a link to each source (the reader's own "
                "subscriptions); do not reproduce full articles."},
        ]
        text = ask(messages)
        html_doc = extract_html(text)
    if html_doc is None:
        log("retry was not HTML either; first 300 chars: "
            + text[:300].replace("\n", " "))
        raise ValueError("model response did not contain an HTML document")
    return html_doc


def extract_html(text):
    """Pull an HTML document out of a model reply, tolerating preambles,
    markdown fences, and bare fragments. Returns None if no HTML found."""
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    low = text.lower()
    starts = [i for i in (low.find("<!doctype"), low.find("<html")) if i != -1]
    if starts:
        doc = text[min(starts):]
        if "</html>" in doc.lower():
            return doc
        return doc + "</body></html>"
    m = re.search(r"<(body|div|table)[\s>]", text, re.I)
    if not m:
        return None
    frag = text[m.start():]
    head = ('<head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"></head>')
    if frag.lower().startswith("<body"):
        return f"<!DOCTYPE html><html>{head}{frag}" + \
            ("" if "</html>" in frag.lower() else "</html>")
    return (f'<!DOCTYPE html><html>{head}<body style="margin:0;'
            f'background:#f4f1ea">{frag}</body></html>')


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

    # 2a. reader feedback: merge new feedback emails into feedback.json
    fb_path = REPO_ROOT / "feedback.json"
    feedback = load_json(fb_path, {"entries": []})
    known = {(e["verb"], canonical_key(e["url"])) for e in feedback["entries"]}
    for e in gather_feedback():
        sig = (e["verb"], canonical_key(e["url"]))
        if sig not in known:
            feedback["entries"].append(e)
            known.add(sig)
    save_json(fb_path, feedback)
    never_keys = {canonical_key(e["url"])
                  for e in feedback["entries"] if e["verb"] == "NEVER"}

    # 2b. dedupe against everything already published + 'never again' items
    published = load_json(REPO_ROOT / "published.json",
                          {"used": [], "recent_candidates": []})
    used_keys = {canonical_key(u) for u in published["used"]}
    fresh, seen = [], set()
    for c in candidates:
        key = canonical_key(c["url"]) if c.get("url") else c["title"].lower()
        if key in used_keys or key in seen or key in never_keys:
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
        html_doc = call_claude(prompt_text, fresh, digest_type, pretty_date,
                               leftovers, feedback["entries"])
    except Exception as e:
        log(f"WARNING: Claude call failed: {e}")
        notes.append(f"[curation failed this run — {e.__class__.__name__}]")
        html_doc = fallback_html(digest_type, pretty_date, fresh, str(e)[:200])

    # add per-article 'more / less / never again' feedback links
    u