
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
import urllib.parse
import urllib.request
import http.cookiejar
import xml.etree.ElementTree as ET

from datetime import datetime

# ============================================================
# TIVIBU GO API - EPG TEST
# ============================================================

BASE_URL = "https://itvepg10002.tmp.tivibu.com.tr/iptvepg/frame3046"

CHANNEL_LIST_URL = (
    BASE_URL +
    "/sdk_getchannellist.jsp"
)

CURRENT_PREVUE_URL = (
    BASE_URL +
    "/sdk_getcurrentprevue.jsp"
)

OUTPUT_FILE = "epg.xml"

TIMEZONE = "+0300"

REQUEST_DELAY = 0.15
TIMEOUT = 45
RETRIES = 2

CHANNEL_MINIMUM = 20
PROGRAM_MINIMUM = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

COOKIE_JAR = http.cookiejar.CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================

def clean(value):
    if value is None:
        return ""

    value = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def get_value(item, keys):
    if not isinstance(item, dict):
        return ""

    lowered = {
        str(k).lower(): v
        for k, v in item.items()
    }

    for key in keys:
        value = lowered.get(key.lower())

        if value is not None:
            value = clean(value)

            if value:
                return value

    return ""


def parse_datetime(value):
    value = clean(value)

    if not value:
        return None

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None


# ============================================================
# HTTP İSTEĞİ
# ============================================================

def http_get(url, referer=None):

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Origin": "https://www.tivibu.com.tr",
    }

    if referer:
        headers["Referer"] = referer
    else:
        headers["Referer"] = "https://www.tivibu.com.tr/"

    last_error = None

    for attempt in range(1, RETRIES + 2):

        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET"
        )

        try:
            with OPENER.open(
                request,
                timeout=TIMEOUT
            ) as response:

                raw = response.read()

                charset = (
                    response.headers.get_content_charset()
                    or "utf-8"
                )

                return raw.decode(
                    charset,
                    errors="replace"
                )

        except Exception as exc:

            last_error = exc

            print(
                f"  HTTP denemesi {attempt} başarısız: {exc}"
            )

            time.sleep(1)

    raise RuntimeError(
        f"HTTP isteği başarısız:\n{url}\n{last_error}"
    )


def get_json(url, referer=None):

    response = http_get(
        url,
        referer=referer
    )

    try:
        data = json.loads(response)

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "JSON çözümlenemedi: "
            + response[:500]
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "API beklenen JSON nesnesini döndürmedi."
        )

    return data


# ============================================================
# KANAL LİSTESİ
# ============================================================

def get_channel_list():

    print("\n[1/3] Tivibu kanal listesi alınıyor...")

    params = {
        "columncode": "020000",
        "pageno": "1",
        "numperpage": "500",
        "ordertype": "1",
        "isqueryfavorite": "1",
        "isqueryprevue": "1",
        "ischecklock": "1",
    }

    url = (
        CHANNEL_LIST_URL
        + "?"
        + urllib.parse.urlencode(params)
    )

    data = get_json(url)

    if str(data.get("returncode", "")) != "0":
        raise RuntimeError(
            "Kanal API hatası: "
            + str(data.get("errormsg", ""))
        )

    raw_channels = data.get("data") or []

    channels = {}

    for item in raw_channels:

        if not isinstance(item, dict):
            continue

        code = get_value(
            item,
            [
                "channelcode",
                "channelCode",
                "code",
            ]
        )

        if not code:
            continue

        name = get_value(
            item,
            [
                "channelname",
                "channelName",
                "name",
                "displayname",
                "displayName",
                "title",
            ]
        )

        if not name:
            name = code

        icon = get_value(
            item,
            [
                "channelimage",
                "channelImage",
                "channelicon",
                "channelIcon",
                "image",
                "logo",
            ]
        )

        channels[code] = {
            "name": name,
            "icon": icon,
        }

    print(
        f"  API toplam kanal sayısı: "
        f"{data.get('totalcount', '?')}"
    )

    print(
        f"  Eşleştirilen kanal: {len(channels)}"
    )

    if len(channels) < CHANNEL_MINIMUM:
        raise RuntimeError(
            f"Kanal sayısı yetersiz: {len(channels)}. "
            "Mevcut XML korunuyor."
        )

    return channels


