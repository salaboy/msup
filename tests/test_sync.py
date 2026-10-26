"""Tests for the Etsy feed parser and the works.json merge rules.

The FIX fixture is the real feed as returned by Etsy on 2026-08-31.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import msuplib as m  # noqa: E402

FIX_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    <title>Etsy Shop for MSupShop</title>
    <link>https://www.etsy.com/shop/MSupShop?ref=rss</link>
    <item>
        <title>FIX by MSupShop</title>
        <description>&lt;p class="image"&gt;&lt;img src="https://i.etsystatic.com/59829812/r/il/a00f5d/8503570133/il_570xN.8503570133_1rfk.jpg" border="0" width="570" height="429" /&gt;&lt;/p&gt;&lt;p class="price"&gt;25.00 GBP&lt;/p&gt;&lt;p class="description"&gt;Title: FIX&lt;br /&gt;Description: The weekend is over and you need to start fixing your life.&lt;br /&gt;Type: hand drawn original&lt;br /&gt;Size: A3 (29.7 x 42.0 cm)&lt;br /&gt;Framed: Recycled Frame&lt;/p&gt;</description>
        <pubDate>Mon, 31 Aug 2026 04:35:46 -0400</pubDate>
        <link>https://www.etsy.com/uk/listing/4565863595/fix?ref=rss</link>
        <guid>https://www.etsy.com/uk/listing/4565863595/fix</guid>
    </item>
</channel></rss>
""".encode()

EMPTY_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Etsy Shop for MSupShop</title></channel></rss>
"""


def feed_with(description, title="THING by MSupShop", guid="https://www.etsy.com/uk/listing/999/thing"):
    import html as h
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Etsy Shop for MSupShop</title><item>"
        "<title>%s</title><description>%s</description><guid>%s</guid>"
        "</item></channel></rss>" % (title, h.escape(description), guid)
    ).encode()


class TestParse(unittest.TestCase):
    def test_fix_listing(self):
        items, warnings = m.parse_feed(FIX_FEED)
        self.assertEqual(warnings, [])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["listing_id"], "4565863595")
        self.assertEqual(it["slug"], "fix")
        self.assertEqual(it["title"], "FIX")
        self.assertEqual(it["price"], {"amount": "25.00", "currency": "GBP", "raw": "25.00 GBP"})
        self.assertEqual(it["fields"]["type"], "hand drawn original")
        self.assertEqual(it["fields"]["size"], "A3 (29.7 x 42.0 cm)")
        self.assertEqual(it["fields"]["framed"], "Recycled Frame")
        self.assertEqual(len(it["images"]), 1, "RSS carries exactly one photo")
        self.assertEqual(it["images"][0]["width"], 570)
        self.assertEqual(it["image_source"], "rss")
        self.assertEqual(it["pub_date"], "2026-08-31")

    def test_canonical_url_drops_locale_and_ref(self):
        """/uk/ would push non-UK buyers into a GBP-first experience."""
        it = m.parse_feed(FIX_FEED)[0][0]
        self.assertEqual(it["url"], "https://www.etsy.com/listing/4565863595/fix")

    def test_shop_suffix_derived_not_hardcoded(self):
        self.assertEqual(m.shop_name("Etsy Shop for OtherShop"), "OtherShop")
        self.assertEqual(m.strip_shop_suffix("FIX by OtherShop", "OtherShop"), "FIX")
        # A title that merely contains "by" must survive intact.
        self.assertEqual(m.strip_shop_suffix("Stand by Me", "MSupShop"), "Stand by Me")

    def test_image_base_and_srcset(self):
        base = m.image_base(
            "https://i.etsystatic.com/59829812/r/il/a00f5d/8503570133/il_570xN.8503570133_1rfk.jpg"
        )
        self.assertIn("il_{size}", base)
        self.assertIn("il_1140xN", m.srcset(base))
        self.assertIn("1140w", m.srcset(base))

    def test_image_base_unrecognised_returns_none(self):
        """An unknown URL shape must degrade to a bare src, not a guessed srcset."""
        self.assertIsNone(m.image_base("https://example.com/photo.jpg"))
        self.assertIsNone(m.srcset(None))

    def test_unknown_labels_preserved(self):
        items, _ = m.parse_feed(feed_with(
            '<p class="description">Title: T<br />Edition: 2 of 5<br />Paper: Cotton rag</p>'
        ))
        self.assertEqual(items[0]["fields"]["title"], "T")
        self.assertEqual(items[0]["extra_fields"], {"Edition": "2 of 5", "Paper": "Cotton rag"})

    def test_no_labels_falls_back_to_body(self):
        items, _ = m.parse_feed(feed_with(
            '<p class="description">Just some free text with no labels at all.</p>'
        ))
        self.assertEqual(items[0]["fields"], {})
        self.assertEqual(items[0]["body"], "Just some free text with no labels at all.")

    def test_continuation_lines_append(self):
        items, _ = m.parse_feed(feed_with(
            '<p class="description">Description: First para.<br />Second para.</p>'
        ))
        self.assertEqual(items[0]["fields"]["description"], "First para.\nSecond para.")

    def test_missing_price_and_image_survive(self):
        items, _ = m.parse_feed(feed_with('<p class="description">Title: T</p>'))
        self.assertEqual(items[0]["price"]["amount"], None)
        self.assertEqual(items[0]["images"], [])

    def test_item_without_listing_id_is_skipped_not_fatal(self):
        items, warnings = m.parse_feed(feed_with("<p>x</p>", guid="https://example.com/nope"))
        self.assertEqual(items, [])
        self.assertEqual(len(warnings), 1)

    def test_escaped_entities_in_prose_are_not_treated_as_tags(self):
        """A literal "<" in the artist's text must not be stripped as a tag.

        At HTML level the prose reads "Ampersands &amp; &lt;brackets&gt;".
        feed_with() escapes once more for XML, ElementTree unescapes that
        layer, and the parser must strip tags BEFORE unescaping the rest —
        otherwise "&lt;brackets&gt;" would become a real tag and be deleted.
        """
        items, _ = m.parse_feed(feed_with(
            '<p class="description">Description: Ampersands &amp; &lt;brackets&gt; survive.</p>'
        ))
        self.assertEqual(items[0]["fields"]["description"], "Ampersands & <brackets> survive.")


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.items, _ = m.parse_feed(FIX_FEED)

    def test_new_work_added(self):
        db, summary = m.merge(m.empty_db(), self.items, now="2026-08-31")
        self.assertEqual(summary["new"], ["4565863595"])
        self.assertTrue(db["works"][0]["sync"]["available"])
        self.assertEqual(db["works"][0]["curation"], m.CURATION_DEFAULTS)

    def test_resync_is_a_noop(self):
        db, _ = m.merge(m.empty_db(), self.items, now="2026-08-31")
        again, summary = m.merge(db, self.items, now="2026-09-05")
        self.assertEqual(summary["changed"], [])
        self.assertEqual(m.dumps(db), m.dumps(again))

    def test_delisted_work_marked_sold_not_deleted(self):
        db, _ = m.merge(m.empty_db(), self.items, now="2026-08-31")
        db, summary = m.merge(db, [], now="2026-09-10")
        self.assertEqual(summary["sold"], ["4565863595"])
        self.assertEqual(len(db["works"]), 1, "the work must survive delisting")
        self.assertFalse(db["works"][0]["sync"]["available"])
        self.assertEqual(db["works"][0]["sync"]["delisted_at"], "2026-09-10")
        # Frozen last-known-good data is what keeps the page renderable.
        self.assertEqual(db["works"][0]["etsy"]["title"], "FIX")
        self.assertTrue(db["works"][0]["etsy"]["images"][0]["src"])

    def test_relist_clears_sold_state(self):
        db, _ = m.merge(m.empty_db(), self.items, now="2026-08-31")
        db, _ = m.merge(db, [], now="2026-09-10")
        db, summary = m.merge(db, self.items, now="2026-09-20")
        self.assertEqual(summary["relisted"], ["4565863595"])
        self.assertTrue(db["works"][0]["sync"]["available"])
        self.assertIsNone(db["works"][0]["sync"]["delisted_at"])

    def test_curation_survives_every_transition(self):
        """The artist's manual edits must never be clobbered by a sync."""
        db, _ = m.merge(m.empty_db(), self.items, now="2026-08-31")
        db["works"][0]["curation"].update(
            {"featured": True, "order": 3, "title_override": "Fix (2026)",
             "alt": "hand written alt", "notes": "framed in oak"}
        )
        snapshot = json.dumps(db["works"][0]["curation"], sort_keys=True)

        db, _ = m.merge(db, self.items, now="2026-09-01")   # update
        db, _ = m.merge(db, [], now="2026-09-10")           # sold
        db, _ = m.merge(db, self.items, now="2026-09-20")   # relisted

        self.assertEqual(json.dumps(db["works"][0]["curation"], sort_keys=True), snapshot)
        self.assertEqual(db["works"][0]["sync"]["first_seen"], "2026-08-31")

    def test_manual_works_are_never_touched(self):
        db = m.empty_db()
        db["works"].append({
            "id": "manual-1", "source": "manual", "etsy": None,
            "sync": {"first_seen": "2020-01-01", "delisted_at": None,
                     "relisted_at": None, "available": False},
            "curation": dict(m.CURATION_DEFAULTS, title_override="Old Piece"),
        })
        before = json.dumps(db["works"][0], sort_keys=True)
        db, _ = m.merge(db, [], now="2026-09-10")
        after = [w for w in db["works"] if w["id"] == "manual-1"][0]
        self.assertEqual(json.dumps(after, sort_keys=True), before)

    def test_output_is_byte_stable_regardless_of_feed_order(self):
        """git diff --quiet is only a valid change detector if this holds."""
        two = self.items + [dict(self.items[0], listing_id="1111111111", slug="other")]
        a, _ = m.merge(m.empty_db(), two, now="2026-08-31")
        b, _ = m.merge(m.empty_db(), list(reversed(two)), now="2026-08-31")
        self.assertEqual(m.dumps(a), m.dumps(b))


