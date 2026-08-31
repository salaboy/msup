"""Shared helpers for the Maryland Supreme site.

Standard library only — no pip installs, no build tooling. Split out from the
scripts so the RSS parsing and merge rules can be unit tested without touching
the network or the filesystem.
"""

import html
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

SCHEMA_VERSION = 2
DEFAULT_FEED = "https://www.etsy.com/shop/MSupShop/rss"
ETSY_API = "https://openapi.etsy.com/v3/application"

# Etsy sits behind DataDome, which fronts the RSS host too. A default urllib
# User-Agent gets challenged; a browser-shaped one is served normally.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Labels the artist uses in listing descriptions. Anything else is preserved
# verbatim in extra_fields, so a new label needs no code change.
KNOWN_LABELS = {
    "title": "title",
    "description": "description",
    "type": "type",
    "size": "size",
    "framed": "framed",
}

# Widths offered in srcset. il_fullxfull is deliberately excluded: its intrinsic
# width is unknown so it cannot carry an honest `w` descriptor, and 400KB has no
# place in a grid. It is linked separately as "view full size".
SRCSET_WIDTHS = (570, 794, 1140)

CURATION_DEFAULTS = {
    "featured": False,
    "order": None,
    "hidden": False,
    "status_override": None,
    "title_override": None,
    "alt": None,
    "tags": [],
    "notes": "",
    "image_override": None,
    "year": None,
}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


class FetchError(Exception):
    """Network, HTTP, or obviously-not-RSS response."""


def fetch(url, timeout=30, retries=3, sleep=time.sleep, headers=None):
    """GET a URL, returning bytes. Retries transient failures with backoff.

    403 is retried because DataDome verdicts are probabilistic — the same
    request from the same runner often succeeds on a second attempt.
    """
    backoff = (1, 4, 10)
    last = None
    for attempt in range(retries):
        hdrs = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        hdrs.update(headers or {})
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (403, 429) and exc.code < 500:
                raise FetchError("HTTP %s for %s" % (exc.code, url)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last = exc
        if attempt < retries - 1:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    raise FetchError("giving up on %s after %d attempts: %s" % (url, retries, last))


# --------------------------------------------------------------------------
# Etsy API — optional enrichment for the images RSS cannot provide
# --------------------------------------------------------------------------


def api_listing_images(listing_id, api_key, fetcher=None):
    """All images for a listing, via the Etsy v3 API.

    The RSS feed carries only the primary photo, and the other URLs are not
    derivable — the per-image suffix (e.g. `_1rfk`) is required and cannot be
    guessed. This endpoint is public: it needs only an API keystring, no OAuth.

    Returns a list of image dicts in Etsy's own display order (`rank`).
    """
    url = "%s/listings/%s/images" % (ETSY_API, listing_id)
    raw = (fetcher or fetch)(url, headers={"x-api-key": api_key, "Accept": "application/json"})
    payload = json.loads(raw)

    images = []
    for entry in sorted(payload.get("results") or [], key=lambda r: r.get("rank") or 0):
        src = entry.get("url_570xN") or entry.get("url_fullxfull")
        if not src:
            continue
        images.append({
            "src": src,
            "base": image_base(src),
            "width": entry.get("full_width"),
            "height": entry.get("full_height"),
            "alt": entry.get("alt_text") or None,
        })
    return images


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_IMG_RE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.I)
_ATTR_RE = r'{0}="(\d+)"'
_LISTING_RE = re.compile(r"/listing/(\d+)(?:/([^/?#]*))?")
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _/&-]{0,40}):\s*(.*)$")
_SIZE_TOKEN_RE = re.compile(r"il_(?:\d+x\d+|\d+xN|fullxfull)")
_PRICE_RE = re.compile(r"^([\d.,]+)\s*([A-Z]{3})$")
_PRICE_RE_ALT = re.compile(r"^([A-Z]{3})\s*([\d.,]+)$")


def _block(desc, cls):
    """Extract the inner HTML of <p class="cls">...</p>, or None."""
    m = re.search(r'<p class="%s">(.*?)</p>' % cls, desc, re.S)
    return m.group(1) if m else None


