#!/usr/bin/env python3
"""
Builds a REVEILLE or DEFILADE digest, fully in the cloud, at no cost.

Pipeline:
  1. Gather candidate articles from (a) Gmail via IMAP and (b) RSS feeds.
     Newsletter tracking links are resolved to their real destinations and
     every candidate is tagged free / subscribed / paid (see sources.yml).
  2. Drop anything already published (published.json) or marked 'never'.
  3. Ask Claude (paid for by the Max plan's monthly API credits) to PICK items
     and quote spans. The model returns a short JSON list; it never writes the
     issue. Optional second opinion if Claude is unavailable: Gemini free tier.
  4. The script checks every pick (quote must appear verbatim in the source,
     link must come from the candidate data, paid-link and per-outlet caps)
     and renders the HTML issue itself.
  5. Write the issue file, update index.json and published.json.
  6. Email the issue to the reader via Gmail SMTP.

If no model is available (credits used up, outage), a rules-based issue is
built from the same candidates — sorted by desk, free sources first — so the
morning never comes up empty.

Environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY  (a key from the Console org linked to the Max plan)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD
Optional: CLAUDE_MODEL (overrides the Claude model list below),
          GEMINI_API_KEY / GEMINI_MODEL (free backup editor)
"""

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import email
import email.header
import email.utils
import html as htmllib
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
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
# A normal browser identity: several publishers block anything named "bot".
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
      "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
      "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,es;q=0.8"}

# Claude models, tried in order until one answers (best first).
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5"]

# Free-tier Gemini models: backup editor, used only if GEMINI_API_KEY is set.
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.1-flash-lite"]

DESKS = ["Washington", "Middle East", "Europe & NATO-Russia", "Military",
         "Essays", "Continental", "Back Page"]

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


def esc(s):
    return htmllib.escape(s or "", quote=True)


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def clean_text(s):
    """Strip newsletter plumbing (bare URLs, rules, markdown debris) from text."""
    s = re.sub(r"View this post on the web at\s*\S+", " ", s or "")
    s = re.sub(r"\(?<?https?://[^\s)>]+>?\)?", " ", s)
    s = re.sub(r"[-=_*~]{3,}|\*\*|\(#\)|\[\s*\]", " ", s)
    return squash(s)


def host_of(url):
    return urlparse(url).netloc.lower().split(":")[0]


def domain_match(host, domains):
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# ------------------------------------------------------------ URL cleaning

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid",
                   "ref", "referrer", "s", "r", "triedRedirect"}

SKIP_URL_PATTERNS = re.compile(
    r"(unsubscribe|manage.?preferences|email.?settings|privacy.?policy|"
    r"\.(png|jpe?g|gif|webp|svg|ico)(\?|$)|substackcdn\.com|"
    r"open\.substack\.com/pub/[^/]+/p/.*action=|mailto:|"
    r"beehiiv\.com/(subscribe|upgrade)|"
    r"accounts\.google|twitter\.com/intent|facebook\.com/sharer)",
    re.IGNORECASE,
)

# Link text we never follow or offer (so resolving can't unsubscribe you).
SKIP_ANCHOR = re.compile(
    r"(unsubscribe|opt.?out|manage|preferences|update your|settings|"
    r"sign ?up|subscribe|advertis|sponsor|forward|share|donate|"
    r"privacy|terms|contact us|download the app|log ?in|upgrade|"
    r"se d[ée]sabonner|d[ée]sinscri|darse de baja|cancelar suscripci)",
    re.IGNORECASE,
)

VIEW_IN_BROWSER = re.compile(
    r"(view (this|it|email|post)? ?(in|on) (your |the )?(browser|web)|"
    r"view online|read online|web version|voir (la version )?en ligne|"
    r"ver en (el )?navegador)", re.IGNORECASE)

# Hosts that are click-trackers / redirectors rather than real articles.
TRACKER_HOST = re.compile(
    r"(^|\.)(email\.|click\.|clicks\.|link\.|links\.|url\d+\.|t\.|go\.|"
    r"trk\.|track\.|e\.)|cmail\d+\.com$|list-manage\.com$|sendgrid\.net$|"
    r"mandrillapp\.com$|hubspotlinks\.com$|mailchimpapp\.net$|"
    r"substack\.com$|beehiiv\.com$|ct\.sendgrid|lnks\.gd$|bit\.ly$",
    re.IGNORECASE)

PAYWALL_TEXT = re.compile(
    r"(this post is for paid subscribers|this post is for paying subscribers|"
    r"keep reading with a 7-day free trial|upgrade to paid to read|"
    r"available to paid subscribers|for premium subscribers only|"
    r"subscribe to read the full|réservé aux abonnés|"
    r"exclusivo para suscriptores|contenido exclusivo para suscriptores)",
    re.IGNORECASE)


