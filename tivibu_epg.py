#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import json
import re
import html
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from html.parser import HTMLParser
from collections import OrderedDict


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"
API_URL = BASE_URL + "/Channel/GetMultiPrevueData"

OUTPUT_FILE = "epg.xml"

DAYS = 7

CHANNEL_COLUMN_CODE = "020002"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


# ============================================================
# COOKIE
# ============================================================

COOKIE_JAR = http.cookiejar.CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)

OPENER.addheaders = [
    ("User-Agent", USER_AGENT),
    (
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    (
        "Accept-Language",
        "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
    ),
    ("Connection", "keep-alive"),
]


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_channel_id(name):

    name = clean_text(name).lower()

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
        name = name.replace(old, new)

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

    return name[:100] or "channel"


def parse_time(value):

    if value is None:
        return None

    match = re.search(
        r"\b(\d{1,2}):(\d{2})\b",
        str(value)
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def make_datetime(date_obj, value):

    parsed = parse_time(value)

    if parsed is None:
        return None

    hour, minute = parsed

    return date_obj.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )


def xmltv_time(dt):

    return dt.strftime(
        "%Y%m%d%H%M%S +0300"
    )


# ============================================================
# TARİH KONTROLÜ
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
# KATEGORİ KONTROLÜ
# ============================================================

CATEGORY_NAMES = {
    "film",
    "dizi",
    "kirala & satın al",
    "çocuk",
    "spor",
    "tivibu nedir?",
    "diğer",
    "global",
    "favori kanallarım",
    "tüm kanallar",
    "ulusal",
    "müzik",
    "yaşam-stil",
    "haber",
    "belgesel",
    "sinema",
    "canlı tv",
    "canli tv",
    "cocuk",
    "muzik",
    "yasam-stil",
    "diger",
}


def is_category(text):

    return clean_text(text).lower() in CATEGORY_NAMES


# ============================================================
# PROGRAM SAAT ARALIĞI
# ============================================================

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2})"
    r"\s*(?:→|->|–|—|-)"
    r"\s*"
    r"(?P<stop>\d{1,2}:\d{2})"
)


# ============================================================
# HTML ANCHOR PARSER
# ============================================================

class TivibuHTMLParser(HTMLParser):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.anchors = []

        self.in_anchor = False

        self.current_href = ""
        self.current_text = []

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        if tag.lower() != "a":
            return

        attrs_dict = dict(attrs)

        self.in_anchor = True

        self.current_href = (
            attrs_dict.get(
                "href",
                ""
            )
            or ""
        )

        self.current_text = []

    def handle_data(self, data):

        if self.in_anchor:

            self.current_text.append(
                data
            )

    def handle_endtag(self, tag):

        if tag.lower() != "a":
            return

        if not self.in_anchor:
            return

        text = clean_text(
            " ".join(
                self.current_text
            )
        )

        self.anchors.append({
            "href": self.current_href,
            "text": text
        })

        self.in_anchor = False

        self.current_href = ""

        self.current_text = []


# ============================================================
# PROGRAM METNİNİ AYIR
# ============================================================

def parse_program_text(text):

    text = clean_text(text)

    match = TIME_RANGE_RE.search(
        text
    )

    if not match:
        return None

    start = match.group(
        "start"
    )

    stop = match.group(
        "stop"
    )

    title_part = text[
        :match.start()
    ].strip()

    # Saatten önceki son "-" karakteri
    # kategori ayırıcı olabilir.
    title_part = re.sub(
        r"\s*-\s*$",
        "",
        title_part
    ).strip()

    # Canlı yazısını temizle.
    title_part = re.sub(
        r"\s+Canlı\s*$",
        "",
        title_part,
        flags=re.IGNORECASE
    ).strip()

    if not title_part:
        return None

    category = ""

    category_match = re.search(
        r"\s+(Film|Dizi|Spor|Haber|Belgesel|Çocuk|Müzik|"
        r"Yaşam|Yaşam-Stil|Sinema|Ulusal|Global|Diğer)\s*$",
        title_part,
        flags=re.IGNORECASE
    )

    if category_match:

        category = category_match.group(
            1
        ).strip()

        title_part = title_part[
            :category_match.start()
        ].strip()

    if not title_part:
        return None

    return {
        "title": title_part,
        "category": category,
        "start": start,
        "stop": stop
    }


