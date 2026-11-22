
import json
import os
import re
import time
import html
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright


# ============================================================
# AYARLAR
# ============================================================

BASE_API = (
    "https://itvepg10002.tmp.tivibu.com.tr/"
    "iptvepg/frame3046/"
)

CHANNEL_LIST_URL = BASE_API + "sdk_getchannellist.jsp"

OUTPUT_FILE = "epg.xml"
ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7

MIN_CHANNELS = 30
MIN_PROGRAMS = 100
MIN_DAYS = 6

TIMEOUT_MS = 30000

CHANNEL_LIST_PARAMS = {
    "columncode": "020000",
    "pageno": "1",
    "numperpage": "500",
    "ordertype": "1",
    "isqueryfavorite": "1",
    "isqueryprevue": "1",
    "ischecklock": "1",
}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tivibu.com.tr/canli-tv",
    "Origin": "https://www.tivibu.com.tr",
}


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================

def clean_text(value):
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)
        value = value.replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")

    return value or "channel"


def parse_api_datetime(value):
    value = clean_text(value)

    if not value:
        return None

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            result = datetime.strptime(value, fmt)
            return result.replace(tzinfo=ISTANBUL)
        except ValueError:
            pass

    return None


def get_json_response(response, description):
    status = response.status
    body = response.text()

    if status != 200:
        raise RuntimeError(
            f"{description}: HTTP {status}\n"
            f"{body[:500]}"
        )

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{description}: JSON çözümlenemedi.\n"
            f"Sunucu cevabı:\n{body[:700]}"
        )


def walk_objects(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_objects(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def first_value(item, keys):
    for key in keys:
        value = item.get(key)

        if value is not None and clean_text(value):
            return clean_text(value)

    return ""


# ============================================================
# KANAL LİSTESİ
# ============================================================

def get_channels(api_request):
    print("\nKANAL LİSTESİ ALINIYOR...", flush=True)

    url = CHANNEL_LIST_URL + "?" + urlencode(
        CHANNEL_LIST_PARAMS
    )

    response = api_request.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT_MS,
    )

    payload = get_json_response(
        response,
        "Kanal listesi",
    )

    channels = {}

    name_keys = [
        "channelname",
        "channelName",
        "displayname",
        "displayName",
        "name",
        "channeltitle",
        "channelTitle",
    ]

    for item in walk_objects(payload):
        code = first_value(
            item,
            ["channelcode", "channelCode"],
        ).lower()

        if not re.fullmatch(r"ch\d+", code):
            continue

        name = first_value(item, name_keys)

        if not name:
            continue

        channels[code] = {
            "name": name,
        }

    if not channels:
        print(
            "\nKanal listesi cevabından kanal bulunamadı.",
            flush=True,
        )
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
            )[:2000],
            flush=True,
        )

        raise RuntimeError(
            "Kanal listesi çözümlenemedi."
        )

    print(
        f"Bulunan kanal sayısı: {len(channels)}",
        flush=True,
    )

    return channels


# ============================================================
# GÜNLÜK PROGRAM LİSTESİ
# ============================================================

def get_day_programs(
    api_request,
    channel_code,
    selected_day,
):
    date_text = selected_day.strftime("%Y.%m.%d")

    params = {
        "channelcode": channel_code,
        "begintime": date_text + " 00:00:00",
        "endtime": date_text + " 23:59:59",
    }

    url = (
        BASE_API
        + "sdk_getprevuellist.jsp?"
        + urlencode(params)
    )

    response = api_request.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT_MS,
    )

    payload = get_json_response(
        response,
        f"{channel_code} / {date_text}",
    )

    result = []

    for item in walk_objects(payload):
        code = first_value(
            item,
            ["channelcode", "channelCode"],
        ).lower()

        # Yanıtta farklı kanal kayıtları varsa alma.
        if code and code != channel_code:
            continue

        title = first_value(
            item,
            [
                "prevuename",
                "prevueName",
                "programname",
                "programName",
                "title",
            ],
        )

        if not title:
            continue

        start = parse_api_datetime(
            first_value(
                item,
                ["begintime", "beginTime"],
            )
        )

        stop = parse_api_datetime(
            first_value(
                item,
                ["endtime", "endTime"],
            )
        )

        if not start or not stop:
            continue

        if stop <= start:
            continue

        if (stop - start).total_seconds() > 24 * 3600:
            continue

        description = first_value(
            item,
            ["description", "desc"],
        )

        genre = first_value(
            item,
            ["genre", "category"],
        )

        result.append({
            "title": title,
            "description": description,
            "genre": genre,
            "start": start,
            "stop": stop,
        })

    return result


# ============================================================
# TÜM KANALLAR VE 7 GÜN
# ============================================================

