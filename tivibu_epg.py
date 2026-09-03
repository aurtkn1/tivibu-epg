#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import html
import json
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from datetime import datetime, timedelta
from collections import OrderedDict


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS = 7

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


# ============================================================
# HTTP / COOKIE
# ============================================================

COOKIE_JAR = http.cookiejar.CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)

OPENER.addheaders = [
    ("User-Agent", USER_AGENT),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"),
    ("Connection", "keep-alive"),
]


# ============================================================
# GENEL
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(str(value))

    value = value.replace("\xa0", " ")

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def channel_id(name):

    name = clean_text(name).lower()

    table = str.maketrans({
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    })

    name = name.translate(table)

    name = re.sub(
        r"[^a-z0-9]+",
        "_",
        name
    )

    name = re.sub(
        r"_+",
        "_",
        name
    )

    name = name.strip("_")

    return name or "channel"


# ============================================================
# TARİH
# ============================================================

def is_date_text(text):

    text = clean_text(text)

    if re.fullmatch(
        r"\d{2}\.\d{2}\.\d{4}",
        text
    ):
        return True

    if text.lower() in {
        "dün",
        "bugün",
        "yarın",
    }:
        return True

    return False


# ============================================================
# KATEGORİLER
# ============================================================

CATEGORY_NAMES = {
    "canlı tv",
    "canli tv",
    "tüm kanallar",
    "tum kanallar",
    "ulusal",
    "haber",
    "spor",
    "sinema",
    "dizi",
    "belgesel",
    "çocuk",
    "cocuk",
    "müzik",
    "muzik",
    "global",
    "diğer",
    "diger",
    "yaşam-stil",
    "yasam-stil",
}


def is_category(text):

    return clean_text(text).lower() in CATEGORY_NAMES


# ============================================================
# GERÇEK KANAL TESPİTİ
# ============================================================

def looks_like_channel(
    text,
    href="",
    attrs=None
):

    text = clean_text(text)
    href = clean_text(href).lower()

    if not text:
        return False

    if is_date_text(text):
        return False

    if is_category(text):
        return False

    if len(text) > 100:
        return False

    # Program satırları kanal değildir.
    if re.search(
        r"\b\d{1,2}:\d{2}\s*(?:→|->|–|—|-)\s*\d{1,2}:\d{2}\b",
        text
    ):
        return False

    # Tivibu kanal URL'leri.
    if any(x in href for x in [
        "/kanal/",
        "/kanallar/",
        "/channel/",
        "/channels/",
    ]):
        return True

    # Bazı yapılarda href yerine data alanı olabilir.
    if attrs:

        raw = " ".join(
            str(v)
            for k, v in attrs.items()
            if k.startswith("data-")
            or k in {
                "id",
                "class",
                "onclick"
            }
        ).lower()

        if any(x in raw for x in [
            "channel",
            "kanal",
        ]):
            return True

    # Bilinen Tivibu kanal isimleri.
    known_words = [
        "tivibu",
        "sinema",
        "tarih tv",
        "benim kanalım",
        "filmscreen",
        "bbc ",
        "cosmo",
        "show tv",
        "atv",
        "star tv",
        "kanal d",
        "tv8",
        "trt ",
        "ntv",
        "a haber",
        "haber türk",
        "habertürk",
        "cnn türk",
        "tv100",
        "teve2",
        "fox",
        "now",
    ]

    low = text.lower()

    for word in known_words:

        if word in low:
            return True

    return False


# ============================================================
# SAAT
# ============================================================

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2})"
    r"\s*(?:→|->|–|—|-)"
    r"\s*"
    r"(?P<stop>\d{1,2}:\d{2})"
)


def parse_time_range(text):

    text = clean_text(text)

    match = TIME_RANGE_RE.search(
        text
    )

    if not match:
        return None

    return (
        match.group("start"),
        match.group("stop"),
        match
    )


