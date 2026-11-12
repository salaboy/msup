#!/usr/bin/env python3
"""Sync artworks from the Etsy shop RSS feed into data/works.json.

The feed is the only reachable source of shop data — Etsy's shop and listing
HTML pages sit behind DataDome (403) and the v3 API needs a key.

This is an UPSERT, never a mirror, and it is ADDITIVE by default: the RSS feed
returns a rolling window of ~10 listings rather than the shop inventory, so a
work missing from it has not necessarily sold. Absence is reported, never
acted on. Sales are recorded by hand with `curation.status_override = "sold"`.

Pass --complete-inventory only when the source really does list every active
listing. Even then the run is capped by --max-sold, so a truncated or partial
response fails loudly instead of emptying the shop.

Exit codes:
    0  success (including "nothing changed")
    1  network / HTTP / parse failure
    2  write failure, or an unreadable existing works.json
    3  refused: feed was empty but the database is not (see --allow-empty)
    4  refused: --complete-inventory would mark more works sold than --max-sold
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
        ("sold", "no longer on Etsy"),
        ("relisted", "relisted"),
    ):
        if summary[key]:
            parts.append("%d %s (%s)" % (len(summary[key]), label, ", ".join(summary[key])))
    lines = ["Etsy sync: " + ("; ".join(parts) if parts else "no changes")]
    if summary.get("relisted_as"):
        lines.append(
            "followed %d relisted piece(s) to a new listing id: %s"
            % (len(summary["relisted_as"]),
               ", ".join("%s -> %s" % pair for pair in summary["relisted_as"]))
        )
    if summary.get("absent"):
        lines.append(
            "%d work(s) not in this feed window, left untouched: %s"
            % (len(summary["absent"]), ", ".join(summary["absent"]))
        )
    if summary.get("archived"):
        lines.append("archived %d image(s): %s" % (len(summary["archived"]), ", ".join(summary["archived"])))
    lines += ["warning: " + w for w in warnings]
    return "\n".join(lines)



def describe_duplicates(dupes):
    """One artwork under several listing ids — reported, never auto-merged."""
    if not dupes:
        return ""
    lines = ["", "note: %d artwork(s) stored under more than one listing id." % len(dupes)]
    for key, works in dupes.items():
        ids = []
        for w in works:
            state = "available" if (w.get("sync") or {}).get("available") else "sold"
            price = ((w.get("etsy") or {}).get("price") or {}).get("raw") or "?"
            ids.append("%s (%s, %s)" % (w["id"], state, price))
        lines.append("      %s: %s" % (key, "; ".join(ids)))
    lines.append("      Pick one and set curation.hidden on the other — they may")
    lines.append("      disagree on price, so this is not merged automatically.")
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


def apply_relists(db, plan):
    """Follow a piece onto its new listing id, in memory only.

    Etsy mints a new id when a piece is relisted, which strands three things on
    the dead id: the artist's curation, the first_seen date the whole grid is
    ordered by, and the captured gallery in data/images/<id>.json — the one
    asset here a human had to produce by hand.

    The old record is kept and marked `superseded_by` rather than deleted, so
    its URL keeps resolving for anyone who shared it.

    Curation is moved only onto a record that has none of its own, so a
    hand-edited new listing is never overwritten by a stale one.

    Touches no files: the gallery rename is move_galleries(), which runs only
    once the run has survived the breaker and is actually being written.
    """
    moved, by_id = [], {w["id"]: w for w in db.get("works", [])}
    for old_id, new_id in plan:
        old, new = by_id.get(old_id), by_id.get(new_id)
        if not old or not new:
            continue

        old_cur = old.get("curation") or {}
        new_cur = new.get("curation") or {}
        pristine = all(new_cur.get(k) == v for k, v in m.CURATION_DEFAULTS.items())
        if pristine and any(old_cur.get(k) != v for k, v in m.CURATION_DEFAULTS.items()):
            new["curation"] = dict(old_cur)

        # The piece has been in the catalogue since the ORIGINAL listing; the
        # grid is ordered by first_seen, so losing this reshuffles the page.
        old_seen = (old.get("sync") or {}).get("first_seen")
        new_sync = new.setdefault("sync", {})
        if old_seen and old_seen < (new_sync.get("first_seen") or "9999"):
            new_sync["first_seen"] = old_seen

        old.setdefault("sync", {})["superseded_by"] = new_id
        moved.append((old_id, new_id))
    return moved


def move_galleries(relists, images_dir, verbose=False):
    """Rename data/images/<old_id>.json to the live listing id.

    Separate from apply_relists, and called only on the write path, so a
    --dry-run or a run the breaker refuses cannot leave the captured galleries
    renamed against a works.json that was never written.

    A failure here is reported, not fatal: the sync has already succeeded, and
    the cost is one piece falling back to its API photos until the file is
    renamed by hand.
    """
    moved = []
    for old_id, new_id in relists:
        source = os.path.join(images_dir, "%s.json" % old_id)
        target = os.path.join(images_dir, "%s.json" % new_id)
        if not os.path.exists(source) or os.path.exists(target):
            continue
        try:
            os.replace(source, target)
        except OSError as exc:
            sys.stderr.write(
                "warning: could not move gallery %s.json -> %s.json: %s\n"
                % (old_id, new_id, exc)
            )
            continue
        moved.append("%s.json -> %s.json" % (old_id, new_id))
        if verbose:
            sys.stderr.write("moved gallery %s.json -> %s.json\n" % (old_id, new_id))
    return moved


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
        help="proceed even though the feed returned nothing (see exit code 3)",
    )
    ap.add_argument(
        "--use-api",
        action="store_true",
        help="read the shop's full active inventory from the Etsy v3 API "
             "instead of the RSS feed. This is the only source that can prove "
             "a listing is gone, so it implies --complete-inventory. Needs "
             "--etsy-api-key.",
    )
    ap.add_argument(
        "--shop",
        default=m.DEFAULT_FEED.rstrip("/").split("/shop/")[-1].split("/")[0],
        help="Etsy shop name, for --use-api (default derived from the feed URL)",
    )
    ap.add_argument(
        "--complete-inventory",
        action="store_true",
        help="assert the source lists EVERY active listing, so absence means a "
             "sale. Off by default: the shop RSS feed is a ~10-item window and "
             "absence from it proves nothing. Still capped by --max-sold.",
    )
    ap.add_argument(
        "--max-sold",
        type=int,
        default=3,
        help="refuse a --complete-inventory run that would mark more than this "
             "many works sold at once (default 3). A real shop does not sell "
             "its catalogue overnight; a truncated response looks exactly like "
             "it does. Raise it deliberately for a genuine clear-out.",
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

    complete = args.complete_inventory or args.use_api

    if args.use_api:
        if not args.etsy_api_key:
            sys.stderr.write("error: --use-api needs --etsy-api-key or $ETSY_API_KEY\n")
            return 1
        try:
            items, warnings = m.api_inventory(args.shop, args.etsy_api_key)
        except (m.FetchError, OSError) as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        except (ValueError, KeyError, TypeError) as exc:
            # A shape change in the API is a hard stop, never a partial read:
            # half an inventory is indistinguishable from a sold-out shop.
            sys.stderr.write("error: could not read the Etsy API response: %s\n" % exc)
            return 1
        if args.verbose:
            sys.stderr.write("Etsy API: %d active listing(s)\n" % len(items))
    else:
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

    if args.etsy_api_key and items and not args.use_api:
        # RSS carries one photo per listing; --use-api already asked for the
        # whole gallery in the same request.
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
            "Nothing would be marked sold (absence is not a sale here), but an\n"
            "empty feed almost always means a bot challenge or a renamed shop,\n"
            "so this run is refused rather than recorded as a no-op.\n"
            "Re-run with --allow-empty if the shop really is empty.\n" % len(existing_etsy)
        )
        return 3


    new_db, summary = m.merge(db, items, complete=complete)

    # Must run before the breaker. A relisted piece leaves its old id behind,
    # which merge() has just counted as gone; pairing it to the live listing is
    # what tells those two events apart from a real sale.
    relists = []
    if complete:
        plan = m.plan_relists(new_db, {i["listing_id"] for i in items})
        if plan:
            relists = apply_relists(new_db, plan)
            superseded = {old for old, _ in relists}
            summary["sold"] = [w for w in summary["sold"] if w not in superseded]
            summary["relisted_as"] = relists

    # The breaker is the last line of defence behind --complete-inventory: if
    # the "complete" response was in fact truncated, this is what stops the
    # whole shop going sold out. Refuse the entire run rather than write a
    # partial one — a sync that does nothing is always recoverable.
    if summary["sold"] and len(summary["sold"]) > args.max_sold:
        sys.stderr.write(
            "error: this run would mark %d works sold, over the --max-sold limit "
            "of %d.\nRefusing: a source that drops this much at once is far more "
            "likely truncated than\nsold out. Inspect it with --dry-run, then raise "
            "--max-sold if the sales are real.\nWorks: %s\n"
            % (len(summary["sold"]), args.max_sold, ", ".join(summary["sold"]))
        )
        return 4


    if args.archive_images and not args.dry_run:
        # Must run before serialising so the stored path is part of this write.
        summary["archived"] = archive_images(new_db, set(summary["sold"]), args.verbose)
    after_text = m.dumps(new_db)
    report = describe(summary, warnings) + describe_duplicates(m.duplicate_listings(new_db))


    if args.dry_run:
        diff = difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile="a/%s" % os.path.relpath(args.out, ROOT),
            tofile="b/%s" % os.path.relpath(args.out, ROOT),
        )
        sys.stdout.writelines(diff)
        print(report)
        if relists:
            print("(dry run — would move %d gallery file(s))" % len(relists))
        print("(dry run — nothing written)")
        return 0

    if after_text != before_text:
        try:
            m.write_atomic(args.out, after_text)
        except OSError as exc:
            sys.stderr.write("error: could not write %s: %s\n" % (args.out, exc))
            return 2

    # Only now, with works.json safely on disk, do the galleries follow their
    # listing ids. The reverse order could rename a file for a write that then
    # failed, leaving the gallery pointing at an id nothing refers to.
    if relists:
        moved = move_galleries(relists, args.images_dir, args.verbose)
        if moved:
            report += "\n" + "moved %d gallery file(s): %s" % (len(moved), ", ".join(moved))


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
