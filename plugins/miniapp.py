"""
Telegram Mini App for this bot.
(Adapted from GouthamSER/KuttuBot - plugins/miniapp.py)

Routes (mounted first in plugins/__init__.py, so they win over the /{path} stream catch-all):
  GET  /app              -> miniapp/index.html
  POST /app/api/search   -> {init_data, query, offset}  -> movies (grouped, with posters)
  POST /app/api/movie    -> {init_data, keys, qs}       -> one movie's files + poster/details
  POST /app/api/link     -> {init_data, ids:[file_id..]} -> {bot, start, count}

Files are never sent from the web server. /app/api/link returns a deep-link start parameter and the
page opens https://t.me/<bot>?start=<start>. The bot's own /start handler then delivers the file(s),
so force-subscribe, shortlink verification, premium, protect-content and auto-delete all still apply.

Every API call is authenticated with Telegram WebApp `initData` (HMAC-SHA256 signed with the bot token).
"""
import hmac
import json
import time
import hashlib
import logging
import asyncio
import re
import secrets
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from info import BOT_TOKEN, MINIAPP_URL, MULTIPLE_DB
from database.ia_filterdb import Media, Media2, get_search_results, get_file_details
from utils import temp, get_size
from plugins.miniposters import parse_name, get_info, get_infos

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

INDEX_FILE = Path(__file__).resolve().parent.parent / "miniapp" / "index.html"

INIT_DATA_MAX_AGE = 24 * 3600   # seconds initData stays valid
PAGE_SIZE = 30                  # files fetched per search page (grouped into movies)
MOVIE_PAGE_SIZE = 100           # files per DB page when loading one movie
MOVIE_MAX_PAGES = 3             # DB pages read per search text when loading one movie
MOVIE_MAX_FILES = 300           # cap on files shown for one movie/series
SEARCH_MAX_PAGES = 3            # extra pages read when a page has no video titles
MAX_KEYS = 6                    # filename variants merged into one movie
MAX_SEND = 10                   # max files per request (keep equal to MAX_SEND in miniapp/index.html)
LINK_COOLDOWN = 2               # seconds between link requests per user
MAX_SELECTIONS = 500            # multi-file selections kept in memory (oldest dropped first)
_last_link = {}                 # user_id -> monotonic time of last link request
_selection_keys = deque()       # our keys inside temp.GETALL, oldest first

_REGEX_META = re.compile(r'[\\^$.*+?{}\[\]|()]')


def _norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def _years_compatible(a, b):
    return not a or not b or a == b


