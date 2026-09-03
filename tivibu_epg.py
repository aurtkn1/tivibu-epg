#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU
======================================================================

Tivibu Canlı TV sayfasından EPG üretir.

Öncelik:
1. GetMultiPrevueData API
2. Gerçek HTML program akışı

Özellikler:
- CookieJar
- CSRF token
- CSRF form alanı
- XMLHttpRequest header'ları
- 7 günlük EPG
- HTMLParser ile sağlam HTML ayrıştırma
- XMLTV çıktı
- Gereksiz kanal üretimini engelleme
- API cevabının tamamını teşhis amacıyla kaydetme

======================================================================
"""

import html
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from html.parser import HTMLParser


# =====================================================================
# AYARLAR
# =====================================================================

BASE_URL = "https://www.tivibu.com.tr"

LIVE_TV_URL = f"{BASE_URL}/canli-tv"

MULTI_PREVUE_URL = f"{BASE_URL}/Channel/GetMultiPrevueData"

OUTPUT_FILE = "epg.xml"

DAYS = 7

REQUEST_DELAY = 1.0

TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# =====================================================================
# COOKIE
# =====================================================================

COOKIE_JAR = http.cookiejar.CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


# =====================================================================
# HTTP
# =====================================================================

def http_request(
    url,
    data=None,
    headers=None,
):

    request_headers = {

        "User-Agent":
            USER_AGENT,

        "Accept":
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8",

        "Accept-Language":
            "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",

        "Cache-Control":
            "no-cache",

        "Pragma":
            "no-cache",

        "Connection":
            "keep-alive",
    }

    if headers:
        request_headers.update(
            headers
        )

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method="POST" if data is not None else "GET",
    )

    try:

        with OPENER.open(
            request,
            timeout=60,
        ) as response:

            raw = response.read()

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            return raw.decode(
                charset,
                errors="replace",
            )

    except urllib.error.HTTPError as exc:

        try:

            raw_error = exc.read()

            charset = (
                exc.headers.get_content_charset()
                or "utf-8"
            )

            error_body = raw_error.decode(
                charset,
                errors="replace",
            )

        except Exception:

            error_body = ""

        preview = re.sub(
            r"\s+",
            " ",
            error_body,
        ).strip()

        if len(preview) > 5000:
            preview = preview[:5000]

        raise RuntimeError(
            f"HTTP {exc.code}\n"
            f"URL: {url}\n"
            f"Tivibu cevabı: "
            f"{preview or '(boş)'}"
        ) from exc

    except Exception as exc:

        raise RuntimeError(
            f"HTTP isteği başarısız:\n"
            f"URL: {url}\n"
            f"Hata: {exc}"
        ) from exc


# =====================================================================
# ANA SAYFA
# =====================================================================

def get_main_page():

    print(
        "[1/5] Tivibu canlı TV sayfası alınıyor..."
    )

    page = http_request(
        LIVE_TV_URL,
        headers={
            "Referer":
                BASE_URL + "/",
        },
    )

    if not page:

        raise RuntimeError(
            "Tivibu sayfası boş döndü."
        )

    print(
        f"      Sayfa alındı. "
        f"Boyut: {len(page):,} byte"
    )

    print(
        f"      Cookie sayısı: "
        f"{len(COOKIE_JAR)}"
    )

    return page


# =====================================================================
# CSRF
# =====================================================================

def extract_csrf_token(page):

    patterns = [

        r'name=["\']'
        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\'][^>]*value=["\']([^"\']+)["\']',

        r'value=["\']([^"\']+)["\'][^>]*'
        r'name=["\']'
        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\']?\s*[:=]\s*'
        r'["\']([^"\']+)["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'.{0,1500}?value=["\']([^"\']+)["\']',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page,
            re.IGNORECASE | re.DOTALL,
        )

        if match:

            token = html.unescape(
                match.group(1)
            ).strip()

            if token:
                return token

    return ""


# =====================================================================
# COOKIE YAZDIR
# =====================================================================

def print_cookies():

    if not COOKIE_JAR:

        print(
            "      Cookie bulunamadı."
        )

        return

    print(
        "      Cookie'ler:"
    )

    for cookie in COOKIE_JAR:

        print(
            f"        - {cookie.name}"
        )


# =====================================================================
# METİN
# =====================================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# =====================================================================
# URL
# =====================================================================

def make_absolute_url(value):

    if not value:
        return ""

    value = html.unescape(
        str(value)
    ).strip()

    if value.startswith("//"):
        return "https:" + value

    if value.startswith("/"):
        return BASE_URL + value

    if value.startswith(
        "http://"
    ) or value.startswith(
        "https://"
    ):
        return value

    return urllib.parse.urljoin(
        BASE_URL + "/",
        value,
    )


# =====================================================================
# TARİH
# =====================================================================

def parse_date(value):

    if not value:
        return None

    value = clean_text(
        value
    )

    formats = [

        "%Y.%m.%d %H:%M:%S",

        "%d.%m.%Y %H:%M:%S",

        "%Y-%m-%d %H:%M:%S",

        "%Y/%m/%d %H:%M:%S",

        "%Y.%m.%d %H:%M",

        "%d.%m.%Y %H:%M",

        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:

            pass

    return None


# =====================================================================
# SAAT
# =====================================================================

def parse_time_range(text):

    text = clean_text(
        text
    )

    match = re.search(
        r'(\d{1,2}):(\d{2})'
        r'\s*(?:→|->|–|—|-)\s*'
        r'(\d{1,2}):(\d{2})',
        text,
    )

    if not match:
        return None

    return (
        f"{int(match.group(1)):02d}:{match.group(2)}",
        f"{int(match.group(3)):02d}:{match.group(4)}",
    )


# =====================================================================
# DATETIME
# =====================================================================

def time_to_datetime(
    value,
    date_value,
):

    if not value:
        return None

    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})",
        value.strip(),
    )

    if not match:
        return None

    hour = int(
        match.group(1)
    )

    minute = int(
        match.group(2)
    )

    return datetime.combine(
        date_value,
        datetime.min.time(),
    ).replace(
        hour=hour,
        minute=minute,
    )


# =====================================================================
# KANAL ADI TEMİZLE
# =====================================================================

def normalize_channel_name(name):

    name = clean_text(
        name
    )

    if not name:
        return ""

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    return name


# =====================================================================
# KANAL KODU
# =====================================================================

def make_channel_code(name):

    name = normalize_channel_name(
        name
    )

    if not name:
        return ""

    code = name.lower()

    replacements = {

        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
    }

    for old, new in replacements.items():

        code = code.replace(
            old,
            new,
        )

    code = re.sub(
        r"[^a-z0-9]+",
        "_",
        code,
    )

    code = code.strip(
        "_"
    )

    return code


# =====================================================================
# HTML PARSER
# =====================================================================

class TivibuHTMLParser(
    HTMLParser
):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.links = []

        self.current_href = ""

        self.current_attrs = {}

        self.current_text = []

        self.in_a = False

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        if tag.lower() != "a":
            return

        if self.in_a:

            return

        self.in_a = True

        self.current_href = ""

        self.current_attrs = dict(
            attrs
        )

        self.current_text = []

        for key, value in attrs:

            if key.lower() == "href":

                self.current_href = (
                    value or ""
                )

    def handle_data(
        self,
        data,
    ):

        if self.in_a:

            self.current_text.append(
                data
            )

    def handle_endtag(
        self,
        tag,
    ):

        if tag.lower() != "a":
            return

        if not self.in_a:
            return

        text = clean_text(
            " ".join(
                self.current_text
            )
        )

        self.links.append(
            {
                "href":
                    self.current_href,

                "attrs":
                    self.current_attrs,

                "text":
                    text,
            }
        )

        self.current_href = ""

        self.current_attrs = {}

        self.current_text = []

        self.in_a = False


# =====================================================================
# HTML PROGRAM PARSER
# =====================================================================

def parse_html_programs(
    page,
    target_date,
):

    parser = TivibuHTMLParser()

    try:

        parser.feed(
            page
        )

        parser.close()

    except Exception as exc:

        print(
            f"      HTML parser hata: {exc}"
        )

        return []


    programs = []

    current_channel = ""

    current_channel_icon = ""

    seen = set()


    for link in parser.links:

        text = clean_text(
            link.get(
                "text",
                ""
            )
        )

        href = link.get(
            "href",
            ""
        )

        attrs = link.get(
            "attrs",
            {}
        )

        if not text:
            continue


        time_range = parse_time_range(
            text
        )


        # -------------------------------------------------------------
        # PROGRAM
        # -------------------------------------------------------------

        if time_range:

            begin_time, end_time = (
                time_range
            )

            start = time_to_datetime(
                begin_time,
                target_date,
            )

            stop = time_to_datetime(
                end_time,
                target_date,
            )

            if not start or not stop:
                continue

            if stop <= start:

                stop += timedelta(
                    days=1
                )


            # ---------------------------------------------------------
            # Başlık
            #
            # Örnek:
            #
            # "Count Me In Film - 20:00 → 21:30 Canlı"
            #
            # Zaman ve "Canlı" çıkarılır.
            # ---------------------------------------------------------

            title = re.sub(
                r'\s*(?:Film|Dizi|Spor|Haber|'
                r'Belgesel|Çocuk|Müzik|'
                r'Yaşam|Sinema|Ulusal|'
                r'Diğer|Global)'
                r'\s*-\s*'
                r'\d{1,2}:\d{2}'
                r'\s*(?:→|->|–|—|-)\s*'
                r'\d{1,2}:\d{2}'
                r'(?:\s+Canlı)?\s*$',
                "",
                text,
                flags=re.IGNORECASE,
            )

            if title == text:

                title = re.sub(
                    r'\s+-\s+'
                    r'\d{1,2}:\d{2}'
                    r'\s*(?:→|->|–|—|-)\s*'
                    r'\d{1,2}:\d{2}'
                    r'(?:\s+Canlı)?\s*$',
                    "",
                    text,
                    flags=re.IGNORECASE,
                )

            title = clean_text(
                title
            )

            if not title:
                continue


            # ---------------------------------------------------------
            # Kategori
            # ---------------------------------------------------------

            category = ""

            category_match = re.search(
                r'\b('
                r'Film|Dizi|Spor|Haber|'
                r'Belgesel|Çocuk|Müzik|'
                r'Yaşam|Sinema|Ulusal|'
                r'Diğer|Global'
                r')\b',
                text,
                re.IGNORECASE,
            )

            if category_match:

                category = (
                    category_match.group(1)
                )


            # ---------------------------------------------------------
            # Kanal
            # ---------------------------------------------------------

            if not current_channel:

                continue

            channel_code = make_channel_code(
                current_channel
            )

            if not channel_code:
                continue


            key = (
                channel_code,
                title.lower(),
                start,
                stop,
            )

            if key in seen:
                continue

            seen.add(
                key
            )


            icon = ""

            for attr_name in (
                "src",
                "data-src",
                "icon",
            ):

                if attrs.get(
                    attr_name
                ):

                    icon = make_absolute_url(
                        attrs[
                            attr_name
                        ]
                    )

                    break


            programs.append(
                {
                    "title":
                        title,

                    "description":
                        "",

                    "category":
                        category,

                    "start":
                        start,

                    "stop":
                        stop,

                    "channel_code":
                        channel_code,

                    "channel_name":
                        current_channel,

                    "icon":
                        icon
                        or current_channel_icon,
                }
            )

            continue


        # -------------------------------------------------------------
        # KANAL
        #
        # Program olmayan ve anlamlı kısa anchor'lar kanal olarak
        # değerlendiriliyor.
        # -------------------------------------------------------------

        if len(text) > 80:
            continue

        if text.lower() in (
            "canlı",
            "izle",
            "detay",
            "yayın görünümü",
            "favoriler",
            "favori kanallarım",
            "tüm kanallar",
        ):
            continue

        if re.fullmatch(
            r"\d{1,2}:\d{2}",
            text,
        ):
            continue

        if text in (
            "Bugün",
            "Dün",
            "Yarın",
        ):
            continue

        if re.fullmatch(
            r"\d{2}\.\d{2}\.\d{4}",
            text,
        ):
            continue


        # -------------------------------------------------------------
        # Tivibu kanal linkleri genellikle href taşır.
        # Görsel linkleri kanal olarak kabul etme.
        # -------------------------------------------------------------

        href_lower = href.lower()

        if not href:
            continue

        if (
            "facebook" in href_lower
            or "twitter" in href_lower
            or "instagram" in href_lower
            or "youtube" in href_lower
            or "google" in href_lower
        ):
            continue


        # -------------------------------------------------------------
        # Program linklerinde olmayan kanal başlıklarını kabul et.
        # -------------------------------------------------------------

        if (
            "/channel/" in href_lower
            or "/kanal/" in href_lower
            or "/canli-tv" in href_lower
            or href_lower.startswith(
                "#"
            )
        ):

            current_channel = (
                normalize_channel_name(
                    text
                )
            )

            current_channel_icon = ""

            continue


    return programs


# =====================================================================
# HTML KANALLARI
# =====================================================================

def parse_html_channels(
    page
):

    parser = TivibuHTMLParser()

    try:

        parser.feed(
            page
        )

        parser.close()

    except Exception:

        return {}

    channels = {}

    for link in parser.links:

        text = clean_text(
            link.get(
                "text",
                ""
            )
        )

        href = (
            link.get(
                "href",
                ""
            )
            or ""
        )

        if not text:
            continue

        if len(text) > 80:
            continue

        if parse_time_range(
            text
        ):
            continue

        if text.lower() in (
            "canlı",
            "izle",
            "detay",
            "bugün",
            "dün",
            "yarın",
            "tüm kanallar",
            "favoriler",
            "favori kanallarım",
            "yayın görünümü",
        ):
            continue

        if re.fullmatch(
            r"\d{2}\.\d{2}\.\d{4}",
            text,
        ):
            continue

        href_lower = href.lower()

        if not href:
            continue

        if (
            "facebook" in href_lower
            or "twitter" in href_lower
            or "instagram" in href_lower
            or "youtube" in href_lower
        ):
            continue

        if not (
            "/channel/" in href_lower
            or "/kanal/" in href_lower
            or "/canli-tv" in href_lower
        ):
            continue

        code = make_channel_code(
            text
        )

        if not code:
            continue

        if code not in channels:

            channels[
                code
            ] = {

                "name":
                    text,

                "icon":
                    "",
            }

    return channels


# =====================================================================
# TARİH SEÇENEKLERİ
# =====================================================================

def extract_date_options(
    page
):

    results = []

    patterns = [

        re.compile(
            r'channeldatebegin=["\']'
            r'([^"\']+)'
            r'["\'][^>]*'
            r'channeldateend=["\']'
            r'([^"\']+)'
            r'["\']',
            re.IGNORECASE,
        ),

        re.compile(
            r'channelDateBegin["\']?\s*[:=]\s*'
            r'["\']([^"\']+)["\']'
            r'.{0,1500}?'
            r'channelDateEnd["\']?\s*[:=]\s*'
            r'["\']([^"\']+)["\']',
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    for pattern in patterns:

        for match in pattern.finditer(
            page
        ):

            results.append(
                {
                    "begin":
                        html.unescape(
                            match.group(1)
                        ).strip(),

                    "end":
                        html.unescape(
                            match.group(2)
                        ).strip(),
                }
            )

    unique = []

    seen = set()

    for item in results:

        key = (
            item["begin"],
            item["end"],
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        unique.append(
            item
        )

    return unique


# =====================================================================
# 7 GÜN
# =====================================================================

def build_target_dates(
    page
):

    today = datetime.now().date()

    result = []

    page_dates = extract_date_options(
        page
    )

    for item in page_dates:

        dt = parse_date(
            item["begin"]
        )

        if not dt:
            continue

        if dt.date() < today:
            continue

        result.append(
            {
                "date":
                    dt.date(),

                "begin":
                    item["begin"],

                "end":
                    item["end"],
            }
        )

    unique = {}

    for item in result:

        unique[
            item["date"]
        ] = item

    result = list(
        unique.values()
    )

    existing = {
        item["date"]
        for item in result
    }

    for offset in range(
        DAYS
    ):

        day = (
            today
            + timedelta(
                days=offset
            )
        )

        if day in existing:
            continue

        result.append(
            {
                "date":
                    day,

                "begin":
                    day.strftime(
                        "%Y.%m.%d 00:00:00"
                    ),

                "end":
                    day.strftime(
                        "%Y.%m.%d 23:59:59"
                    ),
            }
        )

    result.sort(
        key=lambda x:
            x["date"]
    )

    return result[:DAYS]


# =====================================================================
# API
# =====================================================================

def try_api(
    csrf_token,
    begin_date,
    end_date,
):

    if not csrf_token:
        return None

    payloads = [

        {
            "channelColumnCode":
                "020002",

            "channelDateBegin":
                begin_date,

            "channelDateEnd":
                end_date,

            "channelSearchValue":
                "",

            "pageNo":
                "1",

            "CSRF-TOKEN-TVBUDNBX!-FORM":
                csrf_token,
        },

        {
            "channelColumnCode":
                "020002",

            "channelDateBegin":
                begin_date,

            "channelDateEnd":
                end_date,

            "channelSearchValue":
                "",

            "page":
                "1",

            "CSRF-TOKEN-TVBUDNBX!-FORM":
                csrf_token,
        },

        {
            "channelColumnCode":
                "020002",

            "channelDateBegin":
                begin_date,

            "channelDateEnd":
                end_date,

            "CSRF-TOKEN-TVBUDNBX!-FORM":
                csrf_token,
        },
    ]


    for attempt, payload in enumerate(
        payloads,
        1,
    ):

        print(
            f"      API deneme "
            f"{attempt}/3..."
        )

        body = urllib.parse.urlencode(
            payload
        ).encode(
            "utf-8"
        )

        headers = {

            "Content-Type":
                "application/x-www-form-urlencoded; "
                "charset=UTF-8",

            "Origin":
                BASE_URL,

            "Referer":
                LIVE_TV_URL,

            "X-Requested-With":
                "XMLHttpRequest",

            "RequestVerificationToken":
                csrf_token,

            "X-CSRF-TOKEN":
                csrf_token,

            "Accept":
                "application/json, "
                "text/javascript, */*; q=0.01",
        }

        try:

            response = http_request(
                MULTI_PREVUE_URL,
                data=body,
                headers=headers,
            )

            if not response:
                continue

            # ---------------------------------------------------------
            # JSON değilse teşhis bilgisi göster.
            # ---------------------------------------------------------

            try:

                data = json.loads(
                    response
                )

            except json.JSONDecodeError:

                preview = re.sub(
                    r"\s+",
                    " ",
                    response,
                ).strip()

                if len(preview) > 3000:
                    preview = preview[:3000]

                print(
                    "      API JSON döndürmedi."
                )

                print(
                    f"      API cevap: "
                    f"{preview}"
                )

                continue

            if isinstance(
                data,
                dict,
            ):

                return data

            if isinstance(
                data,
                list,
            ):

                return {
                    "programs":
                        data
                }

        except Exception as exc:

            print(
                f"      API hata: {exc}"
            )

    return None


# =====================================================================
# JSON DEĞER
# =====================================================================

def json_first(
    item,
    keys,
):

    if not isinstance(
        item,
        dict,
    ):
        return ""

    for key in keys:

        if key not in item:
            continue

        value = item.get(
            key
        )

        if value is None:
            continue

        if isinstance(
            value,
            (dict, list),
        ):
            continue

        value = clean_text(
            value
        )

        if value:
            return value

    return ""


# =====================================================================
# JSON PROGRAM LİSTESİ
# =====================================================================

def find_program_lists(
    data
):

    results = []

    if isinstance(
        data,
        list,
    ):

        return [
            data
        ]

    if not isinstance(
        data,
        dict,
    ):
        return []

    preferred = [

        "prevueListViewModel",

        "mobilPrevueViewModel",

        "programListViewModel",

        "programs",

        "programList",

        "epgList",

        "data",

        "result",
    ]

    for key in preferred:

        value = data.get(
            key
        )

        if isinstance(
            value,
            list,
        ):

            results.append(
                value
            )

    # -------------------------------------------------------------
    # Birinci seviyede bulunamadıysa recursive ara.
    # -------------------------------------------------------------

    def recursive(
        obj,
        depth=0,
    ):

        if depth > 5:
            return

        if isinstance(
            obj,
            dict,
        ):

            for key, value in obj.items():

                if isinstance(
                    value,
                    list,
                ):

                    if value:

                        dict_count = sum(
                            1
                            for x in value
                            if isinstance(
                                x,
                                dict
                            )
                        )

                        if dict_count:
                            results.append(
                                value
                            )

                elif isinstance(
                    value,
                    dict,
                ):

                    recursive(
                        value,
                        depth + 1,
                    )

    recursive(
        data
    )

    unique = []

    seen_ids = set()

    for items in results:

        marker = id(
            items
        )

        if marker in seen_ids:
            continue

        seen_ids.add(
            marker
        )

        unique.append(
            items
        )

    return unique


# =====================================================================
# JSON PROGRAM DÖNÜŞTÜR
# =====================================================================

def convert_json_programs(
    data
):

    result = []

    lists = find_program_lists(
        data
    )

    for items in lists:

        for item in items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            channel_code = json_first(
                item,
                [
                    "channelCode",
                    "channelId",
                    "channelID",
                    "code",
                ],
            )

            channel_name = json_first(
                item,
                [
                    "channelName",
                    "channel",
                    "displayName",
                ],
            )

            title = json_first(
                item,
                [
                    "prevueName",
                    "programName",
                    "title",
                    "name",
                ],
            )

            begin = json_first(
                item,
                [
                    "beginTime",
                    "startTime",
                    "start",
                    "begin",
                    "dateBegin",
                ],
            )

            end = json_first(
                item,
                [
                    "endTime",
                    "stopTime",
                    "end",
                    "stop",
                    "dateEnd",
                ],
            )

            if not title:
                continue

            start = parse_date(
                begin
            )

            stop = parse_date(
                end
            )

            if not start or not stop:
                continue

            if stop <= start:

                stop += timedelta(
                    days=1
                )

            icon = make_absolute_url(
                json_first(
                    item,
                    [
                        "channelImage",
                        "channelIcon",
                        "image",
                        "icon",
                    ],
                )
            )

            result.append(
                {
                    "title":
                        title,

                    "description":
                        json_first(
                            item,
                            [
                                "description",
                                "desc",
                                "summary",
                            ],
                        ),

                    "category":
                        json_first(
                            item,
                            [
                                "genre",
                                "category",
                            ],
                        ),

                    "start":
                        start,

                    "stop":
                        stop,

                    "channel_code":
                        channel_code,

                    "channel_name":
                        channel_name,

                    "icon":
                        icon,
                }
            )

    return result


# =====================================================================
# PROGRAM KEY
# =====================================================================

def program_key(
    program
):

    return (

        program.get(
            "channel_code",
            "",
        ),

        program.get(
            "channel_name",
            "",
        ).lower(),

        program.get(
            "title",
            "",
        ).lower(),

        program.get(
            "start"
        ),

        program.get(
            "stop"
        ),
    )


# =====================================================================
# XML METİN
# =====================================================================

def xml_add_text(
    parent,
    tag,
    text,
    lang=None,
):

    if not text:
        return None

    attrs = {}

    if lang:
        attrs[
            "lang"
        ] = lang

    element = ET.SubElement(
        parent,
        tag,
        attrs,
    )

    element.text = clean_text(
        text
    )

    return element


# =====================================================================
# XMLTV
# =====================================================================

def create_xmltv(
    channels,
    programs,
):

    print(
        "[5/5] XMLTV oluşturuluyor..."
    )

    tv = ET.Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",

            "generator-info-url":
                LIVE_TV_URL,
        },
    )


    # -------------------------------------------------------------
    # KANALLAR
    # -------------------------------------------------------------

    for code in sorted(
        channels,
        key=lambda x:
            channels[x]["name"].lower(),
    ):

        info = channels[
            code
        ]

        channel = ET.SubElement(
            tv,
            "channel",
            {
                "id":
                    code,
            },
        )

        xml_add_text(
            channel,
            "display-name",
            info.get(
                "name",
                code,
            ),
            "tr",
        )

        icon = info.get(
            "icon",
            "",
        )

        if icon.startswith(
            "http"
        ):

            ET.SubElement(
                channel,
                "icon",
                {
                    "src":
                        icon,
                },
            )


    # -------------------------------------------------------------
    # PROGRAMLAR
    # -------------------------------------------------------------

    xml_count = 0

    for program in sorted(
        programs,
        key=lambda x: (
            x.get(
                "channel_code",
                "",
            ),

            x.get(
                "start"
            )
            or datetime.min,
        ),
    ):

        channel_code = program.get(
            "channel_code",
            "",
        )

        if not channel_code:
            continue

        if channel_code not in channels:
            continue

        start = program.get(
            "start"
        )

        stop = program.get(
            "stop"
        )

        if not start or not stop:
            continue

        if stop <= start:
            continue

        programme = ET.SubElement(
            tv,
            "programme",
            {
                "start":
                    start.strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " "
                    + TIMEZONE,

                "stop":
                    stop.strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " "
                    + TIMEZONE,

                "channel":
                    channel_code,
            },
        )

        xml_add_text(
            programme,
            "title",
            program.get(
                "title",
                "",
            ),
            "tr",
        )

        xml_add_text(
            programme,
            "desc",
            program.get(
                "description",
                "",
            ),
            "tr",
        )

        xml_add_text(
            programme,
            "category",
            program.get(
                "category",
                "",
            ),
            "tr",
        )

        xml_count += 1


    tree = ET.ElementTree(
        tv
    )

    try:

        ET.indent(
            tree,
            space="  ",
        )

    except AttributeError:

        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return xml_count


# =====================================================================
# ANA PROGRAM
# =====================================================================

def main():

    start_time = time.time()

    print()

    print(
        "=" * 70
    )

    print(
        "TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU"
    )

    print(
        "=" * 70
    )

    print()


    # ================================================================
    # 1 - SAYFA
    # ================================================================

    page = get_main_page()


    # ================================================================
    # 2 - CSRF
    # ================================================================

    print()

    print(
        "[2/5] CSRF token aranıyor..."
    )

    csrf_token = extract_csrf_token(
        page
    )

    if csrf_token:

        print(
            "      Token bulundu. "
            f"Uzunluk: {len(csrf_token)}"
        )

    else:

        print(
            "      CSRF token bulunamadı."
        )

    print_cookies()


    # ================================================================
    # 3 - TARİHLER
    # ================================================================

    target_dates = build_target_dates(
        page
    )

    print()

    print(
        f"[3/5] Hedef tarih sayısı: "
        f"{len(target_dates)}"
    )

    for index, item in enumerate(
        target_dates,
        1,
    ):

        print(
            f"      {index}. "
            f"{item['date'].strftime('%d.%m.%Y')} "
            f"-> "
            f"{item['begin']} / "
            f"{item['end']}"
        )


    # ================================================================
    # 4 - VERİ TOPLA
    # ================================================================

    print()

    print(
        "[4/5] EPG verileri toplanıyor..."
    )

    print()


    channels = {}

    programs = {}


    successful_days = 0

    failed_days = 0

    api_success_days = 0

    html_success_days = 0


    # ================================================================
    # HTML KANALLARI
    # ================================================================

    first_channels = parse_html_channels(
        page
    )

    for code, info in first_channels.items():

        channels[
            code
        ] = info

    print(
        f"      HTML kanal sayısı: "
        f"{len(first_channels)}"
    )


    # ================================================================
    # 7 GÜN
    # ================================================================

    for index, date_info in enumerate(
        target_dates,
        1,
    ):

        target_date = date_info[
            "date"
        ]

        print()

        print(
            f"--- GÜN "
            f"{index}/{len(target_dates)} ---"
        )

        print(
            f"Tarih: "
            f"{target_date.strftime('%d.%m.%Y')}"
        )


        day_programs = []


        # ============================================================
        # API
        # ============================================================

        api_data = None

        if csrf_token:

            api_data = try_api(
                csrf_token,
                date_info["begin"],
                date_info["end"],
            )


        if api_data:

            print(
                "      API: BAŞARILI"
            )

            api_success_days += 1

            api_programs = (
                convert_json_programs(
                    api_data
                )
            )

            day_programs.extend(
                api_programs
            )

            print(
                f"      API program: "
                f"{len(api_programs)}"
            )

        else:

            print(
                "      API: kullanılamadı."
            )


        # ============================================================
        # HTML
        #
        # HTML yalnızca gerçekten sayfada bulunan bugünkü veriyi
        # kullanır.
        # ============================================================

        html_programs = []

        if target_date == datetime.now().date():

            html_programs = (
                parse_html_programs(
                    page,
                    target_date,
                )
            )

        if html_programs:

            print(
                f"      HTML program: "
                f"{len(html_programs)}"
            )

            html_success_days += 1

            day_programs.extend(
                html_programs
            )

        else:

            print(
                "      HTML program: 0"
            )


        # ============================================================
        # PROGRAMLAR
        # ============================================================

        new_count = 0

        for program in day_programs:

            channel_code = program.get(
                "channel_code",
                "",
            )

            channel_name = program.get(
                "channel_name",
                "",
            )


            if not channel_code and channel_name:

                channel_code = (
                    make_channel_code(
                        channel_name
                    )
                )

                program[
                    "channel_code"
                ] = channel_code


            if not channel_code:

                continue


            if channel_code not in channels:

                channels[
                    channel_code
                ] = {

                    "name":
                        channel_name
                        or channel_code,

                    "icon":
                        program.get(
                            "icon",
                            "",
                        ),
                }

            else:

                if (
                    not channels[
                        channel_code
                    ].get(
                        "icon"
                    )
                    and program.get(
                        "icon"
                    )
                ):

                    channels[
                        channel_code
                    ]["icon"] = (
                        program.get(
                            "icon"
                        )
                    )


            key = program_key(
                program
            )

            if key in programs:
                continue

            programs[
                key
            ] = program

            new_count += 1


        if day_programs:

            successful_days += 1

        else:

            failed_days += 1


        print(
            f"      Yeni program: "
            f"{new_count}"
        )


        time.sleep(
            REQUEST_DELAY
        )


    # ================================================================
    # XML
    # ================================================================

    all_programs = list(
        programs.values()
    )

    xml_count = create_xmltv(
        channels,
        all_programs,
    )


    elapsed = (
        time.time()
        - start_time
    )


    # ================================================================
    # SONUÇ
    # ================================================================

    print()

    print(
        "=" * 70
    )

    print(
        "TAMAMLANDI"
    )

    print(
        "=" * 70
    )

    print(
        f"Kullanılan gün sayısı : "
        f"{len(target_dates)}"
    )

    print(
        f"Başarılı gün          : "
        f"{successful_days}"
    )

    print(
        f"Hatalı gün            : "
        f"{failed_days}"
    )

    print(
        f"API başarılı gün      : "
        f"{api_success_days}"
    )

    print(
        f"HTML başarılı gün     : "
        f"{html_success_days}"
    )

    print(
        f"Kanal sayısı          : "
        f"{len(channels)}"
    )

    print(
        f"Toplanan program      : "
        f"{len(all_programs)}"
    )

    print(
        f"XML program sayısı    : "
        f"{xml_count}"
    )

    print(
        f"Dosya                 : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Süre                  : "
        f"{elapsed:.1f} saniye"
    )

    print(
        "=" * 70
    )


    if xml_count == 0:

        print()

        print(
            "UYARI: XML'e hiç program yazılamadı."
        )

        print(
            "API hâlâ 400 veriyorsa üstteki "
            "'Tivibu cevabı:' satırına bak."
        )

    else:

        print()

        print(
            f"BAŞARILI: {xml_count} program "
            f"epg.xml dosyasına yazıldı."
        )


# =====================================================================
# ÇALIŞTIR
# =====================================================================

if __name__ == "__main__":

    main()
