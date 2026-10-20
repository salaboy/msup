Exhibition posters, and the photos taken at each show.

The poster for a show is a single file named after its slug in site.json:

  static/img/exhibitions/sabiduria-universal-2026.jpg

Photos from a show go in a folder of the same name, with no extension:

  static/img/exhibitions/sabiduria-universal-2026/01-opening.jpg
  static/img/exhibitions/sabiduria-universal-2026/02-wall.jpg

Commit them and they publish — there is no JSON to edit. The build lists the
folder and uses filename order as the running order, so number the files to
control it. Anything that isn't an image (.DS_Store, a notes file) is ignored.

Until the folder exists the show's page says photos will follow, so there is
nothing to create ahead of time.

Alt text is optional and lives in data/site.json under the show's `photo_alt`:

  "photo_alt": { "01-opening.jpg": "The room filling up before the talk." }

A photo not listed there is treated as decorative, which is the honest default
— a made-up description is worse than none.
