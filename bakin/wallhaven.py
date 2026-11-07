"""Wallpapers from wallhaven.cc, through its public API
(https://wallhaven.cc/help/api).

SFW, and «sketchy» when asked; never NSFW (the user decided so). An API key
is optional (PLAN 0.13.4): it lives in the Windows Credential Manager, goes
only to Wallhaven's API (never to the image servers, never to logs) and
lets the user list and follow their own collections, private ones too. The API allows 45 requests
a minute: this client keeps one request every 1.5 s (40 a minute), image
files included, and stops on 429 like the other downloaders.

Downloads go to <download folder>/Wallhaven/<search>/, a folder marked like
the music ones so the library lists it as a collection; each wallpaper's
Wallhaven tags become its Bakin tags.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__

API = "https://wallhaven.cc/api/v1"
FOLDER = "Wallhaven"
ARCHIVE = ".wallhaven-archive.txt"
MIN_GAP = 1.5        # seconds between requests (the API allows 45 a minute)
PER_PAGE = 24        # the API's page size
MAX_PER_JOB = 500    # a search can be huge: one job never takes more than this
MAX_TAGS = 20

CATEGORIES = ("general", "anime", "people")
SORTS = ("toplist", "date_added", "relevance", "random", "views", "favorites")
TOP_RANGES = ("1d", "3d", "1w", "1M", "3M", "6M", "1y")
# The colour palette the site accepts.
COLORS = ("660000", "990000", "cc0000", "cc3333", "ea4c88", "993399", "663399", "333399", "0066cc", "0099cc",
          "66cccc", "77cc33", "669900", "336600", "666600", "999900", "cccc33", "ffff00", "ffcc33", "ff9900",
          "ff6600", "cc6633", "996633", "663300", "000000", "999999", "cccccc", "ffffff", "424153")
_SIZE = re.compile(r"^\d{2,5}x\d{2,5}$")
_ID = re.compile(r"^[a-z0-9]{4,10}$")


class WallhavenError(Exception):
    pass


class Blocked(WallhavenError):
    """Wallhaven answered 429: too many requests. Stop and wait."""


_gate = threading.Lock()
_last = 0.0

KEY_TARGET = "bakin-wallhaven"  # its name in the Windows Credential Manager


def key_target() -> str:
    """The Credential Manager name for this copy of Bakin: a test copy
    (BAKIN_HOME) gets its own, so it never reads or overwrites the real one."""
    import hashlib
    import os
    home = os.environ.get("BAKIN_HOME")
    return KEY_TARGET if not home else f"{KEY_TARGET}-test-{hashlib.sha1(home.encode('utf-8')).hexdigest()[:8]}"
_key: str | None = None
_key_read = False


def api_key() -> str | None:
    """The saved API key (read once from the Credential Manager), or None."""
    global _key, _key_read
    if not _key_read:
        from . import credstore, redact
        try:
            _key = credstore.get(key_target())
        except OSError:
            _key = None
        _key_read = True
        redact.register_secret(_key)
    return _key


def forget_key_cache() -> None:
    global _key, _key_read
    _key, _key_read = None, False


def _wait_turn() -> None:
    global _last
    with _gate:
        gap = MIN_GAP - (time.monotonic() - _last)
        if gap > 0:
            time.sleep(gap)
        _last = time.monotonic()


def _open(url: str, timeout: float = 30, key: str | None = None):
    _wait_turn()
    headers = {"User-Agent": f"Bakin/{__version__} (+https://github.com/BladeHoly/bakin)"}
    # the key only ever goes to the API, not to the image servers (w.wallhaven.cc)
    key = key if key is not None else (api_key() if url.startswith(API + "/") else None)
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise Blocked("Wallhaven pidió ir más despacio (demasiados pedidos). Se reintenta solo en un rato.") from None
        if exc.code == 404:
            raise WallhavenError("Ese fondo o esa colección no existe en Wallhaven (o no es pública).") from None
        if exc.code == 401:
            if key:
                raise WallhavenError("Wallhaven no aceptó la clave de API: revísala en Configuración › Descargas.") from None
            raise WallhavenError("Wallhaven pide una cuenta para eso: agrega tu clave de API en Configuración › Descargas.") from None
        raise WallhavenError(f"Wallhaven respondió con un error ({exc.code}). Prueba en un rato.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WallhavenError("No se pudo conectar con Wallhaven: revisa la conexión a internet. "
                             f"({getattr(exc, 'reason', exc)})") from None


def _get(path: str, params: dict | None = None, key: str | None = None) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    with _open(f"{API}{path}{'?' + q if q else ''}", key=key) as r:
        try:
            return json.loads(r.read().decode("utf-8"))
        except ValueError:
            raise WallhavenError("Wallhaven devolvió una respuesta que no se entiende.") from None


# ------------------------------------------------------------------ filters

def clean_filters(f: dict | None) -> dict:
    """What Bakin's dialog (or a pasted search link) asks, as API parameters.
    Purity is only ever SFW, or SFW + sketchy: never NSFW."""
    f = dict(f or {})
    cats = f.get("categories")
    if isinstance(cats, str) and re.fullmatch(r"[01]{3}", cats):
        bits = cats
    else:
        chosen = set(cats or CATEGORIES)
        bits = "".join("1" if c in chosen else "0" for c in CATEGORIES)
    if bits == "000":
        bits = "111"
    purity = str(f.get("purity") or "")
    sketchy = bool(f.get("sketchy")) or (len(purity) == 3 and purity[1] == "1")
    sorting = f.get("sorting") if f.get("sorting") in SORTS else "toplist"
    out = {
        "q": " ".join(str(f.get("q") or "").split())[:200],
        "categories": bits,
        "purity": "110" if sketchy else "100",
        "sorting": sorting,
        "order": "asc" if f.get("order") == "asc" else "desc",
    }
    if sorting == "toplist":
        out["topRange"] = f.get("topRange") if f.get("topRange") in TOP_RANGES else "1M"
    if sorting == "random" and re.fullmatch(r"[a-zA-Z0-9]{6}", str(f.get("seed") or "")):
        out["seed"] = f["seed"]
    if _SIZE.match(str(f.get("atleast") or "")):
        out["atleast"] = f["atleast"]
    ratios = f.get("ratios")
    if isinstance(ratios, str):
        ratios = ratios.split(",")
    ratios = [r for r in (ratios or []) if re.fullmatch(r"\d{1,3}x\d{1,3}", str(r))]
    if ratios:
        out["ratios"] = ",".join(ratios[:6])
    color = str(f.get("colors") or "").lstrip("#").lower()
    if color in COLORS:
        out["colors"] = color
    return out


def _slim(w: dict) -> dict | None:
    if w.get("purity") == "nsfw":  # never, even if the site sent one
        return None
    thumbs = w.get("thumbs") or {}
    return {"id": w.get("id"), "url": w.get("url"), "path": w.get("path"),
            "thumb": thumbs.get("large") or thumbs.get("small"), "small": thumbs.get("small"),
            "width": w.get("dimension_x"), "height": w.get("dimension_y"), "resolution": w.get("resolution"),
            "ratio": w.get("ratio"), "file_size": w.get("file_size"), "file_type": w.get("file_type"),
            "purity": w.get("purity"), "category": w.get("category"), "colors": w.get("colors") or []}


def _page(data: dict) -> dict:
    meta = data.get("meta") or {}
    items = [x for x in (_slim(w) for w in data.get("data") or []) if x and x["id"] and x["path"]]
    return {"items": items, "page": meta.get("current_page", 1), "last_page": meta.get("last_page", 1),
            "total": meta.get("total", len(items)), "seed": meta.get("seed")}


def search(filters: dict | None, page: int = 1) -> dict:
    params = clean_filters(filters)
    params["page"] = max(1, int(page or 1))
    out = _page(_get("/search", params))
    out["filters"] = params
    return out


def collection(user: str, cid: str, page: int = 1, sketchy: bool = False) -> dict:
    if not re.fullmatch(r"[\w.-]{1,40}", user or "") or not str(cid).isdigit():
        raise WallhavenError("Ese enlace de colección no es válido.")
    return _page(_get(f"/collections/{urllib.parse.quote(user)}/{cid}",
                      {"purity": "110" if sketchy else "100", "page": max(1, int(page or 1))}))


def check_key(key: str) -> None:
    """Raises WallhavenError unless Wallhaven accepts this API key."""
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", key or ""):
        raise WallhavenError("Esa no parece una clave de API de Wallhaven (se copia de wallhaven.cc › Settings › Account).")
    from . import redact
    redact.register_secret(key)
    _get("/settings", key=key)


def my_collections() -> list[dict]:
    """The user's own collections, private ones too (needs the API key)."""
    if not api_key():
        raise WallhavenError("Para ver tus colecciones agrega tu clave de API en Configuración › Descargas.")
    data = _get("/collections").get("data") or []
    return [{"id": str(c.get("id")), "label": str(c.get("label") or ""), "count": int(c.get("count") or 0),
             "public": bool(c.get("public"))} for c in data if c.get("id")]


