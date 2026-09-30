# Sources: keys, limits and license rules

Checked against the live APIs on 2026-09-30. Limits change; `find_image.py
check` tells what works now.

## Stock photo sources

| Source | Key | Sign up | Free limit | Credit required | Re-hosting |
|---|---|---|---|---|---|
| Pexels | `PEXELS_API_KEY` | https://www.pexels.com/api/ (free account, key shown at once) | 200 req/hour, 20 000/month | No, appreciated | Allowed |
| Pixabay | `PIXABAY_API_KEY` | https://pixabay.com/api/docs/ (log in, key is on the docs page) | 100 req/minute | No, appreciated | **Required**: no permanent hotlinking, download the file. The API serves 1280 px at most. |
| Unsplash | `UNSPLASH_ACCESS_KEY` | https://unsplash.com/oauth/applications (new app, copy Access Key) | 50 req/hour in demo, 5 000 after production review | **Yes**: "Photo by X on Unsplash" with links | **Forbidden**: display from Unsplash URLs, send the download ping (the tool does). |
| Openverse | none | optional OAuth app for higher limits | low anonymous quota, an OAuth app raises it | Depends on the license (CC BY: yes) | Allowed within the license |
| Wikimedia Commons | none | none | generous, identify with a User-Agent (the tool does) | Yes for CC BY | Allowed within the license |

`UNSPLASH_APP_NAME` (optional) sets the `utm_source` of Unsplash credit links;
Unsplash asks that it match the application name.

## License policy

`--license strict` (default) keeps only what a commercial blog can use,
modify (crop, resize, convert) and publish without legal review:

- Pexels License, Pixabay Content License, Unsplash License;
- CC0 and Public Domain Mark;
- CC BY (credit required).

`--license attribution-sa` also accepts CC BY-SA. Share-alike arguably applies
to a cropped version, so the cropped image would have to be offered under CC
BY-SA too. Use it only when the site accepts that.

Always excluded: NC (non commercial), ND (no derivatives, cropping would break
it), fair use and unknown licenses.

What the provider licenses still forbid, whatever the flag: selling the image
unaltered, using a recognizable person to imply an endorsement, using a
trademark or logo visible in the photo as if it were yours. The visual check in
SKILL.md step 3 exists for this.

## AI generators (last resort)

| Generator | Keys | Notes |
|---|---|---|
| NVIDIA FLUX.1 schnell | `NVIDIA_API_KEY` from https://build.nvidia.com (phone verification needed before any key works) | Free credits. Very slow on the shared queue: 100 to 200 s per image measured on 2026-09-30, sometimes more. Sizes snap to 768 to 1344 px. |
| Cloudflare Workers AI FLUX.1 schnell | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (token with Workers AI permission) from https://dash.cloudflare.com | 10 000 neurons per day free, a few hundred images. The most dependable free option. |
| Pollinations | none, or `POLLINATIONS_TOKEN` from https://enter.pollinations.ai | Anonymous access answered "queue full" or 402 on 2026-09-30. A free token gives a dedicated quota. |

Generated images carry `license: AI-generated, see provider terms`. None of
the three claims rights over the output for this use, but the terms change;
re-read them before a large batch.

## Keys file

`~/.config/blog-image-finder/keys.env`, mode 600, `KEY=value` per line (a
template is in `assets/keys.env.example`). Environment variables win over the
file. `BIF_CONFIG` points to another file, `BIF_LEDGER` to another ledger.
