#!/usr/bin/env python3
"""Sync artworks from the Etsy shop RSS feed into data/works.json.

The feed is the only reachable source of shop data — Etsy's shop and listing
HTML pages sit behind DataDome (403) and the v3 API needs a key.

This is an UPSERT, never a mirror. A work that disappears from the feed has
sold; it is marked unavailable and keeps its stored data, page, and URL.

Exit codes:
    0  success (including "nothing changed")
    1  network / HTTP / parse failure
    2  write failure, or an unreadable existing works.json
    3  refused: feed was empty but the database is not (see --allow-empty)
"""

import argparse
import difflib
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import msuplib as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "works.json")
HEARTBEAT = os.path.join(ROOT, "data", ".heartbeat")
ARCHIVE_DIR = os.path.join(ROOT, "static", "works")
IMAGES_DIR = os.path.join(ROOT, "data", "images")


def load_db(path):
    """Read the existing database. Missing is fine; malformed is fatal.

    Never fall back to an empty database on a parse error — that would silently
    discard every curation field the artist has written by hand.
    """
    if not os.path.exists(path):
        return m.empty_db(), True
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), False
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "error: %s exists but could not be read: %s\n"
            "Refusing to overwrite it. Fix or delete the file, then re-run.\n" % (path, exc)
        )
        sys.exit(2)


def curation_snapshot(db):
    """Map id -> serialised curation block, for the --check guard."""
    return {
        w["id"]: json.dumps(w.get("curation"), sort_keys=True)
        for w in db.get("works", [])
    }


def fetch_feed(source, verbose=False):
    """Read the feed from a URL or a local file (used by tests and --source)."""
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in ("http", "https"):
        if verbose:
            sys.stderr.write("fetching %s\n" % source)
        raw = m.fetch(source)
    else:
        with open(source, "rb") as fh:
            raw = fh.read()

    # A DataDome interstitial is HTML and returns 200. Gate on the RSS root so
    # that becomes a loud failure rather than a silently emptied database.
    if b"<rss" not in raw[:2000]:
        raise m.FetchError(
            "response from %s does not look like RSS (bot challenge or shop rename?)" % source
        )
    return raw


def describe(summary, warnings):
    parts = []
    for key, label in (
        ("new", "added"),
        ("changed", "updated"),
        ("sold", "marked sold"),
        ("relisted", "relisted"),
    ):
        if summary[key]:
            parts.append("%d %s (%s)" % (len(summary[key]), label, ", ".join(summary[key])))
    lines = ["Etsy sync: " + ("; ".join(parts) if parts else "no changes")]
    if summary.get("archived"):
        lines.append("archived %d image(s): %s" % (len(summary["archived"]), ", ".join(summary["archived"])))
    lines += ["warning: " + w for w in warnings]
    return "\n".join(lines)


def bump_heartbeat(now):
    """Keep the repo active so the scheduled workflow is not auto-disabled.

    GitHub disables cron workflows after 60 days without repository activity.
    If nothing sells for two months the sync would never commit and the
    schedule would quietly die. This file holds only YYYY-MM, so it changes at
    most once a month — about twelve one-line commits a year.
    """
    stamp = now[:7]
    try:
        with open(HEARTBEAT, encoding="utf-8") as fh:
            if fh.read().strip() == stamp:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
    with open(HEARTBEAT, "w", encoding="utf-8") as fh:
        fh.write(stamp + "\n")
    return True


def archive_images(db, sold_ids, verbose=False):
    """Save a local copy of each newly-sold piece's image.

    Hotlinked Etsy images survive deactivation for a long time but not
    deletion, so this is the only real guarantee that sold work keeps its
    picture. It runs only on the available -> sold transition, so it touches a
    handful of files a year rather than the whole catalogue.

    The path is stored in `sync` (machine-owned), never in `curation`, so the
    ownership boundary that protects the artist's manual edits is preserved.
    """
    if not sold_ids:
        return []
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    saved = []
    for work in db.get("works", []):
        if work["id"] not in sold_ids or work.get("sync", {}).get("archived_image"):
            continue
        image = ((work.get("etsy") or {}).get("images") or [{}])[0]
        url = m.sized(image.get("base"), "1140xN", image.get("src"))
        if not url:
            continue
        rel = "works/%s.jpg" % work["id"]
        try:
            data = m.fetch(url, retries=2)
        except (m.FetchError, OSError) as exc:
            # A missing archive copy is a degraded outcome, not a failed sync.
            sys.stderr.write("warning: could not archive image for %s: %s\n" % (work["id"], exc))
            continue
        with open(os.path.join(ARCHIVE_DIR, "%s.jpg" % work["id"]), "wb") as fh:
            fh.write(data)
        work["sync"]["archived_image"] = rel
        saved.append(rel)
        if verbose:
            sys.stderr.write("archived %s (%d bytes)\n" % (rel, len(data)))
    return saved