def try_base64_url(segment):
    """Some briefings hide the real URL base64-encoded inside the link path."""
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
    url = htmllib.unescape(url or "").strip().rstrip(").,;\"'")
    if not url.startswith("http"):
        return None
    if SKIP_URL_PATTERNS.search(url):
        return None
    p = urlparse(url)
    if re.search(r"(link|click|track|url|redirect|e2t|ct\.)", p.netloc + p.path, re.I):
        for segment in p.path.split("/"):
            real = try_base64_url(segment)
            if real and real != url:
                return clean_url(real)
    q = parse_qs(p.query, keep_blank_values=False)
    q = {k: v for k, v in q.items() if k.lower() not in TRACKING_PARAMS}
    query = "&".join(f"{k}={v[0]}" for k, v in q.items())
    return urlunparse((p.scheme, p.netloc, p.path, "", query, ""))


def canonical_key(url):
    """Key used for dedup: host + path, lowercased, no trailing slash."""
    p = urlparse(url)
    return (p.netloc.lower().replace("www.", "") + p.path.rstrip("/")).lower()


def is_tracker(url):
    h = host_of(url)
    if h.endswith("substack.com"):
        # substack.com/redirect/... is a tracker; <name>.substack.com/p/ is a post
        return "/redirect/" in url
    return bool(TRACKER_HOST.search(h))


_resolve_cache = {}


def resolve_one(url):
    """Follow a tracking link to its real destination (HEAD first)."""
    if url in _resolve_cache:
        return _resolve_cache[url]
    final = url
    try:
        r = requests.head(url, allow_redirects=True, timeout=8, headers=UA)
        final = r.url
        if r.status_code in (403, 405, 501) or final == url:
            r = requests.get(url, allow_redirects=True, timeout=8,
                             headers=UA, stream=True)
            final = r.url
            r.close()
    except Exception:
        pass
    cleaned = clean_url(final) or url
    _resolve_cache[url] = cleaned
    return cleaned


def resolve_links(candidates, budget=1500):
    """Resolve tracker links in all email candidates, in parallel.
    Each email's main link goes first, then the rest in the order they appear."""
    todo, seen = [], set()

    def add(u):
        if u and is_tracker(u) and u not in seen:
            seen.add(u)
            todo.append(u)

    for c in candidates:
        add(c.get("url"))
    for c in candidates:
        for l in c.get("links", []):
            add(l["url"])
    if len(todo) > budget:
        log(f"links: {len(todo)} tracking links, resolving the first {budget}")
    todo = todo[:budget]
    if not todo:
        return
    with cf.ThreadPoolExecutor(32) as ex:
        list(ex.map(resolve_one, todo))
    fixed = 0
    for c in candidates:
        for l in c.get("links", []):
            new = _resolve_cache.get(l["url"])
            if new and new != l["url"]:
                l["url"] = new
                fixed += 1
        if c.get("url") in _resolve_cache:
            c["url"] = _resolve_cache[c["url"]]
        # drop links that still point at a tracker or at junk after resolving
        c["links"] = [l for l in c.get("links", [])
                      if not SKIP_URL_PATTERNS.search(l["url"])
                      and not (is_tracker(l["url"]) and l["url"] not in _resolve_cache)]
    log(f"links: resolved {len(todo)} tracking links ({fixed} rewritten)")


# ------------------------------------------------------- access tagging

class Access:
    def __init__(self, cfg):
        self.subscribed = [d.lower() for d in cfg.get("subscribed_domains", []) or []]
        self.paid = [d.lower() for d in cfg.get("paid_domains", []) or []]

    def of_url(self, url, default="free", paywalled_text=False):
        h = host_of(url)
        if domain_match(h, self.subscribed):
            return "subscribed"
        if domain_match(h, self.paid):
            return "paid"
        if paywalled_text:
            return "paid"
        return default


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


def parse_body(msg):
    """Return (plain_text, [ {text, url} links ]) for an email message."""
    plain, html_part = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part
        elif ctype == "text/html" and html_part is None:
            html_part = part

    def decode(part):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "ignore")

    links, text = [], ""
    if html_part is not None:
        soup = BeautifulSoup(decode(html_part), "html.parser")
        for tag in soup(["style", "script", "head"]):
            tag.decompose()
        for a in soup.find_all("a", href=True):
            anchor = squash(a.get_text(" "))
            links.append({"text": anchor[:140], "url": a["href"]})
        text = soup.get_text(" ", strip=True)
    if plain is not None:
        ptext = decode(plain)
        # plain parts often carry [links] inline; keep text readable
        ptext = re.sub(r"\(?\s*https?://\S+\s*\)?", " ", ptext)
        if len(squash(ptext)) > 200:
            text = ptext
        if not links:
            for u in re.findall(r"https?://[^\s\"'<>\)\]]+", decode(plain)):
                links.append({"text": "", "url": u})
    return squash(text), links


def match_sender(sender_addr, senders):
    for s in senders:
        if s["match"].lower() in sender_addr:
            return s
    return None


