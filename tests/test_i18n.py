from tg_garmin_to_osm import i18n
from tg_garmin_to_osm.config import DEFAULT_TYPES, TYPE_GROUPS, expand_types, parse_types_setting


def test_normalize():
    assert i18n.normalize("ru") == i18n.RU
    assert i18n.normalize("ru-RU") == i18n.RU
    assert i18n.normalize("en") == i18n.EN
    assert i18n.normalize("de") == i18n.EN
    assert i18n.normalize(None) == i18n.EN
    assert i18n.normalize("") == i18n.EN


def test_phone_lang():
    assert i18n.phone_lang("+79161234567") == i18n.RU
    assert i18n.phone_lang("79161234567") == i18n.RU
    assert i18n.phone_lang("+35799123456") is None  # Cyprus decides nothing
    assert i18n.phone_lang("") is None
    assert i18n.phone_lang(None) is None


def test_text_prefers_stored_language_then_client_code_then_english():
    assert "Трек загружен" in i18n.text("ru", "uploaded", desc="", url="", id=1, vis="identifiable")
    assert "Trace uploaded" in i18n.text("en", "uploaded", desc="", url="", id=1, vis="")
    assert "Trace uploaded" in i18n.text("", "uploaded", "de", desc="", url="", id=1, vis="")
    assert "Trace uploaded" in i18n.text("", "uploaded", desc="", url="", id=1, vis="")
    # an unknown stored language falls to the client's language code (ru-RU -> Russian)
    assert "Трек загружен" in i18n.text("xx", "uploaded", "ru-RU", desc="", url="", id=1, vis="")


def test_start_mentions_linking_steps_both_languages():
    for lang in (i18n.RU, i18n.EN):
        s = i18n.text(lang, "start")
        assert "/garmin" in s and "/login" in s and "iD" in s


def test_typo_is_dead():
    for catalog in i18n.MESSAGES.values():
        for key, tpl in catalog.items():
            if isinstance(tpl, str):
                assert "Трейк" not in tpl, key


def test_type_groups_and_expansion():
    assert DEFAULT_TYPES == "bike"
    assert {"bike", "run", "walk", "water", "winter", "fitness"} <= set(TYPE_GROUPS)
    bike = expand_types("bike")
    assert "cycling" in bike and "gravel_cycling" in bike and "running" not in bike
    mixed = expand_types("bike, walk,Swimming")  # group + group + raw key, any case/spaces
    assert {"cycling", "walking", "swimming"} <= mixed
    assert expand_types("") == expand_types(DEFAULT_TYPES)  # empty -> default
    assert "triathlon" in expand_types("triathlon")  # unknown keys pass through as raw typeKeys