# ============================================================
# PROGRAM VERİLERİNİ BUL
# ============================================================

def find_program_records(data):

    records = []

    def walk(obj):

        if isinstance(obj, dict):

            if (
                get_value(
                    obj,
                    ["prevuename", "prevueName"]
                )
                and get_value(
                    obj,
                    ["begintime", "beginTime"]
                )
                and get_value(
                    obj,
                    ["endtime", "endTime"]
                )
            ):

                records.append(obj)
                return

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):

            for value in obj:
                walk(value)

    walk(data)

    return records


# ============================================================
# BİR KANALIN PROGRAMLARINI AL
# ============================================================

def get_channel_programs(channel_code):

    params = {
        "channelcodes": channel_code,
    }

    url = (
        CURRENT_PREVUE_URL
        + "?"
        + urllib.parse.urlencode(params)
    )

    data = get_json(url)

    if str(data.get("returncode", "")) != "0":

        raise RuntimeError(
            str(data.get("errormsg", "Bilinmeyen API hatası"))
        )

    records = find_program_records(data)

    programs = []

    for item in records:

        code = get_value(
            item,
            [
                "channelcode",
                "channelCode",
            ]
        ) or channel_code

        begin_value = get_value(
            item,
            [
                "begintime",
                "beginTime",
            ]
        )

        end_value = get_value(
            item,
            [
                "endtime",
                "endTime",
            ]
        )

        title = get_value(
            item,
            [
                "prevuename",
                "prevueName",
                "name",
            ]
        )

        begin_dt = parse_datetime(begin_value)
        end_dt = parse_datetime(end_value)

        if not begin_dt or not end_dt:
            continue

        if end_dt <= begin_dt:
            continue

        if not title:
            continue

        # ÖNEMLİ:
        # UTC alanları kullanılmıyor.
        # begintime ve endtime Türkiye yerel saatidir.

        programs.append({
            "channel_code": code,
            "begin_dt": begin_dt,
            "end_dt": end_dt,
            "title": title,

            "description": get_value(
                item,
                [
                    "description",
                    "desc",
                ]
            ),

            "genre": get_value(
                item,
                [
                    "genre",
                    "category",
                ]
            ),

            "rating": get_value(
                item,
                [
                    "ratingid",
                ]
            ),
        })

    return programs


# ============================================================
# XML OLUŞTUR
# ============================================================

def add_text(parent, tag, value, lang="tr"):

    if not value:
        return

    node = ET.SubElement(
        parent,
        tag,
        {"lang": lang}
    )

    node.text = clean(value)