def gather_email_candidates(cfg, since_days, access):
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    senders = cfg.get("email_senders", []) or []
    since = dt.datetime.now(UTC) - dt.timedelta(days=since_days)

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
        ids = data[0].split()[::-1]
        log(f"IMAP: {len(ids)} messages since {date_str}; filtering by sender")
        fetched = 0
        per_sender = {}
        for msg_id in ids:
            if fetched >= 120:
                break
            _, hdr = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw_hdr = b"".join(p[1] for p in hdr if isinstance(p, tuple))
            hmsg = email.message_from_bytes(raw_hdr)
            sender = decode_header(hmsg.get("From", ""))
            sender_addr = email.utils.parseaddr(sender)[1].lower()
            conf = match_sender(sender_addr, senders)
            if not conf:
                continue
            _, full = imap.fetch(msg_id, "(BODY.PEEK[])")
            raw = b"".join(p[1] for p in full if isinstance(p, tuple))
            msg = email.message_from_bytes(raw)
            fetched += 1
            text, raw_links = parse_body(msg)
            subject = decode_header(msg.get("Subject", ""))
            source = conf.get("name") or email.utils.parseaddr(sender)[0] or sender_addr
            per_sender[source] = per_sender.get(source, 0) + 1

            links, seen, web_version = [], set(), None
            for l in raw_links:
                if SKIP_ANCHOR.search(l["text"]):
                    continue
                cu = clean_url(l["url"])
                if not cu or cu in seen:
                    continue
                seen.add(cu)
                if VIEW_IN_BROWSER.search(l["text"]) and not web_version:
                    web_version = cu
                    continue
                links.append({"text": l["text"], "url": cu})

            url = None
            m = re.search(r"View this post on the web at\s*<?(https?://\S+)", text)
            if m:
                url = clean_url(m.group(1))
            url = url or web_version or (links[0]["url"] if links else None)
            candidates.append({
                "origin": "email",
                "source": source,
                "desk": conf.get("desk", "Essays"),
                "default_access": conf.get("access", "free"),
                "paywalled_text": bool(PAYWALL_TEXT.search(text[:6000])),
                "title": subject,
                "url": url,
                "links": links[:40],
                "excerpt": clean_text(text)[:6000],
                "date": decode_header(msg.get("Date", "")),
            })
        log(f"IMAP: kept {len(candidates)} newsletter emails "
            + json.dumps(per_sender, ensure_ascii=False))
    except Exception as e:
        log(f"WARNING: IMAP fetch problem: {e}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    resolve_links(candidates)
    for c in candidates:
        c["access"] = access.of_url(c["url"], c["default_access"],
                                    c["paywalled_text"]) if c.get("url") else "free"
    return candidates


# ---------------------------------------------------------- reader feedback

FEEDBACK_PREFIX = "DISPATCH FEEDBACK"


def gather_feedback(days=90):
    """Read feedback emails sent via the digest's 'more / less / never' links.
    Subject format: DISPATCH FEEDBACK <MORE|LESS|NEVER> :: <source> :: <url>"""
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
    [more · less · never again]."""
    seen = set()

    def repl(match):
        url = htmllib.unescape(match.group(2))
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

def fetch_feed(feed, since):
    name, url = feed["name"], feed["url"]
    resp = requests.get(url, timeout=20, headers=UA)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        kind = (resp.headers.get("content-type") or "?").split(";")[0]
        raise ValueError(f"no entries, got {kind}")
    out = []
    for i, entry in enumerate(parsed.entries[:20]):
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        if ts:
            if dt.datetime(*ts[:6], tzinfo=UTC) < since:
                continue
        elif i > 2:
            continue  # undated feeds: only take the top few
        body = entry.get("summary", "") or ""
        if entry.get("content"):
            longer = entry["content"][0].get("value", "")
            if len(longer) > len(body):
                body = longer
        text = clean_text(BeautifulSoup(body, "html.parser").get_text(" ", strip=True))
        link = clean_url(entry.get("link", "")) if entry.get("link") else None
        if not link:
            continue
        out.append({
            "origin": "rss",
            "source": name,
            "desk": feed.get("desk", "Essays"),
            "default_access": feed.get("access", "free"),
            "paywalled_text": bool(PAYWALL_TEXT.search(text)),
            "title": squash(entry.get("title", "(untitled)")),
            "author": squash(entry.get("author", "")),
            "url": link,
            "links": [],
            "excerpt": text[:4000],
            "date": entry.get("published", entry.get("updated", "")),
        })
    return out


def gather_rss_candidates(cfg, since_days, access):
    since = dt.datetime.now(UTC) - dt.timedelta(days=since_days)
    feeds = cfg.get("rss_feeds", []) or []
    candidates, failed = [], []

    def run(feed):
        try:
            return feed, fetch_feed(feed, since), None
        except Exception as e:
            return feed, [], (str(e)[:60] if isinstance(e, ValueError)
                              else e.__class__.__name__)

    with cf.ThreadPoolExecutor(12) as ex:
        for feed, items, err in ex.map(run, feeds):
            if err:
                failed.append(f"{feed['name']} ({err})")
            candidates.extend(items)
    for c in candidates:
        c["access"] = access.of_url(c["url"], c["default_access"], c["paywalled_text"])
    log(f"RSS: {len(candidates)} items from {len(feeds) - len(failed)}/{len(feeds)} feeds"
        + (f"; failed -> {', '.join(failed)}" if failed else ""))
    return candidates, failed


# ------------------------------------------------------------- Gemini picks

REVEILLE_SPEC = """Reply with ONLY a JSON object of this exact shape (no prose, no markdown):
{
  "desks": [
    {"desk": "Washington" | "The Middle East" | "Europe & NATO–Russia" | "The Military" | "Back Page",
     "items": [{"id": "<candidate id>", "lede": "<a few factual words>", "quote": "<verbatim passage from that candidate's excerpt>", "url": "<optional: one of that candidate's links>"}]}
  ],
  "long_reads": [{"id": "...", "quote": "<a paragraph or two, verbatim>", "url": "<optional>"}],
  "from_the_branch": [{"id": "...", "lede": "...", "quote": "...", "url": "<optional>"}]
}"""

DEFILADE_SPEC = """Reply with ONLY a JSON object of this exact shape (no prose, no markdown):
{
  "sections": [
    {"title": "<section name>", "deep_dive": false,
     "items": [{"id": "<candidate id>", "frame": "<optional one neutral line, or empty>", "read_min": <estimated minutes to read the full piece>, "quote": "<a paragraph or two, verbatim from that candidate's excerpt>", "url": "<optional: one of that candidate's links>"}]}
  ]
}
Mark exactly one section "deep_dive": true when you build a Deep Dive."""


def model_view(cands, max_chars):
    """The slimmed-down candidate list the model sees."""
    out = []
    for c in cands:
        item = {"id": c["id"], "source": c["source"], "access": c["access"],
                "desk_hint": c["desk"], "title": c["title"],
                "excerpt": c["excerpt"][:max_chars]}
        if c.get("author"):
            item["author"] = c["author"]
        if c.get("links"):
            item["links"] = [{"text": l["text"][:80], "url": l["url"]}
                             for l in c["links"][:25] if l["text"]]
        out.append(item)
    return out


def build_payload(digest_type, today_str, cands, leftovers, feedback_entries):
    # Keep the request comfortably inside free-tier token limits (~100k tokens).
    budget = 380_000
    for max_chars in (3000, 2200, 1600, 1100, 800):
        payload = {"date": today_str, "digest_type": digest_type.upper(),
                   "candidates": model_view(cands, max_chars)}
        if leftovers:
            payload["earlier_this_week_unused"] = model_view(leftovers, min(max_chars, 900))
        if feedback_entries:
            payload["reader_feedback"] = [
                {"verb": e["verb"], "source": e["source"], "url": e["url"]}
                for e in feedback_entries[-150:]]
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) <= budget:
            break
    log(f"model payload: {len(text):,} chars, excerpts capped at {max_chars}")
    return text, max_chars


def call_gemini(system_text, user_text):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    models = ([os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL")
              else []) + GEMINI_MODELS
    body = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.4, "maxOutputTokens": 32768},
    }
    last_err = None
    for model in dict.fromkeys(models):
        for attempt in (1, 2):
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            try:
                r = requests.post(url, json=body, timeout=300,
                                  headers={"x-goog-api-key": key})
            except Exception as e:
                last_err = f"{model}: {e.__class__.__name__}"
                break
            if r.status_code in (429, 500, 503) and attempt == 1:
                log(f"gemini {model}: HTTP {r.status_code}, retrying in 30s")
                time.sleep(30)
                continue
            if r.status_code != 200:
                last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                log(f"gemini {last_err}")
                break
            data = r.json()
            cand = (data.get("candidates") or [{}])[0]
            finish = cand.get("finishReason")
            usage = data.get("usageMetadata", {})
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])
                           if not p.get("thought"))
            log(f"gemini {model}: finish={finish} in={usage.get('promptTokenCount')} "
                f"out={usage.get('candidatesTokenCount')}")
            try:
                return parse_json(text), model
            except ValueError as e:
                last_err = f"{model}: {e} (finish={finish})"
                log(f"gemini {last_err}")
                break
    raise RuntimeError(f"all Gemini models failed; last: {last_err}")


def call_claude(system_text, user_text):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    models = ([os.environ["CLAUDE_MODEL"]] if os.environ.get("CLAUDE_MODEL")
              else []) + CLAUDE_MODELS
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    last_err = None
    for model in dict.fromkeys(models):
        body = {"model": model, "max_tokens": 16000, "system": system_text,
                "messages": [{"role": "user", "content": user_text}]}
        for attempt in (1, 2):
            try:
                r = requests.post("https://api.anthropic.com/v1/messages",
                                  json=body, headers=headers, timeout=600)
            except Exception as e:
                last_err = f"{model}: {e.__class__.__name__}"
                break
            if r.status_code in (429, 500, 529) and attempt == 1:
                log(f"claude {model}: HTTP {r.status_code}, retrying in 30s")
                time.sleep(30)
                continue
            if r.status_code != 200:
                last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                log(f"claude {last_err}")
                # out of credits: every Claude model will say the same
                if "credit balance" in r.text.lower():
                    raise RuntimeError(f"Claude API credits exhausted ({last_err})")
                break
            data = r.json()
            usage = data.get("usage", {})
            stop = data.get("stop_reason")
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
            log(f"claude {model}: stop={stop} in={usage.get('input_tokens')} "
                f"out={usage.get('output_tokens')}")
            if stop == "max_tokens":
                last_err = f"{model}: reply cut off at max_tokens"
                log(f"claude {last_err}")
                break
            try:
                return parse_json(text), model
            except ValueError as e:
                last_err = f"{model}: {e} (stop={stop})"
                log(f"claude {last_err}")
                break
    raise RuntimeError(f"all Claude models failed; last: {last_err}")


def call_model(system_text, user_text):
    """Claude first; Gemini free tier as backup if a key is configured."""
    errors = []
    try:
        return call_claude(system_text, user_text)
    except Exception as e:
        errors.append(str(e))
        log(f"WARNING: {e}")
    if os.environ.get("GEMINI_API_KEY", "").strip():
        try:
            return call_gemini(system_text, user_text)
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(" | ".join(errors))


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError("reply was not valid JSON")


# ------------------------------------------------------ pick validation

def norm_quote(s):
    s = (s or "").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace(" ", " ")
    return squash(s)


def verbatim_or_none(quote_text, excerpt):
    """Return the quote as it appears in the excerpt, or None if it doesn't."""
    q = norm_quote(quote_text).strip(' "…')
    if len(q) < 20:
        return None
    ex = norm_quote(excerpt)
    q = re.sub(r"\s*(\.\.\.|…)\s*$", "", q)
    i = ex.find(q)
    if i != -1:
        return ex[i:i + len(q)]
    # model joined two passages with an ellipsis: keep the longest real one
    pieces = sorted((p.strip(' "') for p in re.split(r"\.\.\.|…|\[…\]", q)),
                    key=len, reverse=True)
    for p in pieces:
        if len(p) >= 40 and p in ex:
            return p
    return None


def lead_passage(excerpt, target):
    """First sentences of an excerpt, up to ~target chars, cut at a sentence."""
    ex = squash(excerpt)
    if len(ex) <= target:
        return ex if re.search(r"[.!?»”\"')]$", ex) else ex + " …"
    cut = ex[:target]
    m = list(re.finditer(r"[.!?»”\"](\s|$)", cut))
    if m and m[-1].end() > target * 0.4:
        return cut[:m[-1].end()].strip()
    return cut.rsplit(" ", 1)[0] + " …"


class Picker:
    """Applies the hard rules to every pick, whoever made it."""

    def __init__(self, by_id, access, max_paid, max_per_outlet):
        self.by_id = by_id
        self.access = access
        self.max_paid = max_paid
        self.max_per_outlet = max_per_outlet
        self.paid_used = 0
        self.outlets = {}
        self.used_ids = set()
        self.used_urls = set()
        self.stats = {"kept": 0, "bad_id": 0, "dup": 0, "paid_cap": 0,
                      "outlet_cap": 0, "quote_fixed": 0}

    def take(self, pick, quote_len):
        c = self.by_id.get(str(pick.get("id", "")).strip())
        if not c:
            self.stats["bad_id"] += 1
            return None
        if c["id"] in self.used_ids:
            self.stats["dup"] += 1
            return None
        # link: a link from this candidate's own data, else its main url
        url = c.get("url")
        wanted = (pick.get("url") or "").strip()
        if wanted:
            allowed = {c.get("url")} | {l["url"] for l in c.get("links", [])}
            if wanted in allowed:
                url = wanted
        if not url:
            self.stats["bad_id"] += 1
            return None
        if canonical_key(url) in self.used_urls:
            self.stats["dup"] += 1
            return None
        acc = self.access.of_url(url, c["default_access"],
                                 c["paywalled_text"] and url == c.get("url"))
        if acc == "paid" and self.paid_used >= self.max_paid:
            self.stats["paid_cap"] += 1
            return None
        if self.outlets.get(c["source"], 0) >= self.max_per_outlet:
            self.stats["outlet_cap"] += 1
            return None
        q = verbatim_or_none(pick.get("quote"), c["excerpt"])
        if not q:
            if pick.get("quote"):
                self.stats["quote_fixed"] += 1
            q = lead_passage(c["excerpt"], quote_len)
        if len(q) < 30:
            self.stats["bad_id"] += 1
            return None
        self.used_ids.add(c["id"])
        self.used_urls.add(canonical_key(url))
        self.outlets[c["source"]] = self.outlets.get(c["source"], 0) + 1
        if acc == "paid":
            self.paid_used += 1
        self.stats["kept"] += 1
        return {"cand": c, "url": url, "access": acc, "quote": q,
                "lede": squash(pick.get("lede", ""))[:120],
                "frame": squash(pick.get("frame", ""))[:240],
                "read_min": pick.get("read_min")}


# ------------------------------------------------------ rules-based picks

DESK_TO_REVEILLE = {"Washington": "Washington", "Middle East": "The Middle East",
                    "Europe & NATO-Russia": "Europe & NATO–Russia",
                    "Military": "The Military", "Back Page": "Back Page",
                    "Continental": "Europe & NATO–Russia"}

DESK_TO_DEFILADE = {"Essays": "Essays & Polemic", "Washington": "America",
                    "Middle East": "The Middle East",
                    "Europe & NATO-Russia": "Britain & Europe",
                    "Continental": "Continental", "Military": "From the Field",
                    "Back Page": "Aesthetics & the Good Life"}


def access_rank(c):
    return {"free": 0, "subscribed": 0, "paid": 2}.get(c["access"], 1)


def round_robin(cands, n):
    """Pick up to n, one per outlet per pass, readable sources first."""
    pools = {}
    for c in sorted(cands, key=lambda c: (access_rank(c), -len(c["excerpt"]))):
        pools.setdefault(c["source"], []).append(c)
    out = []
    while len(out) < n and any(pools.values()):
        for src in list(pools):
            if pools[src]:
                out.append(pools[src].pop(0))
                if len(out) >= n:
                    break
    return out


PROMO = re.compile(r"(\bsale\b|% off|discount|webinar|register now|tickets|"
                   r"giveaway|save \d+%|last chance|ending soon|event:|podcast)",
                   re.IGNORECASE)


def rules_picks(digest_type, cands):
    usable = [c for c in cands if c.get("url") and len(c["excerpt"]) >= 120
              and not PROMO.search(c["title"])]
    if digest_type == "reveille":
        essays = [c for c in usable if c["desk"] == "Essays" and len(c["excerpt"]) > 600]
        long_reads = round_robin(essays, 3)
        taken = {c["id"] for c in long_reads}
        desks = []
        for desk_key, desk_name in DESK_TO_REVEILLE.items():
            pool = [c for c in usable if c["desk"] == desk_key and c["id"] not in taken]
            items = round_robin(pool, 4 if desk_key != "Back Page" else 2)
            taken |= {c["id"] for c in items}
            if items:
                desks.append({"desk": desk_name, "items": [
                    {"id": c["id"], "lede": c["title"]} for c in items]})
        merged = {}
        for d in desks:
            merged.setdefault(d["desk"], []).extend(d["items"])
        return {"desks": [{"desk": k, "items": v} for k, v in merged.items()],
                "long_reads": [{"id": c["id"]} for c in long_reads],
                "from_the_branch": []}
    sections, taken = [], set()
    for desk_key, title in DESK_TO_DEFILADE.items():
        pool = [c for c in usable if c["desk"] == desk_key and c["id"] not in taken
                and len(c["excerpt"]) > 400]
        items = round_robin(pool, 3 if desk_key == "Essays" else 2)
        taken |= {c["id"] for c in items}
        if items:
            sections.append({"title": title, "items": [{"id": c["id"]} for c in items]})
    return {"sections": sections}


# ------------------------------------------------------------- rendering

def paid_tag(item):
    if item["access"] == "paid":
        return ' <span style="font-size:11px;color:#7a1f1f;border:1px solid #7a1f1f;border-radius:3px;padding:0 3px">paywall</span>'
    return ""


def render_reveille(picks, picker, pretty_date, note):
    navy, gold, maroon = "#0f2a3f", "#c9b27e", "#7a1f1f"
    parts = []

    def punch(p):
        c = p["cand"]
        lede = p["lede"] or c["title"]
        return (f'<div style="margin:0 0 18px 0">'
                f'<p style="margin:0 0 6px 0;font-size:16px"><strong>{esc(lede)}</strong></p>'
                f'<p style="margin:0 0 6px 0;font-style:italic;line-height:1.5">“{esc(p["quote"])}”</p>'
                f'<p style="margin:0;font-size:13px;color:#444">— {esc(c["source"])} · '
                f'<a href="{esc(p["url"])}" style="color:{maroon};text-decoration:none">Read ›</a>'
                f'{paid_tag(p)}</p></div>')

    def header(t):
        return (f'<h2 style="font-variant:small-caps;font-size:20px;color:{navy};'
                f'border-bottom:2px solid {gold};padding-bottom:6px;margin:28px 0 14px 0">{esc(t)}</h2>')

    # long reads are validated first so they get first claim on the paid cap
    long_reads = [x for x in (picker.take(p, 1200) for p in picks.get("long_reads", [])[:3]) if x]
    desks = []
    for d in picks.get("desks", []):
        items = [x for x in (picker.take(p, 380) for p in d.get("items", [])) if x]
        if items:
            desks.append((d.get("desk", ""), items))
    branch = [x for x in (picker.take(p, 500) for p in picks.get("from_the_branch", [])[:1]) if x]

    for name, items in desks:
        parts.append(header(name))
        parts.extend(punch(p) for p in items)
    if long_reads:
        parts.append(header("The Long Reads"))
        for p in long_reads:
            c = p["cand"]
            byline = esc(c["source"]) + (f' · {esc(c["author"])}' if c.get("author") else "")
            parts.append(
                f'<div style="margin:0 0 24px 0">'
                f'<p style="margin:0 0 4px 0;font-size:18px"><a href="{esc(p["url"])}" '
                f'style="color:{navy};text-decoration:none"><strong>{esc(c["title"])}</strong></a>{paid_tag(p)}</p>'
                f'<p style="margin:0 0 10px 0;font-size:13px;color:#555">{byline}</p>'
                f'<blockquote style="margin:0;padding:0 0 0 14px;border-left:3px solid {gold};'
                f'line-height:1.6;color:#222">{esc(p["quote"])}</blockquote>'
                f'<p style="margin:8px 0 0 0;font-size:13px"><a href="{esc(p["url"])}" '
                f'style="color:{maroon};text-decoration:none">Read the full piece ›</a></p></div>')
    if branch:
        parts.append(header("From the Branch"))
        parts.extend(punch(p) for p in branch)

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>REVEILLE — {esc(pretty_date)}</title></head>
<body style="margin:0;padding:0;background-color:#eee9df;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:660px;margin:0 auto;background-color:#ffffff">
<tr><td style="background-color:{navy};padding:24px 28px">
<div style="color:#ffffff;font-size:26px;letter-spacing:2px;font-variant:small-caps">Reveille</div>
<div style="color:{gold};font-size:13px;margin-top:4px">Your Daily Brief · {esc(pretty_date)}</div>
</td></tr>
<tr><td style="height:6px;background-color:{gold};line-height:6px;font-size:1px">&nbsp;</td></tr>
<tr><td style="padding:4px 28px 28px 28px">
{f'<p style="font-size:12px;color:#7a1f1f;margin:16px 0 0 0"><i>{esc(note)}</i></p>' if note else ''}
{body}
</td></tr></table></body></html>
"""


def render_defilade(picks, picker, pretty_date, note):
    dark, cream, gold, maroon = "#2a1414", "#f3ead8", "#d8b25a", "#7a1f1f"
    parts = []
    for s in picks.get("sections", []):
        items = [x for x in (picker.take(p, 1400) for p in s.get("items", [])) if x]
        if not items:
            continue
        title = s.get("title", "")
        if s.get("deep_dive"):
            parts.append(f'<div style="background:{dark};color:{cream};padding:14px 18px;margin:30px 0 16px 0">'
                         f'<div style="color:{gold};font-size:12px;letter-spacing:2px">DEEP DIVE</div>'
                         f'<div style="font-size:20px;margin-top:2px">{esc(title)}</div></div>')
        else:
            parts.append(f'<h2 style="font-size:20px;color:#111;font-weight:bold;border-bottom:2px solid {maroon};'
                         f'padding-bottom:6px;margin:30px 0 14px 0">{esc(title)}</h2>')
        for p in items:
            c = p["cand"]
            meta = esc(c["source"]) + (f' · {esc(c["author"])}' if c.get("author") else "")
            try:
                rm = int(p["read_min"])
                if 1 <= rm <= 180:
                    meta += f" · ~{rm} min"
            except (TypeError, ValueError):
                pass
            frame = (f'<p style="margin:0 0 8px 0;font-size:14px;color:#444">{esc(p["frame"])}</p>'
                     if p["frame"] else "")
            parts.append(
                f'<div style="margin:0 0 26px 0">'
                f'<p style="margin:0 0 4px 0;font-size:18px"><a href="{esc(p["url"])}" '
                f'style="color:#111;text-decoration:none"><strong>{esc(c["title"])}</strong></a>{paid_tag(p)}</p>'
                f'<p style="margin:0 0 8px 0;font-size:13px;color:#555">{meta}</p>{frame}'
                f'<blockquote style="margin:0;padding:0 0 0 14px;border-left:3px solid {maroon};'
                f'line-height:1.6;color:#222">{esc(p["quote"])}</blockquote>'
                f'<p style="margin:8px 0 0 0;font-size:13px"><a href="{esc(p["url"])}" '
                f'style="color:{maroon};text-decoration:none">Read the full piece ›</a></p></div>')
    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DEFILADE — {esc(pretty_date)}</title></head>
<body style="margin:0;padding:0;background-color:#efe9dd;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:660px;margin:0 auto;background-color:#fffdf8">
<tr><td style="background-color:{dark};padding:24px 28px">
<div style="color:{cream};font-size:26px;letter-spacing:3px">DEFILADE</div>
<div style="color:{gold};font-size:13px;margin-top:4px">Weekend Reading · {esc(pretty_date)}</div>
</td></tr>
<tr><td style="padding:4px 28px 28px 28px">
{f'<p style="font-size:12px;color:#7a1f1f;margin:16px 0 0 0"><i>{esc(note)}</i></p>' if note else ''}
{body}
</td></tr></table></body></html>
"""


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

def curate(digest_type, cfg, fresh, leftovers, feedback_entries, pretty_date, access):
    """Get picks (Claude, else Gemini, else rules), enforce the rules, render HTML."""
    pool = fresh + leftovers
    by_id = {c["id"]: c for c in pool}
    max_paid = int((cfg.get("max_paid_per_issue") or {}).get(digest_type, 2))
    max_outlet = 3
    note = ""
    prompt_text = (REPO_ROOT / "prompts" / f"{digest_type}.md").read_text(encoding="utf-8")
    spec = REVEILLE_SPEC if digest_type == "reveille" else DEFILADE_SPEC
    try:
        payload, _ = build_payload(digest_type, pretty_date, fresh, leftovers, feedback_entries)
        user_text = ("Candidate articles from the reader's own newsletter subscriptions "
                     "and public RSS feeds, as JSON. Pick today's issue per your "
                     "instructions.\n\n" + spec + "\n\n" + payload)
        picks, model = call_model(prompt_text, user_text)
        log(f"picks from {model}")
    except Exception as e:
        log(f"WARNING: model curation failed, using rules-based issue: {e}")
        note = "Backup edition: sorted automatically today (the AI editor was unavailable)."
        picks = rules_picks(digest_type, fresh)

    picker = Picker(by_id, access, max_paid, max_outlet)
    render = render_reveille if digest_type == "reveille" else render_defilade
    html_doc = render(picks, picker, pretty_date, note)
    log(f"picks: {json.dumps(picker.stats)}; paid links used {picker.paid_used}/{max_paid}")

    # if the model's picks mostly failed validation, fall back to rules
    if picker.stats["kept"] < (6 if digest_type == "reveille" else 4) and not note:
        log("WARNING: too few valid picks from the model; using rules-based issue")
        note = "Backup edition: sorted automatically today (the AI editor's picks didn't check out)."
        picker = Picker(by_id, access, max_paid, max_outlet)
        html_doc = render(rules_picks(digest_type, fresh), picker, pretty_date, note)
        log(f"rules picks: {json.dumps(picker.stats)}")
    return html_doc, note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["reveille", "defilade"], required=True)
    args = ap.parse_args()
    digest_type = args.type

    cfg = load_yaml(REPO_ROOT / "sources.yml")
    access = Access(cfg)
    since_days = 1 if digest_type == "reveille" else 7
    today = dt.datetime.now(UTC).date()
    today_str = today.isoformat()
    pretty_date = today.strftime("%A, %d %B %Y")

    # Idempotency guard: a *scheduled* (backup) run skips when today's issue
    # already exists; manual / external-cron runs always build.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" \
            and (REPO_ROOT / f"{digest_type}-{today_str}.html").exists():
        log(f"{digest_type}-{today_str}.html already published today; "
            "scheduled backup run — skipping to avoid a duplicate")
        return

    # 1. gather
    email_cands = gather_email_candidates(cfg, since_days, access)
    rss_cands, failed_feeds = gather_rss_candidates(cfg, since_days, access)
    candidates = email_cands + rss_cands

    # 2a. reader feedback
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
    # keep the pool balanced: no single outlet may flood the list
    per_src, balanced = {}, []
    for c in fresh:
        per_src[c["source"]] = per_src.get(c["source"], 0) + 1
        if per_src[c["source"]] <= (12 if digest_type == "reveille" else 20):
            balanced.append(c)
    fresh = balanced[:220]
    for i, c in enumerate(fresh, 1):
        c["id"] = f"c{i}"
    counts = {a: sum(1 for c in fresh if c["access"] == a)
              for a in ("free", "subscribed", "paid")}
    log(f"{len(fresh)} fresh candidates after dedup; access mix {counts}")
    if not fresh:
        log("nothing gathered — aborting without publishing")
        sys.exit(1)

    # DEFILADE also mines the week's unused REVEILLE candidates
    leftovers = []
    if digest_type == "defilade":
        for i, c in enumerate(published.get("recent_candidates", []), 1):
            if canonical_key(c["url"]) in never_keys:
                continue
            c = dict(c)
            c.setdefault("desk", "Essays")
            c.setdefault("default_access", "free")
            c.setdefault("paywalled_text", False)
            c.setdefault("links", [])
            c["access"] = c.get("access") or access.of_url(c["url"])
            c["excerpt"] = clean_text(c.get("excerpt", ""))
            c["id"] = f"p{i}"
            leftovers.append(c)

    # 3. curate + render
    notes = []
    if failed_feeds:
        notes.append(f"[{len(failed_feeds)} RSS feeds unreachable this run]")
    html_doc, note = curate(digest_type, cfg, fresh, leftovers, feedback["entries"],
                            pretty_date, access)
    if note:
        notes.append(note)

    url_sources = {canonical_key(c["url"]): c["source"]
                   for c in fresh + leftovers if c.get("url")}
    for c in fresh:
        for l in c.get("links", []):
            url_sources.setdefault(canonical_key(l["url"]), c["source"])
    html_doc = inject_feedback_links(html_doc, os.environ["GMAIL_ADDRESS"], url_sources)

    # 4. write issue + index + ledger
    filename = f"{digest_type}-{today_str}.html"
    (REPO_ROOT / filename).write_text(html_doc, encoding="utf-8")
    title = "Daily Brief" if digest_type == "reveille" else "Weekend Reading"
    update_index(f"{digest_type}-{today_str}", digest_type, title, today_str, filename)

    used_now = [htmllib.unescape(u) for u in re.findall(r'href="(https?://[^"]+)"', html_doc)]
    published["used"] = (published["used"] + used_now)[-3000:]
    used_now_keys = {canonical_key(u) for u in used_now}
    pool = published.get("recent_candidates", []) if digest_type == "reveille" else []
    if digest_type == "reveille":
        for c in fresh:
            if c.get("url") and canonical_key(c["url"]) not in used_now_keys:
                pool.append({"seen": today_str, "source": c["source"],
                             "title": c["title"], "url": c["url"],
                             "desk": c["desk"], "access": c["access"],
                             "author": c.get("author", ""),
                             "excerpt": c["excerpt"][:1500]})
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
    log("run complete")


if __name__ == "__main__":
    main()
