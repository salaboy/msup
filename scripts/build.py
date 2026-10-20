#!/usr/bin/env python3
"""Render the static site from data/works.json and templates/ into _site/.

Two rules shape most of the code here:

1. Internal links and assets are DEPTH-RELATIVE, computed per page from its
   output path. A relative-linked site is correct at both /msup/ and / with no
   rebuild, so the custom-domain cutover cannot leave every page 404ing.
   `base_url` is used only where an absolute URL is required — canonical, og:,
   sitemap, JSON-LD. 404.html is the sole exception (see below).

2. Every value entering a template is escaped in one place. All artwork text is
   third-party-authored via Etsy, so nothing reaches the output unescaped
   unless its placeholder is named *_html and was produced by this file.
"""

import argparse
import datetime
import html
import json
import os
import re
import shutil
import sys
import urllib.parse
from string import Template
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import msuplib as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CONTENT = os.path.join(ROOT, "content")
STATIC = os.path.join(ROOT, "static")
DEFAULT_OUT = os.path.join(ROOT, "_site")
IMAGES_DIR = os.path.join(ROOT, "data", "images")

# Gallery selection is CSS-only (radio + :has()), and style.css carries one
# rule per index. Keep these in step.
MAX_GALLERY = 12

# Notes left in static/ for whoever is adding files — instructions to a human,
# not site content, so they are not copied into the build and never get a URL.
# .DS_Store is here for the same reason it is in .gitignore.
STATIC_SKIP = ("README.txt", ".DS_Store")


def esc(value):
    """The only escaping entry point. quote=True is explicit, not incidental —
    these values land in attributes as often as in text."""
    return html.escape("" if value is None else str(value), quote=True)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def template(name):
    return Template(read(os.path.join(TEMPLATES, name)))