def _text(fragment):
    """Strip tags then unescape.

    Order matters. The RSS <description> arrives entity-escaped and
    ElementTree already unescaped one level, so the value here is real markup.
    Unescaping the *whole blob* before stripping tags would turn a literal
    "&amp;lt;" in the artist's prose into a fake tag and eat the text after it.
    """
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def parse_description(desc):
    """Split a listing description into labelled fields.

    Returns (fields, extra_fields, body). Degrades in stages: known labels ->
    fields, unknown labels -> extra_fields, continuation lines append to the
    previous field, and a description with no labels at all lands in body.
    """
    fields, extra, body_lines = {}, {}, []
    current = None
    for raw in _BR_RE.split(desc or ""):
        line = _text(raw)
        if not line:
            continue
        m = _LABEL_RE.match(line)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
            key = KNOWN_LABELS.get(label.lower())
            if key:
                fields[key] = value
                current = ("fields", key)
            else:
                extra[label] = value
                current = ("extra", label)
        elif current:
            bucket, key = current
            target = fields if bucket == "fields" else extra
            target[key] = (target[key] + "\n" + line).strip()
        else:
            body_lines.append(line)

    body = "\n".join(body_lines) or None
    if not fields and not extra and body is None:
        body = None
    return fields, extra, body


def parse_price(raw):
    """'25.00 GBP' -> {'amount': '25.00', 'currency': 'GBP', 'raw': ...}.

    Always keeps `raw` so an unrecognised format loses nothing.
    """
    raw = (raw or "").strip()
    price = {"amount": None, "currency": None, "raw": raw or None}
    m = _PRICE_RE.match(raw)
    if m:
        price["amount"], price["currency"] = m.group(1), m.group(2)
        return price
    m = _PRICE_RE_ALT.match(raw)
    if m:
        price["currency"], price["amount"] = m.group(1), m.group(2)
    return price


def image_base(src):
    """Rewrite an Etsy image URL with {size} in place of its size token.

    The size tokens are interchangeable (il_570xN / il_794xN / il_1140xN /
    il_fullxfull all resolve), so one URL yields a whole srcset by
    substitution. Returns None if the token is not recognised, in which case
    the build emits a bare src with no srcset.
    """
    if not src:
        return None
    replaced, n = _SIZE_TOKEN_RE.subn("il_{size}", src)
    return replaced if n == 1 else None


def srcset(base):
    """Build a srcset string from an image_base template."""
    if not base:
        return None
    return ", ".join(
        "%s %dw" % (base.replace("{size}", "%dxN" % w), w) for w in SRCSET_WIDTHS
    )


def sized(base, size, fallback=None):
    """Render one variant from an image_base template."""
    if not base:
        return fallback
    return base.replace("{size}", size)


def shop_name(channel_title):
    """'Etsy Shop for MSupShop' -> 'MSupShop'.

    Derived rather than hardcoded so a future shop rename does not leave every
    title on the site reading "FIX by MSupShop".
    """
    m = re.match(r"\s*Etsy Shop for\s+(.+?)\s*$", channel_title or "")
    return m.group(1) if m else None


def strip_shop_suffix(title, shop):
    """'FIX by MSupShop' -> 'FIX'."""
    title = (title or "").strip()
    if shop:
        m = re.match(r"^(.*?)\s+by\s+%s\s*$" % re.escape(shop), title, re.I)
        if m:
            return m.group(1).strip()
    return title


def parse_listing_url(url):
    """Extract (listing_id, slug) from an Etsy listing URL."""
    m = _LISTING_RE.search(url or "")
    if not m:
        return None, None
    return m.group(1), (m.group(2) or "")


def canonical_listing_url(listing_id, slug):
    """Rebuild a clean listing URL.

    Drops ?ref=rss and the /uk/ locale prefix — a locale-free URL redirects
    correctly for every visitor, whereas /uk/ pushes US buyers into a
    GBP-first experience.
    """
    tail = "/%s" % slug if slug else ""
    return "https://www.etsy.com/listing/%s%s" % (listing_id, tail)