# ============================================================
# GERÇEK KANAL LİNKİ Mİ?
# ============================================================

def is_real_channel_link(
    href,
    text
):

    href = clean_text(
        href
    ).lower()

    text = clean_text(
        text
    )

    if not href or not text:
        return False

    # Program bağlantısı değildir.
    if TIME_RANGE_RE.search(text):
        return False

    # Tarih sekmeleri değildir.
    if is_date_text(text):
        return False

    # Kategori değildir.
    if is_category(text):
        return False

    # Çok uzun içerik başlığı değildir.
    if len(text) > 100:
        return False

    # Genel site linklerini ele.
    blocked = {
        "ana sayfa",
        "canlı tv",
        "canli tv",
        "programlar",
        "paketler",
        "kampanyalar",
        "giriş yap",
        "üye ol",
        "arama",
        "menü",
        "favoriler",
    }

    if text.lower() in blocked:
        return False

    # Gerçek Tivibu kanal bağlantıları.
    if (
        "/kanal/" in href
        or "/kanallar/" in href
        or "/channel/" in href
        or "/channels/" in href
    ):
        return True

    return False


# ============================================================
# HTML'DEN PROGRAMLARI ÇEK
#
# ÖNEMLİ:
# Tivibu sayfasında yapı:
#
# KANAL
# PROGRAM
# PROGRAM
# PROGRAM
# ...
# KANAL
# PROGRAM
# PROGRAM
#
# Dolayısıyla programın href'ine bakarak kanal
# bulmaya çalışmıyoruz.
# SON GÖRÜLEN KANALI aktif kanal kabul ediyoruz.
# ============================================================

