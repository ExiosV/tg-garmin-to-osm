import json
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

    def buttons(self, chat_id, text: str, rows) -> int:
        """Send text with an inline keyboard; rows = [[("Label", "callback_data"), ...], ...]."""
        keyboard = [[{"text": label, "callback_data": data} for label, data in row] for row in rows]
        return self.bot.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                             disable_web_page_preview="true",
                             reply_markup=json.dumps({"inline_keyboard": keyboard}))

    def answer_callback(self, callback_query_id: str) -> None:
        try:
            self.bot.call("answerCallbackQuery", callback_query_id=callback_query_id)
        except Exception as e:  # answering is cosmetic; never fail the update loop over it
            log.debug("answerCallbackQuery failed: %s", e)

    def photo(self, chat_id, caption: str, image: bytes, filename: str = "step.png") -> int:
        """One local photo with a caption (guide screenshots)."""
        return self.bot.call("sendPhoto", chat_id=chat_id, caption=caption, parse_mode="HTML",
                             files={"photo": (filename, image)})

    def reply_msg(self, msg: dict, text: str, silent: bool = False) -> int:
        return self.send((msg.get("chat") or {}).get("id"), text, silent)

    def delete(self, chat_id, message_id) -> bool:
        return self.bot.delete(message_id, chat_id=chat_id)

    def updates(self, offset: int, wait: int = 25) -> list:
        return self.bot.updates(offset, wait=wait, allowed=("message", "callback_query"))

    def whoami(self) -> dict:
        return self.bot.whoami()

    def chat_info(self, chat_id) -> dict:
        return self.bot.call("getChat", chat_id=chat_id)

    def webhook_info(self) -> dict:
        return self.bot.call("getWebhookInfo")
