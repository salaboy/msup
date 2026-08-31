Gallery photos, one file per Etsy listing.

Etsy's listing pages are bot-protected, so a server can't read them. The photo
URLs are captured in a real browser with the bookmarklet (see /tools/ on the
site, or tools/grab-photos.js) and committed here.

  data/images/4565863595.json

Each file is a JSON list. Either shape works:

  ["https://i.etsystatic.com/.../il_fullxfull.8503570133_1rfk.jpg"]

  [{"url": "https://i.etsystatic.com/.../il_fullxfull.8503570133_1rfk.jpg",
    "w": 3000, "h": 2250}]

The first entry is the cover image used in the grid. Reorder to change it.

This directory is yours — the sync reads it and never writes to it. A listing
with no file here falls back to the single photo the RSS feed provides.
