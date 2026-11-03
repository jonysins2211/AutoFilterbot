from aiohttp import web
import re
import math
import logging
import secrets
import mimetypes
import io
import json
import html
import hmac
import hashlib
import time
from urllib.parse import quote, parse_qsl
from aiohttp.http_exceptions import BadStatusLine
from dreamxbotz.Bot import multi_clients, work_loads, dreamxbotz
from dreamxbotz.server.exceptions import FIleNotFound, InvalidHash
from dreamxbotz.util.custom_dl import ByteStreamer
from dreamxbotz.util.render_template import render_page
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import info
from info import PREMIUM_WEB_PLANS, OWNER_UPI_ID, QR_CODE, PREMIUM_LOGS
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


# ── Web premium buying (ported from Mawa-movies) ──────────────────────────
# Flow: bot premium_info -> buy_info WebApp button -> GET /activate-plan
# (2-step page: Select Plan -> Pay & Upload Screenshot) -> POST /submit-payment
# -> proof photo to PREMIUM_LOGS with Accept/Reject buttons -> admin accepts
# in plugins/Premium.py, which grants premium via db.update_user().
#
# User identity: Mawa's page read tg.initDataUnsafe.user.id in JS and POSTed
# it, and the server only checked it was a positive integer. This port is
# stricter: the page also POSTs the full tg.initData string, and the server
# verifies its HMAC-SHA256 signature against the bot token (same algorithm as
# plugins/miniapp.py validate_init_data) and requires the verified user id
# to match the posted user_id. A submission with missing, expired (>24h) or
# tampered initData is rejected with 403, and a user_id in a URL parameter
# alone is never trusted — /activate-plan takes no user_id parameter at all.

_WEBAPP_INITDATA_MAX_AGE = 24 * 3600


