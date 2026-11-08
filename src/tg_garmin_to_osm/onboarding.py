"""The /start onboarding dialog: inline buttons instead of commands. A small state machine walks the
user through Garmin (Personal API key with a picture guide) and OSM (site or out-of-band code).
Callback data and pending-dialog stages live here; everything is bilingual (see i18n)."""
import logging

log = logging.getLogger("tg_garmin_to_osm")

# callback data
CB_LINK_MENU = "onb:link"
CB_GARMIN_START = "onb:garmin"
CB_GARMIN_HAVE_KEY = "onb:garmin:key"
CB_GARMIN_GUIDE = "onb:garmin:guide"
CB_OSM_MENU = "onb:osm"
CB_OSM_SITE = "onb:osm:site"
CB_OSM_CODE = "onb:osm:code"
CB_STATUS = "onb:status"
CB_BACK = "onb:back"

# pending stages (stored in PENDING[uid]["stage"])
STAGE_GARMIN_KEY = "onb_garmin_key"
STAGE_OSM_CODE = "osm_code"  # legacy stage id, reused from the /login fallback


def main_menu_rows(lang: str):
    """(text-key, rows) for the main onboarding menu."""
    if lang == "ru":
        rows = [
            [("🔗 Привязать Garmin", CB_GARMIN_START)],
            [("🗺 Привязать OSM", CB_OSM_MENU)],
            [("📊 Мой статус", CB_STATUS)],
        ]
        text = ("Что делаем дальше? Всё можно пройти кнопками — команды вводить не нужно.\n"
                "Garmin привязывается Personal API ключом (покажу, где его взять), OSM — через "
                "сайт или код.")
    else:
        rows = [
            [("🔗 Link Garmin", CB_GARMIN_START)],
            [("🗺 Link OSM", CB_OSM_MENU)],
            [("📊 My status", CB_STATUS)],
        ]
        text = ("What next? Everything is button-driven — no commands to type.\n"
                "Garmin links with a Personal API key (I will show you where to get it), OSM via "
                "the site or a code.")
    return text, rows


def garmin_menu_rows(lang: str, linked: bool):
    if lang == "ru":
        text = (
            "🔗 <b>Привязка Garmin</b>\n\n"
            "1. Откройте страницу ключа в Garmin Connect (со смартфона или компьютера, вы должны "
            "быть залогинены):\nhttps://connect.garmin.com/app/settings/personalApiToken\n"
            "2. Нажмите «Create Key», примите условия, выберите максимальный срок действия.\n"
            "3. Скопируйте показанный ключ (показывается один раз!) и нажмите кнопку ниже — "
            "пришлёте его следующим сообщением. Сообщение с ключом удаляется сразу.\n\n"
            "Ключ даёт боту доступ только к чтению ваших активностей и отзывается на той же "
            "странице в любой момент."
        )
        rows = [[("🔑 У меня есть ключ — ввести", CB_GARMIN_HAVE_KEY)],
                [("🖼 Показать картинки-подсказки", CB_GARMIN_GUIDE)],
                [("⬅️ Назад", CB_BACK)]]
        if linked:
            text = "✅ Garmin уже привязан.\n\n" + text
            rows.append([("🚫 Отвязать Garmin", "onb:garmin:unlink")])
    else:
        text = (
            "🔗 <b>Link Garmin</b>\n\n"
            "1. Open the key page in Garmin Connect (phone or computer, signed in):\n"
            "https://connect.garmin.com/app/settings/personalApiToken\n"
            "2. Press “Create Key”, accept the terms, choose the longest expiration.\n"
            "3. Copy the shown key (it is displayed only once!) and press the button below — you "
            "will send it as the next message. The message with the key is deleted immediately.\n\n"
            "The key gives the bot read-only access to your activities and can be revoked on the "
            "same page at any time."
        )
        rows = [[("🔑 I have the key — enter it", CB_GARMIN_HAVE_KEY)],
                [("🖼 Show me the pictures", CB_GARMIN_GUIDE)],
                [("⬅️ Back", CB_BACK)]]
        if linked:
            text = "✅ Garmin is already linked.\n\n" + text
            rows.append([("🚫 Unlink Garmin", "onb:garmin:unlink")])
    return text, rows


def garmin_guide_caption(lang: str) -> str:
    if lang == "ru":
        return ("🖼 Garmin Connect → Account Settings → вкладка «Personal API Key» → "
                "кнопка «Create Key». Если ключ уже есть — он показан как «Active», "
                "нажмите ниже, чтобы ввести его.")
    return ("🖼 Garmin Connect → Account Settings → the “Personal API Key” tab → the "
            "“Create Key” button. If a key already exists it shows as “Active” — "
            "press the button below to enter it.")


def osm_menu_rows(lang: str, linked: bool):
    if lang == "ru":
        text = (
            "🗺 <b>Привязка OpenStreetMap</b>\n\n"
            "Вариант 1 (проще): сайт — войдите через Telegram и нажмите «Link OSM», доступ "
            "подтвердите прямо на openstreetmap.org.\n"
            "Вариант 2: получите код по ссылке и пришлите его сюда сообщением."
        )
        rows = [[("🌐 Через сайт", CB_OSM_SITE)],
                [("🔑 Код из браузера", CB_OSM_CODE)],
                [("⬅️ Назад", CB_BACK)]]
        if linked:
            text = "✅ OSM уже привязан.\n\n" + text
            rows.append([("🚫 Отвязать OSM", "onb:osm:unlink")])
    else:
        text = (
            "🗺 <b>Link OpenStreetMap</b>\n\n"
            "Option 1 (easier): the site — sign in with Telegram and press “Link OSM”; you "
            "confirm access on openstreetmap.org itself.\n"
            "Option 2: get a code from the link and paste it here as a message."
        )
        rows = [[("🌐 Via the website", CB_OSM_SITE)],
                [("🔑 Code from the browser", CB_OSM_CODE)],
                [("⬅️ Back", CB_BACK)]]
        if linked:
            text = "✅ OSM is already linked.\n\n" + text
            rows.append([("🚫 Unlink OSM", "onb:osm:unlink")])
    return text, rows
