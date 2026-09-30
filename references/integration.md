# Wiring a blog pipeline

Three ways, from simplest to most shared.

## 1. CLI from any language

```bash
python3 find_image.py pick -q "query one" -q "query two" \
  --download --out /srv/site/images --name "$SLUG" --site mysite
```

Exit code 0 with JSON on stdout when an image was found, 1 otherwise (the JSON
then carries `error` and the per source `report`).

## 2. Python import

The script is a module; copy it next to the pipeline or add its folder to
`sys.path`:

```python
import sys; sys.path.insert(0, "/home/me/.claude/skills/blog-image-finder/scripts")
import find_image as fi

o = fi.options(fi.argparse.Namespace(
    providers="pexels,pixabay,openverse,wikimedia", orientation="landscape", kind="photo",
    min_width=1200, width=1600, ratio="16:9", license="strict", per_provider=15,
    site="mysite", allow_reuse=False, download=True, out="/srv/site/images",
    format="webp", quality=82, no_crop=False, dry_run=False, alternatives=3,
    generate_fallback=False, prompt=None, name=slug,
))
res = fi.pick(["couple dancing salsa", "dance class studio"], o)
if res["ok"]:
    path, credit = res["saved"]["file"], res["image"]["attribution"]
```

Upload `path` to the site's storage, store `credit`, `source_url` and
`license` with the article.

## 3. HTTP service

```bash
python3 find_image.py serve --host 127.0.0.1 --port 8765 --out /srv/blog-images --site default
```

Set `BIF_SERVER_TOKEN` to require `Authorization: Bearer <token>`; keep the
default `127.0.0.1` unless a reverse proxy with TLS sits in front.

| Method | Path | Body or query | Returns |
|---|---|---|---|
| GET | `/health` | | `{"ok": true, "version": ...}` |
| GET | `/v1/search` | `q` (repeatable), `orientation`, `min_width`, `providers`, `site`, `per_provider` | ranked `results`, `report` |
| POST | `/v1/pick` | `{"queries": [...], "download": true, "name": "slug", "site": "mysite", "orientation": "landscape", "target_width": 1600, "ratio": 1.777, "format": "webp", "generate_fallback": false, "dry_run": false}` | same JSON as the CLI; 404 when nothing fits |
| POST | `/v1/generate` | `{"prompt": "...", "name": "slug"}` | generated image; 502 when every generator failed |

Also accepted by `/v1/pick`: `variants` (`"blog"`), `judge` (true), `context`,
`alt_langs` (`["fr", "en"]`), `judge_min`, `min_ratio`.

Every field is optional except the queries (or the prompt). Server flags set
the defaults, the request overrides them.

### systemd user unit

`~/.config/systemd/user/blog-image-finder.service`:

```ini
[Unit]
Description=blog-image-finder API
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/.claude/skills/blog-image-finder/scripts/find_image.py serve --port 8765 --out %h/blog-images
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now blog-image-finder
loginctl enable-linger "$USER"   # keep it running without a login session
```

## Storage

The tool writes files to a folder and stops there: uploading to S3, Supabase
Storage, R2 or a CMS media library belongs to the pipeline, which already has
those credentials. The JSON sidecar (`<file>.json`) holds everything needed
for the credit line and a later license audit: provider, id, source URL,
author, license, license URL, query, download date.

## Migrating an existing image picker

If a pipeline already calls Pexels or Pixabay directly, keep its upload code
and replace only the search and choice with `pick`. Seed the ledger with the
images already published so they are not picked again:

```bash
printf '{"provider":"pexels","id":"%s","site":"mysite"}\n' 12345 67890 >> ~/.local/share/blog-image-finder/used.jsonl
```