def parse_html(
    page,
    target_date
):

    parser = TivibuHTMLParser()

    try:

        parser.feed(
            page
        )

    except Exception as exc:

        print(
            "      HTML parser hatası:",
            exc
        )

        return [], []

    anchors = parser.anchors

    print(
        f"      HTML anchor sayısı: {len(anchors)}"
    )

    channels = OrderedDict()

    programs = []

    current_channel = None

    # --------------------------------------------------------
    # SAYFAYI SIRAYLA GEZ
    # --------------------------------------------------------

    for item in anchors:

        href = clean_text(
            item.get(
                "href",
                ""
            )
        )

        text = clean_text(
            item.get(
                "text",
                ""
            )
        )

        if not text:
            continue

        # ----------------------------------------------------
        # PROGRAM MI?
        # ----------------------------------------------------

        parsed_program = parse_program_text(
            text
        )

        if parsed_program is not None:

            # Henüz kanal görmediysek programı atla.
            if current_channel is None:
                continue

            start_dt = make_datetime(
                target_date,
                parsed_program["start"]
            )

            stop_dt = make_datetime(
                target_date,
                parsed_program["stop"]
            )

            if start_dt is None or stop_dt is None:
                continue

            # Gece yarısını geçiyorsa.
            if stop_dt <= start_dt:

                stop_dt += timedelta(
                    days=1
                )

            programs.append({
                "channel_id": current_channel["id"],
                "channel_name": current_channel["name"],
                "title": parsed_program["title"],
                "category": parsed_program["category"],
                "start": start_dt,
                "stop": stop_dt
            })

            continue

        # ----------------------------------------------------
        # KANAL MI?
        # ----------------------------------------------------

        if is_real_channel_link(
            href,
            text
        ):

            channel_id = normalize_channel_id(
                text
            )

            channel = {
                "id": channel_id,
                "name": text,
                "href": href
            }

            # Aynı kanal tekrar gelirse
            # mevcut kaydı kullan.
            if channel_id not in channels:

                channels[channel_id] = channel

            current_channel = channels[
                channel_id
            ]

    # --------------------------------------------------------
    # DUPLICATE PROGRAMLARI TEMİZLE
    # --------------------------------------------------------

    unique_programs = []

    seen = set()

    for program in programs:

        key = (
            program["channel_id"],
            program["title"],
            program["start"],
            program["stop"]
        )

        if key in seen:
            continue

        seen.add(key)

        unique_programs.append(
            program
        )

    print(
        f"      Gerçek kanal sayısı: {len(channels)}"
    )

    print(
        f"      Bulunan program sayısı: {len(unique_programs)}"
    )

    # İlk birkaç kanalı göster.
    if channels:

        print(
            "      İlk kanallar:"
        )

        for channel in list(
            channels.values()
        )[:15]:

            print(
                "        -",
                channel["name"]
            )

    # İlk birkaç programı göster.
    if unique_programs:

        print(
            "      İlk programlar:"
        )

        for program in unique_programs[:10]:

            print(
                "        -",
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
# HTTP GET
# ============================================================

def http_get(url):

    request = urllib.request.Request(
        url,
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

    with OPENER.open(
        request,
        timeout=30
    ) as response:

        raw = response.read()

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        text = raw.decode(
            charset,
            errors="replace"
        )

        return (
            response.status,
            response.headers.get(
                "Content-Type",
                ""
            ),
            text
        )


# ============================================================
# CSRF TOKEN
# ============================================================

def get_csrf_token(page):

    patterns = [

        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\'][^>]*value=["\']([^"\']+)',

        r'value=["\']([^"\']+)["\'][^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM["\']?\s*[:=]\s*["\']([^"\']+)',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page,
            flags=re.IGNORECASE
        )

        if match:

            return html.unescape(
                match.group(1)
            )

    return None


# ============================================================
# API İSTEĞİ
# ============================================================

def api_request(
    date_begin,
    date_end,
    csrf_token,
    mode
):

    data = {
        "channelColumnCode": CHANNEL_COLUMN_CODE,
        "channelDateBegin": date_begin,
        "channelDateEnd": date_end,
        "channelSearchValue": ""
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": LIVE_TV_URL,
        "Origin": BASE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }

    if csrf_token:

        headers[
            "X-CSRF-TOKEN"
        ] = csrf_token

    # --------------------------------------------------------
    # FORM POST
    # --------------------------------------------------------

    if mode == "form":

        payload = dict(
            data
        )

        if csrf_token:

            payload[
                "CSRF-TOKEN-TVBUDNBX!-FORM"
            ] = csrf_token

        body = urllib.parse.urlencode(
            payload
        ).encode(
            "utf-8"
        )

        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/x-www-form-urlencoded; charset=UTF-8"
        )

    # --------------------------------------------------------
    # JSON POST
    # --------------------------------------------------------

    elif mode == "json":

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode(
            "utf-8"
        )

        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/json; charset=UTF-8"
        )

    # --------------------------------------------------------
    # TOKEN FORM
    # --------------------------------------------------------

    elif mode == "token-form":

        payload = dict(
            data
        )

        payload[
            "__RequestVerificationToken"
        ] = csrf_token or ""

        if csrf_token:

            payload[
                "CSRF-TOKEN-TVBUDNBX!-FORM"
            ] = csrf_token

        body = urllib.parse.urlencode(
            payload
        ).encode(
            "utf-8"
        )

        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/x-www-form-urlencoded; charset=UTF-8"
        )

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    elif mode == "get":

        query = urllib.parse.urlencode(
            data
        )

        request = urllib.request.Request(
            API_URL
            + "?"
            + query,
            method="GET"
        )

    else:

        return None

    for key, value in headers.items():

        request.add_header(
            key,
            value
        )

    try:

        with OPENER.open(
            request,
            timeout=30
        ) as response:

            raw = response.read()

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            text = raw.decode(
                charset,
                errors="replace"
            )

            return {
                "status": response.status,
                "content_type": response.headers.get(
                    "Content-Type",
                    ""
                ),
                "text": text
            }

    except urllib.error.HTTPError as exc:

        try:

            body = exc.read().decode(
                "utf-8",
                errors="replace"
            )

        except Exception:

            body = ""

        print(
            f"      API {mode}: HTTP {exc.code}"
        )

        if body:

            print(
                "      API cevap:",
                clean_text(body)[:700]
            )

        return {
            "status": exc.code,
            "content_type": "",
            "text": body
        }

    except Exception as exc:

        print(
            f"      API {mode} hatası:",
            exc
        )

        return None


# ============================================================
# JSON RECURSIVE ARAMA
# ============================================================

