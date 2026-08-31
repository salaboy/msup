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

## Photos: why the API key matters

Etsy's RSS feed carries **only the first photo** of each listing, and the other
image URLs cannot be guessed — each one ends in a required per-image suffix
(`..._1rfk.jpg`) with no derivable pattern. Without a key, every piece on the
site shows one photo.

With a free API key, the sync pulls **every photo** for every listing and the
artwork pages get a proper gallery.

**One-time setup:**

1. Register an app at <https://www.etsy.com/developers/register> (read-only
   access to public listing data is all this needs).
2. Copy the **keystring**.
3. In this repo: **Settings → Secrets and variables → Actions → New repository
   secret**, named `ETSY_API_KEY`.

That's it — the workflow picks it up automatically. Locally:
`ETSY_API_KEY=... python3 scripts/sync_etsy.py`.

If the key is missing or a request fails, the sync falls back to the single RSS
photo rather than failing the run.

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
python3 -m unittest discover -s tests    # 35 tests, no network needed
```

`_site/` is generated and gitignored — never edit it. The only file the sync
robot ever commits is `data/works.json`, so its history reads as a log of what
was listed and what sold.

## How it fits together

```
Etsy RSS  ─┐
           ├─▶ scripts/sync_etsy.py ──▶ data/works.json ──▶ build.py ──▶ _site/ ──▶ Pages
Etsy API  ─┘        (upsert)           (source of truth)   (templates/)
 (photos)
```

| File | Role |
|---|---|
| `scripts/msuplib.py` | Feed parsing, API images, merge rules, srcset. All the logic worth testing. |
| `scripts/sync_etsy.py` | Etsy → `works.json`. Never deletes. |
| `scripts/build.py` | `works.json` + `templates/` → `_site/`. |
| `data/site.json` | Titles, URLs, nav. |
| `content/*.txt` | Bio and page intro, as plain text. |

### Why the site is built this way

A few decisions that look odd until you know the reason:

- **RSS for listings, the API only for photos.** RSS needs no key, so the site
  keeps working if the key is ever revoked — it just loses galleries.
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

- **Without `ETSY_API_KEY`, one photo per piece.** See above.
- **Hotlinked images depend on Etsy.** Photos of sold or deactivated listings
  usually keep serving for years; photos of *deleted* listings eventually 404,
  and a broken image falls back to a placeholder. For a guaranteed archive, run
  the sync with `--archive-images`: it saves a copy of a piece's main photo at
  the moment it sells (a handful of files a year). Off by default.
- **A gallery is capped at 12 photos**, matching the CSS rules in `style.css`.
  Raise `MAX_GALLERY` in `build.py` and extend those rules together.

## Still to do

- [ ] Enable Pages: **Settings → Pages → Source: GitHub Actions**
- [ ] Add the `ETSY_API_KEY` secret so pieces get full galleries
- [ ] Verify the live site at `https://salaboy.github.io/msup/`
- [ ] Point `marylandsupreme.com` at Pages, then cancel Big Cartel
- [ ] Optional: backfill older Big Cartel work as `source: "manual"` records
      (the schema already supports it and the sync leaves them alone)

> **Before cancelling Big Cartel**, mirror the old site — those images and
> descriptions don't exist anywhere else:
> `wget --mirror --page-requisites --convert-links https://www.marylandsupreme.com/`
