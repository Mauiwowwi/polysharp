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
        self.callbacks = {}     # prefix -> async fn(data) for inline-button taps

    async def close(self):
        await self.http.aclose()

    async def send(self, text, chat_id=None, buttons=None):
        """buttons: list of rows, each a list of (label, callback_data) tuples."""
        chunks = _chunks(text, 3900)
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id or self.chat_id, "text": chunk,
                       "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": lbl, "callback_data": data} for lbl, data in row] for row in buttons]}
            await self._post_message(payload)

    async def _post_message(self, payload):
        for attempt in range(3):
            try:
                r = await self.http.post(f"{self.base}/sendMessage", json=payload)
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

    def callback(self, prefix):
        def deco(fn):
            self.callbacks[prefix] = fn
            return fn
        return deco

    async def answer_callback(self, cb_id, text=None):
        try:
            await self.http.post(f"{self.base}/answerCallbackQuery",
                                 json={"callback_query_id": cb_id, **({"text": text} if text else {})})
        except httpx.HTTPError:
            pass

    def command(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco

    async def _on_callback(self, cb):
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
        data = cb.get("data") or ""
        if chat != self.chat_id:
            await self.answer_callback(cb.get("id"))
            return
        prefix = data.split(":", 1)[0]
        fn = self.callbacks.get(prefix)
        await self.answer_callback(cb.get("id"), "Loading…" if fn else None)
        if not fn:
            return
        try:
            reply = await fn(data.split(":", 1)[1] if ":" in data else "")
        except Exception as e:
            log.exception("callback %s failed", data)
            reply = f"⚠️ failed: {esc(e)}"
        if isinstance(reply, tuple):
            await self.send(reply[0], chat, buttons=reply[1])
        elif reply:
            await self.send(reply, chat)

    async def poll_commands(self):
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message","callback_query"]'}
                if offset:
                    params["offset"] = offset
                r = await self.http.get(f"{self.base}/getUpdates", params=params)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    if upd.get("callback_query"):
                        await self._on_callback(upd["callback_query"])
                        continue
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
                    if isinstance(reply, tuple):          # (text, buttons)
                        await self.send(reply[0], chat, buttons=reply[1])
                    elif reply:
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
