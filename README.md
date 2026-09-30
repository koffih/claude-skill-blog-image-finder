# blog-image-finder

A [Claude Code](https://claude.com/claude-code) skill, and a small HTTP API, that finds a free and legally usable image for a blog article.

It searches **Pexels, Pixabay, Unsplash, Openverse and Wikimedia Commons** in parallel, keeps only licenses safe for a commercial blog, ranks the results (relevance, resolution, orientation, source quality), skips images the site already used, then downloads the winner, crops it to 16:9 and converts it to WebP, with a JSON sidecar holding author, license and credit line. When no photo fits, it can fall back to **free AI generation** (Cloudflare Workers AI, NVIDIA FLUX, Pollinations).

Built for content pipelines that publish articles every day and need an image for each one without paying for generation.

Part of the [koffih skills catalog](https://github.com/koffih/claude-skills).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/koffih/claude-skill-blog-image-finder/main/install.sh | bash
```

Options: `bash -s -- --project` installs into `./.claude/skills`, `bash -s -- --uninstall` removes it. Running the command again updates it.

Requirements: Python 3.10+. [Pillow](https://pypi.org/project/pillow/) is optional (`pip install pillow`); without it images are saved as downloaded, uncropped.

## Keys

Openverse and Wikimedia need no key, so the skill works right after install. For much better photos, add free keys (two minutes each, links in [references/providers.md](references/providers.md)):

```bash
mkdir -p ~/.config/blog-image-finder
cp ~/.claude/skills/blog-image-finder/assets/keys.env.example ~/.config/blog-image-finder/keys.env
chmod 600 ~/.config/blog-image-finder/keys.env
# fill PEXELS_API_KEY, PIXABAY_API_KEY, UNSPLASH_ACCESS_KEY
python3 ~/.claude/skills/blog-image-finder/scripts/find_image.py check
```

## Use

In Claude Code:

```
/blog-image-finder cover image for "Cours de salsa pour debutants a Montreal"
```

Or just ask for an image for an article: the skill triggers on its own. Claude turns the article into concrete English search queries, picks, **looks at the image** to reject off-topic results, and hands back the file, alt text and credit line.

From a script or cron job:

```bash
S=~/.claude/skills/blog-image-finder/scripts/find_image.py
python3 $S pick -q "couple dancing salsa" -q "dance class studio" \
  --download --out ./images --name salsa-debutants --site mysite
```

As an API for pipelines in any language:

```bash
python3 $S serve --port 8765 --out /srv/blog-images
curl -s -X POST localhost:8765/v1/pick \
  -d '{"queries":["couple dancing salsa"],"download":true,"name":"salsa-debutants","site":"mysite"}'
```

```json
{
  "ok": true,
  "image": {
    "provider": "pexels",
    "author": "Jane Doe",
    "license": "Pexels License",
    "attribution": "Photo by Jane Doe on Pexels",
    "source_url": "https://www.pexels.com/photo/...",
    "score": 91.4
  },
  "saved": { "file": "/srv/blog-images/salsa-debutants-pexels-1a2b3c4d.webp", "width": 1600, "height": 900, "bytes": 184233 },
  "alternatives": ["..."]
}
```

Endpoints, a systemd unit and a Python import example: [references/integration.md](references/integration.md).

## What it takes care of

- **Licenses.** Default policy keeps Pexels, Pixabay, Unsplash licenses, CC0, public domain and CC BY; never NC, ND or unknown. `--license attribution-sa` also accepts CC BY-SA.
- **Each source's rules.** Unsplash photos are hotlink only and get the required download ping; Pixabay files are downloaded, never hotlinked; every result keeps the credit line its license asks for.
- **No reuse.** Every pick is recorded in a ledger per site, so two articles never get the same photo.
- **Outages.** A source that is down or rate limited is reported and skipped, the others still answer.
- **Blog format.** Landscape, 16:9, at least 1200 px, resized to 1600 px, WebP. All adjustable.

## Files

| File | Role |
|---|---|
| `SKILL.md` | How Claude writes queries, picks, checks the image and returns credit and alt text |
| `scripts/find_image.py` | The engine: `search`, `pick`, `generate`, `check`, `serve` |
| `references/providers.md` | Keys, sign up links, rate limits, license rules per source |
| `references/integration.md` | CLI, Python import, HTTP API, systemd, storage, migrating a pipeline |
| `assets/keys.env.example` | Keys file template |
| `install.sh` | Install, update, uninstall |

## License

MIT. The images themselves stay under their own licenses: check the sidecar JSON of each file.
