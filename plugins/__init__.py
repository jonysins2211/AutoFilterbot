from aiohttp import web
from .route import routes
from .website import routes as website_routes
from .miniapp import routes as miniapp_routes
from asyncio import sleep 
from datetime import datetime
from database.users_chats_db import db
from info import URL, PREMIUM_LOGS
from Script import script
import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

async def web_server():
    web_app = web.Application(client_max_size=30000000)
    # Order matters: website (/) and miniapp (/app) first, then Auto's stream
    # routes. Auto's catch-all /{path} in route.py would otherwise swallow them.
    web_app.add_routes(website_routes)
    web_app.add_routes(miniapp_routes)
    web_app.add_routes(routes)
    return web_app

async def check_expired_premium(client):
    while 1:
        data = await db.get_expired(datetime.now())
        for user in data:
            user_id = user["id"]
            await db.remove_premium_access(user_id)
            try:
                user = await client.get_users(user_id)
                await client.send_message(
                    chat_id=user_id,
                    text=script.PREMIUM_END_TEXT.format(user.mention)
                )
                await client.send_message(PREMIUM_LOGS, text=f"<b>#Premium_Expire\n\nUser name: {user.mention}\nUser id: <code>{user_id}</code>")
            except Exception as e:
                if "CHAT_ID_INVALID" in str(e) or "UserIsBlocked" in str(e) or "PEER_ID_INVALID" in str(e):
                    logger.info("Premium expire notification: User %s has invalid chat or blocked the bot.", user_id)
                else:
                    logger.warning("Premium expire notification error for user %s: %s", user_id, e)
            await sleep(0.5)
        await sleep(60)

async def keep_alive():
    """Keep bot alive by sending periodic pings."""
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(298)
            try:
                async with session.get(URL) as resp:
                    if resp.status != 200:
                        logger.info(f"⚠️ Ping Error! Status: {resp.status}")
            except Exception as e:
                logger.info(f"❌ Ping Failed: {e}")           

