"""Minimal Telegram Bot API client: send messages + long-poll commands."""
import asyncio
import html
import logging

import httpx

log = logging.getLogger(__name__)


def esc(s):
    return html.escape(str(s or ""), quote=False)


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = httpx.AsyncClient(timeout=70, trust_env=True)
        self.handlers = {}

    async def close(self):
        await self.http.aclose()

    async def send(self, text, chat_id=None):
        for chunk in _chunks(text, 3900):
            for attempt in range(3):
                try:
                    r = await self.http.post(f"{self.base}/sendMessage", json={
                        "chat_id": chat_id or self.chat_id, "text": chunk,
                        "parse_mode": "HTML", "disable_web_page_preview": True})
                    if r.status_code == 429:
                        wait = r.json().get("parameters", {}).get("retry_after", 3)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code != 200:
                        log.warning("telegram send %s: %s", r.status_code, r.text[:200])
                    break
                except httpx.HTTPError as e:
                    log.warning("telegram send error: %s", e)
                    await asyncio.sleep(2)

    def command(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco

    async def poll_commands(self):
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset:
                    params["offset"] = offset
                r = await self.http.get(f"{self.base}/getUpdates", params=params)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat = str(msg.get("chat", {}).get("id", ""))
                    if not text.startswith("/") or chat != self.chat_id:
                        continue
                    cmd, *args = text.split()
                    cmd = cmd[1:].split("@")[0].lower()
                    fn = self.handlers.get(cmd) or self.handlers.get("help")
                    try:
                        reply = await fn(args)
                    except Exception as e:
                        log.exception("command %s failed", cmd)
                        reply = f"⚠️ /{esc(cmd)} failed: {esc(e)}"
                    if reply:
                        await self.send(reply, chat)
            except Exception as e:
                log.warning("getUpdates error: %s", e)
                await asyncio.sleep(5)


def _chunks(text, n):
    if len(text) <= n:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out
