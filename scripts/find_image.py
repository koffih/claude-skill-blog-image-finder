#!/usr/bin/env python3
"""Find a free, legally usable image for a blog article.

One command queries several free image sources at once (Pexels, Pixabay,
Unsplash, Openverse, Wikimedia Commons), keeps only licenses safe for a
commercial blog, ranks the results, skips images already used, and can
download, crop and convert the winner to WebP. When nothing fits, it can fall
back to a free AI image generator (NVIDIA FLUX, Cloudflare Workers AI,
Pollinations).

Standard library only. Pillow is optional: with it, downloads are cropped,
resized and converted; without it, the original file is saved as is.

Subcommands:
  search    rank candidates and print them as JSON
  pick      choose the best unused image, record it, optionally download it
  generate  make an image with a free AI generator
  check     test every source with the configured keys
  serve     expose search and pick over HTTP for pipelines in any language

Keys are read from the environment, then from
~/.config/blog-image-finder/keys.env (KEY=value lines). Sources without a key
are skipped; Openverse and Wikimedia need none.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0"
USER_AGENT = f"Mozilla/5.0 (compatible; blog-image-finder/{VERSION}; +https://github.com/koffih/claude-skill-blog-image-finder)"
CONFIG_FILE = Path(os.environ.get("BIF_CONFIG", "~/.config/blog-image-finder/keys.env")).expanduser()
LEDGER_FILE = Path(os.environ.get("BIF_LEDGER", "~/.local/share/blog-image-finder/used.jsonl")).expanduser()
TIMEOUT = 20

# Perceived photo quality and license simplicity, used as one ranking factor.
PROVIDER_WEIGHT = {"pexels": 1.0, "unsplash": 0.95, "pixabay": 0.85, "openverse": 0.6, "wikimedia": 0.5}
SEARCH_PROVIDERS = list(PROVIDER_WEIGHT)
GENERATORS = ["nvidia", "cloudflare", "pollinations"]

STOPWORDS = set(
    "a an the and or of for to in on with at by from into over under about as is are be this that "
    "how why what your our my their its best top guide tips new".split()
)


# ---------------------------------------------------------------- config

def load_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if v:
            keys[k] = v
    return {k: v for k, v in keys.items() if v}


KEYS = load_keys()


def key(name: str) -> str | None:
    return KEYS.get(name)


# ---------------------------------------------------------------- http

class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def http(url: str, *, params: dict | None = None, headers: dict | None = None,
         data: dict | None = None, timeout: int = TIMEOUT, raw: bool = False):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if raw:
                return payload, resp.headers.get("Content-Type", "")
            return json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from None


# ---------------------------------------------------------------- model

@dataclass
class Image:
    provider: str
    id: str
    image_url: str            # full size, what gets downloaded
    preview_url: str          # small, for a quick look
    source_url: str           # page on the provider site
    width: int
    height: int
    author: str = ""
    author_url: str = ""
    license: str = ""
    license_url: str = ""
    attribution: str = ""
    text: str = ""            # title, alt, tags: what the relevance score reads
    query: str = ""
    rank: int = 0             # position in the provider's own results
    hotlink_only: bool = False  # Unsplash: must be displayed from its own CDN
    download_ping: str = ""   # Unsplash: URL to call when the photo is used
    score: float = 0.0
    score_detail: dict = field(default_factory=dict)
    judge: dict = field(default_factory=dict)  # visual check: fit, place, reason
    alt: dict = field(default_factory=dict)    # alt text per language, from the judge

    @property
    def orientation(self) -> str:
        if not self.width or not self.height:
            return "unknown"
        r = self.width / self.height
        return "landscape" if r > 1.1 else "portrait" if r < 0.9 else "square"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["orientation"] = self.orientation
        d.pop("download_ping")
        return d


# ---------------------------------------------------------------- providers

def pexels(q: str, o: dict) -> list[Image]:
    k = key("PEXELS_API_KEY")
    if not k:
        return []
    params = {"query": q, "per_page": o["per_provider"]}
    if o["orientation"] != "any":
        params["orientation"] = o["orientation"]
    d = http("https://api.pexels.com/v1/search", params=params, headers={"Authorization": k})
    out = []
    for i, p in enumerate(d.get("photos", [])):
        src = p.get("src", {})
        out.append(Image(
            provider="pexels", id=str(p["id"]), image_url=src.get("original", ""),
            preview_url=src.get("medium", ""), source_url=p.get("url", ""),
            width=p.get("width", 0), height=p.get("height", 0),
            author=p.get("photographer", ""), author_url=p.get("photographer_url", ""),
            license="Pexels License", license_url="https://www.pexels.com/license/",
            attribution=f"Photo by {p.get('photographer', '')} on Pexels",
            text=p.get("alt", "") or "", rank=i,
        ))
    return out


def pixabay(q: str, o: dict) -> list[Image]:
    k = key("PIXABAY_API_KEY")
    if not k:
        return []
    params = {
        "key": k, "q": q[:100], "per_page": max(3, o["per_provider"]), "safesearch": "true",
        "image_type": "photo" if o["kind"] == "photo" else "all", "min_width": o["min_width"],
        "orientation": {"landscape": "horizontal", "portrait": "vertical"}.get(o["orientation"], "all"),
    }
    d = http("https://pixabay.com/api/", params=params)
    out = []
    for i, p in enumerate(d.get("hits", [])):
        # largeImageURL is capped at 1280 px on the long side without full API access.
        w, h = p.get("imageWidth", 0), p.get("imageHeight", 0)
        f = min(1.0, 1280 / max(w, h, 1))
        out.append(Image(
            provider="pixabay", id=str(p["id"]), image_url=p.get("largeImageURL", ""),
            preview_url=p.get("webformatURL", ""), source_url=p.get("pageURL", ""),
            width=round(w * f), height=round(h * f),
            author=p.get("user", ""),
            author_url=f"https://pixabay.com/users/{p.get('user', '')}-{p.get('user_id', '')}/",
            license="Pixabay Content License", license_url="https://pixabay.com/service/license-summary/",
            attribution=f"Image by {p.get('user', '')} from Pixabay",
            text=p.get("tags", ""), rank=i,
        ))
    return out


def unsplash(q: str, o: dict) -> list[Image]:
    k = key("UNSPLASH_ACCESS_KEY")
    if not k:
        return []
    params = {"query": q, "per_page": min(30, o["per_provider"]), "content_filter": "high"}
    if o["orientation"] != "any":
        params["orientation"] = "squarish" if o["orientation"] == "square" else o["orientation"]
    d = http("https://api.unsplash.com/search/photos", params=params,
             headers={"Authorization": f"Client-ID {k}", "Accept-Version": "v1"})
    app = urllib.parse.quote(key("UNSPLASH_APP_NAME") or "blog_image_finder")
    utm = f"?utm_source={app}&utm_medium=referral"
    out = []
    for i, p in enumerate(d.get("results", [])):
        u = p.get("user", {})
        urls = p.get("urls", {})
        name = u.get("name", "")
        out.append(Image(
            provider="unsplash", id=p["id"],
            image_url=urls.get("raw", "") + "&w=2400&q=80&fm=jpg",
            preview_url=urls.get("small", ""), source_url=p.get("links", {}).get("html", "") + utm,
            width=p.get("width", 0), height=p.get("height", 0),
            author=name, author_url=u.get("links", {}).get("html", "") + utm,
            license="Unsplash License", license_url="https://unsplash.com/license",
            attribution=f"Photo by {name} on Unsplash",
            text=" ".join(filter(None, [p.get("alt_description"), p.get("description")])),
            rank=i, hotlink_only=True, download_ping=p.get("links", {}).get("download_location", ""),
        ))
    return out


# Openverse licenses allowed per policy. "strict": no share-alike, no NC, no ND.
OPENVERSE_LICENSES = {"strict": "cc0,pdm,by", "attribution-sa": "cc0,pdm,by,by-sa"}


def openverse(q: str, o: dict) -> list[Image]:
    params = {"q": q, "page_size": min(20, o["per_provider"]),
              "license": OPENVERSE_LICENSES[o["license_policy"]], "mature": "false"}
    if o["orientation"] != "any":
        params["aspect_ratio"] = {"landscape": "wide", "portrait": "tall", "square": "square"}[o["orientation"]]
    if o["kind"] == "photo":
        params["category"] = "photograph"
    d = http("https://api.openverse.org/v1/images/", params=params)
    out = []
    for i, p in enumerate(d.get("results", [])):
        lic = p.get("license", "")
        ver = p.get("license_version") or ""
        lic_name = "Public Domain Mark" if lic == "pdm" else "CC0" if lic == "cc0" else f"CC {lic.upper()} {ver}".strip()
        tags = " ".join(t.get("name", "") for t in (p.get("tags") or []))
        out.append(Image(
            provider="openverse", id=p["id"], image_url=p.get("url", ""),
            preview_url=p.get("thumbnail", ""), source_url=p.get("foreign_landing_url", ""),
            width=p.get("width") or 0, height=p.get("height") or 0,
            author=p.get("creator") or "", author_url=p.get("creator_url") or "",
            license=lic_name, license_url=p.get("license_url") or "",
            attribution=p.get("attribution") or "",
            text=f"{p.get('title', '')} {tags}", rank=i,
        ))
    return out


_TAG = re.compile(r"<[^>]+>")


def _wm_license_ok(short: str, policy: str) -> bool:
    s = short.lower().replace("-", " ")
    if "nc" in s.split() or "nd" in s.split() or "fair use" in s:
        return False
    if "cc0" in s or "public domain" in s or s.startswith("pd"):
        return True
    if "cc by sa" in s:
        return policy == "attribution-sa"
    return s.startswith("cc by")


def wikimedia(q: str, o: dict) -> list[Image]:
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{q} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": min(20, o["per_provider"]), "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
    }
    d = http("https://commons.wikimedia.org/w/api.php", params=params)
    pages = sorted(d.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for i, p in enumerate(pages):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata", {})
        short = meta.get("LicenseShortName", {}).get("value", "")
        if not _wm_license_ok(short, o["license_policy"]):
            continue
        artist = _TAG.sub("", meta.get("Artist", {}).get("value", "")).strip()
        title = p.get("title", "").removeprefix("File:")
        desc = _TAG.sub("", meta.get("ImageDescription", {}).get("value", ""))[:300]
        out.append(Image(
            provider="wikimedia", id=str(p.get("pageid")),
            image_url=info.get("thumburl") or info.get("url", ""),
            preview_url=info.get("thumburl", ""), source_url=info.get("descriptionurl", ""),
            width=info.get("thumbwidth") or info.get("width", 0),
            height=info.get("thumbheight") or info.get("height", 0),
            author=artist, license=short, license_url=meta.get("LicenseUrl", {}).get("value", ""),
            attribution=f"{title} by {artist or 'unknown'}, {short}, via Wikimedia Commons",
            text=f"{title} {desc}", rank=i,
        ))
    return out


FETCHERS = {"pexels": pexels, "pixabay": pixabay, "unsplash": unsplash,
            "openverse": openverse, "wikimedia": wikimedia}
NEEDS_KEY = {"pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY", "unsplash": "UNSPLASH_ACCESS_KEY"}


# ---------------------------------------------------------------- ranking

def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in STOPWORDS}


def score(img: Image, o: dict, n_results: int) -> None:
    q = tokens(img.query)
    t = tokens(img.text)
    # Prefix match so "dancers" meets "dance" and "offices" meets "office".
    hits = sum(1 for w in q if any(x.startswith(w[:5]) or w.startswith(x[:5]) for x in t if len(x) > 3))
    relevance = hits / len(q) if q else 0.5
    rank = 1 - img.rank / max(n_results, 1)
    resolution = min(img.width / max(o["target_width"], 1), 1.0) if img.width else 0.3
    want = o["orientation"]
    if want == "any":
        orient = 1.0
    elif img.orientation == want:
        orient = 1.0
        if want == "landscape" and img.height:
            orient -= min(abs(img.width / img.height - o["ratio"]) / o["ratio"], 0.5)
    else:
        orient = 0.2  # still usable after a crop, but a poor start
    weight = PROVIDER_WEIGHT.get(img.provider, 0.5)
    img.score_detail = {k: round(v, 3) for k, v in
                        dict(relevance=relevance, rank=rank, resolution=resolution,
                             orientation=orient, provider=weight).items()}
    img.score = round(35 * relevance + 15 * rank + 20 * resolution + 15 * orient + 15 * weight, 2)


def shape_ok(w: int, h: int, o: dict) -> bool:
    """Strict orientation: a portrait photo cropped to 16:9 keeps a sliver of the subject."""
    want = o["orientation"]
    if want == "any" or not w or not h:
        return True
    r = w / h
    if want == "landscape":
        return r >= o["min_ratio"]
    if want == "portrait":
        return r <= 1 / o["min_ratio"]
    return 0.9 <= r <= 1.1


# ---------------------------------------------------------------- ledger

_ledger_lock = threading.Lock()


def used_ids(site: str) -> set[str]:
    if not LEDGER_FILE.exists():
        return set()
    out = set()
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if site == "*" or r.get("site", "") in (site, "*"):
            out.add(f"{r['provider']}:{r['id']}")
    return out


def record(img: Image, site: str, extra: dict) -> None:
    with _ledger_lock:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"provider": img.provider, "id": img.id, "site": site,
                                "query": img.query, "source_url": img.source_url,
                                "date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                **extra}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- search

def run_search(queries: list[str], o: dict) -> tuple[list[Image], dict]:
    providers = [p for p in o["providers"] if p in FETCHERS and (p not in NEEDS_KEY or key(NEEDS_KEY[p]))]
    if o["download"]:
        # Unsplash photos must be shown from Unsplash's CDN, never re-hosted.
        providers = [p for p in providers if p != "unsplash"]
    report: dict[str, dict] = {}
    results: list[Image] = []

    def one(p: str, q: str):
        t0 = time.monotonic()
        try:
            imgs = FETCHERS[p](q, o)
            err = None
        except Exception as e:  # one source down never stops the others
            imgs, err = [], str(e)[:200]
        for im in imgs:
            im.query = q
        return p, q, imgs, err, time.monotonic() - t0

    jobs = [(p, q) for q in queries for p in providers]
    with ThreadPoolExecutor(max_workers=min(12, len(jobs) or 1)) as pool:
        for p, q, imgs, err, secs in pool.map(lambda a: one(*a), jobs):
            r = report.setdefault(p, {"results": 0, "errors": [], "seconds": 0.0})
            r["results"] += len(imgs)
            r["seconds"] = round(max(r["seconds"], secs), 2)
            if err:
                r["errors"].append(f"{q!r}: {err}")
            for im in imgs:
                score(im, o, len(imgs))
            results.extend(imgs)

    skipped = used_ids(o["site"]) if o["skip_used"] else set()
    seen: set[str] = set()
    kept = []
    for im in sorted(results, key=lambda i: i.score, reverse=True):
        uid = f"{im.provider}:{im.id}"
        if uid in seen or uid in skipped or not im.image_url:
            continue
        if im.width and im.width < o["min_width"]:
            continue
        if not shape_ok(im.width, im.height, o):
            continue
        seen.add(uid)
        kept.append(im)
    return kept, {"providers": report, "skipped_as_used": len(skipped & {f"{i.provider}:{i.id}" for i in results})}


# ---------------------------------------------------------------- image processing

# name -> (width, height, format). "blog" is what a blog template needs: the header
# image, the card in article lists, and the Open Graph image that Facebook, LinkedIn
# and WhatsApp show (JPEG, because WebP previews are unreliable on those networks).
VARIANT_PRESETS = {
    "blog": "cover:1600x900:webp,card:800x450:webp,og:1200x630:jpg",
}


def parse_variants(spec: str | None, o: dict) -> list[tuple[str, int, int, str]]:
    """"single" (or empty) keeps the historical one-file output; otherwise name:WxH:fmt,..."""
    if not spec or spec == "single":
        return []
    out = []
    for part in VARIANT_PRESETS.get(spec, spec).split(","):
        name, size, fmt = part.strip().split(":")
        w, h = size.lower().split("x")
        out.append((name, int(w), int(h), fmt))
    return out


def _encode(im, fmt: str, quality: int) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "webp":
        im.save(buf, "WEBP", quality=quality, method=6)
    elif fmt == "avif":
        im.save(buf, "AVIF", quality=quality)
    else:
        fmt = "jpg"
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue(), fmt


def _crop_to(im, ratio: float):
    w, h = im.size
    target_h = int(w / ratio)
    if h > target_h:
        top = int((h - target_h) * 0.35)  # subjects sit a little above center
        return im.crop((0, top, w, top + target_h))
    if h < target_h:
        target_w = int(h * ratio)
        left = (w - target_w) // 2
        return im.crop((left, 0, left + target_w, h))
    return im


def open_rgb(data: bytes):
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None
    return PILImage.open(io.BytesIO(data)).convert("RGB")


def process(data: bytes, o: dict) -> tuple[bytes, str, int, int]:
    im = open_rgb(data)
    if im is None:
        return data, "orig", 0, 0
    from PIL import Image as PILImage
    if o["crop"] and o["orientation"] == "landscape":
        im = _crop_to(im, o["ratio"])
    if im.width > o["target_width"]:
        im = im.resize((o["target_width"], round(im.height * o["target_width"] / im.width)), PILImage.LANCZOS)
    blob, fmt = _encode(im, o["format"], o["quality"])
    return blob, fmt, im.width, im.height


def render_variant(im, w: int, h: int, fmt: str, quality: int) -> tuple[bytes, str]:
    """Crop to the exact ratio, then resize to the exact size so every page lays out the same."""
    from PIL import Image as PILImage
    return _encode(_crop_to(im, w / h).resize((w, h), PILImage.LANCZOS), fmt, quality)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "image"


def save(img: Image, data: bytes, o: dict) -> dict:
    out_dir = Path(o["out_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{slugify(o.get('name') or img.query)}-{img.provider}-{hashlib.sha1(img.id.encode()).hexdigest()[:8]}"
    variants = parse_variants(o.get("variants"), o)
    im = open_rgb(data) if variants else None
    files: dict[str, dict] = {}
    if im is not None:
        # The listed size can lie (Pixabay, Wikimedia thumbnails): judge the real pixels.
        if not shape_ok(im.width, im.height, o):
            raise HttpError(0, f"real size {im.width}x{im.height} is not {o['orientation']}")
        for name, w, h, fmt in variants:
            blob, ext = render_variant(im, w, h, fmt, o["quality"])
            path = out_dir / f"{stem}-{name}.{ext}"
            path.write_bytes(blob)
            files[name] = {"file": str(path), "width": w, "height": h, "bytes": len(blob), "format": ext}
        main = next(iter(files.values()))
        path, size = Path(main["file"]), main["bytes"]
    else:
        blob, fmt, w, h = process(data, o)
        ext = {b"\xff\xd8": "jpg", b"\x89P": "png", b"RI": "webp"}.get(blob[:2], "jpg") if fmt == "orig" else fmt
        path = out_dir / f"{stem}.{ext}"
        path.write_bytes(blob)
        size = len(blob)
    if files:
        w, h = main["width"], main["height"]
        orig_w, orig_h = im.width, im.height
    else:
        orig_w, orig_h = img.width, img.height
    meta = {**{k: v for k, v in img.as_dict().items() if k not in ("score_detail", "width", "height", "orientation")},
            "file": str(path), "bytes": size, "width": w or img.width, "height": h or img.height,
            "original_width": orig_w, "original_height": orig_h,
            "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if files:
        meta["files"] = files
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def fetch_bytes(url: str) -> bytes:
    data, ctype = http(url, raw=True, timeout=60, headers={"Accept": "image/*"})
    if not ctype.startswith("image/"):
        raise HttpError(0, f"not an image: {ctype}")
    return data


# ---------------------------------------------------------------- visual judge

JUDGE_PROMPT = """You pick the header photo of a blog article. The reader must not be misled.