def make_datetime(
    date_obj,
    time_text
):

    try:

        hour, minute = map(
            int,
            time_text.split(":")
        )

        if hour > 23 or minute > 59:
            return None

        return date_obj.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

    except Exception:

        return None


# ============================================================
# PROGRAM AYIKLA
# ============================================================

def extract_program(text):

    text = clean_text(text)

    parsed = parse_time_range(
        text
    )

    if not parsed:
        return None

    start_time, stop_time, match = parsed

    # Saat aralığından önceki bölüm.
    title = text[
        :match.start()
    ].strip()

    # Baştaki gereksiz ayraçları temizle.
    title = re.sub(
        r"^[\s\-–—→]+",
        "",
        title
    ).strip()

    title = re.sub(
        r"[\s\-–—→]+$",
        "",
        title
    ).strip()

    # Sonunda "Canlı" varsa kaldır.
    title = re.sub(
        r"\s+Canlı\s*$",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()

    # Bazı Tivibu satırlarında saat aralığından sonra
    # "Canlı" bulunabilir.
    rest = text[
        match.end():
    ].strip()

    rest = re.sub(
        r"^Canlı\b",
        "",
        rest,
        flags=re.IGNORECASE
    ).strip()

    if not title:
        return None

    # Menü / tarih gibi yanlış eşleşmeleri ele.
    if is_date_text(title):
        return None

    if is_category(title):
        return None

    return {
        "title": title,
        "start": start_time,
        "stop": stop_time,
    }


# ============================================================
# GELİŞMİŞ HTML PARSER
# ============================================================

class TivibuParser(HTMLParser):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.elements = []

        self.stack = []

        self.current = None

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        attrs_dict = dict(
            attrs
        )

        element = {
            "tag": tag.lower(),
            "attrs": attrs_dict,
            "text": "",
            "depth": len(self.stack),
        }

        self.elements.append(
            element
        )

        self.stack.append(
            element
        )

    def handle_startendtag(
        self,
        tag,
        attrs
    ):

        attrs_dict = dict(
            attrs
        )

        element = {
            "tag": tag.lower(),
            "attrs": attrs_dict,
            "text": "",
            "depth": len(self.stack),
        }

        self.elements.append(
            element
        )

    def handle_data(
        self,
        data
    ):

        if self.stack:

            self.stack[-1]["text"] += (
                " " + data
            )

    def handle_endtag(
        self,
        tag
    ):

        tag = tag.lower()

        # Normal durumda son elemanı kapat.
        for i in range(
            len(self.stack) - 1,
            -1,
            -1
        ):

            if self.stack[i]["tag"] == tag:

                self.stack = self.stack[:i]

                break


# ============================================================
# HTML'DEN PROGRAMLARI ÇIKAR
# ============================================================

def parse_html(
    page,
    target_date
):

    parser = TivibuParser()

    try:

        parser.feed(
            page
        )

    except Exception as exc:

        print(
            "HTML parse hatası:",
            exc
        )

    elements = parser.elements

    print(
        "      HTML element sayısı:",
        len(elements)
    )

    channels = OrderedDict()

    programs = []

    current_channel = None

    # ========================================================
    # 1. AŞAMA
    # Bütün elementleri sırayla incele.
    # ========================================================

    for element in elements:

        tag = element["tag"]

        attrs = element["attrs"]

        text = clean_text(
            element["text"]
        )

        href = clean_text(
            attrs.get(
                "href",
                ""
            )
        )

        # ----------------------------------------------------
        # PROGRAM
        # ----------------------------------------------------

        program = extract_program(
            text
        )

        if program:

            if current_channel is None:
                continue

            start_dt = make_datetime(
                target_date,
                program["start"]
            )

            stop_dt = make_datetime(
                target_date,
                program["stop"]
            )

            if not start_dt or not stop_dt:
                continue

            # Gece yarısı.
            if stop_dt <= start_dt:

                stop_dt += timedelta(
                    days=1
                )

            programs.append({
                "channel_id": current_channel["id"],
                "channel_name": current_channel["name"],
                "title": program["title"],
                "start": start_dt,
                "stop": stop_dt,
            })

            continue

        # ----------------------------------------------------
        # KANAL
        # ----------------------------------------------------

        if looks_like_channel(
            text,
            href,
            attrs
        ):

            cid = channel_id(
                text
            )

            if cid not in channels:

                channels[cid] = {
                    "id": cid,
                    "name": text,
                    "href": href,
                }

            current_channel = channels[
                cid
            ]

    # ========================================================
    # 2. AŞAMA
    # Eğer yukarıdaki yöntem program bulamadıysa,
    # ham HTML içindeki tüm metinleri regex ile tara.
    # ========================================================

    if not programs:

        print(
            "      Normal HTML parser program bulamadı."
        )

        print(
            "      Derin metin taraması başlıyor..."
        )

        # Tüm görünür metinleri birleştir.
        raw_text_parts = []

        for element in elements:

            text = clean_text(
                element["text"]
            )

            if text:

                raw_text_parts.append(
                    text
                )

        raw_text = "\n".join(
            raw_text_parts
        )

        # Saat aralığı geçen satırları bul.
        lines = re.split(
            r"[\r\n]+",
            raw_text
        )

        for line in lines:

            line = clean_text(
                line
            )

            if not line:
                continue

            program = extract_program(
                line
            )

            if not program:
                continue

            if current_channel is None:
                continue

            start_dt = make_datetime(
                target_date,
                program["start"]
            )

            stop_dt = make_datetime(
                target_date,
                program["stop"]
            )

            if not start_dt or not stop_dt:
                continue

            if stop_dt <= start_dt:

                stop_dt += timedelta(
                    days=1
                )

            programs.append({
                "channel_id": current_channel["id"],
                "channel_name": current_channel["name"],
                "title": program["title"],
                "start": start_dt,
                "stop": stop_dt,
            })

    # ========================================================
    # DUPLICATE TEMİZLE
    # ========================================================

    unique_programs = []

    seen = set()

    for program in programs:

        key = (
            program["channel_id"],
            program["title"],
            program["start"],
            program["stop"],
        )

        if key in seen:
            continue

        seen.add(key)

        unique_programs.append(
            program
        )

    # ========================================================
    # SONUÇ
    # ========================================================

    print(
        "      Kanal:",
        len(channels)
    )

    print(
        "      Program:",
        len(unique_programs)
    )

    if unique_programs:

        print()
        print(
            "      PROGRAM ÖRNEKLERİ:"
        )

        for program in unique_programs[:20]:

            print(
                "       ",
                program["channel_name"],
                "|",
                program["title"],
                "|",
                program["start"].strftime("%H:%M"),
                "-",
                program["stop"].strftime("%H:%M")
            )

    return (
        list(channels.values()),
        unique_programs
    )


# ============================================================
# SAYFAYI AL
# ============================================================

def get_page():

    request = urllib.request.Request(
        LIVE_TV_URL,
        method="GET"
    )

    request.add_header(
        "User-Agent",
        USER_AGENT
    )

    request.add_header(
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )

    request.add_header(
        "Accept-Language",
        "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
    )

    try:

        with OPENER.open(
            request,
            timeout=30
        ) as response:

            data = response.read()

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            page = data.decode(
                charset,
                errors="replace"
            )

            print(
                "      HTTP:",
                response.status
            )

            print(
                "      HTML:",
                len(page),
                "byte"
            )

            return page

    except urllib.error.HTTPError as exc:

        print(
            "HTTP HATASI:",
            exc.code
        )

        return ""

    except Exception as exc:

        print(
            "BAĞLANTI HATASI:",
            exc
        )

        return ""


# ============================================================
# XMLTV
# ============================================================

def create_xml(
    channels,
    programs
):

    tv = ET.Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",

            "generator-info-url":
                LIVE_TV_URL
        }
    )

    channel_map = OrderedDict()

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    for channel in channels:

        cid = channel["id"]

        name = channel["name"]

        if not cid or not name:
            continue

        if is_category(name):
            continue

        channel_map[
            cid
        ] = name

    # --------------------------------------------------------
    # PROGRAMLARDAN KANAL EKLE
    # --------------------------------------------------------

    for program in programs:

        cid = program[
            "channel_id"
        ]

        name = program[
            "channel_name"
        ]

        if cid and name:

            channel_map.setdefault(
                cid,
                name
            )

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    for cid, name in channel_map.items():

        ch = ET.SubElement(
            tv,
            "channel",
            {
                "id": cid
            }
        )

        display = ET.SubElement(
            ch,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = name

    # --------------------------------------------------------
    # PROGRAM
    # --------------------------------------------------------

    programs = sorted(
        programs,
        key=lambda x: (
            x["channel_id"],
            x["start"]
        )
    )

    for program in programs:

        if program[
            "channel_id"
        ] not in channel_map:

            continue

        p = ET.SubElement(
            tv,
            "programme",
            {
                "start": program["start"].strftime(
                    "%Y%m%d%H%M%S +0300"
                ),

                "stop": program["stop"].strftime(
                    "%Y%m%d%H%M%S +0300"
                ),

                "channel": program[
                    "channel_id"
                ],
            }
        )

        title = ET.SubElement(
            p,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program[
            "title"
        ]

    # --------------------------------------------------------
    # XML YAZ
    # --------------------------------------------------------

    tree = ET.ElementTree(
        tv
    )

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    return (
        len(channel_map),
        len(programs)
    )


# ============================================================
# ANA
# ============================================================

def main():

    print()
    print("=" * 70)
    print("TIVIBU EPG")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # SAYFA
    # --------------------------------------------------------

    print(
        "[1] Tivibu canlı TV sayfası alınıyor..."
    )

    page = get_page()

    if not page:

        print(
            "Sayfa alınamadı."
        )

        return 1

    # --------------------------------------------------------
    # BUGÜN
    # --------------------------------------------------------

    today = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    print()
    print(
        "[2] Kanal ve programlar çıkarılıyor..."
    )

    channels, programs = parse_html(
        page,
        today
    )

    # --------------------------------------------------------
    # 7 GÜN
    # --------------------------------------------------------

    print()
    print(
        "[3] 7 günlük tarih verisi hazırlanıyor..."
    )

    all_channels = OrderedDict()

    all_programs = []

    for channel in channels:

        all_channels[
            channel["id"]
        ] = channel

    # Şimdilik ana HTML'deki programları ekle.
    # Tivibu'nun tarih API'si ayrıca kullanılmadan
    # sayfada gerçekten bulunan programları kaybetmiyoruz.

    all_programs.extend(
        programs
    )

    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    unique = []

    seen = set()

    for program in all_programs:

        key = (
            program["channel_id"],
            program["title"],
            program["start"],
            program["stop"],
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            program
        )

    all_programs = unique

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    print()
    print(
        "[4] epg.xml oluşturuluyor..."
    )

    channel_count, program_count = create_xml(
        list(
            all_channels.values()
        ),
        all_programs
    )

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        "Kanal sayısı :",
        channel_count
    )

    print(
        "Program sayısı:",
        program_count
    )

    print(
        "Dosya:",
        OUTPUT_FILE
    )

    print("=" * 70)
    print()

    if program_count == 0:

        print(
            "UYARI: Program bulunamadı."
        )

        print(
            "Tivibu sayfası programları JavaScript/API ile"
            " sonradan yüklüyor olabilir."
        )

    else:

        print(
            "PROGRAMLAR XML'E YAZILDI."
        )

    return 0


# ============================================================
# ÇALIŞTIR
# ============================================================

if __name__ == "__main__":

    raise SystemExit(
        main()
    )