def build_xml(channels, programs):

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu Go API EPG",
            "generator-info-url": "https://www.tivibu.com.tr/",
        }
    )

    # Kanallar

    for code in sorted(
        channels,
        key=lambda x: channels[x]["name"].casefold()
    ):

        info = channels[code]

        channel = ET.SubElement(
            root,
            "channel",
            {"id": code}
        )

        add_text(
            channel,
            "display-name",
            info["name"]
        )

        icon = info.get("icon", "")

        if icon.startswith("http"):

            ET.SubElement(
                channel,
                "icon",
                {"src": icon}
            )

    # Programlar

    for program in sorted(
        programs,
        key=lambda p: (
            p["begin_dt"],
            p["channel_code"]
        )
    ):

        begin_dt = program["begin_dt"]
        end_dt = program["end_dt"]

        node = ET.SubElement(
            root,
            "programme",
            {
                "start": (
                    begin_dt.strftime("%Y%m%d%H%M%S")
                    + " "
                    + TIMEZONE
                ),

                "stop": (
                    end_dt.strftime("%Y%m%d%H%M%S")
                    + " "
                    + TIMEZONE
                ),

                "channel": program["channel_code"],
            }
        )

        add_text(
            node,
            "title",
            program["title"]
        )

        add_text(
            node,
            "desc",
            program.get("description", "")
        )

        add_text(
            node,
            "category",
            program.get("genre", "")
        )

        if program.get("rating"):

            add_text(
                node,
                "rating",
                program["rating"],
                lang=""
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


# ============================================================
# ANA İŞLEM
# ============================================================

def main():

    started = time.time()

    print("=" * 65)
    print("TIVIBU GO API EPG TEST")
    print("=" * 65)

    channels = get_channel_list()

    all_programs = {}
    failed_channels = 0
    successful_channels = 0

    print("\n[2/3] Kanal programları alınıyor...")

    total = len(channels)

    for index, (code, info) in enumerate(
        channels.items(),
        1
    ):

        print(
            f"[{index}/{total}] "
            f"{info['name']} ",
            end=""
        )

        try:

            programs = get_channel_programs(code)

            new_count = 0

            for program in programs:

                key = (
                    program["channel_code"],
                    program["begin_dt"],
                    program["end_dt"],
                    program["title"],
                )

                if key in all_programs:
                    continue

                all_programs[key] = program
                new_count += 1

            successful_channels += 1

            print(
                f"-> {new_count} program"
            )

        except Exception as exc:

            failed_channels += 1

            print(
                f"-> HATA: {exc}"
            )

        time.sleep(REQUEST_DELAY)

    programs = list(all_programs.values())

    print("\n[3/3] Sonuçlar kontrol ediliyor...")

    print("-" * 65)

    print(
        f"Toplam kanal: {len(channels)}"
    )

    print(
        f"Başarılı kanal: {successful_channels}"
    )

    print(
        f"Hatalı kanal: {failed_channels}"
    )

    print(
        f"Toplam program: {len(programs)}"
    )

    if not programs:

        raise RuntimeError(
            "Hiç program alınamadı. Mevcut XML korunuyor."
        )

    if len(programs) < PROGRAM_MINIMUM:

        raise RuntimeError(
            f"Program sayısı çok düşük: {len(programs)}. "
            "Mevcut XML korunuyor."
        )

    # Programı olmayan kanallar XML kanal listesinde kalır.

    channel_codes = set(channels)

    invalid = [
        p for p in programs
        if p["channel_code"] not in channel_codes
    ]

    if invalid:

        raise RuntimeError(
            f"{len(invalid)} programın kanal eşleşmesi yok. "
            "XML yazılmadı."
        )

    # XML oluştur

    xml_bytes = build_xml(
        channels,
        programs
    )

    # Önce geçici dosyaya yaz

    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "wb") as file:
        file.write(xml_bytes)

    # Eski XML ancak başarılı kontrolden sonra değiştirilir.

    os.replace(
        temp_file,
        OUTPUT_FILE
    )

    dates = sorted({
        p["begin_dt"].date()
        for p in programs
    })

    print("\n" + "=" * 65)
    print("İŞLEM TAMAMLANDI")
    print("=" * 65)

    print(
        f"Kanal: {len(channels)}"
    )

    print(
        f"Program: {len(programs)}"
    )

    print(
        f"Program bulunan tarih: {len(dates)}"
    )

    if dates:

        print(
            f"İlk tarih: {dates[0]}"
        )

        print(
            f"Son tarih: {dates[-1]}"
        )

    print(
        f"XML dosyası: {OUTPUT_FILE}"
    )

    print(
        f"Süre: {time.time() - started:.1f} saniye"
    )

    print("=" * 65)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:

        print("\nİŞLEM DURDURULDU!")
        print(str(exc))
        print("Mevcut epg.xml korunmuştur.")

        raise