Article:
{context}

Below are {n} candidate photos, numbered in order. For EACH photo, judge from what you SEE:
- fit: 0 to 10, how well it illustrates THIS article. 8+ is a photo an editor would
  publish; 5 or less is generic, off-topic, or wrong.
- place: where it visibly seems to be ("West Africa", "Europe", "North America",
  "unknown"...). If the article is about a specific region and the photo clearly shows
  another one (architecture, street signs, landscape, people, vehicles), fit is 3 at most.
  A photo with NO visible regional cue is not a mismatch: grade it on its subject alone,
  it can reach 8. A photo that visibly shows the article's own region earns +1.
- wrong_region: true when the article is about a region and the photo visibly shows
  another one. This is a hard veto, whatever the fit.
- not_a_photo: true for a 3D render, CGI interior, illustration, drawing or heavy
  digital art. Renders often pass as photos: look for too-perfect surfaces, uniform
  lighting and no dust or wear. Hard veto when a photo is expected.
- Also 3 at most for: visible watermark or large text, a collage, a screenshot, an
  illustration when a photo is expected, a recognisable famous landmark of another city.
- reason: a few words.
- alt: short factual alt text describing what the photo shows (not the article), in
  each of these languages: {langs}. Never claim a place the photo does not prove.

Answer with JSON only:
{{"images": [{{"i": 1, "fit": 7, "place": "...", "wrong_region": false, "not_a_photo": false, "reason": "...", "alt": {{{alt_keys}}}}}]}}"""


def _judge_config() -> tuple[str, str, str] | None:
    url = key("BIF_JUDGE_URL") or "https://api.moonshot.ai/v1"
    k = key("BIF_JUDGE_KEY") or key("MOONSHOT_API_KEY")
    model = key("BIF_JUDGE_MODEL") or "kimi-k2.6"
    return (url.rstrip("/"), k, model) if k else None


def _thumb_data_url(url: str) -> str:
    data = fetch_bytes(url)
    im = open_rgb(data)
    if im is not None:
        im.thumbnail((512, 512))
        data, _ = _encode(im, "jpg", 80)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _json_in(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def judge(cands: list[Image], o: dict, report: dict) -> list[Image]:
    """Look at the best candidates with a vision model and keep those that fit.

    Any OpenAI compatible endpoint that accepts image_url data URLs works
    (Moonshot kimi-k2.6 by default). Returns the accepted images, best first.
    """
    cfg = _judge_config()
    if not cfg:
        report["judge"] = {"error": "no judge key (BIF_JUDGE_KEY or MOONSHOT_API_KEY)"}
        return []
    url, k, model = cfg
    langs = o["alt_langs"]
    accepted: list[Image] = []
    rounds = []
    top = o["judge_top"]
    for start in range(0, min(len(cands), top * o["judge_rounds"]), top):
        batch = cands[start:start + top]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            thumbs = list(pool.map(lambda c: _safe(_thumb_data_url, c.preview_url or c.image_url), batch))
        shown = [(c, t) for c, t in zip(batch, thumbs) if t]
        if not shown:
            continue
        content = [{"type": "text", "text": JUDGE_PROMPT.format(
            context=o["context"] or " / ".join({c.query for c in cands}), n=len(shown),
            langs=", ".join(langs), alt_keys=", ".join(f'"{l}": "..."' for l in langs))}]
        for i, (_, t) in enumerate(shown, 1):
            content += [{"type": "text", "text": f"Photo {i}:"}, {"type": "image_url", "image_url": {"url": t}}]
        body = {"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": content}]}
        if "moonshot" in url:
            body["thinking"] = {"type": "disabled"}  # a 0-10 grade needs no reasoning pass
        t0 = time.monotonic()
        try:
            d = http(f"{url}/chat/completions", headers={"Authorization": f"Bearer {k}"}, data=body, timeout=120)
            verdicts = _json_in(d["choices"][0]["message"]["content"]).get("images", [])
        except Exception as e:
            rounds.append({"shown": len(shown), "error": str(e)[:200]})
            continue
        rounds.append({"shown": len(shown), "seconds": round(time.monotonic() - t0, 1), "usage": d.get("usage")})
        for v in verdicts:
            try:
                c = shown[int(v["i"]) - 1][0]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            c.judge = {"fit": v.get("fit"), "place": v.get("place", ""), "wrong_region": bool(v.get("wrong_region")),
                       "not_a_photo": bool(v.get("not_a_photo")),
                       "reason": v.get("reason", ""), "model": model}
            c.alt = v.get("alt") or {}
            # The fit alone let a "clearly New York" photo through at 6: the region veto is separate.
            if isinstance(v.get("fit"), (int, float)) and v["fit"] >= o["judge_min"] and not c.judge["wrong_region"] \
                    and not (o["kind"] == "photo" and c.judge["not_a_photo"]):
                accepted.append(c)
        if accepted:
            break
    report["judge"] = {"model": model, "rounds": rounds, "accepted": len(accepted),
                       "rejected": [f"{c.provider}:{c.id} fit {c.judge.get('fit')}{' wrong region' if c.judge.get('wrong_region') else ''}"
                                    f"{' not a photo' if c.judge.get('not_a_photo') else ''}"
                                    f" ({c.judge.get('reason')})"
                                    for c in cands if c.judge and c not in accepted]}
    # The judge's fit decides; the heuristic score breaks ties.
    return sorted(accepted, key=lambda c: (c.judge["fit"], c.score), reverse=True)


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


# ---------------------------------------------------------------- pick

def pick(queries: list[str], o: dict) -> dict:
    cands, report = run_search(queries, o)
    if o["judge"] and cands:
        cands = judge(cands, o, report)
    for img in cands:
        result = {"image": img.as_dict()}
        if o["download"]:
            try:
                result["saved"] = save(img, fetch_bytes(img.image_url), o)
            except Exception as e:
                report.setdefault("download_errors", []).append(f"{img.provider}:{img.id}: {str(e)[:150]}")
                continue  # broken link: try the next best
        if img.download_ping:
            try:
                http(img.download_ping, headers={"Authorization": f"Client-ID {key('UNSPLASH_ACCESS_KEY')}"})
            except Exception:
                pass
        if o["record"]:
            record(img, o["site"], {"file": result.get("saved", {}).get("file", "")})
        result["alternatives"] = [c.as_dict() for c in cands[1:1 + o["alternatives"]] if c is not img]
        return {"ok": True, **result, "report": report}
    if o["generate_fallback"]:
        gen = generate(o["generate_prompt"] or queries[0], o)
        if gen.get("ok"):
            gen["report"] = report
            gen["fallback"] = "no free stock image matched; generated instead"
            return gen
        report["generation"] = gen
    return {"ok": False, "error": "no usable image found", "report": report}


# ---------------------------------------------------------------- generation

NVIDIA_SIZES = [768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344]


def _nearest(v: int, allowed: list[int]) -> int:
    return min(allowed, key=lambda a: abs(a - v))


def gen_nvidia(prompt: str, w: int, h: int, seed: int) -> bytes:
    k = key("NVIDIA_API_KEY")
    if not k:
        raise HttpError(0, "NVIDIA_API_KEY not set")
    d = http("https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-schnell",
             headers={"Authorization": f"Bearer {k}"}, timeout=240,
             data={"prompt": prompt, "width": _nearest(w, NVIDIA_SIZES), "height": _nearest(h, NVIDIA_SIZES),
                   "steps": 4, "seed": seed})
    arts = d.get("artifacts") or []
    if not arts or arts[0].get("finishReason") not in (None, "SUCCESS"):
        raise HttpError(0, f"no image returned ({arts[0].get('finishReason') if arts else 'empty'})")
    return base64.b64decode(arts[0]["base64"])


def gen_cloudflare(prompt: str, w: int, h: int, seed: int) -> bytes:
    acct, tok = key("CLOUDFLARE_ACCOUNT_ID"), key("CLOUDFLARE_API_TOKEN")
    if not (acct and tok):
        raise HttpError(0, "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN not set")
    d = http(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell",
             headers={"Authorization": f"Bearer {tok}"}, timeout=120,
             data={"prompt": prompt, "steps": 8, "seed": seed})
    img = (d.get("result") or {}).get("image")
    if not img:
        raise HttpError(0, f"no image returned: {str(d)[:150]}")
    return base64.b64decode(img)


def gen_pollinations(prompt: str, w: int, h: int, seed: int) -> bytes:
    tok = key("POLLINATIONS_TOKEN")
    p = urllib.parse.quote(prompt)
    params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": "flux"}
    if tok:
        url, headers = f"https://gen.pollinations.ai/image/{p}", {"Authorization": f"Bearer {tok}"}
    else:
        url, headers = f"https://image.pollinations.ai/prompt/{p}", {}
    data, ctype = http(url, params=params, headers={**headers, "Accept": "image/*"}, timeout=150, raw=True)
    if not ctype.startswith("image/"):
        raise HttpError(0, f"not an image: {data[:150]!r}")
    return data


GEN_FUNCS = {"nvidia": gen_nvidia, "cloudflare": gen_cloudflare, "pollinations": gen_pollinations}
GEN_MODEL = {"nvidia": "FLUX.1 [schnell] on NVIDIA", "cloudflare": "FLUX.1 [schnell] on Cloudflare Workers AI",
             "pollinations": "Pollinations"}


def generate(prompt: str, o: dict) -> dict:
    w = o["target_width"]
    h = round(w / o["ratio"]) if o["orientation"] == "landscape" else w if o["orientation"] == "square" else round(w * o["ratio"])
    w, h = min(w, 1344), min(h, 1344)
    seed = o.get("seed") or int(hashlib.sha1(prompt.encode()).hexdigest()[:6], 16)
    full = f"{prompt}. {o['style']}".strip(". ")
    errors = {}
    for g in o["generators"]:
        try:
            data = GEN_FUNCS[g](full, w, h, seed)
        except Exception as e:
            errors[g] = str(e)[:200]
            continue
        img = Image(provider=f"ai:{g}", id=f"{seed}-{hashlib.sha1(full.encode()).hexdigest()[:10]}",
                    image_url="", preview_url="", source_url="", width=w, height=h,
                    author=GEN_MODEL[g], license="AI-generated, see provider terms",
                    attribution="", text=full, query=prompt)
        # Generators do not all honour the requested size (Cloudflare returns a square):
        # crop like any photo, but never reject on shape.
        meta = save(img, data, dict(o, orientation="any") if o.get("variants") not in (None, "single") else o)
        if o["record"]:
            record(img, o["site"], {"file": meta["file"]})
        return {"ok": True, "image": img.as_dict(), "saved": meta, "generation_errors": errors}
    return {"ok": False, "error": "every generator failed", "generation_errors": errors}


# ---------------------------------------------------------------- check

def check(o: dict) -> list[dict]:
    rows = []
    o = dict(o, per_provider=3, download=False)
    for p in SEARCH_PROVIDERS:
        need = NEEDS_KEY.get(p)
        if need and not key(need):
            rows.append({"source": p, "status": "no key", "needs": need})
            continue
        t0 = time.monotonic()
        try:
            n = len(FETCHERS[p]("office desk", o))
            rows.append({"source": p, "status": "ok" if n else "empty", "results": n,
                         "seconds": round(time.monotonic() - t0, 1)})
        except Exception as e:
            rows.append({"source": p, "status": "error", "error": str(e)[:160]})
    for g in GENERATORS:
        need = {"nvidia": "NVIDIA_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN"}.get(g)
        rows.append({"source": f"ai:{g}", "status": "configured" if (not need or key(need)) else "no key",
                     "needs": need or "nothing (POLLINATIONS_TOKEN optional)",
                     "note": "not called by check; use `generate` to test"})
    return rows


# ---------------------------------------------------------------- options

def options(ns) -> dict:
    ratio = {"16:9": 16 / 9, "3:2": 1.5, "4:3": 4 / 3, "1.91:1": 1.91, "1:1": 1.0, "2:1": 2.0}
    return {
        "providers": [p.strip() for p in ns.providers.split(",") if p.strip()],
        "orientation": ns.orientation, "kind": ns.kind, "min_width": ns.min_width,
        "target_width": ns.width, "ratio": ratio.get(ns.ratio) or float(ns.ratio),
        "license_policy": ns.license, "per_provider": ns.per_provider, "site": ns.site,
        "skip_used": not ns.allow_reuse, "download": bool(getattr(ns, "download", False)),
        "out_dir": getattr(ns, "out", "./images"), "format": getattr(ns, "format", "webp"),
        "quality": getattr(ns, "quality", 82), "crop": not getattr(ns, "no_crop", False),
        "record": not getattr(ns, "dry_run", False), "alternatives": getattr(ns, "alternatives", 3),
        "generate_fallback": getattr(ns, "generate_fallback", False),
        "generate_prompt": getattr(ns, "prompt", None), "name": getattr(ns, "name", None),
        "generators": [g.strip() for g in getattr(ns, "generators", ",".join(GENERATORS)).split(",") if g.strip()],
        "style": getattr(ns, "style", ""), "seed": getattr(ns, "seed", None),
        "min_ratio": ns.min_ratio, "variants": getattr(ns, "variants", "single"),
        "judge": getattr(ns, "judge", False), "context": getattr(ns, "context", "") or "",
        "judge_min": getattr(ns, "judge_min", 7), "judge_top": getattr(ns, "judge_top", 6),
        "judge_rounds": getattr(ns, "judge_rounds", 2),
        "alt_langs": [l.strip() for l in getattr(ns, "alt_langs", "en").split(",") if l.strip()],
    }


def common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("-q", "--query", action="append", default=[],
                    help="search query, English works best; repeat for fallbacks (tried together, best wins)")
    ap.add_argument("--providers", default=",".join(SEARCH_PROVIDERS))
    ap.add_argument("--orientation", choices=["landscape", "portrait", "square", "any"], default="landscape")
    ap.add_argument("--kind", choices=["photo", "any"], default="photo", help="photo, or also illustrations")
    ap.add_argument("--min-width", type=int, default=1200)
    ap.add_argument("--width", type=int, default=1600, help="target width after resize")
    ap.add_argument("--ratio", default="16:9", help="16:9, 3:2, 4:3, 1.91:1, 2:1, 1:1 or a number")
    ap.add_argument("--license", choices=["strict", "attribution-sa"], default="strict",
                    help="strict: CC0, public domain, CC BY and provider licenses; attribution-sa also allows CC BY-SA")
    ap.add_argument("--per-provider", type=int, default=15)
    ap.add_argument("--site", default="default", help="ledger namespace: images are not reused within a site; * for all")
    ap.add_argument("--allow-reuse", action="store_true", help="ignore the ledger")
    ap.add_argument("--min-ratio", type=float, default=1.3,
                    help="landscape means width/height at least this (portrait: the inverse); others are dropped")


def download_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--download", action="store_true", help="download, crop, resize, convert (excludes Unsplash)")
    ap.add_argument("--out", default="./images")
    ap.add_argument("--name", help="file name stem, for example the article slug")
    ap.add_argument("--format", choices=["webp", "jpg", "avif"], default="webp")
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="do not record the pick in the ledger")
    ap.add_argument("--variants", default="single",
                    help="single (one file), blog (cover 1600x900 webp, card 800x450 webp, og 1200x630 jpg) "
                         "or name:WxH:fmt,...")


def judge_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--judge", action="store_true",
                    help="let a vision model look at the best candidates and reject those that do not fit")
    ap.add_argument("--context", default="", help="what the article is about: title, summary, place")
    ap.add_argument("--judge-min", type=int, default=7, help="minimum fit out of 10")
    ap.add_argument("--judge-top", type=int, default=6, help="candidates shown per round")
    ap.add_argument("--judge-rounds", type=int, default=2)
    ap.add_argument("--alt-langs", default="en", help="alt text languages, for example fr,en")


def gen_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--generators", default=",".join(GENERATORS))
    ap.add_argument("--style", default="editorial photograph, natural light, realistic, no text, no watermark")
    ap.add_argument("--seed", type=int)


# ---------------------------------------------------------------- server

def serve(ns) -> None:
    base = options(ns)
    token = key("BIF_SERVER_TOKEN")

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"ok": False, "error": "unauthorized"})
                return False
            return True

        def _opts(self, params: dict) -> tuple[list[str], dict]:
            o = dict(base)
            q = params.get("queries") or params.get("q") or params.get("query") or []
            queries = [q] if isinstance(q, str) else list(q)
            for k in ("orientation", "kind", "site", "format", "name", "license_policy", "generate_prompt", "style",
                      "variants", "context"):
                if k in params:
                    o[k] = params[k]
            for k in ("min_width", "target_width", "per_provider", "quality", "alternatives", "judge_min", "judge_top",
                      "judge_rounds"):
                if k in params:
                    o[k] = int(params[k])
            if "ratio" in params:
                o["ratio"] = float(params["ratio"])
            if "min_ratio" in params:
                o["min_ratio"] = float(params["min_ratio"])
            if "alt_langs" in params:
                a = params["alt_langs"]
                o["alt_langs"] = a.split(",") if isinstance(a, str) else list(a)
            if "providers" in params:
                p = params["providers"]
                o["providers"] = p.split(",") if isinstance(p, str) else p
            for k in ("download", "generate_fallback", "dry_run", "allow_reuse", "judge"):
                if k in params:
                    v = params[k] in (True, "1", "true", "yes")
                    if k == "dry_run":
                        o["record"] = not v
                    elif k == "allow_reuse":
                        o["skip_used"] = not v
                    else:
                        o[k] = v
            return queries, o

        def do_GET(self) -> None:
            u = urllib.parse.urlparse(self.path)
            if u.path == "/health":
                return self._send(200, {"ok": True, "version": VERSION})
            if not self._auth():
                return
            if u.path == "/v1/search":
                params = {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(u.query).items()}
                queries, o = self._opts(params)
                if not queries:
                    return self._send(400, {"ok": False, "error": "q is required"})
                cands, report = run_search(queries, o)
                return self._send(200, {"ok": True, "results": [c.as_dict() for c in cands[:30]], "report": report})
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if not self._auth():
                return
            try:
                params = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "invalid JSON"})
            queries, o = self._opts(params)
            if self.path == "/v1/pick":
                if not queries:
                    return self._send(400, {"ok": False, "error": "queries is required"})
                res = pick(queries, o)
                return self._send(200 if res["ok"] else 404, res)
            if self.path == "/v1/generate":
                res = generate(params.get("prompt") or (queries[0] if queries else ""), o)
                return self._send(200 if res["ok"] else 502, res)
            self._send(404, {"ok": False, "error": "not found"})

        def log_message(self, fmt, *args) -> None:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    srv = ThreadingHTTPServer((ns.host, ns.port), H)
    print(f"blog-image-finder {VERSION} listening on http://{ns.host}:{ns.port}"
          f"{' (token required)' if token else ''}", file=sys.stderr)
    srv.serve_forever()


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="rank candidates, print JSON")
    common(s)
    s.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("pick", help="choose the best unused image")
    common(p)
    download_opts(p)
    gen_opts(p)
    judge_opts(p)
    p.add_argument("--alternatives", type=int, default=3)
    p.add_argument("--generate-fallback", action="store_true", help="generate with AI when no stock image fits")
    p.add_argument("--prompt", help="generation prompt for the fallback (default: first query)")

    g = sub.add_parser("generate", help="generate an image with a free AI provider")
    common(g)
    download_opts(g)
    gen_opts(g)
    g.add_argument("--prompt", required=True)

    c = sub.add_parser("check", help="test every source with the configured keys")
    common(c)

    v = sub.add_parser("serve", help="HTTP API: GET /v1/search, POST /v1/pick, POST /v1/generate")
    common(v)
    download_opts(v)
    gen_opts(v)
    judge_opts(v)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)

    ns = ap.parse_args()
    if ns.cmd == "serve":
        serve(ns)
        return 0
    o = options(ns)
    if ns.cmd == "check":
        rows = check(o)
        print(json.dumps({"config_file": str(CONFIG_FILE), "ledger": str(LEDGER_FILE), "sources": rows}, indent=2))
        return 0 if any(r["status"] == "ok" for r in rows) else 1
    if ns.cmd == "generate":
        o["download"] = True
        res = generate(ns.prompt, o)
    else:
        if not ns.query:
            ap.error("at least one -q/--query is required")
        if ns.cmd == "search":
            cands, report = run_search(ns.query, o)
            res = {"ok": bool(cands), "results": [c.as_dict() for c in cands[:ns.limit]], "report": report}
        else:
            res = pick(ns.query, o)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
