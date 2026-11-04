"""Daily file / stream-link limits (NEW feature, not in Test1).

Performance design (user requirement: do not slow the bot):
- Exactly ONE atomic Mongo operation per delivery / generation:
  a single ``find_one_and_update`` with an aggregation-pipeline update
  and ``upsert=True`` on the ``daily_usage`` collection, keyed by
  ``"<user_id>_<YYYY-MM-DD>"`` (Asia/Kolkata date, from info.TZ).
- No query at search-listing time, no per-result query, no poster/API
  call and no blocking sync call in this path. Premium users bypass
  the helper entirely (Auto's ``has_premium_access`` result is cached
  for 60s in users_chats_db, so even that check is cheap).
- Indexes are created once (guarded by a module flag), TTL is 3 days,
  so old daily documents disappear on their own.
- Repeat stream links: ``link_files`` records which file_ids were
  already counted today. Re-generating the same file the same day
  returns allowed=True WITHOUT incrementing ``links``.
- Unverified users never reach this helper for files: Auto's /start
  verification gate returns before delivery. If verification is
  disabled globally, non-premium users are still limited.
"""

import datetime
import logging

from pymongo import ReturnDocument

from info import DAILY_FILE_LIMIT, DAILY_STREAM_LINK_LIMIT, TZ
from database.users_chats_db import db

logger = logging.getLogger(__name__)

_indexes_ready = False


def _today():
    return datetime.datetime.now(tz=TZ).strftime("%Y-%m-%d")


def _doc_id(user_id, today=None):
    today = today or _today()
    return f"{int(user_id)}_{today}"


async def _ensure_indexes(collection):
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await collection.create_index("expires_at", expireAfterSeconds=3 * 24 * 3600)
        await collection.create_index([("user_id", 1), ("date", 1)])
    except Exception:
        logger.exception("daily_usage index creation failed (limits still work, just slower on first use)")
    _indexes_ready = True


async def _claim(collection, user_id, kind, file_id=None, limit=None):
    """One atomic pipeline update. Returns True if allowed (and counted, or already counted)."""
    today = _today()
    expires = datetime.datetime.now(tz=TZ) + datetime.timedelta(days=3)
    if kind == "file":
        pipeline = [
            {"$set": {
                "user_id": int(user_id),
                "date": today,
                "expires_at": expires,
                "files": {"$cond": [
                    {"$lt": [{"$ifNull": ["$files", 0]}, limit]},
                    {"$add": [{"$ifNull": ["$files", 0]}, 1]},
                    {"$ifNull": ["$files", 0]},
                ]},
                "allowed": {"$lt": [{"$ifNull": ["$files", 0]}, limit]},
            }}
        ]
    else:  # stream link
        already = {"$in": [file_id, {"$ifNull": ["$link_files", []]}]}
        can = {"$lt": [{"$ifNull": ["$links", 0]}, limit]}
        allowed_expr = {"$or": [already, can]}
        pipeline = [
            {"$set": {
                "user_id": int(user_id),
                "date": today,
                "expires_at": expires,
                "allowed": allowed_expr,
                "links": {"$cond": [
                    already,
                    {"$ifNull": ["$links", 0]},
                    {"$cond": [
                        can,
                        {"$add": [{"$ifNull": ["$links", 0]}, 1]},
                        {"$ifNull": ["$links", 0]},
                    ]},
                ]},
                # Append only for a NEW allowed generation: already-counted
                # files stay as-is, and a blocked file must NOT be recorded,
                # or its retry would bypass the limit.
                "link_files": {"$cond": [
                    already,
                    {"$ifNull": ["$link_files", []]},
                    {"$cond": [
                        can,
                        {"$concatArrays": [{"$ifNull": ["$link_files", []]}, [file_id]]},
                        {"$ifNull": ["$link_files", []]},
                    ]},
                ]},
            }}
        ]
    doc = await collection.find_one_and_update(
        {"_id": _doc_id(user_id, today)},
        pipeline,
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return bool(doc and doc.get("allowed"))


async def claim_daily_file(user_id, collection=None):
    """True if this verified non-premium user may receive one more file today."""
    if await db.has_premium_access(int(user_id)):
        return True
    col = collection if collection is not None else db.daily_usage
    await _ensure_indexes(col)
    try:
        return await _claim(col, int(user_id), "file", limit=DAILY_FILE_LIMIT)
    except Exception:
        logger.exception("daily file limit check failed; failing open so the bot does not break")
        return True


async def claim_daily_stream_link(user_id, file_id, collection=None):
    """True if this verified non-premium user may generate a NEW stream/download link today."""
    if await db.has_premium_access(int(user_id)):
        return True
    col = collection if collection is not None else db.daily_usage
    await _ensure_indexes(col)
    try:
        return await _claim(col, int(user_id), "link", file_id=str(file_id), limit=DAILY_STREAM_LINK_LIMIT)
    except Exception:
        logger.exception("daily stream-link limit check failed; failing open so the bot does not break")
        return True


async def send_daily_limit_note(message, kind="file"):
    """Send the limit-over note with a premium button. Returns the sent message."""
    from pyrogram import enums
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from Script import script

    text = script.DAILY_FILE_LIMIT_TXT if kind == "file" else script.DAILY_LINK_LIMIT_TXT
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Buy Premium: Remove Ads", callback_data="premium_info")]])
    return await message.reply(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
