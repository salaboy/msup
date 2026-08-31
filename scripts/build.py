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
import html
import json
import os
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

# Gallery selection is CSS-only (radio + :has()), and style.css carries one
# rule per index. Keep these in step.
MAX_GALLERY = 12


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


class Site:
    def __init__(self, cfg, db, out):
        self.cfg = cfg
        self.db = db
        self.out = out
        self.base_url = cfg["base_url"]
        self.base_path = cfg["base_path"]
        self.pages = []  # (absolute url, changefreq) for the sitemap

    # -- shared chrome ---------------------------------------------------

    def nav(self, rel, active):
        items = []
        for entry in self.cfg["nav"]:
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

        details_html = ""
        if work["details"]:
            rows = "\n".join(
                "        <div><dt>%s</dt><dd>%s</dd></div>" % (esc(k), esc(v))
                for k, v in work["details"]
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
        for key, value in work["details"]:
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

        home = template("home.html").safe_substitute(
            rel="",
            home_intro=esc(read(os.path.join(CONTENT, "home_intro.txt")).strip()),
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
            src = os.path.join(STATIC, entry)
            dst = os.path.join(self.out, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
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

    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)

    count = Site(cfg, db, args.out).build()
    print("built %d pages into %s" % (count, os.path.relpath(args.out, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