def normalize_key(key):

    return re.sub(
        r"[^a-z0-9]",
        "",
        str(key).lower()
    )


def extract_json_records(obj):

    records = []

    title_keys = {
        normalize_key(x)
        for x in [
            "title",
            "programName",
            "programmeName",
            "programTitle",
            "prevueName",
            "eventName",
            "prevueTitle",
        ]
    }

    start_keys = {
        normalize_key(x)
        for x in [
            "start",
            "startTime",
            "begin",
            "beginTime",
            "dateBegin",
            "startDate",
            "programmeStart",
            "prevueStart",
        ]
    }

    stop_keys = {
        normalize_key(x)
        for x in [
            "end",
            "endTime",
            "stop",
            "stopTime",
            "dateEnd",
            "endDate",
            "programmeEnd",
            "prevueEnd",
        ]
    }

    channel_keys = {
        normalize_key(x)
        for x in [
            "channelName",
            "channelTitle",
            "channelDisplayName",
            "displayName",
        ]
    }

    def walk(value):

        if isinstance(
            value,
            dict
        ):

            normalized = {
                normalize_key(k): k
                for k in value.keys()
            }

            title_key = next(
                (
                    normalized[k]
                    for k in title_keys
                    if k in normalized
                ),
                None
            )

            start_key = next(
                (
                    normalized[k]
                    for k in start_keys
                    if k in normalized
                ),
                None
            )

            stop_key = next(
                (
                    normalized[k]
                    for k in stop_keys
                    if k in normalized
                ),
                None
            )

            channel_key = next(
                (
                    normalized[k]
                    for k in channel_keys
                    if k in normalized
                ),
                None
            )

            if (
                title_key
                and start_key
                and stop_key
            ):

                title = clean_text(
                    value.get(
                        title_key
                    )
                )

                start = value.get(
                    start_key
                )

                stop = value.get(
                    stop_key
                )

                channel = ""

                if channel_key:

                    channel = clean_text(
                        value.get(
                            channel_key
                        )
                    )

                if title and start and stop:

                    records.append({
                        "title": title,
                        "start": start,
                        "stop": stop,
                        "channel": channel
                    })

            for child in value.values():

                walk(child)

        elif isinstance(
            value,
            list
        ):

            for child in value:

                walk(child)

    walk(obj)

    return records


# ============================================================
# API JSON PARSE
# ============================================================

def parse_api_response(
    text,
    target_date
):

    try:

        data = json.loads(
            text
        )

    except Exception:

        return []

    records = extract_json_records(
        data
    )

    result = []

    for record in records:

        start_dt = None
        stop_dt = None

        start_value = record[
            "start"
        ]

        stop_value = record[
            "stop"
        ]

        # ISO tarih.
        if isinstance(
            start_value,
            str
        ):

            match = re.match(
                r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2})",
                start_value
            )

            if match:

                try:

                    start_dt = datetime.strptime(
                        match.group(1)
                        + " "
                        + match.group(2),
                        "%Y-%m-%d %H:%M"
                    )

                except ValueError:

                    pass

        if isinstance(
            stop_value,
            str
        ):

            match = re.match(
                r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2})",
                stop_value
            )

            if match:

                try:

                    stop_dt = datetime.strptime(
                        match.group(1)
                        + " "
                        + match.group(2),
                        "%Y-%m-%d %H:%M"
                    )

                except ValueError:

                    pass

        if start_dt is None:

            start_dt = make_datetime(
                target_date,
                start_value
            )

        if stop_dt is None:

            stop_dt = make_datetime(
                target_date,
                stop_value
            )

        if not start_dt or not stop_dt:
            continue

        if stop_dt <= start_dt:

            stop_dt += timedelta(
                days=1
            )

        channel_name = clean_text(
            record.get(
                "channel",
                ""
            )
        )

        if not channel_name:

            channel_name = (
                "Bilinmeyen Kanal"
            )

        # API'den kategori gelirse
        # kategori isimlerini kanal yapma.
        if is_category(
            channel_name
        ):
            continue

        result.append({
            "channel_id": normalize_channel_id(
                channel_name
            ),
            "channel_name": channel_name,
            "title": record["title"],
            "category": "",
            "start": start_dt,
            "stop": stop_dt
        })

    unique = []

    seen = set()

    for item in result:

        key = (
            item["channel_id"],
            item["title"],
            item["start"],
            item["stop"]
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            item
        )

    return unique