def info(wid: str) -> dict:
    if not _ID.match(str(wid or "")):
        raise WallhavenError("Ese no es un fondo de Wallhaven.")
    data = _get(f"/w/{wid}").get("data") or {}
    if not data or data.get("purity") == "nsfw":
        raise WallhavenError("Ese fondo no es apto: Bakin no baja contenido NSFW.")
    return data


# --------------------------------------------------------------------- links

def parse_url(url: str) -> dict | None:
    """A pasted Wallhaven link: one wallpaper, a search, or a public collection."""
    u = str(url or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:wallhaven\.cc/w/|whvn\.cc/|w\.wallhaven\.cc/full/\w\w/wallhaven-)([a-z0-9]{4,10})", u, re.I)
    if m:
        return {"type": "wallpaper", "id": m.group(1).lower()}
    m = re.match(r"^(?:https?://)?(?:www\.)?wallhaven\.cc/user/([\w.-]{1,40})/favorites/(\d+)", u, re.I)
    if m:
        return {"type": "collection", "user": m.group(1), "id": m.group(2)}
    m = re.match(r"^(?:https?://)?(?:www\.)?wallhaven\.cc/(search|toplist|latest|hot|random)/?(?:\?([^#]*))?", u, re.I)
    if m:
        params = dict(urllib.parse.parse_qsl(m.group(2) or ""))
        params.setdefault("sorting", {"toplist": "toplist", "latest": "date_added", "hot": "views",
                                      "random": "random"}.get(m.group(1).lower(), params.get("sorting")))
        return {"type": "search", "filters": clean_filters(params)}
    return None


