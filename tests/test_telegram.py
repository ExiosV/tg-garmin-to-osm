import json

from tg_botkit import bot as botmod

from tg_garmin_to_osm.telegram import Telegram


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload


def install_fake_post(monkeypatch, responses):
    """Replace tg_botkit's requests.post; `responses` is a list of Bot API payloads returned in order.
    An `error_code` in a payload is used as the HTTP status (tg_botkit keys its retry logic on it)."""
    calls = []

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append({"url": url, "data": data, "files": files})
        payload = responses.pop(0) if len(responses) > 1 else responses[0]
        return _Resp(status=payload.get("error_code", 200) if isinstance(payload, dict) else 200,
                     payload=payload)

    monkeypatch.setattr(botmod.requests, "post", fake_post)
    return calls


def test_send_targets_chat_no_default_channel(monkeypatch):
    calls = install_fake_post(monkeypatch, [{"ok": True, "result": {"message_id": 5}}])
    tg = Telegram("TOK", "https://api.local")  # no channel: multi-user, every send names its chat
    assert tg.send(-1001, "привет") == 5
    assert calls[0]["url"] == "https://api.local/botTOK/sendMessage"
    assert calls[0]["data"]["chat_id"] == -1001


def test_delete_message(monkeypatch):
    calls = install_fake_post(monkeypatch, [{"ok": True, "result": True}])
    tg = Telegram("TOK", "https://api.local")
    assert tg.delete(-1001, 77) is True
    assert calls[0]["url"].endswith("/deleteMessage")
    assert calls[0]["data"]["message_id"] == 77 and calls[0]["data"]["chat_id"] == -1001


def test_updates_passes_offset_wait_allowed(monkeypatch):
    calls = install_fake_post(monkeypatch, [{"ok": True, "result": [{"update_id": 3}]}])
    tg = Telegram("TOK", "https://api.local")
    assert tg.updates(2, wait=7) == [{"update_id": 3}]
    assert calls[0]["data"]["offset"] == 2 and calls[0]["data"]["timeout"] == 7
    assert json.loads(calls[0]["data"]["allowed_updates"]) == ["message"]


def test_send_html_falls_back_to_plain(monkeypatch):
    bad = {"ok": False, "description": "Bad Request: can't parse entities", "error_code": 400}
    good = {"ok": True, "result": {"message_id": 9}}
    calls = install_fake_post(monkeypatch, [bad, good])
    tg = Telegram("TOK", "https://api.local")
    assert tg.send(-1001, "<b>трек</b> загружен") == 9
    assert calls[0]["data"].get("parse_mode") == "HTML"
    assert "parse_mode" not in calls[1]["data"]
    assert "трек" in calls[1]["data"]["text"]


def test_webhook_info(monkeypatch):
    install_fake_post(monkeypatch, [{"ok": True, "result": {"url": ""}}])
    tg = Telegram("TOK", "https://api.local")
    assert tg.webhook_info() == {"url": ""}