def collect_programs(api_request, channels, today):
    all_programs = {}

    window_start = datetime.combine(
        today,
        datetime.min.time(),
        tzinfo=ISTANBUL,
    )

    last_day = today + timedelta(days=DAYS)

    window_end = datetime.combine(
        last_day,
        datetime.min.time(),
        tzinfo=ISTANBUL,
    )

    channel_total = len(channels)

    for channel_number, (code, info) in enumerate(
        channels.items(),
        start=1,
    ):
        channel_name = info["name"]

        print(
            f"\n[{channel_number}/{channel_total}] "
            f"{channel_name}",
            flush=True,
        )

        channel_count = 0

        for day_number in range(DAYS):
            selected_day = today + timedelta(
                days=day_number
            )

            date_label = selected_day.strftime(
                "%d.%m.%Y"
            )

            try:
                programs = get_day_programs(
                    api_request,
                    code,
                    selected_day,
                )

            except Exception as error:
                print(
                    f"  {date_label}: HATA - {error}",
                    flush=True,
                )
                raise

            new_count = 0

            for program in programs:
                start = program["start"]
                stop = program["stop"]

                # İstenen 7 günlük pencerenin dışında kalanları alma.
                if stop <= window_start:
                    continue

                if start >= window_end:
                    continue

                key = (
                    code,
                    start,
                    stop,
                    program["title"].casefold(),
                )

                if key in all_programs:
                    continue

                all_programs[key] = {
                    "channel_code": code,
                    "channel_name": channel_name,
                    **program,
                }

                new_count += 1
                channel_count += 1

            print(
                f"  {date_label}: "
                f"{len(programs)} kayıt, "
                f"{new_count} yeni program",
                flush=True,
            )

        print(
            f"  Kanal toplamı: {channel_count}",
            flush=True,
        )

    return list(all_programs.values())


# ============================================================
# XMLTV OLUŞTURMA
# ============================================================

def build_xml(channels, programs):
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": (
                "https://www.tivibu.com.tr/canli-tv"
            ),
        },
    )

    used_ids = set()
    channel_ids = {}

    for code, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold(),
    ):
        base = slugify(info["name"])
        channel_id = base
        suffix = 2

        while channel_id in used_ids:
            channel_id = f"{base}_{suffix}"
            suffix += 1

        used_ids.add(channel_id)
        channel_ids[code] = channel_id

        node = ET.SubElement(
            root,
            "channel",
            {"id": channel_id},
        )

        display = ET.SubElement(
            node,
            "display-name",
            {"lang": "tr"},
        )

        display.text = info["name"]

    for program in sorted(
        programs,
        key=lambda item: (
            item["start"],
            item["channel_code"],
        ),
    ):
        node = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[
                    program["channel_code"]
                ],
                "start": program["start"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": program["stop"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
            },
        )

        title = ET.SubElement(
            node,
            "title",
            {"lang": "tr"},
        )

        title.text = program["title"]

        if program.get("description"):
            desc = ET.SubElement(
                node,
                "desc",
                {"lang": "tr"},
            )

            desc.text = program["description"]

        if program.get("genre"):
            category = ET.SubElement(
                node,
                "category",
                {"lang": "tr"},
            )

            category.text = program["genre"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True,
    )

    os.replace(
        temp_file,
        OUTPUT_FILE,
    )


# ============================================================
# VERİ KONTROLÜ
# ============================================================

def validate_data(channels, programs):
    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Kanal sayısı yetersiz: {len(channels)}. "
            f"Minimum: {MIN_CHANNELS}. "
            "Mevcut epg.xml korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Program sayısı yetersiz: {len(programs)}. "
            f"Minimum: {MIN_PROGRAMS}. "
            "Mevcut epg.xml korunuyor."
        )

    days = {
        item["start"].date()
        for item in programs
    }

    if len(days) < MIN_DAYS:
        raise RuntimeError(
            f"Yalnızca {len(days)} yayın günü bulundu. "
            f"Minimum: {MIN_DAYS}. "
            "Mevcut epg.xml korunuyor."
        )


# ============================================================
# ANA İŞLEM
# ============================================================

def main():
    started = time.time()

    today = datetime.now(
        ISTANBUL
    ).date()

    print("=" * 65)
    print("TIVIBU 7 GÜNLÜK API EPG")
    print("=" * 65)

    print(
        "Başlangıç tarihi:",
        today.strftime("%d.%m.%Y"),
    )

    print(
        "Bitiş tarihi:",
        (today + timedelta(days=DAYS - 1)).strftime(
            "%d.%m.%Y"
        ),
    )

    print(
        "API:",
        BASE_API,
    )

    print("=" * 65)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
        )

        try:
            # Önce Tivibu ana sayfasına giderek oturum bağlamı oluştur.
            page = context.new_page()

            print(
                "\nTivibu ana sayfası açılıyor...",
                flush=True,
            )

            page.goto(
                "https://www.tivibu.com.tr/canli-tv",
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )

            api_request = context.request

            channels = get_channels(
                api_request
            )

            programs = collect_programs(
                api_request,
                channels,
                today,
            )

        finally:
            browser.close()

    print("\nVERİ KONTROLÜ YAPILIYOR...", flush=True)

    validate_data(
        channels,
        programs,
    )

    build_xml(
        channels,
        programs,
    )

    days = sorted({
        item["start"].date()
        for item in programs
    })

    print("\n" + "=" * 65)
    print("EPG BAŞARIYLA OLUŞTURULDU")
    print("=" * 65)

    print("Kanal:", len(channels))
    print("Program:", len(programs))
    print("Yayın günü:", len(days))

    print(
        "İlk gün:",
        days[0].strftime("%d.%m.%Y"),
    )

    print(
        "Son gün:",
        days[-1].strftime("%d.%m.%Y"),
    )

    print("Dosya:", OUTPUT_FILE)

    print(
        "Süre:",
        f"{time.time() - started:.1f} saniye",
    )

    print("=" * 65)


if __name__ == "__main__":
    main()