def enrich_images(items, api_key, verbose=False):
    """Replace each listing's single RSS photo with its full Etsy gallery.

    Optional by design: without a key the site still builds, just with one
    image per piece. A per-listing failure degrades to the RSS photo rather
    than failing the sync — a missing gallery is not worth losing a run over.
    """
    enriched = 0
    for item in items:
        try:
            images = m.api_listing_images(item["listing_id"], api_key)
        except (m.FetchError, ValueError, OSError) as exc:
            sys.stderr.write("warning: could not fetch images for %s: %s\n" % (item["listing_id"], exc))
            continue
        if images:
            item["images"] = images
            item["image_source"] = "api"
            enriched += 1
            if verbose:
                sys.stderr.write("  %s: %d images\n" % (item["listing_id"], len(images)))
    return enriched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=m.DEFAULT_FEED, help="feed URL or local file")
    ap.add_argument("--out", default=DEFAULT_OUT, help="path to works.json")
    ap.add_argument("--images-dir", default=IMAGES_DIR,
                    help="directory of <listing_id>.json gallery files")
    ap.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="permit an empty feed to mark every work sold (see exit code 3)",
    )
    ap.add_argument("--check", action="store_true", help="verify curation blocks survived the write")
    ap.add_argument(
        "--archive-images",
        action="store_true",
        help="save a local copy of an image when its piece sells (default off)",
    )
    ap.add_argument("--no-heartbeat", action="store_true", help="skip the monthly heartbeat file")
    ap.add_argument(
        "--etsy-api-key",
        default=os.environ.get("ETSY_API_KEY"),
        help="Etsy API keystring, if you ever get one. Captured galleries in "
             "--images-dir take precedence. Defaults to $ETSY_API_KEY.",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        raw = fetch_feed(args.source, args.verbose)
        items, warnings = m.parse_feed(raw)
    except (m.FetchError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except Exception as exc:  # malformed XML
        sys.stderr.write("error: could not parse feed: %s\n" % exc)
        return 1

    db, bootstrapped = load_db(args.out)

    # Captured galleries win over both RSS and the API: they are the artist's
    # explicit choice of which photos to show, in which order.
    try:
        galleries = m.load_manual_images(args.images_dir)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if args.etsy_api_key and items:
        n = enrich_images(items, args.etsy_api_key, args.verbose)
        if args.verbose:
            sys.stderr.write("enriched %d/%d listings from the Etsy API\n" % (n, len(items)))

    applied = m.apply_manual_images(items, galleries)
    if args.verbose and applied:
        sys.stderr.write("used captured galleries for: %s\n" % ", ".join(applied))

    missing = [i["listing_id"] for i in items
               if i["image_source"] == "rss" and len(i["images"]) < 2]
    if missing:
        sys.stderr.write(
            "note: only one photo for %s.\n"
            "      Grab the rest with the bookmarklet (see README) and save them to\n"
            "      %s/<listing_id>.json\n"
            % (", ".join(missing), os.path.relpath(args.images_dir, ROOT))
        )

    before_text = "" if bootstrapped else m.dumps(db)
    before_curation = curation_snapshot(db)

    existing_etsy = [w for w in db.get("works", []) if w.get("source") == "etsy"]
    if not items and existing_etsy and not args.allow_empty:
        sys.stderr.write(
            "error: feed returned 0 listings but %d Etsy works are on record.\n"
            "Refusing to mark the whole shop sold. If the shop really is empty,\n"
            "re-run with --allow-empty.\n" % len(existing_etsy)
        )
        return 3

    new_db, summary = m.merge(db, items)
    if args.archive_images and not args.dry_run:
        # Must run before serialising so the stored path is part of this write.
        summary["archived"] = archive_images(new_db, set(summary["sold"]), args.verbose)
    after_text = m.dumps(new_db)
    report = describe(summary, warnings)

    if args.dry_run:
        diff = difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile="a/%s" % os.path.relpath(args.out, ROOT),
            tofile="b/%s" % os.path.relpath(args.out, ROOT),
        )
        sys.stdout.writelines(diff)
        print(report)
        print("(dry run — nothing written)")
        return 0

    if after_text != before_text:
        try:
            m.write_atomic(args.out, after_text)
        except OSError as exc:
            sys.stderr.write("error: could not write %s: %s\n" % (args.out, exc))
            return 2

    if not args.no_heartbeat:
        bump_heartbeat(m.today())

    if args.check:
        verify, _ = load_db(args.out)
        after_curation = curation_snapshot(verify)
        for wid, blob in before_curation.items():
            if after_curation.get(wid) != blob:
                sys.stderr.write("error: curation block for %s changed during sync\n" % wid)
                return 2
        print("check: all %d curation blocks intact" % len(before_curation))

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