class TestView(unittest.TestCase):
    def build_db(self):
        db, _ = m.merge(m.empty_db(), m.parse_feed(FIX_FEED)[0], now="2026-08-31")
        return db

    def test_view_flattens_curation_over_etsy(self):
        db = self.build_db()
        db["works"][0]["curation"]["title_override"] = "Fix (2026)"
        v = m.view(db["works"][0])
        self.assertEqual(v["title"], "Fix (2026)")
        self.assertEqual(v["slug"], "4565863595-fix")
        self.assertTrue(v["available"])

    def test_generated_alt_text_is_descriptive(self):
        v = m.view(self.build_db()["works"][0])
        self.assertEqual(v["alt"], "FIX — hand drawn original, A3 (29.7 x 42.0 cm)")
        self.assertEqual(v["cover"]["alt"], v["alt"])

    def test_status_override_beats_feed(self):
        db = self.build_db()
        db["works"][0]["curation"]["status_override"] = "sold"
        self.assertFalse(m.view(db["works"][0])["available"])

    def test_hidden_works_excluded(self):
        db = self.build_db()
        db["works"][0]["curation"]["hidden"] = True
        self.assertEqual(m.visible_works(db), [])

    def test_ordering_ignores_pub_date(self):
        """Etsy pubDate is the renewal date; sorting by it would reshuffle the
        grid every time a listing is renewed."""
        db = self.build_db()
        second = json.loads(json.dumps(db["works"][0]))
        second["id"] = "1111111111"
        second["etsy"]["pub_date"] = "2099-01-01"   # far newer renewal
        second["sync"]["first_seen"] = "2026-09-09"  # but seen later
        db["works"].append(second)
        order = [w["id"] for w in m.visible_works(db)]
        self.assertEqual(order, ["4565863595", "1111111111"])

    def test_curation_order_wins(self):
        db = self.build_db()
        second = json.loads(json.dumps(db["works"][0]))
        second["id"] = "1111111111"
        second["sync"]["first_seen"] = "2026-12-01"
        second["curation"]["order"] = 1
        db["works"].append(second)
        self.assertEqual([w["id"] for w in m.visible_works(db)][0], "1111111111")


    def test_archived_image_wins_over_etsy_but_loses_to_override(self):
        """Precedence: curation.image_override > sync.archived_image > Etsy."""
        db = self.build_db()
        work = db["works"][0]
        self.assertFalse(m.view(work)["cover"]["is_local"])

        work["sync"]["archived_image"] = "works/4565863595.jpg"
        v = m.view(work)
        self.assertTrue(v["cover"]["is_local"])
        self.assertEqual(v["cover"]["src"], "works/4565863595.jpg")
        self.assertEqual(len(v["images"]), 1, "a local copy replaces the gallery")
        self.assertIsNone(v["cover"]["srcset"], "a local file has no Etsy size variants")

        work["curation"]["image_override"] = "works/better-shot.jpg"
        self.assertEqual(m.view(work)["cover"]["src"], "works/better-shot.jpg")