def parse_pubdate(raw):
    """RFC 2822 date -> 'YYYY-MM-DD' in UTC, or None."""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def parse_feed(xml_bytes):
    """Parse an Etsy shop RSS feed into a list of normalised Etsy dicts.

    Items that lack a usable listing id are skipped with a warning rather than
    aborting the run — one malformed entry should not cost the whole sync.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise FetchError("feed has no <channel>")

    shop = shop_name(_findtext(channel, "title"))
    items, warnings = [], []

    for item in channel.findall("item"):
        guid = _findtext(item, "guid") or _findtext(item, "link")
        listing_id, slug = parse_listing_url(guid)
        if not listing_id:
            warnings.append("skipped item with no listing id: %r" % (guid,))
            continue

        desc = _findtext(item, "description") or ""
        fields, extra, body = parse_description(_block(desc, "description") or "")
        img_block = _block(desc, "image") or ""
        m = _IMG_RE.search(img_block)
        src = m.group(1) if m else None

        def _dim(name):
            d = re.search(_ATTR_RE.format(name), img_block)
            return int(d.group(1)) if d else None

        # A list even though RSS only ever yields one: the Etsy API can fill in
        # the rest later, and the templates render a gallery either way.
        images = []
        if src:
            images.append({
                "src": src,
                "base": image_base(src),
                "width": _dim("width"),
                "height": _dim("height"),
                "alt": None,
            })

        items.append(
            {
                "listing_id": listing_id,
                "slug": slug,
                "url": canonical_listing_url(listing_id, slug),
                "title": strip_shop_suffix(_findtext(item, "title"), shop),
                "price": parse_price(_text(_block(desc, "price") or "")),
                "images": images,
                "image_source": "rss",
                "fields": fields,
                "extra_fields": extra,
                "body": body,
                "pub_date": parse_pubdate(_findtext(item, "pubDate")),
            }
        )

    return items, warnings


def _findtext(elem, tag):
    found = elem.find(tag)
    return found.text if found is not None and found.text else ""


def normalise_image(entry):
    """Accept a plain URL string or {"url":…, "w":…, "h":…} and normalise it.

    Both shapes are allowed because these files are hand-pasted: the
    bookmarklet emits objects with dimensions, but a URL typed by hand should
    work just as well.
    """
    if isinstance(entry, str):
        url, width, height, alt = entry, None, None, None
    elif isinstance(entry, dict):
        url = entry.get("url") or entry.get("src")
        width = entry.get("w") or entry.get("width")
        height = entry.get("h") or entry.get("height")
        alt = entry.get("alt")
    else:
        return None
    if not url or not isinstance(url, str):
        return None
    return {
        "src": url,
        "base": image_base(url),
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "alt": alt or None,
    }


def load_manual_images(directory):
    """Read data/images/<listing_id>.json into {listing_id: [image, …]}.

    Etsy's listing pages are bot-protected, so the gallery URLs are captured in
    a real browser (see tools/) and committed here. This directory is
    human-owned: the sync reads it and never writes to it.
    """
    galleries = {}
    if not os.path.isdir(directory):
        return galleries
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        listing_id = name[: -len(".json")]
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError("%s is not valid JSON: %s" % (path, exc))
        if not isinstance(raw, list):
            raise ValueError("%s should contain a list of image URLs" % path)
        images = [img for img in (normalise_image(e) for e in raw) if img]
        if images:
            galleries[listing_id] = images
    return galleries


def apply_manual_images(items, galleries):
    """Swap in the captured gallery for any listing that has one."""
    applied = []
    for item in items:
        gallery = galleries.get(item["listing_id"])
        if gallery:
            item["images"] = gallery
            item["image_source"] = "file"
            applied.append(item["listing_id"])
    return applied


def apply_manual_images_to_db(db, galleries):
    """Layer captured galleries over a stored works.json.

    The build calls this as well as the sync. Without it, adding a
    data/images/<id>.json file and pushing would deploy a page that still shows
    the single RSS photo until the next sync happened to run — the file would
    look ignored. Applying it at build time makes a plain push enough.
    """
    applied = []
    for work in db.get("works", []):
        gallery = galleries.get(work.get("id"))
        if not gallery or work.get("source") != "etsy":
            continue
        etsy = work.get("etsy")
        if not isinstance(etsy, dict):
            continue
        etsy["images"] = gallery
        etsy["image_source"] = "file"
        applied.append(work["id"])
    return applied


# --------------------------------------------------------------------------
# merge / upsert
# --------------------------------------------------------------------------


def today():
    return datetime.now(timezone.utc).date().isoformat()


def empty_db(feed=DEFAULT_FEED):
    return {"schema_version": SCHEMA_VERSION, "source_feed": feed, "works": []}


def merge(db, feed_items, now=None):
    """Upsert feed items into the works database.

    The ownership boundary is structural, not per-field: `etsy` and `sync` are
    machine-owned, `curation` is human-owned and copied through untouched.
    Nothing is ever deleted — a work that leaves the feed has sold, and keeps
    its page, its frozen last-known-good `etsy` data, and its URL.

    Returns (new_db, summary).
    """
    now = now or today()
    by_id = {w["id"]: w for w in db.get("works", [])}
    seen = set()
    summary = {"new": [], "sold": [], "relisted": [], "changed": [], "manual": []}

    for item in feed_items:
        wid = item["listing_id"]
        seen.add(wid)
        existing = by_id.get(wid)

        if existing is None:
            by_id[wid] = {
                "id": wid,
                "source": "etsy",
                "etsy": item,
                "sync": {
                    "first_seen": now,
                    "delisted_at": None,
                    "relisted_at": None,
                    "available": True,
                },
                "curation": dict(CURATION_DEFAULTS),
            }
            summary["new"].append(wid)
            continue

        if existing.get("source") != "etsy":
            # Hand-added record that happens to share an id: leave it alone.
            summary["manual"].append(wid)
            continue

        was_available = existing.get("sync", {}).get("available", True)
        if existing.get("etsy") != item:
            summary["changed"].append(wid)
        existing["etsy"] = item
        sync = existing.setdefault("sync", {})
        sync.setdefault("first_seen", now)
        sync["available"] = True
        sync["delisted_at"] = None
        if not was_available:
            sync["relisted_at"] = now
            summary["relisted"].append(wid)
        # curation is deliberately not touched.

    for wid, work in by_id.items():
        if wid in seen or work.get("source") != "etsy":
            continue
        sync = work.setdefault("sync", {})
        if sync.get("available", True):
            sync["available"] = False
            sync["delisted_at"] = sync.get("delisted_at") or now
            summary["sold"].append(wid)

    out = dict(db)
    out["schema_version"] = SCHEMA_VERSION
    out.setdefault("source_feed", DEFAULT_FEED)
    out["works"] = [by_id[k] for k in sorted(by_id)]
    return out, summary


def dumps(db):
    """Byte-stable serialisation.

    sort_keys + indent + id-sorted works means the output depends only on the
    data, never on feed order or dict insertion order. That is what makes
    `git diff --quiet` a trustworthy change detector in the workflow.
    """
    return json.dumps(db, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_atomic(path, text):
    """Write via temp file + rename so a crash can never truncate the file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # mkstemp creates 0600; restore normal file permissions honouring umask,
        # or the committed file ends up unreadable to other users.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# view model — the one place curation is layered over etsy
