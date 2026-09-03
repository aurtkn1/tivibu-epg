#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from http.cookiejar import CookieJar


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"

LIVE_TV_URL = (
    f"{BASE_URL}/canli-tv"
)

MULTI_PREVUE_URL = (
    f"{BASE_URL}/Channel/GetMultiPrevueData"
)

OUTPUT_FILE = "epg.xml"

DAYS = 7

REQUEST_DELAY = 0.60

TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ============================================================
# COOKIE
# ============================================================

COOKIE_JAR = CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(
        COOKIE_JAR
    )
)


# ============================================================
# GERÇEK KANAL OLMAYAN / MÜZİK KATEGORİLERİ
# ============================================================

INVALID_CHANNEL_NAMES = {
    "90’ LAR",
    "90' LAR",
    "90 LAR",

    "AKUSTİK",
    "ARABESK",
    "BLUES",
    "DİNİ MUSİKİ",
    "FİLM MÜZİKLERİ",
    "JAZZ",
    "KLASİK MÜZİK",
    "LOUNGE",
    "MUTLU",
    "OLDIES",
    "RETRO",
    "TAŞ PLAK",
    "TÜRK HALK MÜZİĞİ",
    "TÜRK SANAT MÜZİĞİ",
    "TÜRKÇE POP",
    "TÜRKÇE SLOW",
    "YABANCI POP",
    "YABANCI ROCK",
    "YABANCI SLOW",
    "ÇALIŞIRKEN",

    # Sayfanın diğer bağlantıları
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
    "FAVORİ KANALLARIM",
    "TİVİBU SPOR CANLI İZLE",
    "TRT1 CANLI İZLE",
}


# ============================================================
# HTTP
# ============================================================

def http_request(
    url,
    data=None,
    headers=None,
):

    request_headers = {
        "User-Agent": USER_AGENT,

        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/json,text/javascript,"
            "*/*;q=0.8"
        ),

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
        method=(
            "POST"
            if data is not None
            else "GET"
        ),
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
            error_body = (
                exc.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )
        except Exception:
            error_body = ""

        raise RuntimeError(
            f"HTTP {exc.code} hatası\n"
            f"URL: {url}\n"
            f"Cevap: {error_body[:1000]}"
        ) from exc

    except Exception as exc:

        raise RuntimeError(
            f"HTTP isteği başarısız\n"
            f"URL: {url}\n"
            f"Hata: {exc}"
        ) from exc


