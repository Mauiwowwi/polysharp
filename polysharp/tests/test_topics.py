import pytest

from polysharp.markets import SPORTS, sport_of
from polysharp.telegram import Telegram

GROUP, ME = "-1001234567890", "555"


def test_sport_buckets():
    assert sport_of("nfl") == "football" and sport_of("CFB") == "football"
    assert sport_of("mlb") == "baseball" and sport_of("euroleague") == "basketball"
    assert sport_of("lal") == "soccer" and sport_of("unl") == "soccer"
    assert sport_of("atp") == "tennis" and sport_of("ufc") == "fighting"
    assert sport_of("") == "other" and sport_of("cs2") == "other"


def tg_capture(all_feed=True):
    tg = Telegram("x", GROUP, admin_ids=[ME])
    tg.all_feed = all_feed
    posted = []

    async def fake_post(payload):
        posted.append((payload["chat_id"], payload.get("message_thread_id"), payload["text"]))
    tg._post_message = fake_post
    return tg, posted


@pytest.mark.asyncio
async def test_alert_goes_to_sport_tab_and_general():
    tg, posted = tg_capture()
    tg.topics = {GROUP: {"football": 11, "soccer": 22, "other": 99}}
    await tg.send_alert("NFL bet", "football")
    await tg.send_alert("darts bet", "darts")          # unknown sport -> Other tab
    assert posted == [(GROUP, 11, "NFL bet"), (GROUP, None, "NFL bet"),
                      (GROUP, 99, "darts bet"), (GROUP, None, "darts bet")]
    await tg.close()


@pytest.mark.asyncio
async def test_all_feed_off_and_no_topics():
    tg, posted = tg_capture(all_feed=False)
    tg.topics = {GROUP: {"tennis": 7}}
    await tg.send_alert("ATP bet", "tennis")
    tg.topics = {}
    await tg.send_alert("plain group", "tennis")         # no topics -> just the group
    assert posted == [(GROUP, 7, "ATP bet"), (GROUP, None, "plain group")]
    await tg.close()


@pytest.mark.asyncio
async def test_command_reply_stays_in_its_topic():
    tg, posted = tg_capture()

    @tg.command("top10")
    async def _t(args):
        return "list"
    await tg.handle_update({"message": {"chat": {"id": int(GROUP)}, "from": {"id": 777},
                                        "text": "/top10", "message_thread_id": 22,
                                        "is_topic_message": True}})
    assert posted == [(GROUP, 22, "list")]
    await tg.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", GROUP)
    monkeypatch.setenv("ADMIN_USER_IDS", ME)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    from polysharp.main import App
    return App()


@pytest.mark.asyncio
async def test_ensure_topics_creates_all_tabs_once(app):
    made = []

    async def fake_create(chat, name):
        made.append(name)
        return 100 + len(made), None
    app.tg.create_topic = fake_create
    line = await app.ensure_topics()
    assert line == f"Sport topics: {len(SPORTS)} tabs ready"
    assert made[0] == "🏈 Football" and len(made) == len(SPORTS)
    assert app.store.get("topics")[GROUP]["football"] == 101
    await app.ensure_topics()                              # restart: nothing re-created
    assert len(made) == len(SPORTS)


@pytest.mark.asyncio
async def test_ensure_topics_explains_missing_forum(app):
    async def fake_create(chat, name):
        return None, "Bad Request: the chat is not a forum"
    app.tg.create_topic = fake_create
    line = await app.ensure_topics()
    assert "not set up" in line and "Topics turned on" in line
    assert app.tg.topics == {}


@pytest.mark.asyncio
async def test_bindtopic_inside_a_tab(app):
    sent = []

    async def fake_send(text, chat_id=None, buttons=None, thread_id=None):
        sent.append((chat_id, thread_id, text))
    app.tg.send = fake_send
    await app.tg.handle_update({"message": {"chat": {"id": int(GROUP)}, "from": {"id": int(ME)},
                                            "text": "/bindtopic soccer", "message_thread_id": 42,
                                            "is_topic_message": True}})
    assert app.tg.topics[GROUP]["soccer"] == 42 and app.store.get("topics")[GROUP]["soccer"] == 42
    assert sent[-1][1] == 42 and "Soccer alerts will post in this tab" in sent[-1][2]


OLD, NEW = "-4991234567", "-1002991234567"


def migrating_transport(log):
    import json as _json

    import httpx

    def handler(request):
        body = _json.loads(request.content or b"{}")
        log.append((request.url.path.rsplit("/", 1)[-1], str(body.get("chat_id"))))
        if str(body.get("chat_id")) == OLD:
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                             "parameters": {"migrate_to_chat_id": int(NEW)}})
        if request.url.path.endswith("createForumTopic"):
            return httpx.Response(200, json={"ok": True, "result": {"message_thread_id": 500 + len(log)}})
        return httpx.Response(200, json={"ok": True, "result": {}})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_supergroup_migration_is_followed(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OLD)
    monkeypatch.setenv("ADMIN_USER_IDS", ME)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    from polysharp.main import App
    app = App()
    calls = []
    app.tg.http = httpx.AsyncClient(transport=migrating_transport(calls))
    line = await app.ensure_topics()
    assert line == f"Sport topics: {len(SPORTS)} tabs ready"
    assert app.tg.chat_ids == [NEW] and NEW in app.tg.topics and OLD not in app.tg.topics
    assert app.store.get("chat_migrations") == {OLD: NEW}
    # admin was told the new id
    assert any(m == ("sendMessage", ME) for m in calls)
    # alerts now go to the new group
    calls.clear()
    await app.tg.send_alert("NFL bet", "football")
    assert all(chat == NEW for _, chat in calls) and len(calls) == 2
    # a restart with the OLD env var still lands on the new id
    app2 = App()
    app2.tg.http = httpx.AsyncClient(transport=migrating_transport([]))
    await app2.ensure_topics()
    assert app2.tg.chat_ids == [NEW]
    await app.tg.close()
    await app2.tg.close()