def _validate_webapp_init_data(init_data):
    """Return the Telegram user dict if initData is genuine and fresh, else None."""
    if not init_data or not info.BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", info.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > _WEBAPP_INITDATA_MAX_AGE:
        return None
    try:
        user = json.loads(pairs.get("user", ""))
    except Exception:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def build_premium_payment_page():
    plans_json = json.dumps(PREMIUM_WEB_PLANS)
    pay_type = "UPI ID"
    pay_id = html.escape(OWNER_UPI_ID)
    qr_code = html.escape(QR_CODE, quote=True)
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Buy Premium</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{ color-scheme: dark; --bg:#080b16; --card:#111827; --accent:#7c3aed; --accent2:#06b6d4; --text:#f8fafc; --muted:#94a3b8; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:Inter,system-ui,Arial,sans-serif; background:radial-gradient(circle at top,#1e1b4b,#080b16 58%); color:var(--text); }}
    .wrap {{ width:min(960px,100%); margin:auto; padding:24px 14px 40px; }}
    .hero,.card {{ background:rgba(17,24,39,.88); border:1px solid rgba(148,163,184,.18); border-radius:24px; box-shadow:0 22px 70px rgba(0,0,0,.34); }}
    .hero {{ padding:26px; text-align:center; margin-bottom:18px; }}
    h1 {{ margin:0 0 8px; font-size:clamp(28px,8vw,48px); }}
    h2 {{ margin-top:0; }}
    .muted {{ color:var(--muted); }}
    .stepper {{ display:flex; gap:10px; margin:0 0 16px; }}
    .step {{ flex:1; padding:10px; border-radius:999px; text-align:center; font-weight:800; background:#0f172a; color:var(--muted); }}
    .step.active {{ color:white; background:linear-gradient(135deg,var(--accent),var(--accent2)); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; margin:18px 0; }}
    .plan {{ cursor:pointer; padding:18px; border-radius:20px; border:1px solid rgba(148,163,184,.22); background:#0f172a; transition:.2s; }}
    .plan.active,.plan:hover {{ transform:translateY(-2px); border-color:var(--accent2); box-shadow:0 0 0 2px rgba(6,182,212,.2); }}
    .plan b {{ display:block; font-size:22px; }} .price {{ font-size:28px; font-weight:900; margin-top:8px; }}
    .card {{ padding:20px; margin-top:16px; }}
    .hidden {{ display:none; }}
    .summary {{ display:grid; grid-template-columns:1fr auto; gap:8px; padding:14px; border-radius:18px; background:#0b1220; margin-bottom:16px; }}
    .qr {{ display:block; width:min(260px,82vw); height:auto; margin:14px auto; border-radius:18px; background:#fff; padding:8px; }}
    button,input {{ width:100%; border:0; border-radius:14px; padding:14px; font:inherit; }}
    button {{ color:white; font-weight:800; cursor:pointer; background:linear-gradient(135deg,var(--accent),var(--accent2)); margin-top:12px; }}
    button.secondary {{ background:#1f2937; }} button:disabled {{ opacity:.55; cursor:not-allowed; }}
    input {{ margin-top:10px; background:#0b1220; color:var(--text); border:1px solid rgba(148,163,184,.25); }}
    .pill {{ display:flex; justify-content:center; background:#0b1220; border:1px dashed rgba(148,163,184,.35); border-radius:999px; padding:10px 14px; word-break:break-all; }}
    #status {{ margin-top:12px; min-height:24px; }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero"><h1>🚀 Buy Premium</h1><p class="muted">First select a plan. After you tap Continue Pay, payment details and screenshot upload will open on the next page.</p></section>
    <div class="stepper"><div id="stepOne" class="step active">1. Select Plan</div><div id="stepTwo" class="step">2. Pay & Upload</div></div>
    <section id="planPage" class="card"><h2>Select Premium Plan</h2><div id="plans" class="grid"></div><button id="continueBtn" type="button" disabled>Continue Pay</button></section>
    <section id="payPage" class="card hidden"><h2>Pay & Upload Screenshot</h2><div class="summary"><span class="muted">Selected Plan</span><b id="selectedText">Choose a plan</b><span class="muted">Amount</span><b id="selectedPrice">₹0</b></div><img class="qr" src="{qr_code}" alt="Payment QR code"><p class="muted">Scan the QR or copy payment details:</p><div class="pill" id="payId">{pay_id}</div><button type="button" class="secondary" onclick="copyPayId()">Copy {pay_type}</button><form id="paymentForm"><input type="file" name="slip" accept="image/*" required><button id="submitBtn" type="submit">Submit for Verification</button></form><button type="button" class="secondary" onclick="showPlanPage()">Back to Plans</button><div id="status" class="muted"></div></section>
  </main>
<script>
const tg = window.Telegram?.WebApp; if (tg) {{ tg.ready(); tg.expand(); }}
const plans = {plans_json}; let selected = null;
const planWrap = document.getElementById('plans'), selectedText = document.getElementById('selectedText'), selectedPrice = document.getElementById('selectedPrice'), continueBtn = document.getElementById('continueBtn'), submitBtn = document.getElementById('submitBtn');
Object.entries(plans).forEach(([days,p]) => {{
  const el=document.createElement('div'); el.className='plan'; el.innerHTML=`<b>${{p.name}}</b><span class="muted">${{days}} days access</span><div class="price">₹${{p.price}}</div>`;
  el.onclick=()=>{{ document.querySelectorAll('.plan').forEach(x=>x.classList.remove('active')); el.classList.add('active'); selected=days; selectedText.textContent=p.name; selectedPrice.textContent=`₹${{p.price}}`; continueBtn.disabled=false; }};
  planWrap.appendChild(el);
}});
function showPayPage() {{ if (!selected) return; document.getElementById('planPage').classList.add('hidden'); document.getElementById('payPage').classList.remove('hidden'); document.getElementById('stepOne').classList.remove('active'); document.getElementById('stepTwo').classList.add('active'); window.scrollTo(0,0); }}
function showPlanPage() {{ document.getElementById('payPage').classList.add('hidden'); document.getElementById('planPage').classList.remove('hidden'); document.getElementById('stepTwo').classList.remove('active'); document.getElementById('stepOne').classList.add('active'); window.scrollTo(0,0); }}
continueBtn.addEventListener('click', showPayPage);
function copyPayId() {{ navigator.clipboard?.writeText(document.getElementById('payId').textContent.trim()); tg?.showAlert('Payment details copied'); }}
document.getElementById('paymentForm').addEventListener('submit', async e => {{
  e.preventDefault(); if (!selected) return;
  const user = tg?.initDataUnsafe?.user || {{}}; const fd = new FormData(e.target);
  fd.append('days', selected); fd.append('user_id', user.id || '0'); fd.append('user_name', [user.first_name,user.last_name].filter(Boolean).join(' ') || user.username || 'Unknown'); fd.append('init_data', tg?.initData || '');
  submitBtn.disabled=true; document.getElementById('status').textContent='Submitting payment proof...';
  const res = await fetch('/submit-payment', {{ method:'POST', body:fd }}); const data = await res.json().catch(()=>({{message:'Unexpected response'}}));
  if (res.ok) {{ document.getElementById('status').textContent='✅ Submitted! Admin will verify and activate your plan soon.'; tg?.showAlert('Payment submitted for verification'); }}
  else {{ document.getElementById('status').textContent='❌ '+(data.message || 'Submit failed'); submitBtn.disabled=false; }}
}});
</script>
</body>
</html>
"""


@routes.get("/activate-plan", allow_head=True)
@routes.get("/activate-plan/", allow_head=True)
async def activate_plan_handler(request):
    return web.Response(text=build_premium_payment_page(), content_type="text/html")


@routes.post("/submit-payment")
@routes.post("/submit-payment/")
async def submit_payment_handler(request):
    data = await request.post()
    days = int(data.get("days", 0)) if str(data.get("days", "")).isdigit() else 0
    if days not in PREMIUM_WEB_PLANS:
        return web.json_response({"status": "error", "message": "Invalid plan selected."}, status=400)
    # Identity must come from a signed Telegram WebApp initData, not from a
    # typed user_id: verify the signature and require it to match user_id.
    user = _validate_webapp_init_data(data.get("init_data") or "")
    user_id = str(data.get("user_id") or "0")
    if not user or str(user.get("id")) != user_id or not user_id.isdigit() or int(user_id) <= 0:
        return web.json_response({"status": "error", "message": "Please open this page from the Telegram Web App button."}, status=403)
    slip = data.get("slip")
    if not slip or not getattr(slip, "file", None):
        return web.json_response({"status": "error", "message": "No payment screenshot uploaded."}, status=400)
    file_bytes = slip.file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        return web.json_response({"status": "error", "message": "Image too large. Max 5MB."}, status=413)
    user_name = html.escape(str(data.get("user_name") or "Unknown"))
    plan = PREMIUM_WEB_PLANS[days]
    photo = io.BytesIO(file_bytes)
    photo.name = f"payment_{user_id}_{days}.jpg"
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"accept_payment-{user_id}-{days}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_payment-{user_id}-{days}"),
    ]])
    caption = (f"#Premium_Payment_Request\n\n👤 User: {user_name}\n⚡ User ID: <code>{user_id}</code>\n"
               f"📦 Plan: <b>{plan['name']}</b> ({days} days)\n💰 Price: ₹{plan['price']}")
    try:
        await dreamxbotz.send_photo(PREMIUM_LOGS, photo=photo, caption=caption, reply_markup=buttons)
        await dreamxbotz.send_message(int(user_id), f"✅ Payment proof received for {plan['name']}. Admin will verify and activate your premium soon.")
    except Exception as e:
        logger.exception("Web premium submit failed for user %s: %s", user_id, e)
        return web.json_response({"status": "error", "message": "Could not forward your proof. Please try again or contact support."}, status=502)
    return web.json_response({"status": "success"})


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