# ============================================================
# TEMİZ METİN
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        value,
        flags=re.I | re.S,
    )

    value = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        value,
        flags=re.I | re.S,
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = value.replace(
        "\xa0",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ============================================================
# NORMALIZE
# ============================================================

def normalize_name(value):

    value = clean_text(
        value
    ).upper()

    value = (
        value
        .replace("İ", "I")
        .replace("Ğ", "G")
        .replace("Ü", "U")
        .replace("Ş", "S")
        .replace("Ö", "O")
        .replace("Ç", "C")
        .replace("’", "'")
    )

    value = re.sub(
        r"[^A-Z0-9]+",
        "",
        value,
    )

    return value


# ============================================================
# ANA SAYFA
# ============================================================

def get_main_page():

    print(
        "[1] Tivibu canlı TV sayfası alınıyor..."
    )

    page = http_request(
        LIVE_TV_URL,
        headers={
            "Referer":
                BASE_URL + "/",
        },
    )

    print(
        f"    Sayfa uzunluğu: "
        f"{len(page):,}"
    )

    return page


# ============================================================
# CSRF
# ============================================================

def extract_csrf_token(page):

    patterns = [

        r'name=["\']'
        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\'][^>]*value=["\']'
        r'([^"\']+)["\']',

        r'value=["\']'
        r'([^"\']+)["\'][^>]*name=["\']'
        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'["\']?\s*[:=]\s*["\']'
        r'([^"\']+)["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'.{0,1000}?value=["\']'
        r'([^"\']+)["\']',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page,
            flags=re.I | re.S,
        )

        if match:

            token = html.unescape(
                match.group(1)
            ).strip()

            if token:
                return token

    raise RuntimeError(
        "CSRF token bulunamadı."
    )


# ============================================================
# TARİH PARSE
# ============================================================

def parse_datetime(value):

    if not value:
        return None

    value = clean_text(
        value
    )

    formats = [

        "%Y.%m.%d %H:%M:%S",

        "%Y-%m-%d %H:%M:%S",

        "%d.%m.%Y %H:%M:%S",

        "%Y.%m.%d %H:%M",

        "%Y-%m-%d %H:%M",

        "%d.%m.%Y %H:%M",

    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:
            pass

    # ISO benzeri değerler
    try:

        value2 = value.replace(
            "Z",
            "",
        )

        return datetime.fromisoformat(
            value2
        )

    except Exception:
        return None


# ============================================================
# TARİH SEÇENEKLERİ
# ============================================================

def extract_date_options(page):

    results = []

    # Önce bütün <a> taglarını al.
    anchor_pattern = re.compile(
        r"<a\b(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)"
        r"</a>",
        flags=re.I | re.S,
    )

    for match in anchor_pattern.finditer(
        page
    ):

        attrs = match.group(
            "attrs"
        )

        body = match.group(
            "body"
        )

        begin_match = re.search(
            r'(?:data-)?channeldatebegin'
            r'\s*=\s*["\']([^"\']+)["\']',
            attrs,
            flags=re.I,
        )

        end_match = re.search(
            r'(?:data-)?channeldateend'
            r'\s*=\s*["\']([^"\']+)["\']',
            attrs,
            flags=re.I,
        )

        if not begin_match:
            continue

        if not end_match:
            continue

        begin = html.unescape(
            begin_match.group(1)
        ).strip()

        end = html.unescape(
            end_match.group(1)
        ).strip()

        dt = parse_datetime(
            begin
        )

        if not dt:
            continue

        text = clean_text(
            body
        )

        results.append(
            {
                "date":
                    dt.date(),

                "begin":
                    begin,

                "end":
                    end,

                "text":
                    text,
            }
        )

    # Tarihe göre tekilleştir
    unique = {}

    for item in results:

        unique[
            item["date"]
        ] = item

    return [
        unique[key]
        for key in sorted(
            unique
        )
    ]


# ============================================================
# 7 GÜNÜ OLUŞTUR
# ============================================================

def build_target_dates(page):

    today = datetime.now().date()

    page_dates = (
        extract_date_options(
            page
        )
    )

    print()
    print(
        "[2] Tivibu tarihleri:"
    )

    for item in page_dates:

        print(
            "    "
            + item["date"].strftime(
                "%d.%m.%Y"
            )
            + " -> "
            + item["begin"]
            + " / "
            + item["end"]
        )

    target = []

    for item in page_dates:

        if item["date"] < today:
            continue

        target.append(
            item
        )

    # Eğer Tivibu 7 tarihi açıkça verdiyse
    # onları kullan.
    target = target[:DAYS]

    # Eksik gün varsa tamamla.
    existing = {
        item["date"]
        for item in target
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

        target.append(
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

                "text":
                    "",
            }
        )

    target.sort(
        key=lambda x:
            x["date"]
    )

    return target[:DAYS]


# ============================================================
# CHANNEL COLUMN CODE
# ============================================================

def extract_channel_column_code(
    page
):

    patterns = [

        r'channelColumnCode'
        r'\s*=\s*["\']([^"\']+)["\']',

        r'channelColumnCode'
        r'\s*:\s*["\']([^"\']+)["\']',

        r'"channelColumnCode"'
        r'\s*:\s*"([^"]+)"',

        r"'channelColumnCode'"
        r'\s*:\s*\'([^\']+)\'',

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            page,
            flags=re.I,
        )

        for value in matches:

            value = html.unescape(
                value
            ).strip()

            if value:
                return value

    return ""


# ============================================================
# CHANNEL SEARCH
# ============================================================

def extract_channel_search_value(
    page
):

    patterns = [

        r'channelSearchValue'
        r'\s*=\s*["\']([^"\']*)["\']',

        r'channelSearchValue'
        r'\s*:\s*["\']([^"\']*)["\']',

        r'"channelSearchValue"'
        r'\s*:\s*"([^"]*)"',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            page,
            flags=re.I,
        )

        for value in matches:

            value = html.unescape(
                value
            ).strip()

            if value:
                return value

    return ""


# ============================================================
# HREF'TEN SLUG
# ============================================================

def channel_slug_from_href(
    href
):

    if not href:
        return ""

    href = html.unescape(
        href
    ).strip()

    parsed = urllib.parse.urlparse(
        href
    )

    path = parsed.path.rstrip(
        "/"
    )

    match = re.search(
        r"/kanallar/([^/?#]+)",
        path,
        flags=re.I,
    )

    if not match:
        return ""

    return match.group(1)


# ============================================================
# GERÇEK KANALLARI HTML'DEN AL
# ============================================================

def extract_real_channels(
    page
):

    print()
    print(
        "[3] Gerçek Tivibu kanalları çıkarılıyor..."
    )

    channels = {}

    anchor_pattern = re.compile(
        r"<a\b(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)"
        r"</a>",
        flags=re.I | re.S,
    )

    for match in anchor_pattern.finditer(
        page
    ):

        attrs = match.group(
            "attrs"
        )

        body = match.group(
            "body"
        )

        href_match = re.search(
            r'href\s*=\s*["\']'
            r'([^"\']+)'
            r'["\']',
            attrs,
            flags=re.I,
        )

        if not href_match:
            continue

        href = href_match.group(
            1
        )

        slug = channel_slug_from_href(
            href
        )

        if not slug:
            continue

        # Anchor'ın içindeki bütün metni temizle.
        name = clean_text(
            body
        )

        if not name:
            continue

        # Bazı anchorlarda kanal adı dışında
        # program adı da bulunabiliyor.
        pieces = [
            clean_text(x)
            for x in re.split(
                r"[\r\n]+",
                body,
            )
            if clean_text(x)
        ]

        if pieces:

            # Çok uzun metin yerine en makul
            # kısa isim seç.
            short_pieces = [
                x
                for x in pieces
                if 2 <= len(x) <= 80
            ]

            if short_pieces:

                short_pieces.sort(
                    key=len
                )

                name = short_pieces[0]

        upper = name.upper().strip()

        if upper in INVALID_CHANNEL_NAMES:
            continue

        # Program gibi görünen metinleri kanal yapma.
        if re.search(
            r"\b\d{1,2}:\d{2}\b",
            name,
        ):
            continue

        if "→" in name:
            continue

        normalized = normalize_name(
            name
        )

        if not normalized:
            continue

        if normalized in channels:
            continue

        full_href = (
            href
            if href.startswith(
                "http"
            )
            else urllib.parse.urljoin(
                BASE_URL,
                href,
            )
        )

        channels[
            normalized
        ] = {
            "name":
                name,

            "slug":
                slug,

            "href":
                full_href,

            "id":
                (
                    "tivibu_"
                    + normalized.lower()
                ),

            "icon":
                "",
        }

    print(
        f"    Gerçek kanal sayısı: "
        f"{len(channels)}"
    )

    for item in sorted(
        channels.values(),
        key=lambda x:
            x["name"].lower(),
    ):

        print(
            f"      + {item['name']}"
        )

    return channels


# ============================================================
# API POST
# ============================================================

def post_multi_prevue(
    csrf_token,
    channel_column_code,
    channel_search_value,
    begin_date,
    end_date,
    page_no=1,
):

    form = {
        "channelColumnCode":
            channel_column_code,

        "channelDateBegin":
            begin_date,

        "channelDateEnd":
            end_date,

        "channelSearchValue":
            channel_search_value,

        "pageNo":
            str(page_no),
    }

    body = urllib.parse.urlencode(
        form
    ).encode(
        "utf-8"
    )

    headers = {

        "Content-Type":
            "application/x-www-form-urlencoded; charset=UTF-8",

        "Origin":
            BASE_URL,

        "Referer":
            LIVE_TV_URL,

        "X-Requested-With":
            "XMLHttpRequest",

        "RequestVerificationToken":
            csrf_token,

        "Accept":
            "application/json, text/javascript, */*; q=0.01",
    }

    response = http_request(
        MULTI_PREVUE_URL,
        data=body,
        headers=headers,
    )

    try:

        return json.loads(
            response
        )

    except json.JSONDecodeError:

        raise RuntimeError(
            "Tivibu JSON yerine farklı cevap döndürdü.\n"
            + response[:1000]
        )


# ============================================================
# ALAN YARDIMCISI
# ============================================================

def first_nonempty(
    item,
    keys,
):

    if not isinstance(
        item,
        dict,
    ):
        return ""

    for key in keys:

        value = item.get(
            key
        )

        if value is None:
            continue

        value = clean_text(
            value
        )

        if value:
            return value

    return ""


# ============================================================
# API KANALLARI
# ============================================================

def normalize_channels(
    data
):

    result = {}

    if not isinstance(
        data,
        dict,
    ):
        return result

    possible_lists = [

        data.get(
            "channelListViewModel"
        ),

        data.get(
            "channelList"
        ),

        data.get(
            "channels"
        ),

        data.get(
            "channelViewModel"
        ),
    ]

    items = []

    for value in possible_lists:

        if isinstance(
            value,
            list,
        ):

            items.extend(
                value
            )

    for item in items:

        if not isinstance(
            item,
            dict,
        ):
            continue

        code = first_nonempty(
            item,
            [
                "channelCode",
                "code",
                "channelId",
                "id",
            ],
        )

        name = first_nonempty(
            item,
            [
                "channelName",
                "name",
                "displayName",
                "title",
            ],
        )

        icon = first_nonempty(
            item,
            [
                "channelImage",
                "image",
                "channelIcon",
                "icon",
            ],
        )

        if not code:
            continue

        if not name:
            name = code

        result[
            str(code).strip()
        ] = {
            "name":
                name,

            "icon":
                icon,
        }

    return result


# ============================================================
# PROGRAM LİSTESİ
# ============================================================

def normalize_programs(
    data
):

    if not isinstance(
        data,
        dict,
    ):
        return []

    possible = [

        data.get(
            "prevueListViewModel"
        ),

        data.get(
            "mobilPrevueViewModel"
        ),

        data.get(
            "programList"
        ),

        data.get(
            "programs"
        ),

        data.get(
            "prevues"
        ),
    ]

    for value in possible:

        if isinstance(
            value,
            list,
        ) and value:

            return value

    return []


# ============================================================
# PROGRAM KANAL ADI
# ============================================================

def get_program_channel_name(
    program
):

    return first_nonempty(
        program,
        [
            "channelName",
            "channelDisplayName",
            "displayName",
            "channelTitle",
        ],
    )


# ============================================================
# PROGRAM KODU
# ============================================================

def get_program_channel_code(
    program
):

    return first_nonempty(
        program,
        [
            "channelCode",
            "channelId",
            "channelID",
            "code",
        ],
    )


# ============================================================
# PROGRAM BAŞLANGIÇ / BİTİŞ
# ============================================================

def get_program_times(
    program
):

    begin = first_nonempty(
        program,
        [
            "beginTime",
            "startTime",
            "start",
            "programmeStart",
        ],
    )

    end = first_nonempty(
        program,
        [
            "endTime",
            "stopTime",
            "end",
            "programmeEnd",
        ],
    )

    return (
        parse_datetime(begin),
        parse_datetime(end),
    )


# ============================================================
# PROGRAM BAŞLIK
# ============================================================

def get_program_title(
    program
):

    title = first_nonempty(
        program,
        [
            "prevueName",
            "programName",
            "programmeName",
            "name",
            "title",
        ],
    )

    return title


# ============================================================
# KANAL EŞLEŞTİRME
# ============================================================

def build_channel_indexes(
    real_channels,
    api_channels,
):

    code_to_real = {}

    name_to_real = {}

    # HTML gerçek kanalları
    for normalized, info in (
        real_channels.items()
    ):

        name_to_real[
            normalized
        ] = normalized

        slug_normalized = normalize_name(
            info.get(
                "slug",
                "",
            )
        )

        if slug_normalized:
            name_to_real[
                slug_normalized
            ] = normalized

    # API kanal kodlarını gerçek kanala
    # bağla.
    for code, info in (
        api_channels.items()
    ):

        api_name = normalize_name(
            info.get(
                "name",
                "",
            )
        )

        if api_name in real_channels:

            code_to_real[
                str(code).strip()
            ] = api_name

            continue

        # İsmi normalize ederek eşleştir.
        if api_name:

            for real_key, real_info in (
                real_channels.items()
            ):

                real_name = normalize_name(
                    real_info["name"]
                )

                if (
                    api_name
                    == real_name
                ):

                    code_to_real[
                        str(code).strip()
                    ] = real_key

                    break

    return (
        code_to_real,
        name_to_real,
    )


# ============================================================
# PROGRAMU GERÇEK KANALE BAĞLA
# ============================================================

def resolve_program_channel(
    program,
    real_channels,
    api_channels,
    code_to_real,
):

    program_code = (
        get_program_channel_code(
            program
        )
    )

    # 1) API kanal kodu üzerinden
    if program_code:

        program_code = str(
            program_code
        ).strip()

        if program_code in code_to_real:

            return code_to_real[
                program_code
            ]

        # Büyük/küçük harf farkı
        program_code_lower = (
            program_code.lower()
        )

        for code, real_key in (
            code_to_real.items()
        ):

            if (
                str(code).lower()
                == program_code_lower
            ):

                return real_key

    # 2) Programdaki kanal adı
    program_name = (
        get_program_channel_name(
            program
        )
    )

    if program_name:

        normalized_program_name = (
            normalize_name(
                program_name
            )
        )

        if (
            normalized_program_name
            in real_channels
        ):

            return normalized_program_name

        # Daha toleranslı isim karşılaştırması
        for real_key, info in (
            real_channels.items()
        ):

            real_name = normalize_name(
                info["name"]
            )

            if (
                normalized_program_name
                == real_name
            ):

                return real_key

    # 3) API kanal adından
    if program_code:

        api_info = api_channels.get(
            program_code
        )

        if api_info:

            api_name = normalize_name(
                api_info.get(
                    "name",
                    "",
                )
            )

            if api_name in real_channels:

                return api_name

    return None


# ============================================================
# PROGRAM ANAHTARI
# ============================================================

def programme_key(
    program,
    resolved_channel,
):

    begin, end = (
        get_program_times(
            program
        )
    )

    title = get_program_title(
        program
    )

    return (
        resolved_channel,
        begin.isoformat()
        if begin
        else "",
        end.isoformat()
        if end
        else "",
        title,
    )


# ============================================================
# XML METİN
# ============================================================

def add_text(
    parent,
    tag,
    text,
    lang=None,
):

    attributes = {}

    if lang:
        attributes[
            "lang"
        ] = lang

    element = ET.SubElement(
        parent,
        tag,
        attributes,
    )

    element.text = clean_text(
        text
    )

    return element


# ============================================================
# XML OLUŞTUR
# ============================================================

def create_xmltv(
    real_channels,
    programs,
):

    print()
    print(
        "[5] XMLTV oluşturuluyor..."
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

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    channel_id_map = {}

    for normalized, info in sorted(
        real_channels.items(),
        key=lambda x:
            x[1]["name"].lower(),
    ):

        channel_id = info[
            "id"
        ]

        channel_id_map[
            normalized
        ] = channel_id

        channel_element = ET.SubElement(
            tv,
            "channel",
            {
                "id":
                    channel_id
            },
        )

        add_text(
            channel_element,
            "display-name",
            info["name"],
            "tr",
        )

        icon = info.get(
            "icon",
            "",
        )

        if icon and icon.startswith(
            "http"
        ):

            ET.SubElement(
                channel_element,
                "icon",
                {
                    "src":
                        icon
                },
            )

    # --------------------------------------------------------
    # PROGRAM
    # --------------------------------------------------------

    count = 0

    for item in programs:

        channel_key = item[
            "channel_key"
        ]

        if (
            channel_key
            not in channel_id_map
        ):
            continue

        begin = item[
            "begin"
        ]

        end = item[
            "end"
        ]

        title = item[
            "title"
        ]

        if not begin or not end:
            continue

        if not title:
            continue

        # Gece yarısını geçen programlarda
        # Tivibu'nun verdiği tarih korunur.
        if end < begin:

            end += timedelta(
                days=1
            )

        programme_element = ET.SubElement(
            tv,
            "programme",
            {
                "start":
                    begin.strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " "
                    + TIMEZONE,

                "stop":
                    end.strftime(
                        "%Y%m%d%H%M%S"
                    )
                    + " "
                    + TIMEZONE,

                "channel":
                    channel_id_map[
                        channel_key
                    ],
            },
        )

        add_text(
            programme_element,
            "title",
            title,
            "tr",
        )

        description = item.get(
            "description",
            "",
        )

        if description:

            add_text(
                programme_element,
                "desc",
                description,
                "tr",
            )

        genre = item.get(
            "genre",
            "",
        )

        if genre:

            add_text(
                programme_element,
                "category",
                genre,
                "tr",
            )

        count += 1

    # --------------------------------------------------------
    # XML YAZ
    # --------------------------------------------------------

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

    return count


# ============================================================
# XML DOĞRULAMA
# ============================================================

def validate_xml():

    print()
    print(
        "[6] XML doğrulanıyor..."
    )

    tree = ET.parse(
        OUTPUT_FILE
    )

    root = tree.getroot()

    channel_elements = (
        root.findall(
            "channel"
        )
    )

    programme_elements = (
        root.findall(
            "programme"
        )
    )

    channel_ids = {
        item.get("id")
        for item in channel_elements
    }

    bad_channels = []

    for channel in channel_elements:

        display = channel.find(
            "display-name"
        )

        if display is None:
            continue

        name = clean_text(
            display.text
        )

        if (
            name.upper()
            in INVALID_CHANNEL_NAMES
        ):

            bad_channels.append(
                name
            )

    if bad_channels:

        raise RuntimeError(
            "XML içinde geçersiz kanal bulundu: "
            + ", ".join(
                bad_channels
            )
        )

    invalid_programs = 0

    for programme in (
        programme_elements
    ):

        channel_id = (
            programme.get(
                "channel"
            )
        )

        if (
            channel_id
            not in channel_ids
        ):

            invalid_programs += 1

    if invalid_programs:

        raise RuntimeError(
            f"{invalid_programs} program "
            "geçersiz kanala bağlı."
        )

    print(
        f"    Kanal: "
        f"{len(channel_elements)}"
    )

    print(
        f"    Program: "
        f"{len(programme_elements)}"
    )

    print(
        "    XML temiz ve geçerli."
    )


# ============================================================
# BİR GÜNÜ ÇEK
# ============================================================

def fetch_day(
    csrf_token,
    channel_column_code,
    channel_search_value,
    date_info,
    real_channels,
):

    date_text = date_info[
        "date"
    ].strftime(
        "%d.%m.%Y"
    )

    print(
        f"GÜN: {date_text}"
    )

    all_day_programs = []

    all_day_api_channels = {}

    # --------------------------------------------------------
    # İlk istek
    # --------------------------------------------------------

    try:

        data = post_multi_prevue(
            csrf_token,
            channel_column_code,
            channel_search_value,
            date_info["begin"],
            date_info["end"],
            1,
        )

    except Exception as first_error:

        print(
            "    İlk tarih isteği başarısız:"
        )

        print(
            f"    {first_error}"
        )

        # ----------------------------------------------------
        # Alternatif tarih formatları
        # ----------------------------------------------------

        day = date_info[
            "date"
        ]

        alternatives = [

            (
                day.strftime(
                    "%Y-%m-%d 00:00:00"
                ),

                day.strftime(
                    "%Y-%m-%d 23:59:59"
                ),
            ),

            (
                day.strftime(
                    "%d.%m.%Y 00:00:00"
                ),

                day.strftime(
                    "%d.%m.%Y 23:59:59"
                ),
            ),

            (
                day.strftime(
                    "%Y.%m.%d 00:00:00"
                ),

                day.strftime(
                    "%Y.%m.%d 23:59:59"
                ),
            ),
        ]

        data = None

        for alt_begin, alt_end in (
            alternatives
        ):

            if (
                alt_begin
                == date_info["begin"]
            ):
                continue

            try:

                print(
                    "    Alternatif tarih deneniyor: "
                    f"{alt_begin}"
                )

                data = post_multi_prevue(
                    csrf_token,
                    channel_column_code,
                    channel_search_value,
                    alt_begin,
                    alt_end,
                    1,
                )

                print(
                    "    Alternatif tarih başarılı."
                )

                break

            except Exception as exc:

                print(
                    f"    Başarısız: {exc}"
                )

        if data is None:

            raise RuntimeError(
                "Bu gün için Tivibu API "
                "verisi alınamadı."
            )

    # --------------------------------------------------------
    # İlk cevap
    # --------------------------------------------------------

    api_channels = normalize_channels(
        data
    )

    programs = normalize_programs(
        data
    )

    all_day_api_channels.update(
        api_channels
    )

    all_day_programs.extend(
        programs
    )

    print(
        f"    API kanal kaydı: "
        f"{len(api_channels)}"
    )

    print(
        f"    API program kaydı: "
        f"{len(programs)}"
    )

    # --------------------------------------------------------
    # PAGINATION
    # --------------------------------------------------------

    previous_signature = None

    for page_no in range(
        2,
        30,
    ):

        time.sleep(
            REQUEST_DELAY
        )

        try:

            page_data = post_multi_prevue(
                csrf_token,
                channel_column_code,
                channel_search_value,
                date_info["begin"],
                date_info["end"],
                page_no,
            )

        except Exception as exc:

            # Sayfalama bittiyse sessizce çık.
            print(
                f"    Sayfa {page_no}: "
                f"bitti ({exc})"
            )

            break

        page_channels = (
            normalize_channels(
                page_data
            )
        )

        page_programs = (
            normalize_programs(
                page_data
            )
        )

        signature = (
            len(page_channels),
            len(page_programs),
        )

        if (
            signature
            == previous_signature
        ):
            break

        previous_signature = signature

        if not page_channels and not page_programs:
            break

        all_day_api_channels.update(
            page_channels
        )

        all_day_programs.extend(
            page_programs
        )

        print(
            f"    Sayfa {page_no}: "
            f"kanal={len(page_channels)} "
            f"program={len(page_programs)}"
        )

        # Çok büyük sayfalama oluşursa dur.
        if len(page_programs) == 0:
            break

    # --------------------------------------------------------
    # KANAL INDEX
    # --------------------------------------------------------

    (
        code_to_real,
        name_to_real,
    ) = build_channel_indexes(
        real_channels,
        all_day_api_channels,
    )

    # --------------------------------------------------------
    # PROGRAMLARI EŞLEŞTİR
    # --------------------------------------------------------

    resolved = []

    matched = 0
    unmatched = 0

    unmatched_codes = {}

    for program in all_day_programs:

        real_channel = (
            resolve_program_channel(
                program,
                real_channels,
                all_day_api_channels,
                code_to_real,
            )
        )

        if not real_channel:

            unmatched += 1

            code = (
                get_program_channel_code(
                    program
                )
            )

            name = (
                get_program_channel_name(
                    program
                )
            )

            key = (
                code
                or name
                or "BİLİNMİYOR"
            )

            unmatched_codes[
                key
            ] = (
                unmatched_codes.get(
                    key,
                    0,
                )
                + 1
            )

            continue

        begin, end = (
            get_program_times(
                program
            )
        )

        title = get_program_title(
            program
        )

        if not begin or not end:
            continue

        if not title:
            continue

        resolved.append(
            {
                "channel_key":
                    real_channel,

                "begin":
                    begin,

                "end":
                    end,

                "title":
                    title,

                "description":
                    first_nonempty(
                        program,
                        [
                            "description",
                            "desc",
                        ],
                    ),

                "genre":
                    first_nonempty(
                        program,
                        [
                            "genre",
                            "category",
                        ],
                    ),
            }
        )

        matched += 1

    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    unique = {}

    for item in resolved:

        key = (
            item["channel_key"],
            item["begin"],
            item["end"],
            item["title"],
        )

        unique[
            key
        ] = item

    resolved = list(
        unique.values()
    )

    print(
        f"    Eşleşen program: "
        f"{matched}"
    )

    print(
        f"    Eşleşmeyen program: "
        f"{unmatched}"
    )

    if unmatched_codes:

        print(
            "    Eşleşmeyen kanal kodları:"
        )

        for code, count in sorted(
            unmatched_codes.items(),
            key=lambda x:
                -x[1],
        )[:20]:

            print(
                f"      {code}: "
                f"{count}"
            )

    print(
        f"    Kullanılabilir program: "
        f"{len(resolved)}"
    )

    return (
        all_day_api_channels,
        resolved,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    print()
    print(
        "=" * 70
    )

    print(
        "TİVİBU 7 GÜNLÜK EPG"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # SAYFA
    # --------------------------------------------------------

    page = get_main_page()

    # --------------------------------------------------------
    # CSRF
    # --------------------------------------------------------

    print()
    print(
        "[2] CSRF token aranıyor..."
    )

    csrf_token = extract_csrf_token(
        page
    )

    print(
        f"    Token uzunluğu: "
        f"{len(csrf_token)}"
    )

    # --------------------------------------------------------
    # PARAMETRELER
    # --------------------------------------------------------

    channel_column_code = (
        extract_channel_column_code(
            page
        )
    )

    channel_search_value = (
        extract_channel_search_value(
            page
        )
    )

    print(
        f"    channelColumnCode: "
        f"{channel_column_code or '(boş)'}"
    )

    print(
        f"    channelSearchValue: "
        f"{channel_search_value or '(boş)'}"
    )

    # --------------------------------------------------------
    # TARİHLER
    # --------------------------------------------------------

    target_dates = (
        build_target_dates(
            page
        )
    )

    print()
    print(
        "[3] Hedef 7 gün:"
    )

    for index, item in enumerate(
        target_dates,
        1,
    ):

        print(
            f"    {index}. "
            f"{item['date'].strftime('%d.%m.%Y')}"
            f" -> "
            f"{item['begin']}"
            f" / "
            f"{item['end']}"
        )

    if len(target_dates) != DAYS:

        raise RuntimeError(
            f"{DAYS} gün yerine "
            f"{len(target_dates)} gün bulundu."
        )

    # --------------------------------------------------------
    # GERÇEK KANALLAR
    # --------------------------------------------------------

    real_channels = (
        extract_real_channels(
            page
        )
    )

    if not real_channels:

        raise RuntimeError(
            "Gerçek kanal bulunamadı."
        )

    # --------------------------------------------------------
    # EPG
    # --------------------------------------------------------

    print()
    print(
        "[4] 7 günlük EPG çekiliyor..."
    )

    all_programs = {}

    successful_days = 0
    failed_days = 0

    for index, date_info in enumerate(
        target_dates,
        1,
    ):

        print()
        print(
            "-" * 70
        )

        print(
            f"GÜN {index}/{DAYS}"
        )

        try:

            (
                api_channels,
                day_programs,
            ) = fetch_day(
                csrf_token,
                channel_column_code,
                channel_search_value,
                date_info,
                real_channels,
            )

            for program in day_programs:

                key = (
                    program[
                        "channel_key"
                    ],
                    program[
                        "begin"
                    ],
                    program[
                        "end"
                    ],
                    program[
                        "title"
                    ],
                )

                all_programs[
                    key
                ] = program

            successful_days += 1

        except Exception as exc:

            failed_days += 1

            print(
                f"    HATA: {exc}"
            )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # SONUÇ KONTROL
    # --------------------------------------------------------

    print()
    print(
        "=" * 70
    )

    print(
        "EPG TOPLAMA SONUCU"
    )

    print(
        "=" * 70
    )

    print(
        f"Başarılı gün : "
        f"{successful_days}/{DAYS}"
    )

    print(
        f"Hatalı gün   : "
        f"{failed_days}/{DAYS}"
    )

    print(
        f"Program      : "
        f"{len(all_programs)}"
    )

    # 0 program ile XML üretme.
    if not all_programs:

        raise RuntimeError(
            "Hiç program alınamadı. "
            "Yanlış XML oluşturulmayacak."
        )

    # En azından 7 günden birinin alınmış olması.
    if successful_days == 0:

        raise RuntimeError(
            "Hiçbir gün başarıyla alınamadı."
        )

    # --------------------------------------------------------
    # PROGRAMLARI SIRALA
    # --------------------------------------------------------

    sorted_programs = sorted(
        all_programs.values(),
        key=lambda x: (
            x["channel_key"],
            x["begin"],
        ),
    )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    xml_count = create_xmltv(
        real_channels,
        sorted_programs,
    )

    # --------------------------------------------------------
    # XML VALIDATION
    # --------------------------------------------------------

    validate_xml()

    elapsed = (
        time.time()
        - started
    )

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

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
        f"Gerçek kanal : "
        f"{len(real_channels)}"
    )

    print(
        f"Toplam program: "
        f"{len(sorted_programs)}"
    )

    print(
        f"XML program   : "
        f"{xml_count}"
    )

    print(
        f"Başarılı gün  : "
        f"{successful_days}/{DAYS}"
    )

    print(
        f"Hatalı gün    : "
        f"{failed_days}/{DAYS}"
    )

    print(
        f"Dosya         : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Süre          : "
        f"{elapsed:.1f} saniye"
    )

    print()
    print(
        "EPG URL:"
    )

    print(
        "https://aurtkn1.github.io/tivibu-epg/epg.xml"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()
