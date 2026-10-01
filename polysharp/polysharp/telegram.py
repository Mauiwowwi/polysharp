"""Minimal Telegram Bot API client: send messages + long-poll commands."""
import asyncio
import html
import logging

import httpx

log = logging.getLogger(__name__)


def esc(s):
    return html.escape(str(s or ""), quote=False)


class Telegram:
    def __init__(self, token, chat_ids, admin_ids=()):
        """chat_ids: one id or a comma list. Alerts go to all of them; commands are
        accepted from any of them. admin_ids: Telegram USER ids allowed to run
        admin-only commands. If none are given, positive chat ids (private chats,
        whose id equals the user's id) count as admins."""
        self.base = f"https://api.telegram.org/bot{token}"
        ids = [c.strip() for c in str(chat_ids).split(",") if c.strip()]
        self.chat_ids = ids
        self.chat_id = ids[0] if ids else ""
        self.admins = {str(a).strip() for a in admin_ids if str(a).strip()} or \
            {c for c in ids if not c.startswith("-")}
        self.admin_only = set()
        self.is_admin = False      # set per command so handlers (e.g. /help) can adapt
        self.current_chat = self.current_thread = None
        self.topics = {}           # chat_id -> {sport: message_thread_id}
        self.all_feed = True       # also post every alert to the General tab
        self.http = httpx.AsyncClient(timeout=70, trust_env=True)
        self.handlers = {}
        self.callbacks = {}     # prefix -> async fn(data) for inline-button taps

    async def close(self):
        await self.http.aclose()

    async def send(self, text, chat_id=None, buttons=None, thread_id=None):
        """buttons: list of rows, each a list of (label, callback_data) tuples.
        chat_id None -> broadcast to every feed chat. thread_id -> a forum topic."""
        if chat_id is None and len(self.chat_ids) > 1:
            for c in self.chat_ids:
                await self.send(text, c, buttons)
            return
        chunks = _chunks(text, 3900)
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id or self.chat_id, "text": chunk,
                       "parse_mode": "HTML", "disable_web_page_preview": True}
            if thread_id:
                payload["message_thread_id"] = int(thread_id)
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

    async def send_alert(self, text, sport="other"):
        """Bet alerts: every feed chat; in topic-enabled groups also the sport's tab."""
        for c in self.chat_ids:
            tid = (self.topics.get(c) or {}).get(sport) or (self.topics.get(c) or {}).get("other")
            if tid:
                await self.send(text, c, thread_id=tid)
                if self.all_feed:
                    await self.send(text, c)          # General tab = the "All" feed
            else:
                await self.send(text, c)

    async def create_topic(self, chat_id, name):
        """Returns (thread_id, error)."""
        try:
            r = await self.http.post(f"{self.base}/createForumTopic",
                                     json={"chat_id": chat_id, "name": name})
            j = r.json()
            if j.get("ok"):
                return j["result"]["message_thread_id"], None
            return None, j.get("description") or f"HTTP {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            return None, str(e)

    async def send_admins(self, text, buttons=None):
        """Private messages to each admin (they must have /start-ed the bot once)."""
        for a in sorted(self.admins) or [self.chat_id]:
            await self.send(text, a, buttons)

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
        thread = (cb.get("message") or {}).get("message_thread_id")
        data = cb.get("data") or ""
        if chat not in self.chat_ids and chat not in self.admins:
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
            await self.send(reply[0], chat, buttons=reply[1], thread_id=thread)
        elif reply:
            await self.send(reply, chat, thread_id=thread)

    async def handle_update(self, upd):
        if upd.get("callback_query"):
            await self._on_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        user = str((msg.get("from") or {}).get("id", ""))
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        if not text.startswith("/") or (chat not in self.chat_ids and chat not in self.admins):
            return
        cmd, *args = text.split()
        cmd = cmd[1:].split("@")[0].lower()
        self.is_admin = user in self.admins
        self.current_chat, self.current_thread = chat, thread
        if cmd in self.admin_only and not self.is_admin:
            await self.send("🔒 Only the bot admin can do that. Try /top10, /wallets or /help.", chat,
                            thread_id=thread)
            return
        fn = self.handlers.get(cmd) or self.handlers.get("help")
        if fn is None:
            return
        try:
            reply = await fn(args)
        except Exception as e:
            log.exception("command %s failed", cmd)
            reply = f"⚠️ /{esc(cmd)} failed: {esc(e)}"
        if isinstance(reply, tuple):          # (text, buttons)
            await self.send(reply[0], chat, buttons=reply[1], thread_id=thread)
        elif reply:
            await self.send(reply, chat, thread_id=thread)

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
                    await self.handle_update(upd)
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
