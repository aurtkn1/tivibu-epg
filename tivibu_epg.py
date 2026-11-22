
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TIVIBU 7 GÜNLÜK EPG
Tivibu'nun sdk_getchannellist.jsp ve sdk_getprevuellist.jsp
servislerinden kanal ve program bilgilerini alır.

- Gerçek kanal koduyla eşleştirme yapar.
- 7 günü ayrı ayrı sorgular.
- Türkiye yerel saatini kullanır (+0300).
- Gece yarısını aşan programları korur.
- Bütün kontroller geçmeden mevcut epg.xml dosyasını değiştirmez.
"""

import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------
# AYARLAR
# ---------------------------------------------------------

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = "https://www.tivibu.com.tr/canli-tv"

API_BASE = (
    "https://itvepg10002.tmp.tivibu.com.tr/"
    "iptvepg/frame3046/"
)

CHANNEL_LIST_URL = API_BASE + "sdk_getchannellist.jsp"
PREVUE_LIST_URL = API_BASE + "sdk_getprevuellist.jsp"

OUTPUT_FILE = "epg.xml"

DAYS = 7

TIMEZONE = ZoneInfo("Europe/Istanbul")

REQUEST_TIMEOUT = 60000
RETRY_COUNT = 3

# İstekler arasında kısa bekleme
REQUEST_DELAY = 0.20

# EPG kontrol sınırları
MIN_CHANNELS = 30
MIN_PROGRAMS = 100
MIN_DAYS = 6

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------
# GENEL YARDIMCILAR
# ---------------------------------------------------------

def clean_text(value):
    if value is None:
        return ""

    value = str(value)
    value = value.replace("\xa0", " ")

    return re.sub(r"\s+", " ", value).strip()


def parse_datetime(value):
    value = clean_text(value)

    if not value:
        return None

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    )

    for fmt in formats:
        try:
            result = datetime.strptime(value, fmt)
            return result.replace(tzinfo=TIMEZONE)
        except ValueError:
            continue

    try:
        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=TIMEZONE)

        return result.astimezone(TIMEZONE)

    except ValueError:
        return None


def format_api_date(day, end=False):
    if end:
        value = datetime(
            day.year,
            day.month,
            day.day,
            23,
            59,
            59
        )
    else:
        value = datetime(
            day.year,
            day.month,
            day.day,
            0,
            0,
            0
        )

    return value.strftime("%Y.%m.%d %H:%M:%S")


def first_value(item, keys):
    if not isinstance(item, dict):
        return ""

    for key in keys:
        value = item.get(key)

        if value is not None:
            value = clean_text(value)

            if value:
                return value

    return ""


# ---------------------------------------------------------
# JSON YANITINI OKUMA
# ---------------------------------------------------------

def parse_json_response(response_text, url):
    response_text = response_text.lstrip("\ufeff").strip()

    if not response_text:
        raise RuntimeError(
            f"Servis boş yanıt döndürdü.\nURL: {url}"
        )

    try:
        data = json.loads(response_text)

    except json.JSONDecodeError as exc:
        preview = clean_text(response_text[:350])

        raise RuntimeError(
            "Tivibu servisi JSON yerine farklı içerik döndürdü.\n"
            f"URL: {url}\n"
            f"Yanıt başlangıcı: {preview}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Beklenen JSON nesnesi alınamadı: {url}"
        )

    return data


# ---------------------------------------------------------
# GERÇEK TARAYICI ÜZERİNDEN İSTEK
# ---------------------------------------------------------

class TivibuBrowser:

    def __init__(self, page):
        self.page = page

    def get_json(self, url, description):
        last_error = None

        for attempt in range(1, RETRY_COUNT + 1):

            try:
                print(
                    f"  {description} "
                    f"(deneme {attempt}/{RETRY_COUNT})"
                )

                response = self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=REQUEST_TIMEOUT
                )

                if response is None:
                    raise RuntimeError(
                        "Tarayıcıdan HTTP yanıtı alınamadı."
                    )

                status = response.status

                if status != 200:
                    raise RuntimeError(
                        f"HTTP durum kodu: {status}"
                    )

                response_text = response.text()

                data = parse_json_response(
                    response_text,
                    url
                )

                return data

            except Exception as exc:
                last_error = exc

                print(f"  Hata: {exc}")

                if attempt < RETRY_COUNT:
                    time.sleep(attempt * 2)

        raise RuntimeError(
            f"{description} alınamadı.\n"
            f"Son hata: {last_error}"
        ) from last_error


# ---------------------------------------------------------
# KANAL LİSTESİ
# ---------------------------------------------------------

def get_channels(browser):

    print("\nKANAL LİSTESİ ALINIYOR...")

    params = {
        "columncode": "020000",
        "pageno": "1",
        "numperpage": "500",
        "ordertype": "1",
        "isqueryfavorite": "1",
        "isqueryprevue": "1",
        "ischecklock": "1",
    }

    url = CHANNEL_LIST_URL + "?" + urllib.parse.urlencode(params)

    data = browser.get_json(
        url,
        "Kanal listesi"
    )

    if str(data.get("returncode", "0")) != "0":
        raise RuntimeError(
            "Kanal listesi servisi hata döndürdü: "
            + clean_text(data.get("errormsg"))
        )

    rows = data.get("data", [])

    if not isinstance(rows, list):
        raise RuntimeError(
            "Kanal listesindeki data alanı liste değil."
        )

    channels = {}

    for item in rows:

        if not isinstance(item, dict):
            continue

        code = first_value(
            item,
            [
                "channelcode",
                "channelCode",
                "ChannelCode",
            ]
        )

        name = first_value(
            item,
            [
                "channelname",
                "channelName",
                "ChannelName",
            ]
        )

        if not code or not name:
            continue

        icon = first_value(
            item,
            [
                "channelimage",
                "channelImage",
                "channelicon",
                "channelIcon",
                "icon",
                "image",
            ]
        )

        channels[code] = {
            "name": name,
            "icon": icon,
        }

    print(f"Bulunan kanal: {len(channels)}")

    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal bulundu. "
            f"En az {MIN_CHANNELS} kanal bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    return channels


# ---------------------------------------------------------
# KANALIN GÜNLÜK PROGRAMLARI
# ---------------------------------------------------------

def get_channel_programs(browser, channel_code, day):

    params = {
        "channelcode": channel_code,
        "begintime": format_api_date(day),
        "endtime": format_api_date(day, end=True),
    }

    url = PREVUE_LIST_URL + "?" + urllib.parse.urlencode(params)

    data = browser.get_json(
        url,
        f"{channel_code} programları"
    )

    if str(data.get("returncode", "0")) != "0":
        error = clean_text(data.get("errormsg"))

        raise RuntimeError(
            f"{channel_code} program servisi hata döndürdü: "
            f"{error}"
        )

    rows = data.get("data", [])

    if not isinstance(rows, list):
        raise RuntimeError(
            f"{channel_code} için data alanı liste değil."
        )

    return rows


# ---------------------------------------------------------
# XMLTV PROGRAM KAYDI
# ---------------------------------------------------------

def normalize_program(item, channel_code, window_start, window_end):

    if not isinstance(item, dict):
        return None

    # ÖNEMLİ:
    # UTC alanları kullanılmıyor.
    # Tivibu'nun yerel saat alanları kullanılıyor.

    begin_value = first_value(
        item,
        [
            "begintime",
            "beginTime",
            "BeginTime",
        ]
    )

    end_value = first_value(
        item,
        [
            "endtime",
            "endTime",
            "EndTime",
        ]
    )

    title = first_value(
        item,
        [
            "prevuename",
            "prevueName",
            "PrevueName",
        ]
    )

    if not title:
        return None

    begin_dt = parse_datetime(begin_value)
    end_dt = parse_datetime(end_value)

    if not begin_dt or not end_dt:
        return None

    if end_dt <= begin_dt:
        return None

    # Günlük sorgu penceresiyle kesişmeyen kayıtları alma.
    # Önceki gün başlayıp bu gün devam eden programlar korunur.
    if end_dt <= window_start:
        return None

    if begin_dt >= window_end:
        return None

    return {
        "channel_code": channel_code,
        "begin_dt": begin_dt,
        "end_dt": end_dt,
        "title": title,
        "description": first_value(
            item,
            [
                "description",
                "Description",
            ]
        ),
        "genre": first_value(
            item,
            [
                "genre",
                "Genre",
            ]
        ),
        "rating": first_value(
            item,
            [
                "ratingid",
                "ratingId",
                "RatingId",
            ]
        ),
    }


# ---------------------------------------------------------
# XML YAZMA
# ---------------------------------------------------------

def add_text(parent, tag, value, lang=None):

    attrs = {}

    if lang:
        attrs["lang"] = lang

    node = ET.SubElement(
        parent,
        tag,
        attrs
    )

    node.text = clean_text(value)

    return node


def build_xml_bytes(channels, programs):

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_URL,
        }
    )

    # KANALLAR

    for code in sorted(
        channels,
        key=lambda item: channels[item]["name"].casefold()
    ):

        info = channels[code]

        channel = ET.SubElement(
            root,
            "channel",
            {
                "id": code
            }
        )

        add_text(
            channel,
            "display-name",
            info["name"],
            "tr"
        )

        icon = info.get("icon", "")

        if icon.startswith("http://") or icon.startswith("https://"):
            ET.SubElement(
                channel,
                "icon",
                {
                    "src": icon
                }
            )

    # PROGRAMLAR

    for program in sorted(
        programs,
        key=lambda item: (
            item["begin_dt"],
            item["channel_code"]
        )
    ):

        node = ET.SubElement(
            root,
            "programme",
            {
                "start": program["begin_dt"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": program["end_dt"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "channel": program["channel_code"],
            }
        )

        add_text(
            node,
            "title",
            program["title"],
            "tr"
        )

        if program["description"]:
            add_text(
                node,
                "desc",
                program["description"],
                "tr"
            )

        if program["genre"]:
            add_text(
                node,
                "category",
                program["genre"],
                "tr"
            )

        if program["rating"]:
            add_text(
                node,
                "rating",
                program["rating"]
            )

    ET.indent(
        root,
        space="  "
    )

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True
    )


# ---------------------------------------------------------
# 7 GÜNLÜK EPG TOPLAMA
# ---------------------------------------------------------

def collect_epg(browser, channels):

    today = datetime.now(TIMEZONE).date()

    all_programs = {}

    successful_days = 0
    total_received = 0

    channel_items = list(channels.items())

    for day_offset in range(DAYS):

        selected_day = today + timedelta(days=day_offset)

        window_start = datetime(
            selected_day.year,
            selected_day.month,
            selected_day.day,
            0,
            0,
            0,
            tzinfo=TIMEZONE
        )

        next_day = selected_day + timedelta(days=1)

        window_end = datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            0,
            0,
            0,
            tzinfo=TIMEZONE
        )

        print("\n" + "=" * 65)

        print(
            f"GÜN {day_offset + 1}/{DAYS}: "
            f"{selected_day.strftime('%d.%m.%Y')}"
        )

        print("=" * 65)

        day_received = 0
        day_added = 0
        channel_errors = 0

        for channel_index, (channel_code, channel_info) in enumerate(
            channel_items,
            1
        ):

            channel_name = channel_info["name"]

            print(
                f"[{channel_index}/{len(channel_items)}] "
                f"{channel_name}",
                flush=True
            )

            try:

                rows = get_channel_programs(
                    browser,
                    channel_code,
                    selected_day
                )

                day_received += len(rows)
                total_received += len(rows)

                for item in rows:

                    program = normalize_program(
                        item,
                        channel_code,
                        window_start,
                        window_end
                    )

                    if not program:
                        continue

                    key = (
                        program["channel_code"],
                        program["begin_dt"],
                        program["end_dt"],
                        program["title"].casefold()
                    )

                    if key in all_programs:
                        continue

                    all_programs[key] = program
                    day_added += 1

                print(
                    f"    {len(rows)} kayıt alındı."
                )

            except Exception as exc:

                channel_errors += 1

                print(
                    f"    KANAL HATASI: {exc}"
                )

            time.sleep(REQUEST_DELAY)

        # Günün tamamında hiç program alınamadıysa
        # o gün başarılı kabul edilmez.
        if day_received > 0:
            successful_days += 1

        print("\nGÜN SONUCU:")
        print(f"Alınan ham kayıt: {day_received}")
        print(f"Yeni program: {day_added}")
        print(f"Hatalı kanal isteği: {channel_errors}")

        if day_received == 0:
            raise RuntimeError(
                f"{selected_day.strftime('%d.%m.%Y')} günü için "
                "hiç program alınamadı. Mevcut XML korunuyor."
            )

    return list(all_programs.values()), successful_days, total_received


# ---------------------------------------------------------
# DOĞRULAMA
# ---------------------------------------------------------

def validate_data(channels, programs, successful_days):

    print("\nVERİ KONTROLÜ")

    print(f"Kanal sayısı: {len(channels)}")
    print(f"Program sayısı: {len(programs)}")
    print(f"Başarılı gün: {successful_days}/{DAYS}")

    if successful_days != DAYS:
        raise RuntimeError(
            f"Yalnızca {successful_days}/{DAYS} gün alındı. "
            "Mevcut XML korunuyor."
        )

    if len(channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal bulundu. "
            "Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            "Mevcut XML korunuyor."
        )

    days_found = {
        item["begin_dt"].date()
        for item in programs
    }

    if len(days_found) < MIN_DAYS:
        raise RuntimeError(
            f"Yalnızca {len(days_found)} farklı yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )

    channel_codes = set(channels.keys())

    for item in programs:

        if item["channel_code"] not in channel_codes:
            raise RuntimeError(
                "Kanal eşleşmesi bulunamayan program var. "
                "Mevcut XML korunuyor."
            )

        if item["end_dt"] <= item["begin_dt"]:
            raise RuntimeError(
                "Bitiş saati başlangıç saatinden önce olan "
                "program bulundu. Mevcut XML korunuyor."
            )

    print("Veri kontrolleri başarılı.")


# ---------------------------------------------------------
# ATOMİK XML KAYDI
# ---------------------------------------------------------

def save_xml(channels, programs):

    xml_bytes = build_xml_bytes(
        channels,
        programs
    )

    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "wb") as file:

        file.write(xml_bytes)
        file.flush()
        os.fsync(file.fileno())

    os.replace(
        temp_file,
        OUTPUT_FILE
    )

    print(f"\nXML kaydedildi: {OUTPUT_FILE}")


# ---------------------------------------------------------
# ANA PROGRAM
# ---------------------------------------------------------

def main():

    started = time.time()

    print("=" * 65)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("API - GERÇEK TARAYICI OTURUMU")
    print("=" * 65)

    with sync_playwright() as playwright:

        browser_instance = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser_instance.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

        page = context.new_page()

        page.set_default_navigation_timeout(
            REQUEST_TIMEOUT
        )

        try:

            print("\nTivibu ana sayfası açılıyor...")

            page.goto(
                LIVE_URL,
                wait_until="domcontentloaded",
                timeout=REQUEST_TIMEOUT
            )

            page.wait_for_timeout(3000)

            browser = TivibuBrowser(page)

            channels = get_channels(browser)

            programs, successful_days, total_received = collect_epg(
                browser,
                channels
            )

            validate_data(
                channels,
                programs,
                successful_days
            )

            save_xml(
                channels,
                programs
            )

            print("\n" + "=" * 65)
            print("7 GÜNLÜK EPG BAŞARIYLA OLUŞTURULDU")
            print("=" * 65)

            print(f"Kanal: {len(channels)}")
            print(f"Program: {len(programs)}")
            print(f"Ham kayıt: {total_received}")
            print(f"Başarılı gün: {successful_days}/{DAYS}")
            print(f"Dosya: {OUTPUT_FILE}")
            print(
                f"Çalışma süresi: "
                f"{time.time() - started:.1f} saniye"
            )

            print("=" * 65)

        finally:

            context.close()
            browser_instance.close()


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print("\n" + "!" * 65)
        print("EPG OLUŞTURULAMADI")
        print("!" * 65)
        print(str(exc))
        print("Mevcut epg.xml değiştirilmedi.")
        raise