def paragraphs(text):
    """content/*.txt -> escaped <p> blocks. No Markdown, no HTML in content."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return "\n".join("<p>%s</p>" % esc(b.replace("\n", " ")) for b in blocks)


def rel_prefix(out_path):
    """'works/4565-fix/index.html' -> '../../' ; 'index.html' -> ''."""
    return "../" * out_path.count("/")


def json_ld(obj):
    """Serialise JSON-LD for embedding in a <script> block.

    HTML-escaping inside <script> is wrong and would corrupt the JSON, so the
    three characters that could break out of the element are \\u-escaped
    instead. This is a classic source of subtly broken structured data.
    """
    blob = json.dumps(obj, ensure_ascii=False)
    return blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def bookmarklet(path):
    """Turn tools/grab-photos.js into a javascript: URL.

    No minifier: comments are stripped and the rest is percent-encoded with
    newlines intact, which keeps `//` comments harmless and avoids the risk of
    a hand-rolled minifier mangling a regex. Only block comments that start a
    line are removed, so `/*` inside a string or regex is never touched.
    """
    out, skipping = [], False
    for line in read(path).splitlines():
        stripped = line.strip()
        if skipping:
            if "*/" in stripped:
                skipping = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                skipping = True
            continue
        if stripped.startswith("//") or not stripped:
            continue
        out.append(stripped)
    return "javascript:" + urllib.parse.quote("\n".join(out), safe="")


GA_ID_RE = re.compile(r"^G-[A-Z0-9]{4,20}$", re.I)

# A collection slug becomes a directory name and a URL path, so it is validated
# rather than escaped — see collections() for why that distinction matters.
COLLECTION_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def collections(cfg):
    """Validate and normalise cfg['collections'].

    The slug typed into an Etsy listing is only ever a *lookup key* into this
    mapping; every path, href and canonical URL is built from the slug
    configured here. That makes `Collection: ../../../tools` structurally
    incapable of escaping the output tree, rather than relying on slugify()
    happening to neutralise it.

    A malformed slug or banner path is refused outright, following
    analytics_snippet: these values are interpolated into paths, where
    escaping is not the right tool.
    """
    out, seen = [], set()
    for entry in cfg.get("collections") or []:
        coll = dict(entry)
        slug = (coll.get("slug") or "").strip()
        if not COLLECTION_SLUG_RE.match(slug):
            raise ValueError(
                "collection slug must be lowercase words joined by '-', got %r" % slug
            )
        if slug in seen:
            raise ValueError("duplicate collection slug %r — one would overwrite the other" % slug)
        seen.add(slug)

        banner = (coll.get("banner") or "").strip()
        if banner.startswith("/"):
            # A leading slash breaks both consumers: rel + "/img/…" yields a
            # doubled slash, and urljoin() would resolve it against the host
            # root, escaping base_path on the project-pages URL.
            raise ValueError(
                "collection banner must be site-root-relative with no leading '/', got %r" % banner
            )

        coll["slug"] = slug
        coll["banner"] = banner
        coll["title"] = coll.get("title") or slug
        coll["path"] = "collections/%s/" % slug
        out.append(coll)
    return out


def collection_keys(colls):
    """Map the slug of an Etsy `Collection:` label to its collection.

    Both the configured slug and the configured *title* are accepted as keys.
    On Etsy the artist types the title — "Collection: Winter Series" — while
    the slug is a URL choice that may be shorter, and nothing on the listing
    hints at it. Matching the title too means the two cannot silently drift
    apart.

    Explicit slugs are registered first so a title alias can never shadow
    another collection's real slug.
    """
    keys = {c["slug"]: c for c in colls}
    for coll in colls:
        alias = m.slugify(coll["title"])
        if alias:
            keys.setdefault(alias, coll)
    return keys


def collection_members(views, keys):
    """Group works by resolved collection, keyed by configured slug.

    A filter over visible_works()' canonical order, never a re-sort: a piece
    sits in the same position on its collection page as it does on /works/,
    which is what curation.order is for. Resolution goes through `keys` so the
    grid and the work page's back-link can never disagree about membership.
    """
    members = {}
    for work in views:
        coll = keys.get(work["collection"]) if work["collection"] else None
        if coll:
            members.setdefault(coll["slug"], []).append(work)
    return members


EXHIBITIONS_PATH = "exhibitions/"

# Spelled out rather than taken from strftime("%B"), which is locale-dependent:
# the same build must read "10 November 2026" on a laptop and on CI.
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

# What counts as a show photo when the folder is listed. Anything else sitting
# in there — a stray .DS_Store, a notes file — is ignored rather than rendered
# as a broken image.
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")


def one_date(value, field, slug):
    """Parse an ISO date, or refuse it by name.

    Dates are compared (upcoming vs past) and formatted, not just printed, so a
    typo must stop the build rather than silently sort a show into the wrong
    half of the page.
    """
    try:
        return datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(
            "exhibition %r: %s must be an ISO date like '2026-11-10', got %r"
            % (slug, field, value)
        )


def format_date_range(start, end=None):
    """A human date line: '10 November 2026', '10–14 November 2026',
    '28 November – 3 December 2026', '30 December 2026 – 2 January 2027'.

    Written out in full because this is the one line a visitor acts on, and
    '10/11' is read two different ways either side of the Atlantic.
    """
    if end is None or end == start:
        return "%d %s %d" % (start.day, MONTHS[start.month - 1], start.year)
    if (start.year, start.month) == (end.year, end.month):
        return "%d–%d %s %d" % (start.day, end.day, MONTHS[start.month - 1], start.year)
    if start.year == end.year:
        return "%d %s – %d %s %d" % (
            start.day, MONTHS[start.month - 1], end.day, MONTHS[end.month - 1], end.year
        )
    return "%d %s %d – %d %s %d" % (
        start.day, MONTHS[start.month - 1], start.year,
        end.day, MONTHS[end.month - 1], end.year,
    )


def exhibitions(cfg):
    """Validate and normalise cfg['exhibitions'].

    The same bargain as collections(): the slug becomes a directory name, a URL
    path and the name of the folder show photos are read from, so it is
    validated rather than escaped. Dates are parsed here too, so that every
    later consumer is handed a real date object and the Upcoming/Past split
    cannot be fooled by a string comparison.
    """
    out, seen = [], set()
    for entry in cfg.get("exhibitions") or []:
        ex = dict(entry)
        slug = (ex.get("slug") or "").strip()
        if not COLLECTION_SLUG_RE.match(slug):
            raise ValueError(
                "exhibition slug must be lowercase words joined by '-', got %r" % slug
            )
        if slug in seen:
            raise ValueError("duplicate exhibition slug %r — one would overwrite the other" % slug)
        seen.add(slug)

        poster = (ex.get("poster") or "").strip()
        if poster.startswith("/"):
            # Same reasoning as a collection banner: a leading slash both
            # doubles the separator and escapes base_path under urljoin().
            raise ValueError(
                "exhibition poster must be site-root-relative with no leading '/', got %r" % poster
            )

        start = one_date(ex.get("date"), "date", slug)
        end = one_date(ex["end_date"], "end_date", slug) if ex.get("end_date") else None
        if end and end < start:
            raise ValueError(
                "exhibition %r: end_date %s falls before date %s" % (slug, end, start)
            )

        ex["slug"] = slug
        ex["poster"] = poster
        ex["title"] = ex.get("title") or slug
        ex["path"] = "%s%s/" % (EXHIBITIONS_PATH, slug)
        ex["start"] = start
        ex["end"] = end
        ex["date_label"] = ex.get("date_label") or format_date_range(start, end)
        ex["photos_dir"] = "img/exhibitions/%s" % slug
        out.append(ex)
    return out


def nav_entries(cfg, colls, exhibs):
    """Primary nav: site.json's own entries with in_nav collections and, if any
    exhibition asks for it, a single Exhibitions link spliced in just before
    About, which reads better last.

    Collections contribute one entry each — they are destinations in their own
    right. Exhibitions contribute at most one, pointing at the index: shows
    accumulate over the years and a nav that grew with them would be unusable.
    """
    items = [dict(e) for e in cfg["nav"]]
    extra = [
        {"label": c.get("nav_label") or c["title"], "path": c["path"]}
        for c in colls if c.get("in_nav")
    ]
    if any(e.get("in_nav") for e in exhibs):
        extra.append({"label": cfg.get("exhibitions_nav_label") or "Exhibitions",
                      "path": EXHIBITIONS_PATH})
    if not extra:
        return items
    cut = next((i for i, e in enumerate(items) if e["path"] == "about/"), len(items))
    return items[:cut] + extra + items[cut:]


def analytics_snippet(measurement_id):
    """GA4 gtag.js, or "" when no ID is configured.

    The ID is interpolated into JavaScript, so it is validated against GA4's
    format rather than escaped — anything that isn't a plain measurement ID is
    refused outright, which removes the injection path entirely.
    """
    if not measurement_id:
        return ""
    if not GA_ID_RE.match(measurement_id):
        raise ValueError(
            "google_analytics must look like 'G-XXXXXXXXXX', got %r" % measurement_id
        )
    return (
        '<script async src="https://www.googletagmanager.com/gtag/js?id=%s"></script>\n'
        "<script>\n"
        "window.dataLayer = window.dataLayer || [];\n"
        "function gtag(){dataLayer.push(arguments);}\n"
        "gtag('js', new Date());\n"
        "gtag('config', '%s');\n"
        "</script>" % (measurement_id, measurement_id)
    )


class Site:
    def __init__(self, cfg, db, out, today=None):
        self.cfg = cfg
        self.db = db
        self.out = out
        self.base_url = cfg["base_url"]
        self.base_path = cfg["base_path"]
        self.analytics_html = analytics_snippet(cfg.get("google_analytics", ""))
        self.pages = []  # (absolute url, changefreq) for the sitemap
        # Collections are resolved here rather than in main() so that the
        # rendered site stays a pure function of the cfg dict — constructible
        # in a test, and immune to main() being called twice in one process
        # (serve.py imports and calls it).
        self.collections = collections(cfg)
        self.by_collection = collection_keys(self.collections)
        self.exhibitions = exhibitions(cfg)
        # Injected for the same reason: whether a show is upcoming or past is
        # the one thing on this site that changes without anyone editing a
        # file, so "today" is an argument and not a call to the clock buried
        # three frames down. main() leaves it unset; tests pin it.
        self.today = today or datetime.date.today()
        self.nav_items = nav_entries(cfg, self.collections, self.exhibitions)
        self.warnings = []

    # -- exhibition dates ------------------------------------------------

    def is_past(self, ex):
        """A show is past once its last day is behind us — so it stays listed
        as upcoming, and on the home page, throughout the day it happens."""
        return (ex["end"] or ex["start"]) < self.today

    def upcoming(self):
        """Soonest first: the next thing to go to is the thing to lead with."""
        return sorted((e for e in self.exhibitions if not self.is_past(e)),
                      key=lambda e: e["start"])

    def past(self):
        """Most recent first, so the archive reads backwards from now."""
        return sorted((e for e in self.exhibitions if self.is_past(e)),
                      key=lambda e: e["start"], reverse=True)


    # -- shared chrome ---------------------------------------------------

    def nav(self, rel, active):
        items = []
        for entry in self.nav_items:
            current = ' aria-current="page"' if entry["path"] == active else ""
            items.append(
                '      <li><a href="%s%s"%s>%s</a></li>'
                % (rel, esc(entry["path"]), current, esc(entry["label"]))
            )
        return template("partials/nav.html").safe_substitute(
            rel=rel, nav_items_html="\n".join(items)
        )

    def footer(self, rel):
        return template("partials/footer.html").safe_substitute(
            rel=rel,
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
            instagram_url=esc(self.cfg["instagram_url"]),
        )

    def render_page(self, out_path, main_html, title, description,
                    og_type="website", og_image=None, head_extra_html="",
                    active=None, changefreq="monthly", in_sitemap=True):
        rel = rel_prefix(out_path)
        url_path = out_path[: -len("index.html")] if out_path.endswith("index.html") else out_path
        canonical = urllib.parse.urljoin(self.base_url, url_path)

        og_image_html = ""
        if og_image:
            og_image_html = '<meta property="og:image" content="%s">' % esc(og_image)

        page = template("base.html").safe_substitute(
            rel=rel,
            page_title=esc(title),
            page_description=esc(description),
            site_title=esc(self.cfg["title"]),
            canonical=esc(canonical),
            og_type=esc(og_type),
            og_image_html=og_image_html,
            head_extra_html=head_extra_html,
            analytics_html=self.analytics_html,
            nav_html=self.nav(rel, active),
            footer_html=self.footer(rel),
            main_html=main_html,
        )
        self.write(out_path, page)
        if in_sitemap:
            self.pages.append((canonical, changefreq))

    def write(self, out_path, text):
        full = os.path.join(self.out, out_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    # -- images ----------------------------------------------------------

    def img_url(self, image, rel, key="src"):
        """Local files (archived copies, overrides) need the page's depth
        prefix; Etsy URLs are absolute and must be left alone."""
        value = image.get(key) or ""
        return rel + value if value and image.get("is_local") else value

    def abs_img(self, work):
        """Absolute image URL for og: and JSON-LD, which cannot take a relative path."""
        cover = work.get("cover")
        if not cover:
            return None
        value = cover.get("full") or cover.get("src")
        if not value:
            return None
        return urllib.parse.urljoin(self.base_url, value) if cover.get("is_local") else value

    def img_tag(self, image, rel, sizes, cls="", extra="", index=0):
        """One <img>. srcset is omitted when the size token was unrecognised,
        rather than inventing variants that may not exist."""
        bits = ['<img src="%s"' % esc(self.img_url(image, rel))]
        if image.get("srcset"):
            bits.append('srcset="%s"' % esc(image["srcset"]))
            bits.append('sizes="%s"' % esc(sizes))
        if image.get("width") and image.get("height"):
            bits.append('width="%d" height="%d"' % (image["width"], image["height"]))
        bits.append('alt="%s"' % esc(image.get("alt") or ""))
        if cls:
            bits.append('class="%s"' % esc(cls))
        bits.append('data-i="%d"' % index)
        if extra:
            bits.append(extra)
        bits.append('decoding="async" referrerpolicy="no-referrer">')
        return " ".join(bits)

    def gallery(self, work, rel):
        """Big Cartel-style gallery: one large image, thumbnails beneath.

        Selection is CSS-only — a hidden radio per photo, matched by :has() in
        style.css. That keeps the site JavaScript-free and gives keyboard and
        screen-reader support for free from native radio semantics. If :has()
        is unsupported the first photo simply stays visible.
        """
        images = work["images"][:MAX_GALLERY]
        if not images:
            return "", ""

        slides = "\n".join(
            "        " + self.img_tag(
                img, rel, "(max-width: 900px) 92vw, 58vw",
                cls="gallery-slide", index=i,
                extra="" if i == 0 else 'loading="lazy"',
            )
            for i, img in enumerate(images)
        )

        if len(images) < 2:
            return slides, ""

        name = "gallery-%s" % work["id"]
        thumbs = []
        for i, img in enumerate(images):
            base_src = self.img_url(img, rel)
            # Etsy serves square crops at these tokens; a local file has no
            # variants so it is reused at its own size.
            if img.get("srcset") and img.get("full"):
                small = img["full"].replace("il_fullxfull", "il_180x180")
                retina = img["full"].replace("il_fullxfull", "il_300x300")
                src_attr = 'src="%s" srcset="%s 1x, %s 2x"' % (esc(small), esc(small), esc(retina))
            else:
                src_attr = 'src="%s"' % esc(base_src)
            thumbs.append(
                '      <label class="gallery-thumb" data-i="%d">\n'
                '        <input type="radio" name="%s" value="%d"%s '
                'aria-label="Show photo %d of %d">\n'
                '        <img %s alt="" width="180" height="180" loading="lazy" '
                'decoding="async" referrerpolicy="no-referrer">\n'
                '      </label>'
                % (i, esc(name), i, " checked" if i == 0 else "", i + 1, len(images), src_attr)
            )

        thumbs_html = (
            '    <fieldset class="gallery-thumbs">\n'
            '      <legend class="sr-only">Photos of %s</legend>\n%s\n    </fieldset>'
            % (esc(work["title"]), "\n".join(thumbs))
        )
        return slides, thumbs_html

    # -- work pages ------------------------------------------------------

    def card(self, work, rel):
        cover = work["cover"] or {}
        srcset = ' srcset="%s"' % esc(cover["srcset"]) if cover.get("srcset") else ""
        dims = ""
        if cover.get("width") and cover.get("height"):
            dims = ' width="%d" height="%d"' % (cover["width"], cover["height"])

        price = work["price"]
        if work["available"] and price.get("amount"):
            meta = "%s %s" % (price["amount"], price.get("currency") or "")
        elif work["available"]:
            meta = price.get("raw") or "Available"
        else:
            meta = "Sold"

        return template("partials/work_card.html").safe_substitute(
            rel=rel,
            slug=esc(work["slug"]),
            img_src=esc(self.img_url(cover, rel)),
            srcset_attr=srcset,
            dim_attrs=dims,
            alt=esc(cover.get("alt") or work["alt"]),
            title=esc(work["title"]),
            meta=esc(meta.strip()),
            card_modifier="" if work["available"] else " work-card-sold",
        )

    def grid(self, works, rel):
        return "\n".join(self.card(w, rel) for w in works)

    # -- collections -----------------------------------------------------

    def collection_banner(self, coll, rel, heading_html, cta_html=""):
        """One collection banner, rendered per page.

        `rel` is an argument rather than something baked in once: the home
        teaser renders at '' and the collection page at '../../', so a shared
        pre-rendered string would ship a broken image on one of the two.
        """
        dims = ""
        if coll.get("banner_width") and coll.get("banner_height"):
            dims = ' width="%d" height="%d"' % (
                int(coll["banner_width"]), int(coll["banner_height"])
            )
        return template("partials/collection_banner.html").safe_substitute(
            banner_src=esc(rel + coll["banner"]),
            # No fallback to the title: that would read the <h1> twice to a
            # screen reader. An unset alt means the banner is decorative.
            banner_alt=esc(coll.get("banner_alt") or ""),
            dim_attrs=dims,
            heading_html=heading_html,
            intro_html='<p class="lede">%s</p>' % esc(coll["intro"]) if coll.get("intro") else "",
            cta_html=cta_html,
        )

    def collection_foreword(self, coll):
        """The curator's foreword for a collection, or '' if it has none.

        The text lives in `content/collections/<slug>.txt` rather than in
        site.json: it runs to several paragraphs, and site.json's other
        collection fields are one-liners. A collection with no such file
        renders nothing, so this is opt-in per collection.

        The slug is safe to join onto a path — collections() has already
        refused anything that is not lowercase words joined by '-'.
        """
        path = os.path.join(CONTENT, "collections", "%s.txt" % coll["slug"])
        if not os.path.exists(path):
            return ""
        body = paragraphs(read(path))
        if not body:
            return ""
        role = coll.get("foreword_role")
        return template("partials/collection_foreword.html").safe_substitute(
            foreword_body_html=body,
            foreword_by=esc(coll.get("foreword_by") or self.cfg["title"]),
            foreword_role_html=
            '<span class="foreword-date">%s</span>' % esc(" · %s" % role) if role else "",
        )

    def build_collection_page(self, coll, works):
        out_path = "collections/%s/index.html" % coll["slug"]
        rel = rel_prefix(out_path)

        main_html = template("collection.html").safe_substitute(
            rel=rel,
            banner_html=self.collection_banner(coll, rel, "<h1>%s</h1>" % esc(coll["title"])),
            foreword_html=self.collection_foreword(coll),
            works_html=self.grid(works, rel),
            empty_html="" if works else
            '<p class="empty-note">Nothing from this collection is listed right now.</p>',
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
        )

        ld = {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": coll["title"],
            "url": urllib.parse.urljoin(self.base_url, coll["path"]),
            "mainEntity": {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "url": urllib.parse.urljoin(self.base_url, "works/%s/" % w["slug"]),
                        "name": w["title"],
                    }
                    for i, w in enumerate(works)
                ],
            },
        }
        if coll.get("intro"):
            ld["description"] = coll["intro"]

        summary = coll.get("intro") or "%s — a collection by %s." % (coll["title"], self.cfg["title"])
        self.render_page(
            out_path, main_html,
            title="%s — %s" % (coll["title"], self.cfg["title"]),
            description=summary[:300],
            og_image=urllib.parse.urljoin(self.base_url, coll["banner"]) if coll["banner"] else None,
            head_extra_html='<script type="application/ld+json">%s</script>' % json_ld(ld),
            active=coll["path"], changefreq="weekly",
        )

    def check_banner(self, coll):
        """Warn about a banner that will 404.

        The static copy loop validates nothing, and base.html's onerror
        placeholder is styled for `figure.img-missing .frame` — outside one it
        degrades badly, so a missing banner should be loud at build time.
        """
        if not coll["banner"]:
            self.warnings.append("collection %r has no banner image" % coll["slug"])
        elif not os.path.exists(os.path.join(STATIC, coll["banner"])):
            self.warnings.append(
                "collection %r: banner missing — expected static/%s" % (coll["slug"], coll["banner"])
            )

    # -- exhibitions -----------------------------------------------------

    def exhibition_photos(self, ex):
        """Show photos, read from static/img/exhibitions/<slug>/.

        A folder rather than a list in site.json: photos arrive in a batch the
        week after a show, usually off a phone, and GitHub's "Add file →
        Upload files" is then the whole job. Filename order is the running
        order, so `01-`, `02-` prefixes are all the control needed.

        The slug has already been validated by exhibitions(), so joining it
        onto a path here is safe by construction rather than by sanitising.
        """
        folder = os.path.join(STATIC, "img", "exhibitions", ex["slug"])
        if not os.path.isdir(folder):
            return []
        alts = ex.get("photo_alt") or {}
        return [
            {"src": "%s/%s" % (ex["photos_dir"], name), "alt": alts.get(name, "")}
            for name in sorted(os.listdir(folder))
            if not name.startswith(".") and name.lower().endswith(PHOTO_EXTS)
        ]

    def exhibition_meta(self, ex):
        """Date, time and place. The written-out date carries a machine-readable
        datetime beside it, which is what a calendar or a crawler reads."""
        bits = ['<time class="exhibition-date" datetime="%s">%s</time>'
                % (esc(ex["start"].isoformat()), esc(ex["date_label"]))]
        for key, cls in (("time", "exhibition-time"),
                         ("place", "exhibition-place"),
                         ("address", "exhibition-address")):
            if ex.get(key):
                bits.append('<span class="%s">%s</span>' % (cls, esc(ex[key])))
        return '<p class="exhibition-meta">%s</p>' % "".join(bits)

    def exhibition_banner(self, ex, rel, heading_html, cta_html=""):
        """One poster beside its details. `rel` is an argument for the same
        reason as collection_banner()'s: the home teaser renders at '' and the
        show's own page at '../../'."""
        dims = ""
        if ex.get("poster_width") and ex.get("poster_height"):
            dims = ' width="%d" height="%d"' % (
                int(ex["poster_width"]), int(ex["poster_height"])
            )
        return template("partials/exhibition_banner.html").safe_substitute(
            poster_src=esc(rel + ex["poster"]),
            # As with a collection banner, an unset alt means decorative —
            # better than reading the heading out twice.
            poster_alt=esc(ex.get("poster_alt") or ""),
            dim_attrs=dims,
            heading_html=heading_html,
            meta_html=self.exhibition_meta(ex),
            intro_html='<p class="lede">%s</p>' % esc(ex["intro"]) if ex.get("intro") else "",
            cta_html=cta_html,
        )

    def exhibition_card(self, ex, rel):
        meta = " · ".join(b for b in (ex["date_label"], ex.get("place")) if b)
        return template("partials/exhibition_card.html").safe_substitute(
            rel=rel,
            path=esc(ex["path"]),
            poster_src=esc(rel + ex["poster"]) if ex["poster"] else
            esc(rel + "img/placeholder.svg"),
            poster_alt=esc(ex.get("poster_alt") or ""),
            title=esc(ex["title"]),
            meta=esc(meta),
        )

    def exhibition_section(self, heading, slug, items, rel):
        """One half of the index. An empty half renders nothing at all rather
        than a heading over a void — a site with no past shows yet should not
        advertise the fact."""
        if not items:
            return ""
        return (
            '<section class="works" aria-labelledby="%s-heading">\n'
            '  <h2 id="%s-heading" class="section-heading">%s</h2>\n'
            '  <ul class="exhibition-grid">\n%s\n  </ul>\n</section>'
            % (slug, slug, esc(heading),
               "\n".join(self.exhibition_card(e, rel) for e in items))
        )

    def check_poster(self, ex):
        """Warn about a poster that will 404 — same reasoning as check_banner:
        the static copy loop validates nothing, so a missing file should be
        loud at build time rather than a placeholder on the live site."""
        if not ex["poster"]:
            self.warnings.append("exhibition %r has no poster image" % ex["slug"])
        elif not os.path.exists(os.path.join(STATIC, ex["poster"])):
            self.warnings.append(
                "exhibition %r: poster missing — expected static/%s" % (ex["slug"], ex["poster"])
            )

    def build_exhibition_page(self, ex):
        out_path = "%s%s/index.html" % (EXHIBITIONS_PATH, ex["slug"])
        rel = rel_prefix(out_path)
        photos = self.exhibition_photos(ex)

        # A show often *is* a collection hung on a wall, so link the two when
        # the slug resolves. An unmatched slug warns rather than failing: the
        # show itself is still worth publishing.
        coll = next((c for c in self.collections if c["slug"] == ex.get("collection")), None)
        if ex.get("collection") and not coll:
            self.warnings.append(
                "exhibition %r: collection %r matches no entry in site.json — link omitted"
                % (ex["slug"], ex["collection"])
            )
        cta_html = ""
        if coll:
            cta_html = '<p><a class="button button-quiet" href="%s%s">See the collection</a></p>' % (
                rel, esc(coll["path"])
            )

        # The <ul> is built here rather than sitting in the template so that a
        # show with no photos yet emits no empty list at all — an empty <ul>
        # is a node in the accessibility tree announcing nothing.
        photos_html, empty_html = "", ""
        if photos:
            photos_html = '  <ul class="photo-grid">\n%s\n  </ul>' % "\n".join(
                '    <li class="photo-card">\n'
                '      <a href="%s%s">\n'
                '        <div class="frame">\n'
                '          <img src="%s%s" alt="%s" loading="lazy" decoding="async">\n'
                '        </div>\n'
                '      </a>\n'
                '    </li>' % (rel, esc(p["src"]), rel, esc(p["src"]), esc(p["alt"]))
                for p in photos
            )
        elif self.is_past(ex):
            empty_html = '<p class="empty-note">Photos from this show are still being added.</p>'
        else:
            empty_html = '<p class="empty-note">Photos will appear here after the show.</p>'

        main_html = template("exhibition.html").safe_substitute(
            rel=rel,
            banner_html=self.exhibition_banner(
                ex, rel, "<h1>%s</h1>" % esc(ex["title"]), cta_html
            ),
            statement_html=self.exhibition_statement(ex),
            photos_html=photos_html,
            empty_html=empty_html,
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
        )

        ld = {
            "@context": "https://schema.org",
            "@type": "ExhibitionEvent",
            "name": ex["title"],
            "startDate": ex["start"].isoformat(),
            "endDate": (ex["end"] or ex["start"]).isoformat(),
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "url": urllib.parse.urljoin(self.base_url, ex["path"]),
            "performer": {"@type": "Person", "name": self.cfg["author"],
                          "alternateName": self.cfg["title"]},
        }
        if ex.get("place") or ex.get("address"):
            place = {"@type": "Place", "name": ex.get("place") or ex.get("address")}
            if ex.get("address"):
                place["address"] = ex["address"]
            ld["location"] = place
        if ex.get("intro"):
            ld["description"] = ex["intro"]
        if ex["poster"]:
            ld["image"] = urllib.parse.urljoin(self.base_url, ex["poster"])

        summary = ex.get("intro") or "%s — %s%s." % (
            ex["title"], ex["date_label"], ", %s" % ex["place"] if ex.get("place") else ""
        )
        self.render_page(
            out_path, main_html,
            title="%s — %s" % (ex["title"], self.cfg["title"]),
            description=summary[:300],
            og_type="article",
            og_image=urllib.parse.urljoin(self.base_url, ex["poster"]) if ex["poster"] else None,
            head_extra_html='<script type="application/ld+json">%s</script>' % json_ld(ld),
            active=EXHIBITIONS_PATH, changefreq="monthly",
        )

    def exhibition_statement(self, ex):
        """An optional note about the show, from content/exhibitions/<slug>.txt.

        Same shape and same opt-in rule as a collection's foreword, and it
        reuses that partial: two tinted panels that behaved differently would
        be a worse site than one that behaves the same way twice.
        """
        path = os.path.join(CONTENT, "exhibitions", "%s.txt" % ex["slug"])
        if not os.path.exists(path):
            return ""
        body = paragraphs(read(path))
        if not body:
            return ""
        role = ex.get("statement_role") or ex["date_label"]
        return template("partials/collection_foreword.html").safe_substitute(
            foreword_body_html=body,
            foreword_by=esc(ex.get("statement_by") or self.cfg["title"]),
            foreword_role_html=
            '<span class="foreword-date">%s</span>' % esc(" · %s" % role) if role else "",
        )

    def build_exhibitions_index(self):
        out_path = "%sindex.html" % EXHIBITIONS_PATH
        rel = rel_prefix(out_path)
        upcoming, past = self.upcoming(), self.past()

        main_html = template("exhibitions_index.html").safe_substitute(
            rel=rel,
            intro=esc("Shows, openings and events. Dates and places as they are confirmed."),
            upcoming_html=self.exhibition_section("Upcoming", "upcoming", upcoming, rel),
            past_html=self.exhibition_section("Past", "past", past, rel),
            empty_html="" if self.exhibitions else
            '<p class="empty-note">Nothing in the diary right now.</p>',
        )

        ld = {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Exhibitions",
            "url": urllib.parse.urljoin(self.base_url, EXHIBITIONS_PATH),
            "mainEntity": {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "url": urllib.parse.urljoin(self.base_url, e["path"]),
                        "name": e["title"],
                    }
                    for i, e in enumerate(upcoming + past)
                ],
            },
        }
        self.render_page(
            out_path, main_html,
            title="Exhibitions — %s" % self.cfg["title"],
            description="Exhibitions and events featuring work by %s." % self.cfg["title"],
            head_extra_html='<script type="application/ld+json">%s</script>' % json_ld(ld),
            active=EXHIBITIONS_PATH, changefreq="weekly",
        )

    def build_work_page(self, work):
        out_path = "works/%s/index.html" % work["slug"]
        rel = rel_prefix(out_path)
        main_image_html, thumbs_html = self.gallery(work, rel)

        price = work["price"]
        price_html = ""
        if price.get("amount"):
            price_html = '<p class="work-price">%s %s</p>' % (
                esc(price["amount"]), esc(price.get("currency") or "")
            )
        elif price.get("raw"):
            price_html = '<p class="work-price">%s</p>' % esc(price["raw"])

        description_html = "\n    ".join(
            '<p class="work-description">%s</p>' % esc(p.strip())
            for p in (work["description"] or "").split("\n") if p.strip()
        )

        # A resolved collection is rendered as a link using the *configured*
        # title, so no Etsy-authored text reaches the page at all, and its
        # spec row is dropped — here rather than in view(), so that an
        # unrecognised label degrades to the current behaviour instead of
        # losing the artist's text.
        coll = self.by_collection.get(work["collection"])
        details = work["details"]
        collection_html = ""
        if coll:
            details = [(k, v) for k, v in details if k.strip().lower() != m.COLLECTION_LABEL]
            collection_html = '<p class="work-collection">Part of <a href="%s%s">%s</a></p>' % (
                rel, esc(coll["path"]), esc(coll["title"])
            )
        elif work["collection"]:
            self.warnings.append(
                "%s: Collection %r matches no entry in site.json — shown as a plain detail row"
                % (work["id"], work["collection_label"])
            )

        details_html = ""
        if details:
            rows = "\n".join(
                "        <div><dt>%s</dt><dd>%s</dd></div>" % (esc(k), esc(v))
                for k, v in details
            )
            details_html = '<dl class="work-details">\n%s\n      </dl>' % rows

        if work["available"] and work["etsy_url"]:
            buy_html = '<a class="button" href="%s" rel="noopener">Buy on Etsy</a>' % esc(work["etsy_url"])
        elif work["available"]:
            buy_html = '<a class="button" href="%s" rel="noopener">See the shop</a>' % esc(self.cfg["etsy_shop_url"])
        else:
            buy_html = '<a class="button button-quiet" href="%s" rel="noopener">See available work on Etsy</a>' % esc(self.cfg["etsy_shop_url"])

        main_html = template("work.html").safe_substitute(
            rel=rel,
            title=esc(work["title"]),
            status=esc(work["status"]),
            status_label=esc("Available" if work["available"] else "Sold"),
            collection_html=collection_html,
            price_html=price_html,
            description_html=description_html,
            details_html=details_html,
            buy_html=buy_html,
            main_image_html=main_image_html,
            thumbs_html=thumbs_html,
            instagram_url=esc(self.cfg["instagram_url"]),
        )

        offer = {
            "@type": "Offer",
            "availability": "https://schema.org/%s" % ("InStock" if work["available"] else "SoldOut"),
            "url": work["etsy_url"] or self.cfg["etsy_shop_url"],
        }
        if price.get("amount") and price.get("currency"):
            offer["price"] = price["amount"]
            offer["priceCurrency"] = price["currency"]

        ld = {
            "@context": "https://schema.org",
            "@type": "VisualArtwork",
            "name": work["title"],
            "creator": {"@type": "Person", "name": self.cfg["author"],
                        "alternateName": self.cfg["title"]},
            "url": urllib.parse.urljoin(self.base_url, "works/%s/" % work["slug"]),
            "offers": offer,
        }
        if work["description"]:
            ld["description"] = work["description"]
        if self.abs_img(work):
            ld["image"] = [i["full"] or i["src"] for i in work["images"] if not i.get("is_local")] \
                          or self.abs_img(work)
        for key, value in details:
            if key.lower() == "type":
                ld["artform"] = value
            elif key.lower() == "size":
                ld["size"] = value

        summary = work["description"] or "%s by %s." % (work["title"], self.cfg["title"])
        self.render_page(
            out_path, main_html,
            title="%s — %s" % (work["title"], self.cfg["title"]),
            description=summary[:300],
            og_type="article",
            og_image=self.abs_img(work),
            head_extra_html='<script type="application/ld+json">%s</script>' % json_ld(ld),
            active="works/",
        )

    # -- pages -----------------------------------------------------------

    def build(self):
        works = m.visible_works(self.db)
        available = [w for w in works if w["available"]]
        sold = [w for w in works if not w["available"]]
        featured = [w for w in available if w["featured"]] or available
        members = collection_members(works, self.by_collection)

        for coll in self.collections:
            self.check_banner(coll)
        for ex in self.exhibitions:
            self.check_poster(ex)

        home_banners = "\n".join(
            self.collection_banner(
                coll, "",
                "<h2>%s</h2>" % esc(coll["title"]),
                '<p><a class="button" href="%s">View the collection</a></p>' % esc(coll["path"]),
            )
            for coll in self.collections if coll.get("on_home")
        )

        # Only shows still ahead of us, and only those that asked. A past show
        # taking up the top of the home page would be the site telling people
        # to turn up to something that already happened.
        home_exhibitions = "\n".join(
            self.exhibition_banner(
                ex, "",
                "<h2>%s</h2>" % esc(ex["title"]),
                '<p><a class="button" href="%s">Details</a></p>' % esc(ex["path"]),
            )
            for ex in self.upcoming() if ex.get("on_home")
        )

        home = template("home.html").safe_substitute(
            rel="",
            home_intro=esc(read(os.path.join(CONTENT, "home_intro.txt")).strip()),
            exhibition_banner_html=home_exhibitions,
            collection_banner_html=home_banners,
            works_html=self.grid(featured, ""),
            empty_html="" if featured else
            '<p class="empty-note">Nothing listed right now — new work is added as it goes up on Etsy.</p>',
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
        )
        self.render_page(
            "index.html", home,
            title="%s — %s" % (self.cfg["title"], self.cfg["tagline"]),
            description=self.cfg["description"],
            og_image=self.abs_img(featured[0]) if featured else None,
            changefreq="weekly",
        )

        sold_section = ""
        if sold:
            sold_section = (
                '<section class="works" aria-labelledby="sold-heading">\n'
                '  <h2 id="sold-heading" class="section-heading">Sold</h2>\n'
                '  <ul class="work-grid">\n%s\n  </ul>\n</section>' % self.grid(sold, "../")
            )
        works_index = template("works_index.html").safe_substitute(
            rel="../",
            intro=esc("Originals and prints. Everything available is sold through Etsy."),
            available_html=self.grid(available, "../"),
            available_empty_html="" if available else
            '<p class="empty-note">Nothing available at the moment.</p>',
            sold_section_html=sold_section,
        )
        self.render_page(
            "works/index.html", works_index,
            title="Work — %s" % self.cfg["title"],
            description="All work by %s, available and sold." % self.cfg["title"],
            active="works/", changefreq="weekly",
        )

        for work in works:
            self.build_work_page(work)

        for coll in self.collections:
            self.build_collection_page(coll, members.get(coll["slug"], []))

        # The index exists whenever any show is configured; a site with none
        # keeps the section — and its nav entry — off entirely.
        if self.exhibitions:
            self.build_exhibitions_index()
            for ex in self.exhibitions:
                self.build_exhibition_page(ex)

        # Setup page for the photo bookmarklet. Not in the nav or the sitemap —
        # it exists so the artist can reach it from any device.
        tools = template("tools.html").safe_substitute(
            rel="../",
            bookmarklet_href=esc(bookmarklet(os.path.join(ROOT, "tools", "grab-photos.js"))),
        )
        self.render_page(
            "tools/index.html", tools,
            title="Add photos — %s" % self.cfg["title"],
            description="Internal helper for adding artwork photos.",
            head_extra_html='<meta name="robots" content="noindex, nofollow">',
            in_sitemap=False,
        )

        about = template("about.html").safe_substitute(
            rel="../",
            about_html=paragraphs(read(os.path.join(CONTENT, "about.txt"))),
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
            instagram_url=esc(self.cfg["instagram_url"]),
        )
        self.render_page(
            "about/index.html", about,
            title="About — %s" % self.cfg["title"],
            description="About %s (%s), artist and software engineer based in London."
                        % (self.cfg["title"], self.cfg["author"]),
            active="about/", changefreq="yearly",
        )

        # 404 — base_path-absolute, never relative (see module docstring)
        self.write("404.html", template("404.html").safe_substitute(
            analytics_html=self.analytics_html,
            base_path=esc(self.base_path),
            site_title=esc(self.cfg["title"]),
            etsy_shop_url=esc(self.cfg["etsy_shop_url"]),
            instagram_url=esc(self.cfg["instagram_url"]),
        ))

        urls = "\n".join(
            "  <url><loc>%s</loc><changefreq>%s</changefreq></url>" % (xml_escape(url), freq)
            for url, freq in self.pages
        )
        self.write("sitemap.xml", template("sitemap.xml").safe_substitute(urls_xml=urls))
        self.write("robots.txt", template("robots.txt").safe_substitute(base_url=self.base_url))

        for entry in os.listdir(STATIC):
            if entry in STATIC_SKIP:
                continue
            src = os.path.join(STATIC, entry)
            dst = os.path.join(self.out, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*STATIC_SKIP))
            else:
                shutil.copy2(src, dst)
        self.write(".nojekyll", "")

        return len(self.pages)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--base-url", help="override site.json (for rehearsing the domain cutover)")
    ap.add_argument("--base-path", help="override site.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    with open(os.path.join(ROOT, "data", "site.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.base_path:
        cfg["base_path"] = args.base_path
    cfg["base_url"] = cfg["base_url"].rstrip("/") + "/"
    cfg["base_path"] = cfg["base_path"].rstrip("/") + "/"

    works_path = os.path.join(ROOT, "data", "works.json")
    if os.path.exists(works_path):
        with open(works_path, encoding="utf-8") as fh:
            db = json.load(fh)
    else:
        sys.stderr.write("warning: %s missing — building an empty site.\n"
                         "Run scripts/sync_etsy.py first.\n" % works_path)
        db = m.empty_db()

    # Captured galleries are applied here as well as in the sync, so that
    # committing data/images/<id>.json and pushing is enough to publish the
    # gallery — no sync run required.
    try:
        applied = m.apply_manual_images_to_db(db, m.load_manual_images(IMAGES_DIR))
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if applied and args.verbose:
        sys.stderr.write("applied captured galleries for: %s\n" % ", ".join(applied))

    # Construct before wiping the output: a refused GA id or collection slug
    # should not cost you the previous build.
    try:
        site = Site(cfg, db, args.out)
    except ValueError as exc:
        # A bad value is refused, not escaped — say so plainly instead of
        # ending the run in a traceback.
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)

    count = site.build()
    print("built %d pages into %s" % (count, os.path.relpath(args.out, ROOT)))
    # Warnings go on stdout too: nobody reads the stderr of a green CI run.
    for warning in site.warnings:
        print("warning: %s" % warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
