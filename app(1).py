"""Railway cron: new items in the 3 Žarnovica website sections -> Resend email."""

import hashlib
import html
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.zarnovica.sk"
SECTIONS = {
    "Podujatia": (466710, f"{BASE}/mid/466710/ma0/all/.html"),
    "Oznamy": (466708, f"{BASE}/mid/466708/ma0/all/.html"),
    "Úradná tabuľa": (466709, f"{BASE}/mid/466709/ma0/all/.html"),
}
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/seen.json"))
TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def fetch(url):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=TIMEOUT) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        logging.info("GET %s: HTTP %s, %s bytes", url, response.status, len(raw))
        return raw.decode(charset, errors="replace")


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def canonical_url(href):
    url = urljoin(BASE, html.unescape(href or ""))
    parts = urlsplit(url)
    if parts.hostname not in {"zarnovica.sk", "www.zarnovica.sk"}:
        return ""
    # Ignore query/fragment tracking parameters for stable de-duplication.
    return urlunsplit(("https", "www.zarnovica.sk", parts.path, "", ""))


def parse_listing(markup, category):
    """Only follow individual article links from the specified CMS module."""
    module_id, _ = SECTIONS[category]
    soup = BeautifulSoup(markup, "html.parser")
    items = {}
    for a in soup.select("a[href]"):
        url = canonical_url(a.get("href"))
        if not url:
            continue
        path = urlsplit(url).path
        prefix = f"/mid/{module_id}/"
        if prefix not in path:
            continue
        if not path.endswith(".html") or "/ma0/" in path:
            continue
        if category == "Podujatia" and "/akcia/" not in path:
            continue
        title = clean(a.get_text(" ", strip=True))
        if not (3 <= len(title) <= 250) or title.casefold() in {
            "späť na zoznam", "všetky", "späť na úvod", "všetky akcie", "všetky oznamy"
        }:
            continue
        items[url] = {"category": category, "title": title, "url": url, "excerpt": ""}

    result = list(items.values())
    if not result:
        visible_text = clean(soup.get_text(" ", strip=True))
        if category == "Podujatia" and re.search(
            r"nenašli sa žiadne akcie|žiadne podujatia|žiadne akcie", visible_text, re.I
        ):
            return []
        links = [canonical_url(x.get("href")) for x in soup.select("a[href]")]
        hints = [x for x in links if "/mid/" in x][:5]
        raise RuntimeError(
            f"No {category} article links; HTML={len(markup)} chars, "
            f"links={len(links)}, sample module URLs={hints}, "
            f"page text begins={visible_text[:180]!r}"
        )
    return result


def detail_text(markup, title, category):
    """Prefer the non-linked article heading, not the teaser heading in the sidebar."""
    soup = BeautifulSoup(markup, "html.parser")
    for el in soup(["script", "style", "noscript", "nav", "footer", "form"]):
        el.decompose()
    target = clean(title).casefold()
    headings = [h for h in soup.find_all(["h1", "h2", "h3", "h4"])
                if clean(h.get_text(" ", strip=True)).casefold() == target]
    headings.sort(key=lambda h: (h.find("a", href=True) is not None))
    for heading in headings:
        if heading.find("a", href=True):
            continue
        parts = []
        for element in heading.next_elements:
            if isinstance(element, Tag) and element.name in {"h1", "h2", "h3"}:
                break
            if isinstance(element, NavigableString):
                parent = element.parent
                if parent and parent.name in {"script", "style", "option"}:
                    continue
                snippet = clean(str(element))
                if not snippet or snippet.casefold() == target:
                    continue
                if re.match(r"^(Zverejnené|Späť na úvod|Späť na zoznam|Za obsah zodpovedá)", snippet, re.I):
                    break
                parts.append(snippet)
                if sum(map(len, parts)) > 2000:
                    break
        extracted = clean(" ".join(parts))
        if category == "Podujatia" and re.search(r"\bPopis\s*:", extracted, re.I):
            extracted = re.split(r"\bPopis\s*:", extracted, maxsplit=1, flags=re.I)[1].strip()
        if extracted:
            return extracted
    # A listing link sometimes has no separate body; don't send unrelated page text.
    return ""


def load_state():
    if not STATE_FILE.exists():
        return None
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return set(data["seen"])


def save_state(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def key(item):
    return item["category"] + "|" + item["url"]


def send_email(item):
    for variable in ("RESEND_API_KEY", "EMAIL_FROM", "EMAIL_TO"):
        if not os.getenv(variable):
            raise RuntimeError(f"Missing Railway variable: {variable}")
    recipients = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO has no recipients")
    subject = f"Žarnovica [{item['category']}]: {item['title']}"[:250]
    excerpt = item["excerpt"][:500] or "(Text článku sa nepodarilo automaticky načítať.)"
    body = (
        f"<p><strong>{html.escape(item['category'])}</strong></p>"
        f"<h2>{html.escape(item['title'])}</h2>"
        f"<p>{html.escape(excerpt)}</p>"
        f'<p><a href="{html.escape(item["url"], quote=True)}">Otvoriť pôvodný príspevok</a></p>'
    )
    payload = json.dumps({"from": os.environ["EMAIL_FROM"], "to": recipients,
                          "subject": subject, "html": body}, ensure_ascii=False).encode("utf-8")
    token = hashlib.sha256(key(item).encode("utf-8")).hexdigest()
    req = Request("https://api.resend.com/emails", data=payload, method="POST", headers={
        "Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
        "Content-Type": "application/json", "Idempotency-Key": "zarnovica-" + token,
    })
    with urlopen(req, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
        if not data.get("id"):
            raise RuntimeError(f"Resend accepted request without email id: {data}")
    logging.info("Emailed [%s] %s", item["category"], item["title"])


def run():
    items = []
    failures = []
    homepage = None
    for category, (_, url) in SECTIONS.items():
        try:
            try:
                found = parse_listing(fetch(url), category)
            except Exception as exc:
                logging.warning("%s listing did not parse: %s; trying homepage", category, exc)
                if homepage is None:
                    homepage = fetch(BASE + "/")
                found = parse_listing(homepage, category)
            items.extend(found)
            logging.info("%s: %d listed items", category, len(found))
        except Exception as exc:
            failures.append(f"{category}: {exc}")
            logging.exception("Could not check %s", category)

    seen = load_state()
    if seen is None:
        if failures:
            raise RuntimeError("Initial baseline not created: " + "; ".join(failures))
        save_state({key(item) for item in items})
        logging.info("Initialized with %d existing links; no historical emails sent", len(items))
        return

    pending = {key(item): item for item in reversed(items) if key(item) not in seen}
    for item_key, item in pending.items():
        try:
            item["excerpt"] = detail_text(fetch(item["url"]), item["title"], item["category"])
            if not item["excerpt"]:
                logging.warning("Article has no extractable text: %s", item["url"])
        except Exception as exc:
            logging.warning("Failed to extract body for %s: %s", item["url"], exc)
        send_email(item)
        seen.add(item_key)
        save_state(seen)

    logging.info("Checked %d items, found %d new, %d section errors",
                 len(items), len(pending), len(failures))
    if failures:
        raise RuntimeError("Some sections failed (will retry): " + "; ".join(failures))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logging.exception("Monitor execution failed")
        sys.exit(1)