class TestEtsyApiImages(unittest.TestCase):
    """The API is what supplies the photos RSS cannot: the per-image URL
    suffix is required and unguessable, so galleries are impossible without it.
    """

    PAYLOAD = json.dumps({
        "count": 3,
        "results": [
            {"listing_image_id": 2, "rank": 2, "full_width": 2000, "full_height": 1500,
             "url_570xN": "https://i.etsystatic.com/1/r/il/aa/222/il_570xN.222_bbbb.jpg",
             "url_fullxfull": "https://i.etsystatic.com/1/r/il/aa/222/il_fullxfull.222_bbbb.jpg",
             "alt_text": "detail of the ink work"},
            {"listing_image_id": 1, "rank": 1, "full_width": 1000, "full_height": 800,
             "url_570xN": "https://i.etsystatic.com/1/r/il/aa/111/il_570xN.111_aaaa.jpg",
             "url_fullxfull": "https://i.etsystatic.com/1/r/il/aa/111/il_fullxfull.111_aaaa.jpg",
             "alt_text": None},
            {"listing_image_id": 3, "rank": 3, "full_width": 900, "full_height": 900,
             "url_fullxfull": "https://i.etsystatic.com/1/r/il/aa/333/il_fullxfull.333_cccc.jpg"},
        ],
    }).encode()

    def fake_fetch(self, captured):
        def _fetch(url, headers=None, **kw):
            captured["url"] = url
            captured["headers"] = headers or {}
            return self.PAYLOAD
        return _fetch

    def test_images_sorted_by_rank_and_key_sent(self):
        captured = {}
        images = m.api_listing_images("4565863595", "KEY123", fetcher=self.fake_fetch(captured))
        self.assertEqual(captured["headers"]["x-api-key"], "KEY123")
        self.assertIn("/listings/4565863595/images", captured["url"])
        # Etsy's own display order, not the order the payload happened to arrive in.
        self.assertEqual([i["width"] for i in images], [1000, 2000, 900])

    def test_srcset_base_derived_and_alt_carried(self):
        images = m.api_listing_images("1", "K", fetcher=self.fake_fetch({}))
        self.assertIn("il_{size}", images[0]["base"])
        self.assertEqual(images[1]["alt"], "detail of the ink work")
        self.assertIsNone(images[0]["alt"])

    def test_falls_back_to_fullxfull_when_no_570(self):
        images = m.api_listing_images("1", "K", fetcher=self.fake_fetch({}))
        self.assertTrue(images[2]["src"].endswith("il_fullxfull.333_cccc.jpg"))

    def test_view_renders_every_api_image(self):
        db, _ = m.merge(m.empty_db(), m.parse_feed(FIX_FEED)[0], now="2026-08-31")
        db["works"][0]["etsy"]["images"] = m.api_listing_images("1", "K", fetcher=self.fake_fetch({}))
        v = m.view(db["works"][0])
        self.assertEqual(len(v["images"]), 3)
        self.assertEqual(v["cover"], v["images"][0])
        # Only the first image carries the descriptive alt; the rest are numbered.
        self.assertIn("view 3", v["images"][2]["alt"])


