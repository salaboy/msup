# marylandsupreme.com

The website for **Maryland Supreme** (Mauricio Salatino) — a static site on
GitHub Pages, with artworks synced from the
[Etsy shop](https://www.etsy.com/shop/MSupShop).

No build tooling, no dependencies: Python 3 standard library and plain
HTML/CSS. Nothing to install.

Buying happens on Etsy. Everything else goes to
[Instagram](https://instagram.com/marylandsupreme).

---

## Adding a new piece

1. List it on Etsy as normal.
2. Go to **[Actions → Sync Etsy and publish](../../actions/workflows/sync-and-publish.yml)**
   → **Run workflow**. (Works from a phone.)
3. It appears on the site in a couple of minutes.

If you forget, the sync also runs by itself every morning.

**Write listing descriptions like this** and each label becomes a row in the
spec table on the artwork page:

```
Title: FIX
Description: The weekend is over and you need to start fixing your life.
Type: hand drawn original
Size: A3 (29.7 x 42.0 cm)
Framed: Recycled Frame
```

You can invent new labels — `Edition:`, `Paper:`, `Year:` — and they show up
automatically. A description with no labels at all still works; it's used as
plain text.

## Photos: the gallery

Etsy's RSS feed carries **only the first photo** of each listing. The other
image URLs cannot be guessed — each ends in a required per-image suffix
(`..._1rfk.jpg`) with no derivable pattern — and Etsy's listing pages are
behind DataDome, so no server can read them. Verified: plain HTTP, headless
Chrome (with and without anti-automation flags), the mobile site, the apex
domain and oEmbed all return 403 or 404. The shop RSS feed is the only open
surface.

Your own browser, however, is never blocked. So the photos are captured there,
once per artwork, and committed.

**Set up once:** open [`/tools/`](https://salaboy.github.io/msup/tools/) on the
site and drag the **MSup: grab photos** button to your bookmarks bar.

**For each new artwork:**

1. Open the listing on Etsy and click through the photos so they load.
2. Click **MSup: grab photos** — it downloads a file already named after the
   listing, e.g. `4565863595.json`.
3. Move that file into `data/images/`. On GitHub: open the `data/images` folder
   → **Add file → Upload files** → drag it in from Downloads → commit.
   No renaming needed.

(If a browser blocks the download it copies the same text to the clipboard
instead, and tells you the filename to use.)

That's it — committing the file rebuilds and publishes the gallery on its own.
A listing with no file falls back to the single RSS photo, so nothing breaks if
you skip this.

`data/images/` is yours — the sync reads it and never writes to it. The first
entry is the cover shown in the grid; reorder to change it. Full-resolution
URLs are fine: the build serves downsized variants in the grid and keeps the
original for "view full size".

<sub>If Etsy ever grants you an API key, set `ETSY_API_KEY` as a repo secret and
the sync will pull galleries automatically — captured files still win.</sub>

## Analytics

Google Analytics 4 is wired in but **off until you add your ID**. Put your
measurement ID in `data/site.json`:

```json
"google_analytics": "G-XXXXXXXXXX"
```

Find it in GA under **Admin → Data streams → your web stream → Measurement ID**.
Commit, and the tag goes onto every page including the 404. Leave it empty and
no tracking code is emitted at all.

> **Note:** this loads Google's tag immediately, which sets cookies before the
> visitor has agreed to anything. That's the common setup, but it isn't strictly
> compliant with UK/EU cookie rules. If you ever want to fix that, the options
> are a consent banner or a cookieless counter — ask and I'll switch it over.

## When something sells

Nothing to do. Etsy drops sold listings from the feed, the sync notices, and
the piece moves to the **Sold** section of `/works/` instead of disappearing.
Its page and URL stay live.

## Editing a piece by hand

Everything under `curation` in `data/works.json` is yours — **the sync never
overwrites it**. Everything under `etsy` and `sync` is machine-owned and will
be replaced on the next run.

| Field | Effect |
|---|---|
| `featured` | Show on the home page. If nothing is featured, all available work shows. |
| `order` | Manual position. Lower first; unset sorts after those that are set. |
| `hidden` | Hide from the site entirely, without deleting the record. |
| `title_override` | Use a different title than Etsy's (Etsy titles are SEO-shaped). |
| `alt` | Hand-written alt text. Always better than the generated one. |
| `status_override` | Force `"sold"` or `"available"` — e.g. sold at a show before delisting. |
| `image_override` | Use one image from `static/` instead of the Etsy gallery. |
| `year`, `tags`, `notes` | Metadata. `notes` is not published. |

Edit it directly on GitHub and the site rebuilds on save.

---

## Working locally

```sh
python3 scripts/serve.py                 # build + preview at localhost:8000/msup/
python3 scripts/sync_etsy.py --dry-run   # show what a sync would change
python3 scripts/build.py                 # build into _site/
python3 -m unittest discover -s tests    # 50 tests, no network needed
```

`_site/` is generated and gitignored — never edit it. The only file the sync
robot ever commits is `data/works.json`, so its history reads as a log of what
was listed and what sold.

## How it fits together

```
Etsy RSS ─────────┐   (listings, prices, sold status — automatic)
                  ├─▶ sync_etsy.py ──▶ data/works.json ──▶ build.py ──▶ _site/ ──▶ Pages
data/images/*.json┘      (upsert)     (source of truth)   (templates/)
 (photos, captured
  in your browser)
```

| File | Role |
|---|---|
| `scripts/msuplib.py` | Feed parsing, API images, merge rules, srcset. All the logic worth testing. |
| `scripts/sync_etsy.py` | Etsy → `works.json`. Never deletes. |
| `scripts/build.py` | `works.json` + `templates/` → `_site/`. |
| `tools/grab-photos.js` | The bookmarklet source, compiled into `/tools/` at build time. |
| `data/images/` | Captured galleries, one file per listing. Yours to edit. Applied by both the sync and the build, so committing one is enough. |
| `data/site.json` | Titles, URLs, nav, analytics ID. |
| `content/*.txt` | Bio and page intro, as plain text. |

### Why the site is built this way

A few decisions that look odd until you know the reason:

- **RSS for listings, the browser for photos.** RSS needs no key and no
  scraping, so the daily sync keeps working unattended. Only the photos need a
  human, and only once per artwork.
- **No server-side scraping.** Even if a bypass worked today, the sync runs on
  GitHub Actions datacenter IPs — the most heavily scrutinised kind — so it
  would break silently and leave empty galleries.
- **The sync refuses to write when the feed comes back empty** (exit code 3).
  A bot challenge or a shop rename would otherwise mark every piece sold in one
  unattended overnight run. Override with `--allow-empty` if the shop really is
  empty.
- **Images are hotlinked from Etsy**, not copied into the repo. Cheap and
  simple, with the caveat below.
- **The gallery uses no JavaScript.** Photo selection is a hidden radio per
  image plus `:has()` in CSS, which gives keyboard and screen-reader support
  from native radio semantics. Where `:has()` is unsupported the first photo
  stays visible.
- **Sync, build and deploy are one workflow.** Commits made by Actions don't
  trigger other workflows, so a "sync commits → push triggers deploy" split
  would silently never publish.
- **The gallery is never ordered by Etsy's `pubDate`**, which is the *renewal*
  date. Sorting by it would reshuffle the grid every time a listing renews.

### Known limitations

- **Photos need a manual capture per artwork.** See above. Everything else
  (new listings, prices, sold status) is fully automatic.
- **Hotlinked images depend on Etsy.** Photos of sold or deactivated listings
  usually keep serving for years; photos of *deleted* listings eventually 404,
  and a broken image falls back to a placeholder. For a guaranteed archive, run
  the sync with `--archive-images`: it saves a copy of a piece's main photo at
  the moment it sells (a handful of files a year). Off by default.
- **A gallery is capped at 12 photos**, matching the CSS rules in `style.css`.
  Raise `MAX_GALLERY` in `build.py` and extend those rules together.

## Still to do

- [ ] Enable Pages: **Settings → Pages → Source: GitHub Actions**
- [ ] Install the bookmarklet and capture the photos for FIX
- [ ] Verify the live site at `https://salaboy.github.io/msup/`
- [ ] Point `marylandsupreme.com` at Pages, then cancel Big Cartel
- [ ] Optional: backfill older Big Cartel work as `source: "manual"` records
      (the schema already supports it and the sync leaves them alone)

> **Before cancelling Big Cartel**, mirror the old site — those images and
> descriptions don't exist anywhere else:
> `wget --mirror --page-requisites --convert-links https://www.marylandsupreme.com/`
