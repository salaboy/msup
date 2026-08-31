/* Grab the photo URLs from an Etsy listing page.
 *
 * Etsy's listing pages are behind DataDome, so no server can read them — but
 * an ordinary browser can. This runs in the artist's own browser on their own
 * listing and copies the gallery to the clipboard.
 *
 * Two things it has to get right:
 *
 *  1. SCOPE. A listing page also shows "you may also like" and shop thumbnails.
 *     Taking every etsystatic URL on the page would pull in other artworks, so
 *     it prefers the page's JSON-LD Product.image array, which Etsy scopes to
 *     this listing. The DOM scan is a fallback and is restricted to the
 *     carousel.
 *
 *  2. DEDUPE. The same photo appears at several sizes (il_570xN, il_794xN...).
 *     Each is keyed by its image id + suffix, so dedupe on that and normalise
 *     everything to il_fullxfull.
 */
(function () {
  'use strict';

  /* Etsy size tokens come in three shapes: 570xN, 180x180, fullxfull.
     Spell them out — a loose [0-9a-zA-Z]+x[0-9a-zA-Z]+ would be tempting but
     an earlier version used [N0-9]+ after the x and silently failed on
     fullxfull, which is exactly what the JSON-LD serves. */
  var SIZE = '(?:\\d+x\\d+|\\d+xN|fullxfull)';
  var ID_RE = new RegExp('/(\\d+)/il_' + SIZE + '\\.(\\d+)_([a-z0-9]+)\\.');
  var SIZE_RE = new RegExp('il_' + SIZE + '\\.');

  function listingId() {
    var m = location.pathname.match(/\/listing\/(\d+)/);
    return m ? m[1] : null;
  }

  function key(url) {
    var m = url.match(ID_RE);
    return m ? m[2] + '_' + m[3] : null;   // image id + per-image suffix
  }

  function full(url) {
    return url.split('?')[0].replace(SIZE_RE, 'il_fullxfull.');
  }

  /* Etsy embeds a Product with an `image` array scoped to this listing. */
  function fromJsonLd() {
    var out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
      var data;
      try { data = JSON.parse(s.textContent); } catch (e) { return; }
      (Array.isArray(data) ? data : [data]).forEach(function (node) {
        var graph = node && node['@graph'] ? node['@graph'] : [node];
        graph.forEach(function (n) {
          if (!n || n['@type'] !== 'Product' || !n.image) return;
          (Array.isArray(n.image) ? n.image : [n.image]).forEach(function (img) {
            var url = typeof img === 'string' ? img : (img && (img.url || img.contentUrl));
            if (url && url.indexOf('etsystatic') > -1) out.push(url);
          });
        });
      });
    });
    return out;
  }

  /* Fallback: the image carousel only. Deliberately NOT document-wide. */
  function fromCarousel() {
    var scopes = [
      '[data-component="listing-page-image-carousel"]',
      '.listing-page-image-carousel-component',
      'ul.carousel-pane-list',
      '#image-carousel',
      '[data-palette-listing-image]',
    ];
    var root = null;
    for (var i = 0; i < scopes.length && !root; i++) root = document.querySelector(scopes[i]);
    if (!root) return [];

    var out = [];
    root.querySelectorAll('img, source').forEach(function (el) {
      ['data-src-zoom-image', 'data-src-delay', 'data-src', 'src', 'srcset'].forEach(function (attr) {
        var v = el.getAttribute(attr);
        if (!v) return;
        v.split(',').forEach(function (part) {
          var url = part.trim().split(/\s+/)[0];
          if (url && url.indexOf('etsystatic') > -1) out.push(url);
        });
      });
    });
    return out;
  }

  function dimensions() {
    /* Natural sizes, where an image has actually loaded, keyed the same way. */
    var dims = {};
    document.querySelectorAll('img').forEach(function (img) {
      var k = key(img.currentSrc || img.src || '');
      if (k && img.naturalWidth && !dims[k]) {
        dims[k] = { w: img.naturalWidth, h: img.naturalHeight };
      }
    });
    return dims;
  }

  function report(message) {
    var box = document.createElement('div');
    box.textContent = message;
    box.style.cssText = 'position:fixed;z-index:2147483647;left:50%;top:24px;' +
      'transform:translateX(-50%);background:#111;color:#fff;padding:14px 20px;' +
      'border-radius:10px;font:14px/1.5 -apple-system,system-ui,sans-serif;' +
      'max-width:min(90vw,560px);box-shadow:0 8px 30px rgba(0,0,0,.35);white-space:pre-wrap';
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 8000);
  }

  /* Save straight to disk, already named <listing id>.json, so the file can be
     dropped into data/images/ without renaming. Falls back to the clipboard,
     then to a prompt, if a browser refuses the download. */
  function download(name, text) {
    try {
      var url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      var a = document.createElement('a');
      a.href = url;
      a.download = name;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 30000);
      return true;
    } catch (e) {
      return false;
    }
  }

  var id = listingId();
  if (!id) {
    report('Not an Etsy listing page.\nOpen one of your listings and click again.');
    return;
  }

  var found = fromJsonLd();
  var source = 'listing data';
  if (found.length < 2) {
    var carousel = fromCarousel();
    if (carousel.length > found.length) { found = carousel; source = 'image carousel'; }
  }

  var dims = dimensions();
  var seen = {}, images = [];
  found.forEach(function (url) {
    var k = key(url);
    if (!k || seen[k]) return;
    seen[k] = 1;
    var entry = { url: full(url) };
    if (dims[k]) { entry.w = dims[k].w; entry.h = dims[k].h; }
    images.push(entry);
  });

  if (!images.length) {
    report('No photos found.\nScroll through the gallery once so the images load, then click again.');
    return;
  }

  var json = JSON.stringify(images, null, 2);
  var filename = id + '.json';
  var count = images.length + ' photo' + (images.length === 1 ? '' : 's');

  if (download(filename, json)) {
    report('Downloaded ' + filename + '  (' + count + ' from ' + source + ').\n\n' +
           'Move it into data/images/ in the repo.');
    return;
  }

  var copied = function () {
    report('Copied ' + count + ' (from ' + source + ').\n\nSave as:  data/images/' + filename);
  };
  var manual = function () {
    window.prompt('Copy this, then save as data/images/' + filename, json);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(json).then(copied, manual);
  } else {
    manual();
  }
})();