def label_for(job: dict) -> str:
    if job.get("type") == "wallpaper":
        return "Fondos sueltos"
    if job.get("type") == "collection":
        return f"{job['user']} - colección {job['id']}"
    q = (job.get("filters") or {}).get("q") or ""
    sort = {"toplist": "Populares", "date_added": "Recientes", "random": "Al azar", "views": "Más vistos",
            "favorites": "Favoritos", "relevance": "Búsqueda"}.get((job.get("filters") or {}).get("sorting"), "Búsqueda")
    return q or sort


def safe_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(text)).strip(" .")
    return " ".join(name.split())[:60] or "Wallhaven"


# ----------------------------------------------------------------- download

@dataclass
class Stats:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    blocked: bool = False
    files: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def destination(root: Path) -> Path:
    return root / FOLDER


def mark_folder(dest: Path) -> None:
    from .mediadl import MARKER
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / MARKER
    if not marker.exists():
        marker.write_text(json.dumps({"preset": "Wallhaven", "source": "wallhaven.cc",
                                      "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), encoding="utf-8")


def archived(dest: Path) -> set[str]:
    try:
        return {line.strip() for line in (dest / ARCHIVE).read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def _remember(dest: Path, wid: str) -> None:
    with open(dest / ARCHIVE, "a", encoding="utf-8") as f:
        f.write(wid + "\n")


def _wallpapers(job: dict, limit: int, cancel: threading.Event):
    """The wallpapers a job asks for, page by page (never NSFW)."""
    if job["type"] == "wallpaper":
        yield {"id": job["id"]}
        return
    if job["type"] == "ids":
        for wid in job["ids"][:limit]:
            yield {"id": wid}
        return
    page, seed, given = 1, None, 0
    while given < limit and not cancel.is_set():
        if job["type"] == "collection":
            res = collection(job["user"], job["id"], page, sketchy=bool(job.get("sketchy")))
        else:
            res = search({**job["filters"], **({"seed": seed} if seed else {})}, page)
            seed = seed or res.get("seed")
        for w in res["items"]:
            if given >= limit:
                return
            given += 1
            yield w
        if page >= res["last_page"] or not res["items"]:
            return
        page += 1


def download(job: dict, root: Path, emit: Callable[[dict], None], cancel: threading.Event) -> Stats:
    """Downloads a job: {"type": "wallpaper", "id"}, {"type": "ids", "ids"},
    {"type": "search", "filters"} or {"type": "collection", "user", "id"},
    with an optional "limit" (most popular first, at most MAX_PER_JOB)."""
    from . import library
    stats = Stats()
    dest = destination(root)
    mark_folder(dest)
    sub = dest / safe_name(job.get("label") or label_for(job))
    sub.mkdir(parents=True, exist_ok=True)
    have = archived(dest)
    limit = max(1, min(int(job.get("limit") or MAX_PER_JOB), MAX_PER_JOB))
    want = limit if job["type"] not in ("wallpaper", "ids") else len(job.get("ids") or [1])
    col = library.Collection(f"md:{dest.name}", "media", dest.name, dest)
    meta = library.Meta(col)
    n = 0
    try:
        for w in _wallpapers(job, limit, cancel):
            if cancel.is_set():
                break
            n += 1
            wid = w["id"]
            emit({"kind": "wallhaven_progress", "index": n, "count": want})
            if wid in have:
                stats.skipped += 1
                continue
            try:
                full = info(wid)  # the tags and the file, one request
                url = full.get("path") or ""
                if not re.match(r"^https://w\.wallhaven\.cc/", url):
                    raise WallhavenError("Wallhaven no dio el archivo de ese fondo.")
                emit({"kind": "media_item", "title": f"Fondo {wid} · {full.get('resolution', '')}",
                      "extractor_key": "Wallhaven", "uploader": (full.get("uploader") or {}).get("username", "")})
                ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".jpg"
                target = sub / f"wallhaven-{wid}{ext}"
                part = target.with_name(target.name + ".part")
                with _open(url, timeout=120) as r, open(part, "wb") as out:
                    while chunk := r.read(1 << 16):
                        if cancel.is_set():
                            break
                        out.write(chunk)
                if cancel.is_set():
                    part.unlink(missing_ok=True)
                    break
                part.replace(target)
                from . import imageinfo  # its size and colour, for the «Fondos» filters
                imageinfo.remember(target, full.get("dimension_x"), full.get("dimension_y"), full.get("colors"))
                size = target.stat().st_size
                stats.done += 1
                stats.bytes += size
                stats.files.append(str(target))
                tags = [library.clean_tag(t.get("name", "")) for t in (full.get("tags") or [])]
                tags = [t for t in dict.fromkeys(tags) if t][:MAX_TAGS]
                rel = target.relative_to(dest).as_posix()
                if tags:
                    meta.update(rel, tags=tags)
                    meta.save()
                _remember(dest, wid)
                have.add(wid)
                emit({"kind": "media_file", "path": str(target)})
            except Blocked:
                stats.blocked = True
                break
            except (WallhavenError, OSError) as exc:
                stats.failed += 1
                if len(stats.errors) < 5:
                    stats.errors.append(f"{wid}: {exc}")
    except Blocked:
        stats.blocked = True
    return stats