# ============================================================
# BİR GÜNÜ İŞLE
# ============================================================

def process_day(
    target_date,
    first_page=None
):

    print()
    print(
        f"[{target_date.strftime('%d.%m.%Y')}]"
    )

    page = first_page

    # --------------------------------------------------------
    # SAYFAYI AL
    # --------------------------------------------------------

    if page is None:

        try:

            status, content_type, page = http_get(
                LIVE_TV_URL
            )

            print(
                "      Ana sayfa HTTP:",
                status
            )

        except Exception as exc:

            print(
                "      Ana sayfa alınamadı:",
                exc
            )

            page = ""

    csrf_token = get_csrf_token(
        page
    )

    if csrf_token:

        print(
            "      CSRF token bulundu."
        )

    else:

        print(
            "      CSRF token bulunamadı."
        )

    date_begin = (
        target_date.strftime(
            "%Y-%m-%d"
        )
        + " 00:00:00"
    )

    date_end = (
        target_date.strftime(
            "%Y-%m-%d"
        )
        + " 23:59:59"
    )

    # --------------------------------------------------------
    # API
    # --------------------------------------------------------

    for mode in [
        "form",
        "json",
        "token-form",
        "get"
    ]:

        print(
            f"      API deneniyor: {mode}"
        )

        response = api_request(
            date_begin,
            date_end,
            csrf_token,
            mode
        )

        if not response:
            continue

        if response["status"] != 200:
            continue

        text = response["text"]

        if not text.strip():
            continue

        programs = parse_api_response(
            text,
            target_date
        )

        if programs:

            print(
                f"      API başarılı: {len(programs)} program"
            )

            return (
                [],
                programs,
                True,
                False
            )

        print(
            "      API 200 döndü fakat program bulunamadı."
        )

    # --------------------------------------------------------
    # HTML FALLBACK
    # --------------------------------------------------------

    print(
        "      HTML fallback çalışıyor..."
    )

    if not page:

        return (
            [],
            [],
            False,
            False
        )

    channels, programs = parse_html(
        page,
        target_date
    )

    return (
        channels,
        programs,
        False,
        bool(programs)
    )


# ============================================================
# XMLTV
# ============================================================

