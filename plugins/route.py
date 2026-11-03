from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
from aiohttp.http_exceptions import BadStatusLine
from dreamxbotz.Bot import multi_clients, work_loads
from dreamxbotz.server.exceptions import FIleNotFound, InvalidHash
from dreamxbotz.util.custom_dl import ByteStreamer
from dreamxbotz.util.render_template import render_page
from urllib.parse import quote
import info
from database.users_chats_db import db

logger = logging.getLogger(__name__)


routes = web.RouteTableDef()

@routes.get("/favicon.ico")
async def favicon_route_handler(request):
    return web.FileResponse('dreamxbotz/template/favicon.ico')

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("dreamxbotz")

@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def watch_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:\/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return web.Response(text=await render_page(id, secure_hash), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logger.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))

def masked_link_page(title, message, action_html=""):
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title}</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; }}
            .card {{ max-width: 520px; margin: 12vh auto; padding: 28px; background: #111827; border-radius: 18px; text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,.35); }}
            h1 {{ margin-top: 0; font-size: 28px; }}
            p {{ color: #cbd5e1; line-height: 1.6; }}
            a.button {{ display: inline-block; margin-top: 18px; padding: 13px 22px; border-radius: 999px; color: #fff; background: #2563eb; text-decoration: none; font-weight: 700; }}
        </style>
    </head>
    <body><main class="card"><h1>{title}</h1><p>{message}</p>{action_html}</main></body>
    </html>
    """


@routes.get(r"/r/{hash_id}", allow_head=True)
async def masked_redirect_handler(request: web.Request):
    # Test1-style one-time masked link. Registered BEFORE the /{path} catch-all below.
    hash_id = request.match_info["hash_id"]
    if not re.fullmatch(r"[a-fA-F0-9]{32}", hash_id):
        return web.Response(text=masked_link_page("Link Not Found", "This link is invalid or was never created. Please request a new link from the bot."), content_type="text/html", status=404)
    masked_link = await db.get_masked_link(hash_id)
    if not masked_link:
        return web.Response(text=masked_link_page("Link Not Found", "This link is invalid or was never created. Please request a new link from the bot."), content_type="text/html", status=404)
    if masked_link.get("used"):
        return web.Response(text=masked_link_page("Link Expired", "This is a one-time link and it has already been used. Please request a new one from the bot."), content_type="text/html", status=410)
    user_agent = request.headers.get("User-Agent", "").lower()
    if "telegram" in user_agent:
        current_url = f"{request.scheme}://{request.host}{request.rel_url}"
        intent_url = f"intent://{request.host}{request.rel_url}#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url={quote(current_url, safe='')};end"
        action = f'<a class="button" href="{intent_url}">Open in Chrome</a>'
        return web.Response(text=masked_link_page("Open in Chrome", "For verification, open this link in an external Chrome browser instead of Telegram's in-app browser.", action), content_type="text/html")
    claimed_link = await db.mark_masked_link_used(hash_id)
    if not claimed_link:
        return web.Response(text=masked_link_page("Link Expired", "This is a one-time link and it has already been used. Please request a new one from the bot."), content_type="text/html", status=410)
    raise web.HTTPFound(claimed_link["real_url"])


@routes.get(r"/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            # Try to extract ID from path
            id_match = re.search(r"(\d+)(?:\/\S+)?", path)
            if not id_match:
                # Path doesn't contain any numeric ID - return 404
                raise web.HTTPNotFound(text="Not found")
            id = int(id_match.group(1))
            secure_hash = request.rel_url.query.get("hash")
        
        return await media_streamer(request, id, secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except web.HTTPNotFound:
        raise  # Re-raise HTTPNotFound without logging
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logger.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))

class_cache = {}

async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if info.MULTI_CLIENT:
        logger.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logger.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logger.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    file_id = await tg_connect.get_file_properties(id)
    
    if file_id.unique_id[:6] != secure_hash:
        logger.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash
    
    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)[0] or "application/octet-stream"
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"
    
    resp_headers = {
        "Content-Type": f"{mime_type}",
        "Content-Length": str(req_length),
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    
    if range_header:
        resp_headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers=resp_headers
    )
