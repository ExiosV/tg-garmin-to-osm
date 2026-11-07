import logging

from tg_botkit import Bot

log = logging.getLogger("tg_garmin_to_osm")
UPDATES_OFFSET = "updates_offset"


class Telegram:
    """Thin wrapper over the shared tg_botkit.Bot; every send names its chat — a multi-user service
    has no default channel."""
    def __init__(self, token: str, api_base: str):
        self.bot = Bot(token, api_base)

    def send(self, chat_id, text: str, silent: bool = False) -> int:
        return self.bot.send(text, chat_id=chat_id, silent=silent)

    def reply_msg(self, msg: dict, text: str, silent: bool = False) -> int:
        return self.send((msg.get("chat") or {}).get("id"), text, silent)

    def delete(self, chat_id, message_id) -> bool:
        return self.bot.delete(message_id, chat_id=chat_id)

    def updates(self, offset: int, wait: int = 25) -> list:
        return self.bot.updates(offset, wait=wait, allowed=("message",))

    def whoami(self) -> dict:
        return self.bot.whoami()

    def chat_info(self, chat_id) -> dict:
        return self.bot.call("getChat", chat_id=chat_id)

    def webhook_info(self) -> dict:
        return self.bot.call("getWebhookInfo")
