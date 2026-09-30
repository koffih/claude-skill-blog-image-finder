---
name: blog-image-finder
description: Find a free, legally usable image for a blog article by searching Pexels, Pixabay, Unsplash, Openverse and Wikimedia Commons at once, ranking the results, skipping images already used, and downloading the winner as a cropped WebP with its license and attribution. Falls back to free AI image generation when no photo fits. Use when an article, blog post, landing page or newsletter needs a cover or inline image, when a content pipeline needs an image API, or when the user asks for a royalty-free, stock or free image. Also triggers in French: trouver une image, image libre de droits, image pour un article, illustration de blog, photo gratuite.
argument-hint: "[article title, topic or slug]"
---

# blog-image-finder

Pick the right free image for an article, keep its license trail, never reuse
an image on the same site.

The engine is `scripts/find_image.py` (Python 3.10+, standard library, Pillow
optional for crop and WebP). It is both a CLI and an HTTP API, so an agent, a
cron job or a pipeline in any language can use it.

`S=${CLAUDE_SKILL_DIR:-~/.claude/skills/blog-image-finder}/scripts/find_image.py`

## 1. Check the sources once per session

```bash
python3 $S check
```

Openverse and Wikimedia always work. Pexels, Pixabay and Unsplash need a free
key each (see `references/providers.md` to get them in two minutes). Keys go
in `~/.config/blog-image-finder/keys.env` or the environment. With no key at
all the skill still works, on the two keyless sources, with fewer good photos.
Report which sources are missing, and continue with the ones that work.

## 2. Turn the article into visual queries

The quality of the image is decided here, not by the ranking. Write 2 to 4
short **English** queries (the sources index in English, even for a French
article) that describe **what can be photographed**, not the abstract topic:

| Article | Bad query | Good queries |
|---|---|---|
| "Automatiser sa PME avec l'IA" | `AI automation SME` | `small business owner laptop`, `team reviewing dashboard on screen` |
| "Choisir un notaire a Lome" | `notary Lome` | `signing contract documents desk`, `African businessman signing papers` |
| "Cours de salsa pour debutants" | `salsa beginners` | `couple dancing salsa`, `dance class studio` |

Rules: concrete nouns and scenes, one idea per query, the most specific query
first, a broader one last as a safety net. Add the country or people only when
the article is about them. Avoid words that pull logos and text (`logo`,
`infographic`, `icon`) unless that is what is wanted.

## 3. Pick, download, verify

```bash
python3 $S pick -q "couple dancing salsa" -q "dance class studio" \
  --download --out ./public/images/blog --name <article-slug> --site <site-name>
```

Defaults are tuned for a blog cover: landscape, 16:9, at least 1200 px wide,
resized to 1600 px, WebP quality 82, strict commercial licenses. Useful flags:
`--orientation portrait|square|any`, `--ratio 3:2`, `--width 1200`,
`--format jpg`, `--kind any` (also illustrations), `--providers pexels,pixabay`,
`--license attribution-sa` (also CC BY-SA), `--dry-run` (do not record).

Landscape is strict: anything narrower than `--min-ratio` (1.3) is dropped,
both from the listed size and from the real pixels after download.

`--variants blog` writes the three files a blog template needs from one pick:
`cover` 1600x900 WebP, `card` 800x450 WebP, `og` 1200x630 JPEG (social
previews). `saved.files` lists them. Custom sets: `--variants hero:1920x800:webp,og:1200x630:jpg`.

`--judge --context "<title, summary, country>" --alt-langs fr,en` sends the six
best candidates to a vision model (Moonshot `kimi-k2.6` by default, any OpenAI
compatible endpoint through `BIF_JUDGE_URL`, `BIF_JUDGE_KEY`, `BIF_JUDGE_MODEL`).
It grades each photo 0 to 10 on what it shows, caps at 3 a photo from the wrong
region or a famous landmark of another city, keeps those at `--judge-min` (7)
or above, and writes alt text in each language (`image.alt`). About 25 s and
half a US cent per pick. `report.judge.rejected` says why the others lost.

The result is JSON: `image` (provider, author, license, attribution, source
URL, score), `saved` (file path, final size, bytes) and `alternatives`. A JSON
sidecar with the same name keeps the license trail next to the image.

**Then look at the image** (open the saved file with the Read tool). The score
only reads titles and tags. Reject and pick again if the image shows a
different subject, visible brand logos or watermarks, readable text in another
language, a recognizable person in a context that could embarrass them (health,
debt, crime articles), or clashes with the article's country. `pick` records
every choice, so running it again returns the next best image, never the same
one. When searching only (no download), use `search` and choose yourself.

## 4. Hand back what the article needs

Give back: file path (or URL for Unsplash, see below), `alt` text written by
you in the article's language describing what the image shows (not the SEO
keyword list), and the credit line from `attribution`. Pexels and Pixabay do
not require a credit but it is good practice; Openverse, Wikimedia and
Unsplash **do** (see `references/providers.md`). Put the credit in the
figcaption or the article footer.

**Unsplash is hotlink only**: its photos must be displayed from Unsplash's own
URL and cannot be re-hosted, so `--download` excludes Unsplash automatically.
Without `--download`, an Unsplash pick returns `image_url` to embed as is, and
the tool has already sent the required download ping.

## 5. When nothing fits

`pick --generate-fallback --prompt "<scene description>"` generates an image
with the first free AI generator that answers: NVIDIA FLUX (`NVIDIA_API_KEY`,
slow, one to three minutes), Cloudflare Workers AI (`CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN`, free daily quota, the most reliable), Pollinations
(works without a key when its queue is not full; `POLLINATIONS_TOKEN` makes it
reliable). `generate --prompt ...` does it directly. Write the prompt as a
scene, not a topic. The default style asks for an editorial photograph with no
text. Mark generated images as such if the site has a disclosure policy.

## 6. For pipelines

A content pipeline should not shell out per article when it can call the API:

```bash
python3 $S serve --port 8765 --out /srv/blog-images --site mysite
curl -s -X POST localhost:8765/v1/pick -d '{"queries":["couple dancing salsa"],"download":true,"name":"salsa-debutants"}'
```

Endpoints, parameters, a systemd unit and a Python client are in
`references/integration.md`. Read it when wiring an existing blog generator.

## Rules

- Never commit or print API keys. Name the file that holds them.
- One source failing (rate limit, outage) never stops the pick: the report
  lists per source results and errors. Say which sources failed.
- Keep `--site` stable per website so the no-reuse ledger works
  (`~/.local/share/blog-image-finder/used.jsonl`, one JSON line per pick).
- Respect each source's rate limit: one `pick` makes one request per source
  per query. Pexels allows 200 per hour, Pixabay 100 per minute, Unsplash 50
  per hour in demo mode, Openverse has a low anonymous quota.
