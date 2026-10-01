import pytest

from polysharp.telegram import Telegram

GROUP, ME, FRIEND, STRANGER_CHAT = "-1001234567890", "555", "777", "999"


def tg_with_handlers():
    tg = Telegram("x", f"{GROUP},{ME}", admin_ids=[ME])
    sent = []

    async def fake_send(text, chat_id=None, buttons=None):
        sent.append((chat_id, text))
    tg.send = fake_send
    tg.admin_only = {"add", "remove", "mute"}

    @tg.command("top10")
    async def _t(args):
        return "top list"

    @tg.command("add")
    async def _a(args):
        return "added"

    @tg.command("help")
    async def _h(args):
        return "admin help" if tg.is_admin else "public help"
    return tg, sent


def msg(chat, user, text):
    return {"message": {"chat": {"id": int(chat)}, "from": {"id": int(user)}, "text": text}}


@pytest.mark.asyncio
async def test_group_member_can_read_but_not_admin():
    tg, sent = tg_with_handlers()
    await tg.handle_update(msg(GROUP, FRIEND, "/top10@WhaletailBot"))
    await tg.handle_update(msg(GROUP, FRIEND, "/add 0xabc"))
    await tg.handle_update(msg(GROUP, FRIEND, "/help"))
    assert sent[0] == (GROUP, "top list")
    assert sent[1][0] == GROUP and "Only the bot admin" in sent[1][1]
    assert sent[2] == (GROUP, "public help")


@pytest.mark.asyncio
async def test_admin_can_do_everything_in_group_and_dm():
    tg, sent = tg_with_handlers()
    await tg.handle_update(msg(GROUP, ME, "/add 0xabc"))
    await tg.handle_update(msg(ME, ME, "/help"))
    assert sent == [(GROUP, "added"), (ME, "admin help")]


@pytest.mark.asyncio
async def test_unknown_chat_ignored():
    tg, sent = tg_with_handlers()
    await tg.handle_update(msg(STRANGER_CHAT, STRANGER_CHAT, "/top10"))
    assert sent == []


def test_default_admin_is_private_chat_id():
    tg = Telegram("x", f"{GROUP},{ME}")
    assert tg.admins == {ME} and tg.chat_ids == [GROUP, ME]
    assert Telegram("x", GROUP).admins == set()


@pytest.mark.asyncio
async def test_broadcast_and_admin_dm():
    tg = Telegram("x", f"{GROUP},{ME}", admin_ids=[ME])
    posted = []

    async def fake_post(payload):
        posted.append(payload["chat_id"])
    tg._post_message = fake_post
    await tg.send("alert")                      # feed alert -> both chats
    await tg.send_admins("shortlist")           # digest -> only me
    assert posted == [GROUP, ME, ME]
    await tg.close()
