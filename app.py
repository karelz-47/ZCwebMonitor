"""Tiny Žarnovica announcements monitor. Run as a short-lived Railway Cron Job."""

import hashlib
import html
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

BASE = "https://www.zarnovica.sk"
FEEDS = {
    "Oznamy": "https://www.zarnovica.sk/get_rss.php?id=1_atom_2637",
    "Úradná tabuľa": "https://www.zarnovica.sk/get_rss.php?id=1_atom_2636",
}
EVENTS_URL = "https://www.zarnovica.sk/mid/466710/ma0/all/.html"
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/seen.json"))
TIMEOUT = 25
USER_AGENT = "ZarnovicaMonitor/1.0 (personal notifications; 3 checks/day)"
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def fetch(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def plain(value):
    return re.sub(r"\s+", " ", BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ", strip=True)).strip()


def local_name(tag):
    return tag.split("}")[-1].split(":")[-1].lower()


def child_text(element, *names):
    for name in names:
        for child in element:
            if local_name(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def item_link(element):
    for child in element:
        if local_name(child.tag) != "link":
            continue
        if child.attrib.get("rel", "alternate") not in ("alternate", ""):
            continue
        url = child.attrib.get("href") or (child.text or "").strip()
        if url:
            return urljoin(BASE, html.unescape(url))
    # Some RSS feeds have only a GUID with a usable URL.
    guid = child_text(element, "guid", "id")
    return guid if guid.startswith("http") else ""


def parse_feed(xml, category):
    root = ET.fromstring(xml)
    entries = [e for e in root.iter() if local_name(e.tag) in ("entry", "item")]
    if not entries:
        raise RuntimeError(f"{category}: no feed entries; check feed format")
    items = []
    for entry in entries:
        title = plain(child_text(entry, "title"))
        url = item_link(entry)
        excerpt = plain(child_text(entry, "summary", "description", "encoded", "content"))
        if title and url and urlparse(url).hostname in ("www.zarnovica.sk", "zarnovica.sk"):
            items.append({"category": category, "title": title, "url": url, "excerpt": excerpt})
    if not items:
        raise RuntimeError(f"{category}: no valid feed items; check XML structure")
    return items


def parse_events(page):
    soup = BeautifulSoup(page, "html.parser")
    items = []
    used = set()
    for link in soup.select("a[href]"):
        url = urljoin(BASE, html.unescape(link.get("href", "")))
        # Match the actual category's individual event pages, not other links.
        if not re.search(r"/mid/466710/akcia/[^/?#]+", url):
            continue
        if url in used:
            continue
        title = plain(link.get_text(" ", strip=True))
        if not title or len(title) > 300:
            continue
        used.add(url)
        items.append({"category": "Podujatia", "title": title, "url": url, "excerpt": ""})
    if not items:
        raise RuntimeError("Podujatia: no event links found; page structure may have changed")
    return items


def detail_text(page, title):
    """Find article text near the matching heading, excluding site navigation."""
    soup = BeautifulSoup(page, "html.parser")
    for element in soup(["script", "style", "noscript", "footer", "nav", "form"]):
        element.decompose()

    candidates = []
    target = plain(title).casefold()
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        heading_text = plain(heading.get_text(" ", strip=True)).casefold()
        if not heading_text or target not in heading_text:
            continue
        for parent in list(heading.parents)[:5]:
            text = plain(parent.get_text(" ", strip=True))
            if 100 <= len(text) <= 12000:
                candidates.append(text)

    # The smallest meaningful matching container normally holds the article body.
    if candidates:
        raw = min(candidates, key=len)
    else:
        for selector in ("[itemprop='articleBody']", "article", "main", "#content"):
            candidate = soup.select_one(selector)
            if candidate:
                raw = plain(candidate.get_text(" ", strip=True))
                if len(raw) >= 100:
                    break
        else:
            return ""
    # Remove title, event date and common metadata, then trim site footer text.
    ix = raw.casefold().find(target)
    if ix >= 0:
        raw = raw[ix + len(title):].strip(" :-—\n\t")
    raw = re.split(r"\b(?:Zverejnené|Za obsah zodpovedá|Technický prevádzkovateľ|Späť na úvod)\b", raw, maxsplit=1)[0]
    return plain(raw)


def load_state():
    if not STATE_FILE.exists():
        return None
    with STATE_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data["seen"])


def save_state(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def key(item):
    return item["category"] + "|" + item["url"].split("#")[0]


def send_email(item):
    required = ["RESEND_API_KEY", "EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing Railway variables: " + ", ".join(missing))
    recipients = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO contains no recipients")

    excerpt = item["excerpt"][:500]
    subject = f"Žarnovica [{item['category']}]: {item['title']}"[:250]
    body = (f"<p><strong>{html.escape(item['category'])}</strong></p>"
            f"<h2>{html.escape(item['title'])}</h2>"
            f"<p>{html.escape(excerpt)}</p>"
            f'<p><a href="{html.escape(item["url"], quote=True)}">Otvoriť pôvodný príspevok</a></p>')
    payload = json.dumps({"from": os.environ["EMAIL_FROM"], "to": recipients,
                          "subject": subject, "html": body}, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(key(item).encode("utf-8")).hexdigest()
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
                 "Content-Type": "application/json",
                 "Idempotency-Key": "zarnovica-" + digest,
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(request, timeout=TIMEOUT) as response:
        result = json.loads(response.read().decode("utf-8"))
        if not result.get("id"):
            raise RuntimeError("Resend returned no email id: " + str(result))
    logging.info("Sent [%s] %s", item["category"], item["title"])


def run():
    all_items = []
    failures = []
    for category, feed_url in FEEDS.items():
        try:
            items = parse_feed(fetch(feed_url), category)
            all_items.extend(items)
            logging.info("%s: %d feed items", category, len(items))
        except Exception as exc:
            failures.append(f"{category}: {exc}")
            logging.exception("Failed to check %s", category)
    try:
        items = parse_events(fetch(EVENTS_URL))
        all_items.extend(items)
        logging.info("Podujatia: %d event items", len(items))
    except Exception as exc:
        failures.append(f"Podujatia: {exc}")
        logging.exception("Failed to check Podujatia")

    # A failed first run must NOT create a partial baseline, or later items will be missed.
    seen = load_state()
    if seen is None:
        if failures:
            raise RuntimeError("Initial check incomplete: " + "; ".join(failures))
        save_state({key(item) for item in all_items})
        logging.info("Initial baseline saved (%d items); no historical emails sent", len(all_items))
        return

    # Reversed order: oldest/newest does not affect de-duplication.
    pending = [item for item in reversed(all_items) if key(item) not in seen]
    # A given item might occur more than once in the same source; send it only once.
    unique = {key(item): item for item in pending}
    for k, item in unique.items():
        if not item["excerpt"] or len(item["excerpt"]) < 500:
            try:
                detailed = detail_text(fetch(item["url"]), item["title"])
                if len(detailed) > len(item["excerpt"]):
                    item["excerpt"] = detailed
            except Exception as exc:
                logging.warning("Could not retrieve article body %s: %s", item["url"], exc)
        if not item["excerpt"]:
            logging.warning("Empty article text: %s", item["url"])
        send_email(item)
        seen.add(k)
        save_state(seen)  # checkpoint after each accepted email

    logging.info("Checked %d items; %d new; %d source errors", len(all_items), len(unique), len(failures))
    if failures:
        raise RuntimeError("Some sections could not be checked: " + "; ".join(failures))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logging.exception("Monitor run failed")
        sys.exit(1)