# --------------------------------------------------------------------------


def _image_view(image, index, title, alt_hint):
    """One renderable image: srcset variants plus honest dimensions."""
    base = image.get("base")
    src = image.get("src")
    alt = image.get("alt") or (alt_hint if index == 0 else "%s — view %d" % (title, index + 1))
    return {
        "src": sized(base, "570xN", src),
        "srcset": srcset(base),
        "full": sized(base, "fullxfull", src),
        "width": image.get("width"),
        "height": image.get("height"),
        "alt": alt,
        "is_local": False,
    }


def view(work):
    """Flatten a stored work into what the templates actually render."""
    etsy = work.get("etsy") or {}
    cur = work.get("curation") or {}
    sync = work.get("sync") or {}
    fields = etsy.get("fields") or {}

    title = cur.get("title_override") or fields.get("title") or etsy.get("title") or "Untitled"
    status = cur.get("status_override") or ("available" if sync.get("available", True) else "sold")

    description = fields.get("description") or etsy.get("body") or ""
    details = [(k.capitalize(), fields[k]) for k in ("type", "size", "framed") if fields.get(k)]
    details += list((etsy.get("extra_fields") or {}).items())

    alt_hint = cur.get("alt")
    if not alt_hint:
        bits = [title] + [fields[k] for k in ("type", "size") if fields.get(k)]
        alt_hint = bits[0] if len(bits) == 1 else "%s — %s" % (bits[0], ", ".join(bits[1:]))

    # Schema 1 stored a single `image`; keep reading it so an un-synced file
    # still builds.
    raw_images = etsy.get("images")
    if raw_images is None:
        legacy = etsy.get("image")
        raw_images = [legacy] if legacy and legacy.get("src") else []
    images = [_image_view(img, i, title, alt_hint) for i, img in enumerate(raw_images) if img.get("src")]

    # A hand-picked override, or a copy archived when the piece sold, replaces
    # the Etsy gallery entirely — both are single local files.
    local = cur.get("image_override") or sync.get("archived_image")
    if local:
        images = [{
            "src": local, "srcset": None, "full": local,
            "width": None, "height": None, "alt": alt_hint, "is_local": True,
        }]

    return {
        "id": work["id"],
        "slug": "%s-%s" % (work["id"], etsy.get("slug") or "work"),
        "title": title,
        "status": status,
        "available": status == "available",
        "hidden": bool(cur.get("hidden")),
        "featured": bool(cur.get("featured")),
        "order": cur.get("order"),
        "first_seen": sync.get("first_seen") or "",
        "year": cur.get("year"),
        "description": description,
        "details": details,
        "price": etsy.get("price") or {},
        "etsy_url": etsy.get("url"),
        "images": images,
        "cover": images[0] if images else None,
        "alt": alt_hint,
    }


def visible_works(db):
    """Works to render, ordered deterministically.

    Explicitly NOT ordered by pub_date: Etsy's pubDate reflects listing
    *renewal*, so sorting by it would reshuffle the whole grid every time the
    artist renews a listing.
    """
    views = [view(w) for w in db.get("works", [])]
    views = [v for v in views if not v["hidden"]]
    views.sort(
        key=lambda v: (
            v["order"] is None,
            v["order"] if v["order"] is not None else 0,
            v["first_seen"],
            v["id"],
        )
    )
    return views