def create_xml(
    channels,
    programs,
    output_file
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

        channel_id = channel.get(
            "id"
        )

        name = channel.get(
            "name"
        )

        if not channel_id or not name:
            continue

        if is_category(name):
            continue

        channel_map[
            channel_id
        ] = name

    # --------------------------------------------------------
    # PROGRAMLARDAN EKSİK KANAL EKLE
    # --------------------------------------------------------

    for program in programs:

        channel_id = program.get(
            "channel_id"
        )

        name = program.get(
            "channel_name"
        )

        if not channel_id or not name:
            continue

        if is_category(name):
            continue

        channel_map.setdefault(
            channel_id,
            name
        )

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    for channel_id, name in channel_map.items():

        channel_element = ET.SubElement(
            tv,
            "channel",
            {
                "id": channel_id
            }
        )

        display = ET.SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = name

    # --------------------------------------------------------
    # PROGRAM
    # --------------------------------------------------------

    sorted_programs = sorted(
        programs,
        key=lambda x: (
            x["channel_id"],
            x["start"]
        )
    )

    written_programs = 0

    for program in sorted_programs:

        channel_id = program[
            "channel_id"
        ]

        if channel_id not in channel_map:
            continue

        programme = ET.SubElement(
            tv,
            "programme",
            {
                "start": xmltv_time(
                    program["start"]
                ),
                "stop": xmltv_time(
                    program["stop"]
                ),
                "channel": channel_id
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program[
            "title"
        ]

        category = clean_text(
            program.get(
                "category",
                ""
            )
        )

        if category:

            category_element = ET.SubElement(
                programme,
                "category",
                {
                    "lang": "tr"
                }
            )

            category_element.text = category

        written_programs += 1

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
        output_file,
        encoding="utf-8",
        xml_declaration=True
    )

    return (
        len(channel_map),
        written_programs
    )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    start_time = datetime.now()

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    # --------------------------------------------------------
    # ANA SAYFA
    # --------------------------------------------------------

    print()
    print(
        "[1/5] Tivibu ana sayfası alınıyor..."
    )

    try:

        status, content_type, main_page = http_get(
            LIVE_TV_URL
        )

        print(
            "HTTP:",
            status
        )

        print(
            "Content-Type:",
            content_type
        )

    except Exception as exc:

        print(
            "ANA SAYFA HATASI:",
            exc
        )

        return 1

    # --------------------------------------------------------
    # TARİHLER
    # --------------------------------------------------------

    print()
    print(
        "[2/5] Tarihler hazırlanıyor..."
    )

    today = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    target_dates = [
        today + timedelta(
            days=i
        )
        for i in range(DAYS)
    ]

    for date in target_dates:

        print(
            " -",
            date.strftime(
                "%d.%m.%Y"
            )
        )

    # --------------------------------------------------------
    # EPG TOPLA
    # --------------------------------------------------------

    print()
    print(
        "[3/5] EPG verileri toplanıyor..."
    )

    all_channels = OrderedDict()

    all_programs = []

    successful_days = 0
    failed_days = 0

    api_success_days = 0
    html_success_days = 0

    for index, target_date in enumerate(
        target_dates,
        start=1
    ):

        print()
        print(
            f"--- Gün {index}/{DAYS} ---"
        )

        # Sadece bugün ana sayfayı
        # tekrar kullanıyoruz.
        page = (
            main_page
            if target_date == today
            else None
        )

        channels, programs, api_ok, html_ok = process_day(
            target_date,
            page
        )

        if api_ok or html_ok:

            successful_days += 1

        else:

            failed_days += 1

        if api_ok:
            api_success_days += 1

        if html_ok:
            html_success_days += 1

        # ----------------------------------------------------
        # KANALLAR
        # ----------------------------------------------------

        for channel in channels:

            channel_id = channel[
                "id"
            ]

            if channel_id not in all_channels:

                all_channels[
                    channel_id
                ] = channel

        # ----------------------------------------------------
        # PROGRAMLAR
        # ----------------------------------------------------

        all_programs.extend(
            programs
        )

    # --------------------------------------------------------
    # DUPLICATE PROGRAMLAR
    # --------------------------------------------------------

    unique_programs = []

    seen = set()

    for program in all_programs:

        key = (
            program["channel_id"],
            program["title"],
            program["start"],
            program["stop"]
        )

        if key in seen:
            continue

        seen.add(key)

        unique_programs.append(
            program
        )

    all_programs = unique_programs

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    print()
    print(
        "[4/5] XMLTV oluşturuluyor..."
    )

    channel_count, program_count = create_xml(
        list(
            all_channels.values()
        ),
        all_programs,
        OUTPUT_FILE
    )

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    elapsed = (
        datetime.now()
        - start_time
    ).total_seconds()

    print()
    print("=" * 70)
    print("TAMAMLANDI")
    print("=" * 70)

    print(
        f"Kullanılan gün sayısı : {DAYS}"
    )

    print(
        f"Başarılı gün          : {successful_days}"
    )

    print(
        f"Hatalı gün            : {failed_days}"
    )

    print(
        f"API başarılı gün      : {api_success_days}"
    )

    print(
        f"HTML başarılı gün     : {html_success_days}"
    )

    print(
        f"Kanal sayısı          : {channel_count}"
    )

    print(
        f"Toplanan program      : {len(all_programs)}"
    )

    print(
        f"XML program sayısı    : {program_count}"
    )

    print(
        f"Dosya                 : {OUTPUT_FILE}"
    )

    print(
        f"Süre                  : {elapsed:.1f} saniye"
    )

    print("=" * 70)

    if program_count == 0:

        print()
        print(
            "UYARI: XML'e hiç program yazılamadı."
        )

    else:

        print()
        print(
            "EPG başarıyla oluşturuldu."
        )

    print()

    return 0


# ============================================================
# ÇALIŞTIR
# ============================================================

if __name__ == "__main__":

    raise SystemExit(
        main()
    )