class TestManualImages(unittest.TestCase):
    """Galleries captured in the browser (Etsy blocks servers from listing pages)."""

    URL = "https://i.etsystatic.com/59829812/r/il/a00f5d/8503570133/il_fullxfull.8503570133_1rfk.jpg"

    def write_gallery(self, directory, listing_id, payload):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "%s.json" % listing_id), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_fullxfull_urls_yield_a_srcset(self):
        """Regression: an earlier size-token regex matched 570xN and 180x180 but
        not fullxfull — which is the only shape the bookmarklet emits."""
        base = m.image_base(self.URL)
        self.assertIsNotNone(base, "fullxfull must be recognised")
        self.assertIn("il_{size}", base)
        self.assertIn("il_1140xN", m.srcset(base))

    def test_all_etsy_size_tokens_recognised(self):
        for token in ("il_570xN", "il_180x180", "il_fullxfull", "il_75x75"):
            url = self.URL.replace("il_fullxfull", token)
            self.assertIsNotNone(m.image_base(url), token)

    def test_accepts_both_string_and_object_entries(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_gallery(d, "4565863595", [
                self.URL,
                {"url": self.URL.replace("8503570133", "8503570200"), "w": 3000, "h": 2250},
            ])
            galleries = m.load_manual_images(d)
            images = galleries["4565863595"]
            self.assertEqual(len(images), 2)
            self.assertIsNone(images[0]["width"], "a bare URL carries no dimensions")
            self.assertEqual((images[1]["width"], images[1]["height"]), (3000, 2250))

    def test_captured_gallery_replaces_the_rss_photo(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_gallery(d, "4565863595", [self.URL, self.URL.replace("8503570133", "999")])
            items, _ = m.parse_feed(FIX_FEED)
            self.assertEqual(len(items[0]["images"]), 1)
            applied = m.apply_manual_images(items, m.load_manual_images(d))
            self.assertEqual(applied, ["4565863595"])
            self.assertEqual(len(items[0]["images"]), 2)
            self.assertEqual(items[0]["image_source"], "file")

    def test_listing_without_a_file_keeps_its_rss_photo(self):
        with tempfile.TemporaryDirectory() as d:
            items, _ = m.parse_feed(FIX_FEED)
            self.assertEqual(m.apply_manual_images(items, m.load_manual_images(d)), [])
            self.assertEqual(items[0]["image_source"], "rss")
            self.assertEqual(len(items[0]["images"]), 1)

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(m.load_manual_images("/nonexistent/path/images"), {})

    def test_malformed_file_raises_rather_than_silently_dropping(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "123.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(ValueError):
                m.load_manual_images(d)

    def test_non_list_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_gallery(d, "123", {"url": self.URL})
            with self.assertRaises(ValueError):
                m.load_manual_images(d)

    def test_readme_and_junk_files_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_gallery(d, "4565863595", [self.URL])
            with open(os.path.join(d, "README.txt"), "w", encoding="utf-8") as fh:
                fh.write("not a gallery")
            self.assertEqual(list(m.load_manual_images(d)), ["4565863595"])

    def test_first_entry_becomes_the_cover(self):
        with tempfile.TemporaryDirectory() as d:
            second = self.URL.replace("8503570133", "8503570200")
            self.write_gallery(d, "4565863595", [second, self.URL])
            items, _ = m.parse_feed(FIX_FEED)
            m.apply_manual_images(items, m.load_manual_images(d))
            db, _ = m.merge(m.empty_db(), items, now="2026-08-31")
            v = m.view(db["works"][0])
            self.assertEqual(len(v["images"]), 2)
            self.assertEqual(v["cover"]["full"], second, "first entry is the cover")
            # Pasting full-resolution URLs must not put 400KB images in the grid:
            # the display src is downsized, and only "view full size" uses the original.
            self.assertIn("il_570xN", v["cover"]["src"])
            self.assertIn("il_1140xN", v["cover"]["srcset"])


    def test_gallery_applies_to_a_stored_db_without_a_sync(self):
        """Regression: adding data/images/<id>.json and pushing must publish the
        gallery on the next build. Previously only the sync applied these, so a
        plain deploy shipped a stale one-photo page and the file looked ignored.
        """
        with tempfile.TemporaryDirectory() as d:
            self.write_gallery(d, "4565863595", [self.URL, self.URL.replace("8503570133", "999")])
            items, _ = m.parse_feed(FIX_FEED)
            db, _ = m.merge(m.empty_db(), items, now="2026-08-31")
            self.assertEqual(len(db["works"][0]["etsy"]["images"]), 1)

            applied = m.apply_manual_images_to_db(db, m.load_manual_images(d))
            self.assertEqual(applied, ["4565863595"])
            self.assertEqual(len(m.view(db["works"][0])["images"]), 2)

    def test_applying_to_db_leaves_manual_works_alone(self):
        db = m.empty_db()
        db["works"].append({
            "id": "4565863595", "source": "manual", "etsy": None,
            "sync": {"available": True}, "curation": dict(m.CURATION_DEFAULTS),
        })
        self.assertEqual(m.apply_manual_images_to_db(db, {"4565863595": [{"src": self.URL}]}), [])


class TestAnalytics(unittest.TestCase):
    """The measurement ID is interpolated into JavaScript, so it is validated
    rather than escaped — a non-ID value is refused outright."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build
        self.snippet = build.analytics_snippet

    def test_no_id_emits_nothing(self):
        self.assertEqual(self.snippet(""), "")
        self.assertEqual(self.snippet(None), "")

    def test_valid_id_emits_gtag_once(self):
        out = self.snippet("G-ABC1234XYZ")
        self.assertIn("googletagmanager.com/gtag/js?id=G-ABC1234XYZ", out)
        self.assertIn("gtag('config', 'G-ABC1234XYZ')", out)

    def test_injection_attempts_are_refused(self):
        for bad in ("'); alert(1); //", "<script>x</script>", "UA-12345-1",
                    "G-OK'+document.cookie+'"):
            with self.assertRaises(ValueError, msg=bad):
                self.snippet(bad)


class TestSyncScript(unittest.TestCase):
    """End-to-end via the CLI, using a local fixture instead of the network."""

    def run_sync(self, feed_bytes, out, *extra):
        with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as fh:
            fh.write(feed_bytes)
            feed = fh.name
        try:
            return subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "sync_etsy.py"),
                 "--source", feed, "--out", out, "--no-heartbeat",
                 # isolate from the repo's real captured galleries
                 "--images-dir", os.path.join(os.path.dirname(out), "images"), *extra],
                capture_output=True, text=True,
            )
        finally:
            os.unlink(feed)

    def test_empty_feed_is_refused_with_exit_3(self):
        """A bot challenge or shop rename must not silently sell out the shop."""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            self.assertEqual(self.run_sync(FIX_FEED, out).returncode, 0)
            result = self.run_sync(EMPTY_FEED, out)
            self.assertEqual(result.returncode, 3)
            self.assertIn("Refusing", result.stderr)
            # and the file is untouched
            with open(out, encoding="utf-8") as fh:
                self.assertTrue(json.load(fh)["works"][0]["sync"]["available"])

    def test_allow_empty_marks_sold(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            self.run_sync(FIX_FEED, out)
            self.assertEqual(self.run_sync(EMPTY_FEED, out, "--allow-empty").returncode, 0)
            with open(out, encoding="utf-8") as fh:
                self.assertFalse(json.load(fh)["works"][0]["sync"]["available"])

    def test_non_rss_response_is_fatal(self):
        """A DataDome interstitial returns HTTP 200 with HTML."""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            result = self.run_sync(b"<html><body>Please enable JS</body></html>", out)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(os.path.exists(out), "must not write on a failed fetch")

    def test_malformed_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("{ this is not json")
            result = self.run_sync(FIX_FEED, out)
            self.assertEqual(result.returncode, 2)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "{ this is not json")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            result = self.run_sync(FIX_FEED, out, "--dry-run")
            self.assertEqual(result.returncode, 0)
            self.assertIn("dry run", result.stdout)
            self.assertFalse(os.path.exists(out))

    def test_written_file_is_world_readable(self):
        """mkstemp defaults to 0600; the committed file must not be private."""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "works.json")
            self.run_sync(FIX_FEED, out)
            self.assertTrue(os.stat(out).st_mode & 0o044)


class TestCollections(unittest.TestCase):
    """A collection is declared from the phone, by typing `Collection: <Name>`
    into the Etsy listing. These tests pin the data half of that contract."""

    def work_with(self, label):
        feed = feed_with(
            '<p class="description">Title: T<br />Size: A3<br />Collection: %s</p>' % label
        )
        db, _ = m.merge(m.empty_db(), m.parse_feed(feed)[0], now="2026-08-31")
        return db

    def test_label_reaches_extra_fields_with_no_parser_change(self):
        """The whole design rests on this: `Collection` is NOT a known label,
        so it lands in extra_fields untouched and works.json never churns."""
        self.assertNotIn("collection", m.KNOWN_LABELS)
        work = self.work_with("Sabiduría Universal")["works"][0]
        self.assertEqual(work["etsy"]["extra_fields"], {"Collection": "Sabiduría Universal"})

    def test_slugify_flattens_accents_case_and_punctuation(self):
        """One key per collection however it is typed on a phone keyboard."""
        for typed in ("Sabiduría Universal", "sabiduria universal",
                      "SABIDURÍA  UNIVERSAL", "  Sabiduría — Universal!  "):
            self.assertEqual(m.slugify(typed), "sabiduria-universal", typed)

    def test_slugify_returns_none_when_nothing_survives(self):
        """An empty slug would make a `collections//` path constructible."""
        self.assertIsNone(m.slugify(""))
        self.assertIsNone(m.slugify(None))
        self.assertIsNone(m.slugify("・・・"))

    def test_label_match_is_case_insensitive(self):
        """parse_description keeps the artist's capitalisation, so a lookup by
        the exact key "Collection" would miss a listing typed in lower case."""
        for key in ("Collection", "collection", "COLLECTION"):
            self.assertEqual(m.collection_label({key: "Winter Series"}), "Winter Series")
        self.assertIsNone(m.collection_label({"Edition": "2 of 5"}))
        self.assertIsNone(m.collection_label(None))

    def test_label_split_over_two_lines_is_joined(self):
        """A continuation line appends "\\n" + line, which must not produce a
        slug with an embedded dash where the line broke."""
        work = self.work_with("Sabiduría<br />Universal")["works"][0]
        self.assertEqual(m.view(work)["collection"], "sabiduria-universal")

    def test_view_exposes_the_slug_but_keeps_the_detail_row(self):
        """view() cannot tell a real collection from a typo — only build.py
        knows what is configured — so it never drops the artist's text."""
        v = m.view(self.work_with("Winter Series")["works"][0])
        self.assertEqual(v["collection"], "winter-series")
        self.assertEqual(v["collection_label"], "Winter Series")
        self.assertIn(("Collection", "Winter Series"), v["details"])

    def test_work_without_a_label_has_no_collection(self):
        v = m.view(m.merge(m.empty_db(), m.parse_feed(FIX_FEED)[0])[0]["works"][0])
        self.assertIsNone(v["collection"])
        self.assertIsNone(v["collection_label"])


class TestBuildCollections(unittest.TestCase):
    """The rendering half. Site is constructed directly rather than through
    the CLI so the output stays a pure function of the cfg dict."""

    BANNER = "img/collections/winter.jpg"

    def setUp(self):
        import build
        self.build = build

    def cfg(self, collections, base_path="/", base_url="https://example.com/"):
        return {
            "title": "Maryland Supreme", "tagline": "Hand drawn originals",
            "description": "Test site.", "author": "Mauricio Salatino",
            "base_url": base_url, "base_path": base_path,
            "etsy_shop_url": "https://www.etsy.com/shop/MSupShop",
            "instagram_url": "https://instagram.com/marylandsupreme",
            "google_analytics": "",
            "nav": [{"label": "Work", "path": "works/"},
                    {"label": "About", "path": "about/"}],
            "collections": collections,
        }

    def collection(self, **over):
        entry = {
            "slug": "winter", "title": "Winter Series", "in_nav": True,
            "on_home": True, "intro": "Drawings made over one winter.",
            "banner": self.BANNER, "banner_alt": "A snow-covered page.",
            "banner_width": 1600, "banner_height": 1067,
        }
        entry.update(over)
        return entry

    def db(self, label="Winter Series"):
        feed = feed_with(
            '<p class="description">Title: T<br />Size: A3<br />Collection: %s</p>' % label
        )
        db, _ = m.merge(m.empty_db(), m.parse_feed(feed)[0], now="2026-08-31")
        return db

    def render(self, collections, db=None, base_path="/", base_url="https://example.com/"):
        """Build a whole site into a temp dir and return {path: html}."""
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        site = self.build.Site(self.cfg(collections, base_path, base_url), db or self.db(), out)
        site.build()
        pages = {}
        for root, _dirs, names in os.walk(out):
            for name in names:
                if name.endswith((".html", ".xml")):
                    full = os.path.join(root, name)
                    with open(full, encoding="utf-8") as fh:
                        pages[os.path.relpath(full, out)] = fh.read()
        return site, pages

    def test_collection_page_lists_its_members(self):
        _, pages = self.render([self.collection()])
        page = pages["collections/winter/index.html"]
        self.assertIn("Winter Series", page)
        self.assertIn("Drawings made over one winter.", page)
        self.assertIn('href="../../works/999-thing/"', page)

    def test_banner_src_is_depth_relative_on_every_page(self):
        """The one bug this feature invites: the home teaser renders at '' and
        the collection page two levels down, so a shared pre-rendered banner
        would ship a broken image on one of them."""
        _, pages = self.render([self.collection()])
        self.assertIn('src="%s"' % self.BANNER, pages["index.html"])
        self.assertIn('src="../../%s"' % self.BANNER, pages["collections/winter/index.html"])

    def test_no_internal_link_is_host_absolute(self):
        """A relative-linked site is correct at both / and /msup/ with no
        rebuild, which is what makes the domain cutover safe."""
        for base_path, base_url in (("/", "https://example.com/"),
                                    ("/msup/", "https://example.com/msup/")):
            _, pages = self.render([self.collection()], base_path=base_path, base_url=base_url)
            for path, html_text in pages.items():
                if path in ("404.html", "sitemap.xml", "robots.txt"):
                    continue  # 404 is base_path-absolute by design
                self.assertNotIn('href="/', html_text, path)
                self.assertNotIn('src="/', html_text, path)

    def test_no_template_placeholder_survives(self):
        """safe_substitute leaves an unknown $name in place silently, so a
        typo'd slot would ship to production unnoticed."""
        _, pages = self.render([self.collection()])
        for path, html_text in pages.items():
            self.assertNotRegex(html_text, r"\$[a-z_]+", path)

    def test_nav_entry_sits_before_about_and_marks_the_current_page(self):
        _, pages = self.render([self.collection()])
        page = pages["collections/winter/index.html"]
        self.assertIn('href="../../collections/winter/" aria-current="page"', page)
        self.assertLess(page.index(">Winter Series<"), page.index(">About<"))
        self.assertNotIn("aria-current", pages["works/index.html"].split("</nav>")[0]
                         .replace('href="../works/" aria-current="page"', ""))

    def test_member_work_page_links_back_and_drops_the_spec_row(self):
        """A resolved collection reads better as a link than as a table row —
        and the link uses the configured title, not the Etsy text."""
        _, pages = self.render([self.collection()])
        page = pages["works/999-thing/index.html"]
        self.assertIn('Part of <a href="../../collections/winter/">Winter Series</a>', page)
        self.assertNotIn("<dt>Collection</dt>", page)

    def test_unmatched_label_degrades_to_a_plain_detail_row(self):
        """A typo must cost the artist nothing: no page, nothing hidden, and a
        warning loud enough to notice."""
        site, pages = self.render([], db=self.db("Wintr Seriez"))
        self.assertNotIn("collections/wintr-seriez/index.html", pages)
        self.assertIn("<dt>Collection</dt>", pages["works/999-thing/index.html"])
        self.assertTrue(any("matches no entry" in w for w in site.warnings))

    def test_etsy_text_cannot_break_out_of_an_attribute(self):
        """The label is third-party-authored, so it reaches the page only
        through the one escaping entry point."""
        site, pages = self.render([], db=self.db('Winter" onload=alert(1)'))
        page = pages["works/999-thing/index.html"]
        self.assertNotIn('Winter" onload', page, "the quote must not survive raw")
        self.assertIn("Winter&quot; onload=alert(1)", page)
        self.assertFalse(any(p.startswith("collections/") for p in pages))

    def test_label_matches_the_title_not_just_the_url_slug(self):
        """On Etsy the artist types the title; the slug is a URL choice the
        listing gives no hint of. Both must resolve to the same collection."""
        _, by_title = self.render([self.collection(slug="winter")])
        self.assertIn("Part of", by_title["works/999-thing/index.html"])
        _, by_slug = self.render([self.collection(slug="winter", title="Winter")],
                                 db=self.db("Winter"))
        self.assertIn("Part of", by_slug["works/999-thing/index.html"])

    def test_membership_inherits_the_canonical_order(self):
        """A piece must sit in the same position on its collection page as it
        does on /works/, so membership filters rather than re-sorts."""
        db = self.db("Winter Series")
        second = json.loads(json.dumps(db["works"][0]))
        second["id"] = "1111111111"
        second["sync"]["first_seen"] = "2026-12-01"
        second["curation"]["order"] = 1       # pushed to the front by curation
        third = json.loads(json.dumps(db["works"][0]))
        third["id"] = "2222222222"
        third["etsy"]["extra_fields"] = {}    # not a member
        db["works"] += [second, third]

        colls = self.build.collections(self.cfg([self.collection()]))
        members = self.build.collection_members(
            m.visible_works(db), self.build.collection_keys(colls)
        )
        self.assertEqual([w["id"] for w in members["winter"]], ["1111111111", "999"])

    def test_collection_page_is_in_the_sitemap_once(self):
        _, pages = self.render([self.collection()])
        self.assertEqual(pages["sitemap.xml"].count("https://example.com/collections/winter/"), 1)

    def test_empty_collection_still_renders_its_banner(self):
        _, pages = self.render([self.collection()], db=self.db("Something Else"))
        page = pages["collections/winter/index.html"]
        self.assertIn("empty-note", page)
        self.assertIn(self.BANNER, page)

    def test_missing_banner_file_is_reported(self):
        """The static copy loop validates nothing, and the onerror placeholder
        is styled for a work card, not a banner."""
        site, _ = self.render([self.collection()])
        self.assertTrue(any("banner missing" in w for w in site.warnings))

    def test_malformed_slug_and_banner_are_refused_not_escaped(self):
        """These land in filesystem paths, where escaping is the wrong tool."""
        for bad in ({"slug": "Winter Series"}, {"slug": "../../tools"}, {"slug": ""}):
            with self.assertRaises(ValueError):
                self.build.collections(self.cfg([self.collection(**bad)]))
        with self.assertRaises(ValueError):
            self.build.collections(self.cfg([self.collection(banner="/img/w.jpg")]))
        with self.assertRaises(ValueError):
            self.build.collections(self.cfg([self.collection(), self.collection()]))

    def test_site_without_collections_is_unchanged(self):
        """An older site.json with no `collections` key must still build."""
        cfg = self.cfg([])
        del cfg["collections"]
        self.assertEqual(self.build.collections(cfg), [])
        self.assertEqual([e["path"] for e in self.build.nav_entries(cfg, [], [])],
                         ["works/", "about/"])


class TestBuildExhibitions(unittest.TestCase):
    """Exhibitions: validation, the upcoming/past split, and photo discovery.

    `today` is injected rather than read off the clock, so an assertion about
    what is upcoming is still true in 2030. STATIC is redirected to a throwaway
    tree, so a test can put show photos on disk without leaving them in the
    repo.
    """

    SLUG = "winter-show-2026"
    POSTER = "img/exhibitions/winter-show-2026.jpg"

    def setUp(self):
        import build
        self.build = build

    def cfg(self, exhibs, collections=None):
        return {
            "title": "Maryland Supreme", "tagline": "Hand drawn originals",
            "description": "Test site.", "author": "Mauricio Salatino",
            "base_url": "https://example.com/", "base_path": "/",
            "etsy_shop_url": "https://www.etsy.com/shop/MSupShop",
            "instagram_url": "https://instagram.com/marylandsupreme",
            "google_analytics": "",
            "nav": [{"label": "Work", "path": "works/"},
                    {"label": "About", "path": "about/"}],
            "collections": collections or [],
            "exhibitions": exhibs,
        }

    def exhibition(self, **over):
        entry = {
            "slug": self.SLUG, "title": "Winter Show",
            "date": "2026-11-10", "time": "5:30 to 9:00pm",
            "place": "The Residency", "address": "49 Belgrave Square, London",
            "in_nav": True, "on_home": True,
            "intro": "One evening of drawings.",
            "poster": self.POSTER, "poster_alt": "A poster.",
            "poster_width": 1280, "poster_height": 1600,
        }
        entry.update(over)
        return entry

    def static(self, photos=(), poster=True):
        """Point build.STATIC at a temp tree holding the poster and, if asked,
        a folder of show photos."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "img", "exhibitions"))
        if poster:
            open(os.path.join(root, self.POSTER), "wb").close()
        if photos:
            folder = os.path.join(root, "img", "exhibitions", self.SLUG)
            os.makedirs(folder)
            for name in photos:
                open(os.path.join(folder, name), "wb").close()
        real = self.build.STATIC
        self.build.STATIC = root
        self.addCleanup(setattr, self.build, "STATIC", real)
        return root

    def render(self, exhibs, today="2026-01-01", collections=None):
        """Build a whole site into a temp dir and return {path: html}."""
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        site = self.build.Site(
            self.cfg(exhibs, collections), m.empty_db(), out,
            today=datetime.date.fromisoformat(today),
        )
        site.build()
        pages = {}
        for root, _dirs, names in os.walk(out):
            for name in names:
                if name.endswith((".html", ".xml")):
                    full = os.path.join(root, name)
                    with open(full, encoding="utf-8") as fh:
                        pages[os.path.relpath(full, out)] = fh.read()
        return site, pages

    # -- the page itself -------------------------------------------------

    def test_detail_page_carries_the_title_date_time_and_place(self):
        self.static()
        _, pages = self.render([self.exhibition()])
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("Winter Show", page)
        self.assertIn("10 November 2026", page)
        self.assertIn("5:30 to 9:00pm", page)
        self.assertIn("The Residency", page)
        self.assertIn('datetime="2026-11-10"', page)

    def test_date_is_written_out_not_numeric(self):
        """'10/11' is read two different ways either side of the Atlantic, so
        the one line a visitor acts on is never rendered that way."""
        self.static()
        _, pages = self.render([self.exhibition()])
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("10 November 2026", page)
        self.assertNotIn("10/11", page)

    def test_date_ranges_read_naturally(self):
        d = datetime.date
        fmt = self.build.format_date_range
        self.assertEqual(fmt(d(2026, 11, 10)), "10 November 2026")
        self.assertEqual(fmt(d(2026, 11, 10), d(2026, 11, 10)), "10 November 2026")
        self.assertEqual(fmt(d(2026, 11, 10), d(2026, 11, 14)), "10–14 November 2026")
        self.assertEqual(fmt(d(2026, 11, 28), d(2026, 12, 3)),
                         "28 November – 3 December 2026")
        self.assertEqual(fmt(d(2026, 12, 30), d(2027, 1, 2)),
                         "30 December 2026 – 2 January 2027")

    def test_no_template_placeholder_survives(self):
        self.static(photos=["01.jpg"])
        _, pages = self.render([self.exhibition()])
        for path, html in pages.items():
            self.assertNotRegex(html, r"\$[a-z_]+", "placeholder left in %s" % path)

    def test_no_internal_link_is_host_absolute(self):
        """Every internal href is depth-relative so the site is correct at both
        /msup/ and / — see the build.py module docstring."""
        self.static()
        _, pages = self.render([self.exhibition()])
        for path in ("index.html", "exhibitions/index.html",
                     "exhibitions/%s/index.html" % self.SLUG):
            for href in re.findall(r'(?:href|src)="(/[^/][^"]*)"', pages[path]):
                self.fail("host-absolute link %r in %s" % (href, path))

    # -- upcoming vs past ------------------------------------------------

    def test_a_show_stays_upcoming_throughout_the_day_it_happens(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-11-10")
        self.assertIn("Upcoming", pages["exhibitions/index.html"])
        self.assertNotIn(">Past<", pages["exhibitions/index.html"])

    def test_a_show_becomes_past_the_day_after(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-11-11")
        index = pages["exhibitions/index.html"]
        self.assertIn("Past", index)
        self.assertNotIn("Upcoming", index)

    def test_an_end_date_keeps_a_run_upcoming_until_it_closes(self):
        self.static()
        ex = self.exhibition(end_date="2026-11-20")
        _, pages = self.render([ex], today="2026-11-15")
        self.assertIn("Upcoming", pages["exhibitions/index.html"])
        _, pages = self.render([ex], today="2026-11-21")
        self.assertIn("Past", pages["exhibitions/index.html"])

    def test_an_empty_half_renders_no_heading(self):
        """A site with no past shows should not advertise the fact."""
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-01-01")
        self.assertNotIn("past-heading", pages["exhibitions/index.html"])

    def test_home_banner_appears_only_for_an_upcoming_show_that_asked(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-01-01")
        self.assertIn("exhibition-banner", pages["index.html"])

        _, pages = self.render([self.exhibition()], today="2026-11-11")
        self.assertNotIn("exhibition-banner", pages["index.html"])

        _, pages = self.render([self.exhibition(on_home=False)], today="2026-01-01")
        self.assertNotIn("exhibition-banner", pages["index.html"])

    def test_home_row_holds_both_teasers_with_the_show_first(self):
        """They sit side by side in one row, and the show leads — it is the one
        with a date on it."""
        self.static()
        coll = {"slug": "winter", "title": "Winter Series", "on_home": True,
                "banner": "img/w.jpg", "intro": "Drawings from one winter."}
        _, pages = self.render([self.exhibition()], collections=[coll])
        home = pages["index.html"]
        self.assertIn('class="home-banners"', home)
        self.assertLess(home.index("exhibition-banner"), home.index("collection-banner"))

    def test_a_lone_teaser_still_gets_the_row_wrapper(self):
        """The :only-child rule in style.css is what widens a single teaser
        back out, so the wrapper has to be there even for one."""
        self.static()
        _, pages = self.render([self.exhibition()])
        self.assertIn('class="home-banners"', pages["index.html"])

    def test_no_teasers_emits_no_row_at_all(self):
        self.static()
        _, pages = self.render([self.exhibition(on_home=False)])
        self.assertNotIn("home-banners", pages["index.html"])

    # -- rsvp ------------------------------------------------------------

    RSVP = "https://www.eventbrite.com/e/example-tickets-123?aff=oddtdtcreator"

    def test_rsvp_button_and_note_render_for_an_upcoming_show(self):
        self.static()
        _, pages = self.render([self.exhibition(rsvp_url=self.RSVP)], today="2026-01-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn('href="%s" rel="noopener">RSVP</a>' % self.RSVP, page)
        self.assertIn("Reserve your spot here.", page)

    def test_rsvp_disappears_once_the_show_is_past(self):
        """A live ticket button on a past event invites people to book a seat
        at something that has already happened."""
        self.static()
        _, pages = self.render([self.exhibition(rsvp_url=self.RSVP)], today="2026-11-11")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertNotIn("eventbrite", page)
        self.assertNotIn("Reserve your spot", page)

    def test_rsvp_label_and_note_can_be_overridden(self):
        self.static()
        ex = self.exhibition(rsvp_url=self.RSVP, rsvp_label="Get tickets",
                             rsvp_note="Free, but book ahead.")
        _, pages = self.render([ex], today="2026-01-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn(">Get tickets</a>", page)
        self.assertIn("Free, but book ahead.", page)
        self.assertNotIn("Reserve your spot here.", page)

    def test_a_non_http_rsvp_url_is_refused_not_escaped(self):
        """esc() leaves `javascript:alert(1)` completely intact — it contains
        nothing HTML-escaping touches — and it would still run when clicked."""
        for bad in ("javascript:alert(1)", "data:text/html,<script>", "/relative"):
            with self.assertRaises(ValueError):
                self.build.exhibitions(self.cfg([self.exhibition(rsvp_url=bad)]))

    def test_a_show_with_no_rsvp_url_renders_no_button(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-01-01")
        self.assertNotIn("exhibition-rsvp", pages["exhibitions/%s/index.html" % self.SLUG])

    def test_the_home_teaser_carries_the_rsvp_too(self):
        """The home page is where most people land, so booking has to be
        reachable without a hop through the show's own page first."""
        self.static()
        _, pages = self.render([self.exhibition(rsvp_url=self.RSVP)], today="2026-01-01")
        home = pages["index.html"]
        self.assertIn('href="%s" rel="noopener">RSVP</a>' % self.RSVP, home)
        self.assertIn("Reserve your spot here.", home)

    def test_details_goes_quiet_only_when_an_rsvp_sits_beside_it(self):
        """One filled button per teaser, or neither leads."""
        self.static()
        _, pages = self.render([self.exhibition(rsvp_url=self.RSVP)], today="2026-01-01")
        self.assertIn('class="button button-quiet" href="exhibitions/%s/">Details' % self.SLUG,
                      pages["index.html"])

        _, pages = self.render([self.exhibition()], today="2026-01-01")
        self.assertIn('class="button" href="exhibitions/%s/">Details' % self.SLUG,
                      pages["index.html"])

    # -- photos ----------------------------------------------------------

    def test_photos_are_discovered_from_the_folder_in_filename_order(self):
        self.static(photos=["02-wall.jpg", "01-opening.jpg", "03-crowd.JPG"])
        _, pages = self.render([self.exhibition()], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        found = re.findall(r'src="\.\./\.\./img/exhibitions/%s/([\w.-]+)"' % self.SLUG, page)
        self.assertEqual(found, ["01-opening.jpg", "02-wall.jpg", "03-crowd.JPG"])

    def test_non_images_in_the_photo_folder_are_ignored(self):
        self.static(photos=["01.jpg", ".DS_Store", "notes.txt"])
        _, pages = self.render([self.exhibition()], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("01.jpg", page)
        self.assertNotIn("DS_Store", page)
        self.assertNotIn("notes.txt", page)

    def test_each_photo_links_to_its_full_size_file(self):
        self.static(photos=["01.jpg"])
        _, pages = self.render([self.exhibition()], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn('<a href="../../img/exhibitions/%s/01.jpg">' % self.SLUG, page)

    def test_photo_alt_is_applied_and_an_unlisted_photo_is_decorative(self):
        """An invented description is worse than none, so a photo with no alt
        configured is marked decorative rather than guessed at."""
        self.static(photos=["01.jpg", "02.jpg"])
        ex = self.exhibition(photo_alt={"01.jpg": "The room at opening night."})
        _, pages = self.render([ex], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn('alt="The room at opening night."', page)
        self.assertIn('/02.jpg" alt=""', page)

    def test_a_photo_alt_that_is_not_a_map_is_refused_at_construct_time(self):
        """Read only once the photo folder exists, so a bad value would lie
        dormant until the first upload and then take the previous build with
        it. Refused up front instead."""
        for bad in (["01.jpg"], "01.jpg", 7):
            with self.assertRaises(ValueError):
                self.build.exhibitions(self.cfg([self.exhibition(photo_alt=bad)]))

    def test_photo_filenames_are_percent_encoded(self):
        """esc() is the wrong layer for a URL: an unencoded '#' would be read
        as a fragment and 404 the photo."""
        self.static(photos=["01 shot #1.jpg"])
        _, pages = self.render([self.exhibition()], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("01%20shot%20%231.jpg", page)
        self.assertNotIn("01 shot #1.jpg", page)

    def test_no_photos_yet_renders_a_note_and_no_empty_list(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-01-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("Photos will appear here after the show.", page)
        self.assertNotIn("photo-grid", page)

    def test_a_past_show_with_no_photos_says_they_are_still_coming(self):
        self.static()
        _, pages = self.render([self.exhibition()], today="2026-12-01")
        page = pages["exhibitions/%s/index.html" % self.SLUG]
        self.assertIn("still being added", page)

    # -- wiring ----------------------------------------------------------

    def test_nav_entry_sits_before_about_and_marks_the_current_page(self):
        self.static()
        site, pages = self.render([self.exhibition()])
        self.assertEqual([e["path"] for e in site.nav_items],
                         ["works/", "exhibitions/", "about/"])
        self.assertIn('href="../exhibitions/" aria-current="page"',
                      pages["exhibitions/index.html"])
        self.assertIn('href="../../exhibitions/" aria-current="page"',
                      pages["exhibitions/%s/index.html" % self.SLUG])

    def test_many_shows_still_contribute_one_nav_entry(self):
        """Shows accumulate over the years; a nav that grew with them would be
        unusable, so they share the index."""
        self.static()
        site, _ = self.render([self.exhibition(),
                               self.exhibition(slug="spring-2027", date="2027-04-01")])
        self.assertEqual([e["path"] for e in site.nav_items],
                         ["works/", "exhibitions/", "about/"])

    def test_poster_src_is_depth_relative_on_every_page(self):
        self.static()
        _, pages = self.render([self.exhibition()])
        self.assertIn('src="%s"' % self.POSTER, pages["index.html"])
        self.assertIn('src="../%s"' % self.POSTER, pages["exhibitions/index.html"])
        self.assertIn('src="../../%s"' % self.POSTER,
                      pages["exhibitions/%s/index.html" % self.SLUG])

    def test_pages_are_in_the_sitemap_once(self):
        self.static()
        _, pages = self.render([self.exhibition()])
        sitemap = pages["sitemap.xml"]
        self.assertEqual(sitemap.count("<loc>https://example.com/exhibitions/</loc>"), 1)
        self.assertEqual(
            sitemap.count("<loc>https://example.com/exhibitions/%s/</loc>" % self.SLUG), 1)

    def test_a_resolved_collection_is_linked_and_an_unknown_one_warns(self):
        self.static()
        coll = {"slug": "winter", "title": "Winter Series", "banner": "img/w.jpg"}
        site, pages = self.render([self.exhibition(collection="winter")], collections=[coll])
        self.assertIn('href="../../collections/winter/"',
                      pages["exhibitions/%s/index.html" % self.SLUG])

        site, _ = self.render([self.exhibition(collection="nope")], collections=[coll])
        self.assertTrue(any("matches no entry" in w for w in site.warnings))

    def test_missing_poster_file_is_reported(self):
        self.static(poster=False)
        site, _ = self.render([self.exhibition()])
        self.assertTrue(any("poster missing" in w for w in site.warnings))

    def test_malformed_slug_poster_and_date_are_refused_not_escaped(self):
        """Slug and poster land in filesystem paths, and the date is compared
        rather than printed — escaping is the wrong tool for all three."""
        for bad in ({"slug": "Winter Show"}, {"slug": "../../tools"}, {"slug": ""}):
            with self.assertRaises(ValueError):
                self.build.exhibitions(self.cfg([self.exhibition(**bad)]))
        with self.assertRaises(ValueError):
            self.build.exhibitions(self.cfg([self.exhibition(poster="/img/p.jpg")]))
        for bad in ("10/11/2026", "not a date", "", None):
            with self.assertRaises(ValueError):
                self.build.exhibitions(self.cfg([self.exhibition(date=bad)]))
        with self.assertRaises(ValueError):
            self.build.exhibitions(self.cfg([self.exhibition(), self.exhibition()]))

    def test_an_end_date_before_the_start_is_refused(self):
        with self.assertRaises(ValueError):
            self.build.exhibitions(self.cfg([self.exhibition(end_date="2026-11-01")]))

    def test_site_without_exhibitions_is_unchanged(self):
        """An older site.json with no `exhibitions` key must still build, and
        must not grow an empty section or a dead nav link."""
        cfg = self.cfg([])
        del cfg["exhibitions"]
        self.assertEqual(self.build.exhibitions(cfg), [])
        self.assertEqual([e["path"] for e in self.build.nav_entries(cfg, [], [])],
                         ["works/", "about/"])

        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        site = self.build.Site(cfg, m.empty_db(), out)
        site.build()
        self.assertFalse(os.path.exists(os.path.join(out, "exhibitions")))


if __name__ == "__main__":
    unittest.main()