# ── Auth ───────────────────────────────────────────────────────────────────────
def validate_init_data(init_data: str, bot_token: str = BOT_TOKEN, max_age: int = INIT_DATA_MAX_AGE):
    """Return the Telegram user dict if initData is genuine and fresh, else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > max_age:
        return None

    try:
        user = json.loads(pairs.get("user", ""))
    except Exception:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _clean_query(q) -> str:
    q = str(q or "")[:100]
    q = _REGEX_META.sub(" ", q)
    return " ".join(q.split())


async def _read_json(request):
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _err(msg, status=400):
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _auth(request):
    """Return ((user, body), None) or (None, error_response)."""
    body = await _read_json(request)
    if body is None:
        return None, _err("Bad request")
    user = validate_init_data(body.get("init_data", ""))
    if not user:
        return None, _err("Open this app from the bot in Telegram", 401)
    if user["id"] in temp.BANNED_USERS:
        return None, _err("You are banned from using this bot", 403)
    return (user, body), None


# ── Pages ──────────────────────────────────────────────────────────────────────
@routes.get("/app", allow_head=True)
@routes.get("/app/", allow_head=True)
async def app_page(request):
    try:
        html = INDEX_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(text="Mini app files missing (miniapp/index.html)", status=500)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


def _offset(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


async def _latest_files(offset):
    """Newest indexed files. get_search_results() returns nothing for an empty query,
    and the home page ("Latest uploads") searches with an empty query."""
    limit = PAGE_SIZE + 1
    tasks = [Media.find({}).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit)]
    if MULTIPLE_DB:
        tasks.append(Media2.find({}).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit))
    results = await asyncio.gather(*tasks)
    files = [f for batch in results for f in batch][:limit]
    has_next = len(files) > PAGE_SIZE
    if has_next:
        files = files[:PAGE_SIZE]
    return files, (offset + len(files) if has_next else ""), offset + len(files) + (1 if has_next else 0)


# ── Search ─────────────────────────────────────────────────────────────────────
async def search_core(query, offset=0):
    """Grouped movie cards for a search text."""
    query = _clean_query(query)
    offset = _offset(offset)

    # Fetch until we have at least one video title (a page can be all subtitles / audio files).
    groups, cursor = {}, offset
    next_offset, total = "", 0
    for _ in range(SEARCH_MAX_PAGES):
        if query:
            files, next_offset, total = await get_search_results(
                None, query, max_results=PAGE_SIZE, offset=cursor, filter=True
            )
        else:
            files, next_offset, total = await _latest_files(cursor)
        for f in files:
            p = parse_name(f.file_name)
            if p["is_video"]:                       # subtitles / audio / archives only show inside a movie page
                groups.setdefault(p["key"], p)
        if groups or next_offset == "" or not files:
            break
        cursor = next_offset

    infos = await get_infos([(p["title"], p["year"], p["kind"]) for p in groups.values()])

    # Different filenames can be the same movie ("Khalifa The Ruler (2026)" and "Khalifa 2026").
    # Cards that resolve to the same IMDb title are merged into one.
    movies, by_ident = [], {}
    for p, info in zip(groups.values(), infos):
        info = info or {}
        ident = info.get("imdb_id") or (f"t:{_norm(info['title'])}" if info.get("source") and info.get("title") else "")
        target = next((m for m in by_ident.get(ident, []) if _years_compatible(m["_year"], p["year"])), None) if ident else None
        if target:
            if p["key"] not in target["keys"] and len(target["keys"]) < MAX_KEYS:
                target["keys"].append(p["key"])
            if p["title"] not in target["qs"] and len(target["qs"]) < MAX_KEYS:
                target["qs"].append(p["title"])
            if not target["thumb"] and info.get("thumb"):
                target["thumb"] = info["thumb"]
            continue
        m = {
            "mkey": f"{ident}|{p['year']}" if ident else p["key"],
            "key": p["key"], "keys": [p["key"]],
            "q": p["title"], "qs": [p["title"]],
            "title": info.get("title") or p["title"],
            "year": info.get("year") or p["year"],
            "kind": p["kind"],
            "thumb": info.get("thumb"),
            "rating": info.get("rating"),
            "_year": p["year"],
        }
        movies.append(m)
        if ident:
            by_ident.setdefault(ident, []).append(m)
    for m in movies:
        m.pop("_year")
    return {
        "ok": True,
        "movies": movies,
        "next_offset": next_offset if next_offset != "" else None,
        "total": total,
    }


# ── Home sections ──────────────────────────────────────────────────────────
SECTION_LIMIT = 20        # cards per section (site + Mini App share this)
SECTION_POOL = 200        # newest files read to build all sections in one pass


async def _latest_pool(limit):
    """Newest indexed files (both DBs, same pattern as _latest_files)."""
    tasks = [Media.find({}).sort("$natural", -1).limit(limit).to_list(length=limit)]
    if MULTIPLE_DB:
        tasks.append(Media2.find({}).sort("$natural", -1).limit(limit).to_list(length=limit))
    results = await asyncio.gather(*tasks)
    return [f for batch in results for f in batch]


async def sections_core():
    """One pooled pass over the newest files, split into home sections.

    Rows (in order): Latest Movies, Latest Web Series, Popular, Recently
    Added, Top IMDb Rated, Action, Sci-Fi, Horror, Thriller. Empty rows are
    dropped, so the front ends only render rows that actually have titles.

    Notes on definitions (no view/download counter exists in the DB):
    - Popular = most files in the recent pool (series with many episodes /
      titles with many quality variants rank first), rating as tie-break.
    - Top IMDb Rated = highest IMDb rating first.
    - Genre rows need detail=True lookups, because grid lookups usually
      return no genres; results are cached 24h, so this costs once a day.
    """
    files = await _latest_pool(SECTION_POOL)
    groups, counts = {}, {}
    for f in files:
        p = parse_name(f.file_name)
        if not p["is_video"]:
            continue
        if p["key"] not in groups:
            groups[p["key"]] = p
        counts[p["key"]] = counts.get(p["key"], 0) + 1
    if not groups:
        return {"ok": True, "sections": []}

    raw_infos = await asyncio.gather(
        *(get_info(p["title"], p["year"], p["kind"], detail=True) for p in groups.values()),
        return_exceptions=True,
    )
    movies, by_ident = [], {}
    for p, info in zip(groups.values(), raw_infos):
        info = info if isinstance(info, dict) else {}
        ident = info.get("imdb_id") or (f"t:{_norm(info['title'])}" if info.get("source") and info.get("title") else "")
        target = next((m for m in by_ident.get(ident, []) if _years_compatible(m["_year"], p["year"])), None) if ident else None
        if target:
            if p["key"] not in target["keys"] and len(target["keys"]) < MAX_KEYS:
                target["keys"].append(p["key"])
            if p["title"] not in target["qs"] and len(target["qs"]) < MAX_KEYS:
                target["qs"].append(p["title"])
            target["_count"] += counts[p["key"]]
            if not target["thumb"] and info.get("thumb"):
                target["thumb"] = info["thumb"]
            continue
        m = {
            "mkey": f"{ident}|{p['year']}" if ident else p["key"],
            "key": p["key"], "keys": [p["key"]],
            "q": p["title"], "qs": [p["title"]],
            "title": info.get("title") or p["title"],
            "year": info.get("year") or p["year"],
            "kind": p["kind"],
            "thumb": info.get("thumb"),
            "rating": info.get("rating"),
            "_year": p["year"],
            "_count": counts[p["key"]],
            "_genres": [str(g).lower() for g in (info.get("genres") or [])],
        }
        movies.append(m)
        if ident:
            by_ident.setdefault(ident, []).append(m)

    def has_genre(m, *names):
        return any(any(n in g for g in m["_genres"]) for n in names)

    rated = [m for m in movies if m["rating"]]
    defs = [
        ("latest-movies", "Latest Movies", [m for m in movies if m["kind"] == "movie"]),
        ("latest-series", "Latest Web Series", [m for m in movies if m["kind"] == "series"]),
        ("popular", "Popular", sorted(movies, key=lambda m: (-m["_count"], -(m["rating"] or 0)))),
        ("recently-added", "Recently Added", list(movies)),
        ("top-rated", "Top IMDb Rated", sorted(rated, key=lambda m: -(m["rating"] or 0))),
        ("action", "Action", [m for m in movies if has_genre(m, "action")]),
        ("sci-fi", "Sci-Fi", [m for m in movies if has_genre(m, "sci-fi", "science fiction")]),
        ("horror", "Horror", [m for m in movies if has_genre(m, "horror")]),
        ("thriller", "Thriller", [m for m in movies if has_genre(m, "thriller")]),
    ]
    sections = []
    for sid, title, rows in defs:
        rows = rows[:SECTION_LIMIT]
        if not rows:
            continue
        sections.append({"id": sid, "title": title, "movies": [
            {"mkey": m["mkey"], "key": m["key"], "keys": m["keys"], "q": m["q"], "qs": m["qs"],
             "title": m["title"], "year": m["year"], "kind": m["kind"],
             "thumb": m["thumb"], "rating": m["rating"]} for m in rows]})
    return {"ok": True, "sections": sections}


# ── Paginated sections (20 per page) ─────────────────────────────────────
# Why this does NOT repeat the genre-scan slowness: a page request looks up
# posters only for that page's 20 titles. Scanning the DB itself is just
# filename parsing (no network). Genre / top-rated lists are cached, and
# their scan stops once the requested page is filled (genre) or once a
# bounded window is sorted (popular / top-rated).
SECTION_PAGE_SIZE = 20
SECTION_TITLES = {
    "latest-movies": "Latest Movies", "latest-series": "Latest Web Series",
    "popular": "Popular", "recently-added": "Recently Added",
    "top-rated": "Top IMDb Rated", "action": "Action",
    "sci-fi": "Sci-Fi", "horror": "Horror", "thriller": "Thriller",
}
_GENRE_NAMES = {
    "action": ("action",), "sci-fi": ("sci-fi", "science fiction"),
    "horror": ("horror",), "thriller": ("thriller",),
}
_SEC_TTL = 1800
_genre_cache = {}    # gid -> {movies, offset, seen, done, at}
_sorted_cache = {}   # "popular" -> {parsed, at}; "top-rated" -> {movies, at}


async def _db_batch(offset, limit):
    tasks = [Media.find({}).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit)]
    if MULTIPLE_DB:
        tasks.append(Media2.find({}).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit))
    results = await asyncio.gather(*tasks)
    return [f for batch in results for f in batch]


def _pub_from(p, info):
    info = info if isinstance(info, dict) else {}
    return {"mkey": p["key"], "key": p["key"], "keys": [p["key"]],
            "q": p["title"], "qs": [p["title"]],
            "title": info.get("title") or p["title"],
            "year": info.get("year") or p["year"], "kind": p["kind"],
            "thumb": info.get("thumb"), "rating": info.get("rating")}


async def _lookup_titles(titles, detail=False):
    if not titles:
        return []
    raw = await asyncio.gather(
        *(get_info(p["title"], p["year"], p["kind"], detail=detail) for p in titles),
        return_exceptions=True,
    )
    return [_pub_from(p, info) for p, info in zip(titles, raw)]


async def _scan_order_page(section_id, page):
    """Latest / series / recently: newest-first, parse-only scan, then
    poster lookups for the requested page only."""
    kind = {"latest-movies": "movie", "latest-series": "series"}.get(section_id)
    need = page * SECTION_PAGE_SIZE + 1
    collected, seen, offset, scanned, done = [], set(), 0, 0, False
    while len(collected) < need and scanned < 5000:
        files = await _db_batch(offset, 200)
        if not files:
            done = True
            break
        offset += 200
        scanned += len(files)
        for f in files:
            p = parse_name(f.file_name)
            if not p["is_video"] or p["key"] in seen:
                continue
            seen.add(p["key"])
            if kind and p["kind"] != kind:
                continue
            collected.append(p)
    start = (page - 1) * SECTION_PAGE_SIZE
    movies = await _lookup_titles(collected[start:start + SECTION_PAGE_SIZE])
    total = (len(collected) + SECTION_PAGE_SIZE - 1) // SECTION_PAGE_SIZE if done else None
    return movies, len(collected) > page * SECTION_PAGE_SIZE, total


async def _genre_page(gid, page):
    names = _GENRE_NAMES[gid]
    cache = _genre_cache.get(gid)
    if cache is None or time.time() - cache["at"] > _SEC_TTL:
        cache = {"movies": [], "offset": 0, "seen": set(), "done": False, "at": time.time()}
        _genre_cache[gid] = cache
    need = page * SECTION_PAGE_SIZE
    scanned = 0
    tries = 0  # per-request cap: when imdbio/OMDb are timing out, a title
    # with no detail data can never match a genre, so trying hundreds of
    # titles at 10s each only makes the page slower. Stop at 60 tries and
    # return what matched; unprocessed titles are rewound, not lost.
    while len(cache["movies"]) < need and not cache["done"] and scanned < 3000 and tries < 60:
        files = await _db_batch(cache["offset"], 150)
        if not files:
            cache["done"] = True
            break
        batch_start = cache["offset"]
        cache["offset"] += 150
        scanned += len(files)
        groups = {}
        for f in files:
            p = parse_name(f.file_name)
            if not p["is_video"] or p["key"] in cache["seen"]:
                continue
            groups[p["key"]] = p
        if not groups:
            continue
        ordered = list(groups.values())
        processed = ordered[:max(0, 60 - tries)]
        tries += len(processed)
        looked = []
        for i in range(0, len(processed), 10):  # bounded concurrency, 10 at a time
            chunk = processed[i:i + 10]
            looked.extend(chunk)
            for p in chunk:
                cache["seen"].add(p["key"])
            raw = await asyncio.gather(
                *(get_info(p["title"], p["year"], p["kind"], detail=True) for p in chunk),
                return_exceptions=True,
            )
            for p, info in zip(chunk, raw):
                if not isinstance(info, dict):
                    continue
                genres = [str(g).lower() for g in (info.get("genres") or [])]
                if any(any(n in g for g in genres) for n in names):
                    cache["movies"].append(_pub_from(p, info))
            if len(cache["movies"]) >= need:
                break
        if len(looked) < len(ordered):
            # Cap hit, or page filled, mid-batch: rewind so titles never
            # looked up are retried on the next request instead of lost.
            cache["offset"] = batch_start
            break
    start = (page - 1) * SECTION_PAGE_SIZE
    total = (len(cache["movies"]) + SECTION_PAGE_SIZE - 1) // SECTION_PAGE_SIZE if cache["done"] else None
    return cache["movies"][start:start + SECTION_PAGE_SIZE], len(cache["movies"]) > page * SECTION_PAGE_SIZE, total


async def _popular_page(page):
    cache = _sorted_cache.get("popular")
    if cache is None or time.time() - cache["at"] > _SEC_TTL:
        parsed, counts, seen, offset, scanned = [], {}, set(), 0, 0
        while scanned < 1000 and len(parsed) < 300:
            files = await _db_batch(offset, 200)
            if not files:
                break
            offset += 200
            scanned += len(files)
            for f in files:
                p = parse_name(f.file_name)
                if not p["is_video"]:
                    continue
                counts[p["key"]] = counts.get(p["key"], 0) + 1
                if p["key"] not in seen:
                    seen.add(p["key"])
                    parsed.append(p)
        parsed.sort(key=lambda p: -counts[p["key"]])   # stable: ties keep newest-first order
        cache = {"parsed": parsed, "at": time.time()}
        _sorted_cache["popular"] = cache
    parsed = cache["parsed"]
    start = (page - 1) * SECTION_PAGE_SIZE
    movies = await _lookup_titles(parsed[start:start + SECTION_PAGE_SIZE])
    total = (len(parsed) + SECTION_PAGE_SIZE - 1) // SECTION_PAGE_SIZE
    return movies, len(parsed) > page * SECTION_PAGE_SIZE, total


async def _toprated_page(page):
    cache = _sorted_cache.get("top-rated")
    if cache is None or time.time() - cache["at"] > _SEC_TTL:
        parsed, seen, offset, scanned = [], set(), 0, 0
        while scanned < 1000 and len(parsed) < 200:
            files = await _db_batch(offset, 200)
            if not files:
                break
            offset += 200
            scanned += len(files)
            for f in files:
                p = parse_name(f.file_name)
                if not p["is_video"] or p["key"] in seen:
                    continue
                seen.add(p["key"])
                parsed.append(p)
        movies = []
        for i in range(0, len(parsed), 30):
            movies += [m for m in await _lookup_titles(parsed[i:i + 30]) if m["rating"]]
        movies.sort(key=lambda m: -(m["rating"] or 0))
        cache = {"movies": movies, "at": time.time()}
        _sorted_cache["top-rated"] = cache
    movies = cache["movies"]
    start = (page - 1) * SECTION_PAGE_SIZE
    total = (len(movies) + SECTION_PAGE_SIZE - 1) // SECTION_PAGE_SIZE
    return movies[start:start + SECTION_PAGE_SIZE], len(movies) > page * SECTION_PAGE_SIZE, total


async def section_page_core(section_id, page):
    """One page (20 titles) of one section. Returns (movies, has_next, total_pages|None).
    total_pages is None when the end of the DB has not been reached yet, so
    front ends must show 'Page N' + Next, never a made-up total."""
    if section_id not in SECTION_TITLES or page < 1 or page > 50:
        return None
    if section_id in ("latest-movies", "latest-series", "recently-added"):
        movies, has_next, total = await _scan_order_page(section_id, page)
    elif section_id in _GENRE_NAMES:
        movies, has_next, total = await _genre_page(section_id, page)
    elif section_id == "popular":
        movies, has_next, total = await _popular_page(page)
    else:
        movies, has_next, total = await _toprated_page(page)
    return {"ok": True, "section": section_id, "title": SECTION_TITLES[section_id],
            "page": page, "per_page": SECTION_PAGE_SIZE, "movies": movies,
            "has_next": has_next, "total_pages": total}


@routes.post("/app/api/section")
async def api_section(request):
    auth, resp = await _auth(request)
    if resp is not None:
        return resp
    _, body = auth
    try:
        page = max(1, int(body.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        data = await section_page_core(str(body.get("section", "")), page)
    except Exception:
        logger.exception("miniapp section page failed")
        return _err("Database is busy. Try again.", 500)
    return _err("Unknown section") if data is None else web.json_response(data)


@routes.post("/app/api/search")
async def api_search(request):
    auth, resp = await _auth(request)
    if resp is not None:
        return resp
    _, body = auth
    try:
        data = await search_core(body.get("query"), body.get("offset", 0))
    except Exception:
        logger.exception("miniapp search failed")
        return _err("Database is busy. Try again.", 500)
    return web.json_response(data)


@routes.post("/app/api/sections")
async def api_sections(request):
    auth, resp = await _auth(request)
    if resp is not None:
        return resp
    try:
        data = await sections_core()
    except Exception:
        logger.exception("miniapp sections failed")
        return _err("Database is busy. Try again.", 500)
    return web.json_response(data)


# ── One movie ──────────────────────────────────────────────────────────────────
def _str_list(v, single=None, clean=False):
    """Accept a list (or one legacy string) from the client; return up to MAX_KEYS unique short strings."""
    items = v if isinstance(v, list) else [single]
    out = []
    for x in items:
        x = _clean_query(x) if clean else str(x or "")[:200]
        if x and x not in out:
            out.append(x)
    return out[:MAX_KEYS]


async def movie_core(body):
    """All files of one movie/series (every filename variant of it), plus poster and details.
    Returns a dict, or None when the request names no movie."""
    keys = _str_list(body.get("keys"), body.get("key"))
    qs = _str_list(body.get("qs"), body.get("q"), clean=True)
    if not keys or not qs:
        return None
    keyset = set(keys)

    # keys[0] is "<normalized title>|<year>" (movie) or "<normalized title>" (series).
    g_year = keys[0].split("|", 1)[1] if "|" in keys[0] else ""
    g_kind = "movie" if "|" in keys[0] else "series"
    info_task = asyncio.ensure_future(get_info(qs[0], g_year, g_kind, detail=True))

    async def fetch(q):
        out, offset = [], 0
        for _ in range(MOVIE_MAX_PAGES):
            files, next_offset, _total = await get_search_results(
                None, q, max_results=MOVIE_PAGE_SIZE, offset=offset, filter=True
            )
            out += [f for f in files if parse_name(f.file_name)["key"] in keyset]
            if next_offset == "" or not files:
                break
            offset = next_offset
        return out

    try:
        batches = await asyncio.gather(*(fetch(q) for q in qs))
    except Exception:
        info_task.cancel()
        raise

    matched, seen_ids, seen_sig = [], set(), set()
    for batch in batches:
        for f in batch:
            # the same file indexed twice has a different file_id but the same name + size
            sig = (" ".join(f.file_name.lower().split()), f.file_size)
            if f.file_id in seen_ids or sig in seen_sig:
                continue
            seen_ids.add(f.file_id)
            seen_sig.add(sig)
            matched.append(f)
    matched = matched[:MOVIE_MAX_FILES]

    if not matched:
        info_task.cancel()
        return {"ok": True, "info": None, "files": []}

    lead = next((f for f in matched if parse_name(f.file_name)["key"] == keys[0]), matched[0])
    p = parse_name(lead.file_name)
    try:
        info = await info_task
    except Exception:
        info = {}
    info = info or {}
    if p["kind"] == "series":
        matched.sort(key=lambda f: f.file_name.lower())
    return {
        "ok": True,
        "info": {
            "title": info.get("title") or p["title"],
            "year": info.get("year") or p["year"],
            "kind": p["kind"],
            "poster": info.get("poster"),
            "thumb": info.get("thumb"),
            "rating": info.get("rating"),
            "genres": info.get("genres") or [],
            "plot": info.get("plot") or "",
            "runtime": info.get("runtime") or "",
        },
        "files": [{"id": f.file_id, "name": f.file_name, "size": get_size(f.file_size)} for f in matched],
    }


@routes.post("/app/api/movie")
async def api_movie(request):
    auth, resp = await _auth(request)
    if resp is not None:
        return resp
    _, body = auth
    try:
        data = await movie_core(body)
    except Exception:
        logger.exception("miniapp movie failed")
        return _err("Could not load files right now.", 500)
    return _err("Missing movie") if data is None else web.json_response(data)


# ── Get files: return a /start deep link ───────────────────────────────────────
def _remember_selection(files):
    """Register several files in temp.GETALL (what the bot's own 'Send All' uses) and return the key."""
    key = secrets.token_urlsafe(8)
    temp.GETALL[key] = files
    _selection_keys.append(key)
    while len(_selection_keys) > MAX_SELECTIONS:
        temp.GETALL.pop(_selection_keys.popleft(), None)
    return key


@routes.post("/app/api/link")
async def api_link(request):
    auth, resp = await _auth(request)
    if resp is not None:
        return resp
    user, body = auth
    user_id = user["id"]

    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return _err("No files selected")
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and 0 < len(i) <= 128][:MAX_SEND]
    if not ids:
        return _err("No valid files selected")

    now = time.monotonic()
    if now - _last_link.get(user_id, 0) < LINK_COOLDOWN:
        return _err("Slow down, try again in a few seconds", 429)
    _last_link[user_id] = now
    if len(_last_link) > 5000:
        for uid in [u for u, t in _last_link.items() if now - t > 60]:
            _last_link.pop(uid, None)

    docs = []
    for fid in ids:
        found = await get_file_details(fid)
        if found:
            docs.append(found[0])
    if not docs:
        return _err("Those files are no longer available", 404)

    # In PM the bot uses the user's own id as the "group id" for settings, so do the same here.
    start = f"file_{user_id}_{docs[0].file_id}" if len(docs) == 1 else ""
    if not start or len(start) > 64:              # Telegram limits the start parameter to 64 chars
        start = f"allfiles_{user_id}_{_remember_selection(docs)}"
    return web.json_response({"ok": True, "bot": temp.U_NAME, "start": start, "count": len(docs)})


# ── /app command ───────────────────────────────────────────────────────────────
@Client.on_message(filters.command("app") & filters.private & filters.incoming)
async def open_miniapp(client, message):
    if not MINIAPP_URL:
        return await message.reply("The Mini App is not set up yet.")
    await message.reply(
        "<b>🎬 Browse movies with posters, pick your files and get them right here.</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Open Mini App", web_app=WebAppInfo(url=f"{MINIAPP_URL}/app"))
        ]]),
    )
