"""Tests for the Etsy feed parser and the works.json merge rules.

The FIX fixture is the real feed as returned by Etsy on 2026-08-31.
"""

import json
import os
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


if __name__ == "__main__":
    unittest.main()
