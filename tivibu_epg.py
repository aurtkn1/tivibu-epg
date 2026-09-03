```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU
======================================================================

- Tivibu Canlı TV sayfasını alır
- 7 günlük tarihleri belirler
- Sayfadaki kanal/program akışlarını çıkarır
- XMLTV / epg.xml oluşturur
- GetMultiPrevueData 400 hatasına bağımlı değildir
- API varsa ayrıca denenir
- API başarısız olursa HTML parser ile devam eder

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
# HTTP OTURUMU
# =====================================================================

COOKIE_JAR = http.cookiejar.CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


def http_request(
    url,
    data=None,
    headers=None,
):
    """
    GET / POST isteği.

    400 durumunda response body'sini de yakalar.
    """

    request_headers = {
        "User-Agent": USER_AGENT,

        "Accept":
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8",

        "Accept-Language":
            "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",

        "Cache-Control": "no-cache",

        "Pragma": "no-cache",

        "Connection": "keep-alive",
    }

    if headers:
        request_headers.update(headers)

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

        if len(preview) > 2000:
            preview = preview[:2000]

        raise RuntimeError(
            f"HTTP {exc.code}\n"
            f"URL: {url}\n"
            f"Tivibu cevabı: {preview or '(boş)'}"
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
            "Referer": BASE_URL + "/",
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

    return page


# =====================================================================
# CSRF
# =====================================================================

def extract_csrf_token(page):

    patterns = [

        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']'
        r'[^>]*value=["\']([^"\']+)["\']',

        r'value=["\']([^"\']+)["\']'
        r'[^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM["\']?\s*'
        r'[:=]\s*["\']([^"\']+)["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM.{0,500}?'
        r'value=["\']([^"\']+)["\']',

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
# TİVİBU TARİHİ
# =====================================================================

def parse_tivibu_date(value):

    if not value:
        return None

    value = html.unescape(
        value
    ).strip()

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:
            continue

    return None


# =====================================================================
# SAYFADAKİ TARİHLER
# =====================================================================

def extract_date_options(page):

    results = []

    patterns = [

        re.compile(
            r'channeldatebegin=["\']([^"\']+)["\']'
            r'[^>]*'
            r'channeldateend=["\']([^"\']+)["\']',
            re.IGNORECASE,
        ),

        re.compile(
            r'channelDateBegin["\']?\s*[:=]\s*'
            r'["\']([^"\']+)["\']'
            r'.{0,500}?'
            r'channelDateEnd["\']?\s*[:=]\s*'
            r'["\']([^"\']+)["\']',
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    for pattern in patterns:

        for match in pattern.finditer(page):

            begin = html.unescape(
                match.group(1)
            ).strip()

            end = html.unescape(
                match.group(2)
            ).strip()

            results.append(
                {
                    "begin": begin,
                    "end": end,
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

        seen.add(key)

        unique.append(item)

    return unique


# =====================================================================
# 7 GÜN OLUŞTUR
# =====================================================================

def build_target_dates(page):

    today = datetime.now().date()

    page_dates = extract_date_options(
        page
    )

    result = []

    for item in page_dates:

        dt = parse_tivibu_date(
            item["begin"]
        )

        if not dt:
            continue

        if dt.date() < today:
            continue

        result.append(
            {
                "date": dt.date(),
                "begin": item["begin"],
                "end": item["end"],
            }
        )

    unique = {}

    for item in result:

        unique[
            item["date"]
        ] = item

    result = [
        unique[d]
        for d in sorted(unique)
    ]

    # ---------------------------------------------------------------
    # Eksik günleri otomatik oluştur
    # ---------------------------------------------------------------

    existing = {
        x["date"]
        for x in result
    }

    for offset in range(DAYS):

        day = today + timedelta(
            days=offset
        )

        if day in existing:
            continue

        result.append(
            {
                "date": day,

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
        key=lambda x: x["date"]
    )

    return result[:DAYS]


# =====================================================================
# METİN TEMİZLE
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
# HTML ATTRIBUTE
# =====================================================================

def parse_attributes(tag):

    attrs = {}

    pattern = re.compile(
        r'([A-Za-z_:][A-Za-z0-9_.:-]*)'
        r'\s*=\s*'
        r'(["\'])(.*?)\2',
        re.DOTALL,
    )

    for match in pattern.finditer(
        tag
    ):

        key = match.group(1).lower()

        value = html.unescape(
            match.group(3)
        ).strip()

        attrs[key] = value

    return attrs


# =====================================================================
# TARİH SAATİ PARSE
# =====================================================================

def parse_program_datetime(
    value,
    default_date=None,
):

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

    # ---------------------------------------------------------------
    # Sadece saat geldiyse
    # ---------------------------------------------------------------

    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})",
        value,
    )

    if match and default_date:

        hour = int(
            match.group(1)
        )

        minute = int(
            match.group(2)
        )

        return datetime.combine(
            default_date,
            datetime.min.time(),
        ).replace(
            hour=hour,
            minute=minute,
        )

    return None


# =====================================================================
# URL
# =====================================================================

def make_absolute_url(
    value
):

    if not value:
        return ""

    value = html.unescape(
        value
    ).strip()

    if value.startswith(
        "//"
    ):
        return "https:" + value

    if value.startswith(
        "/"
    ):
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
# PROGRAM SAAT ARALIĞI
# =====================================================================

def extract_time_range(
    text
):

    text = clean_text(
        text
    )

    pattern = re.compile(
        r'(\d{1,2}:\d{2})'
        r'\s*(?:→|->|-)\s*'
        r'(\d{1,2}:\d{2})'
    )

    match = pattern.search(
        text
    )

    if not match:
        return None

    return (
        match.group(1),
        match.group(2),
    )


# =====================================================================
# HTML'DEN PROGRAMLAR
# =====================================================================

def parse_html_programs(
    page,
    target_date,
):

    programs = []

    # ---------------------------------------------------------------
    # Önce açık program attribute'larını ara
    # ---------------------------------------------------------------

    element_pattern = re.compile(
        r'<(?:a|div|li|article)[^>]*>'
        r'.{0,3000}?'
        r'</(?:a|div|li|article)>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in element_pattern.finditer(
        page
    ):

        block = match.group(0)

        attrs = parse_attributes(
            block.split(">")[0] + ">"
        )

        text = clean_text(
            block
        )

        if not text:
            continue

        time_range = extract_time_range(
            text
        )

        if not time_range:
            continue

        begin_time, end_time = time_range

        begin_dt = parse_program_datetime(
            begin_time,
            target_date,
        )

        end_dt = parse_program_datetime(
            end_time,
            target_date,
        )

        if not begin_dt or not end_dt:
            continue

        # -----------------------------------------------------------
        # Gece yarısı geçişi
        # -----------------------------------------------------------

        if end_dt <= begin_dt:

            end_dt += timedelta(
                days=1
            )

        # -----------------------------------------------------------
        # Program adı
        # -----------------------------------------------------------

        title = ""

        # Link / başlık attribute'ları
        for key in (
            "title",
            "data-title",
            "programname",
            "program-name",
            "prevuename",
            "name",
        ):

            if attrs.get(key):

                title = clean_text(
                    attrs[key]
                )

                break

        if not title:

            lines = [
                clean_text(x)
                for x in re.split(
                    r"[\r\n]+",
                    text,
                )
            ]

            candidates = []

            for line in lines:

                if not line:
                    continue

                if "→" in line:
                    continue

                if "->" in line:
                    continue

                if "Canlı" == line:
                    continue

                if len(line) < 2:
                    continue

                candidates.append(
                    line
                )

            if candidates:
                title = candidates[0]

        if not title:
            continue

        # -----------------------------------------------------------
        # Kategori
        # -----------------------------------------------------------

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

            category = clean_text(
                category_match.group(1)
            )

        programs.append(
            {
                "title": title,

                "description": "",

                "category": category,

                "start": begin_dt,

                "stop": end_dt,

                "channel_code":
                    attrs.get(
                        "channelcode",
                        ""
                    ),

                "channel_name":
                    attrs.get(
                        "channelname",
                        ""
                    ),

                "icon":
                    make_absolute_url(
                        attrs.get(
                            "src",
                            ""
                        )
                    ),
            }
        )

    return programs


# =====================================================================
# KANALLARI HTML'DEN ÇIKAR
# =====================================================================

def parse_html_channels(
    page
):

    channels = {}

    # ---------------------------------------------------------------
    # Link başlıkları
    # ---------------------------------------------------------------

    pattern = re.compile(
        r'<a([^>]*)>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(
        page
    ):

        attrs = parse_attributes(
            "<a" +
            match.group(1) +
            ">"
        )

        body = match.group(2)

        text = clean_text(
            body
        )

        if not text:
            continue

        name = ""

        # Kanal adı genellikle link text'inde.
        lines = [
            clean_text(x)
            for x in re.split(
                r"[\r\n]+",
                text,
            )
        ]

        for line in lines:

            if not line:
                continue

            if "→" in line:
                continue

            if "Canlı" == line:
                continue

            if re.fullmatch(
                r"\d{1,2}:\d{2}",
                line,
            ):
                continue

            # Çok uzun açıklamaları kanal olarak alma.
            if len(line) > 100:
                continue

            name = line

            break

        if not name:
            continue

        code = (
            attrs.get(
                "channelcode"
            )
            or attrs.get(
                "channel-code"
            )
            or attrs.get(
                "data-channel-code"
            )
            or attrs.get(
                "id"
            )
        )

        if not code:

            # İsimden stabil XMLTV ID
            code = re.sub(
                r"[^a-zA-Z0-9_-]+",
                "_",
                name.lower(),
            ).strip("_")

        if not code:
            continue

        icon = ""

        src_match = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            body,
            re.IGNORECASE,
        )

        if src_match:

            icon = make_absolute_url(
                src_match.group(1)
            )

        if code not in channels:

            channels[code] = {
                "name": name,
                "icon": icon,
            }

    return channels


# =====================================================================
# API DENEMESİ
# =====================================================================

def try_api(
    csrf_token,
    begin_date,
    end_date,
):

    if not csrf_token:
        return None

    # ---------------------------------------------------------------
    # Farklı olası parametre kombinasyonlarını dene.
    # ---------------------------------------------------------------

    payloads = [

        {
            "channelColumnCode": "020002",
            "channelDateBegin": begin_date,
            "channelDateEnd": end_date,
            "channelSearchValue": "",
            "pageNo": "1",
        },

        {
            "channelColumnCode": "020002",
            "channelDateBegin": begin_date,
            "channelDateEnd": end_date,
            "channelSearchValue": "",
            "page": "1",
        },

        {
            "channelColumnCode": "020002",
            "channelDateBegin": begin_date,
            "channelDateEnd": end_date,
        },

    ]

    for payload in payloads:

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

            try:

                data = json.loads(
                    response
                )

            except json.JSONDecodeError:

                continue

            if isinstance(
                data,
                dict
            ):

                return data

        except Exception:

            continue

    return None


# =====================================================================
# JSON PROGRAM NORMALİZASYONU
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

        value = clean_text(
            item.get(key)
        )

        if value:
            return value

    return ""


def normalize_json_channels(
    data
):

    channels = {}

    if not isinstance(
        data,
        dict,
    ):
        return channels

    possible = [

        data.get(
            "channelListViewModel"
        ),

        data.get(
            "channelList"
        ),

        data.get(
            "channels"
        ),

    ]

    for items in possible:

        if not isinstance(
            items,
            list,
        ):
            continue

        for item in items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            code = json_first(
                item,
                [
                    "channelCode",
                    "code",
                    "id",
                ],
            )

            name = json_first(
                item,
                [
                    "channelName",
                    "name",
                    "displayName",
                ],
            )

            if not code:
                continue

            if not name:
                name = code

            icon = json_first(
                item,
                [
                    "channelImage",
                    "image",
                    "channelIcon",
                    "icon",
                ],
            )

            channels[code] = {
                "name": name,
                "icon":
                    make_absolute_url(
                        icon
                    ),
            }

    return channels


def normalize_json_programs(
    data
):

    if not isinstance(
        data,
        dict,
    ):
        return []

    possible_keys = [

        "prevueListViewModel",

        "mobilPrevueViewModel",

        "programListViewModel",

        "programs",

        "programList",

        "epgList",

    ]

    for key in possible_keys:

        value = data.get(
            key
        )

        if isinstance(
            value,
            list,
        ):
            return value

    return []


def convert_json_programs(
    data
):

    result = []

    for item in normalize_json_programs(
        data
    ):

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
            ],
        )

        end = json_first(
            item,
            [
                "endTime",
                "stopTime",
                "end",
                "stop",
            ],
        )

        if not title:
            continue

        begin_dt = parse_program_datetime(
            begin
        )

        end_dt = parse_program_datetime(
            end
        )

        if not begin_dt or not end_dt:
            continue

        if end_dt <= begin_dt:

            end_dt += timedelta(
                days=1
            )

        result.append(
            {
                "title": title,

                "description":
                    json_first(
                        item,
                        [
                            "description",
                            "desc",
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

                "start": begin_dt,

                "stop": end_dt,

                "channel_code":
                    channel_code,

                "channel_name":
                    json_first(
                        item,
                        [
                            "channelName",
                        ],
                    ),

                "icon":
                    make_absolute_url(
                        json_first(
                            item,
                            [
                                "channelImage",
                                "image",
                                "icon",
                            ],
                        )
                    ),
            }
        )

    return result


# =====================================================================
# PROGRAMLARI TEKİLLEŞTİR
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
# XML
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
        attrs["lang"] = lang

    element = ET.SubElement(
        parent,
        tag,
        attrs,
    )

    element.text = clean_text(
        text
    )

    return element


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

    # ---------------------------------------------------------------
    # Kanallar
    # ---------------------------------------------------------------

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
                "id": code,
            },
        )

        xml_add_text(
            channel,
            "display-name",
            info["name"],
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
                    "src": icon,
                },
            )

    # ---------------------------------------------------------------
    # Programlar
    # ---------------------------------------------------------------

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
            ) or datetime.min,
        ),
    ):

        channel_code = program.get(
            "channel_code",
            "",
        )

        if not channel_code:
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

    # ---------------------------------------------------------------
    # XML yaz
    # ---------------------------------------------------------------

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
    print("=" * 70)
    print(
        "TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU"
    )
    print("=" * 70)
    print()

    # ---------------------------------------------------------------
    # 1
    # ---------------------------------------------------------------

    page = get_main_page()

    # ---------------------------------------------------------------
    # 2
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # 3
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # 4
    # ---------------------------------------------------------------

    print()

    print(
        "[4/5] EPG verileri toplanıyor..."
    )

    print()

    channels = {}

    programs = {}

    successful_days = 0

    failed_days = 0

    # ---------------------------------------------------------------
    # İlk sayfadan kanal isimlerini al
    # ---------------------------------------------------------------

    first_channels = parse_html_channels(
        page
    )

    for code, info in first_channels.items():

        channels[
            code
        ] = info

    print(
        f"      Ana sayfadan bulunan kanal: "
        f"{len(first_channels)}"
    )

    # ---------------------------------------------------------------
    # 7 gün
    # ---------------------------------------------------------------

    for index, date_info in enumerate(
        target_dates,
        1,
    ):

        target_date = date_info[
            "date"
        ]

        print(
            f"--- GÜN {index}/{len(target_dates)} ---"
        )

        print(
            f"Tarih: "
            f"{target_date.strftime('%d.%m.%Y')}"
        )

        day_programs = []

        # -----------------------------------------------------------
        # Önce API'yi dene
        # -----------------------------------------------------------

        api_data = None

        if csrf_token:

            api_data = try_api(
                csrf_token,
                date_info["begin"],
                date_info["end"],
            )

        if api_data:

            print(
                "      API: başarılı"
            )

            api_channels = (
                normalize_json_channels(
                    api_data
                )
            )

            api_programs = (
                convert_json_programs(
                    api_data
                )
            )

            for code, info in api_channels.items():

                channels[
                    code
                ] = info

            day_programs.extend(
                api_programs
            )

            print(
                f"      API kanal: "
                f"{len(api_channels)}"
            )

            print(
                f"      API program: "
                f"{len(api_programs)}"
            )

        else:

            print(
                "      API: kullanılamadı "
                "(HTML parser devreye giriyor)"
            )

        # -----------------------------------------------------------
        # HTML parser
        # -----------------------------------------------------------

        html_programs = parse_html_programs(
            page,
            target_date,
        )

        if html_programs:

            print(
                f"      HTML program: "
                f"{len(html_programs)}"
            )

            day_programs.extend(
                html_programs
            )

        # -----------------------------------------------------------
        # Programları kaydet
        # -----------------------------------------------------------

        new_count = 0

        for program in day_programs:

            key = program_key(
                program
            )

            if key in programs:
                continue

            programs[
                key
            ] = program

            # Kanal kodu yoksa isimden oluştur.
            if not program.get(
                "channel_code"
            ):

                name = program.get(
                    "channel_name",
                    "",
                )

                if name:

                    code = re.sub(
                        r"[^a-zA-Z0-9_-]+",
                        "_",
                        name.lower(),
                    ).strip("_")

                    program[
                        "channel_code"
                    ] = code

            code = program.get(
                "channel_code",
                "",
            )

            if code:

                if code not in channels:

                    channels[
                        code
                    ] = {
                        "name":
                            program.get(
                                "channel_name"
                            )
                            or code,

                        "icon":
                            program.get(
                                "icon",
                                "",
                            ),
                    }

                else:

                    if (
                        not channels[
                            code
                        ].get(
                            "icon"
                        )
                        and program.get(
                            "icon"
                        )
                    ):

                        channels[
                            code
                        ]["icon"] = (
                            program.get(
                                "icon"
                            )
                        )

            new_count += 1

        if day_programs:

            successful_days += 1

        else:

            failed_days += 1

        print(
            f"      Yeni program: "
            f"{new_count}"
        )

        print()

        time.sleep(
            REQUEST_DELAY
        )

    # ---------------------------------------------------------------
    # Eğer programlardan kanal çıkmışsa ekle
    # ---------------------------------------------------------------

    for program in programs.values():

        code = program.get(
            "channel_code",
            "",
        )

        if not code:
            continue

        if code not in channels:

            channels[
                code
            ] = {
                "name":
                    program.get(
                        "channel_name"
                    )
                    or code,

                "icon":
                    program.get(
                        "icon",
                        "",
                    ),
            }

    # ---------------------------------------------------------------
    # Program listesi
    # ---------------------------------------------------------------

    all_programs = list(
        programs.values()
    )

    # ---------------------------------------------------------------
    # XML
    # ---------------------------------------------------------------

    xml_count = create_xmltv(
        channels,
        all_programs,
    )

    elapsed = (
        time.time()
        - start_time
    )

    # ---------------------------------------------------------------
    # SONUÇ
    # ---------------------------------------------------------------

    print()

    print("=" * 70)
    print("TAMAMLANDI")
    print("=" * 70)

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

    print("=" * 70)

    # ---------------------------------------------------------------
    # BOŞSA UYARI
    # ---------------------------------------------------------------

    if xml_count == 0:

        print()
        print(
            "UYARI: XML'e hiç program yazılamadı."
        )

        print(
            "Tivibu HTML yapısı değişmiş olabilir."
        )

        print(
            "epg.xml boş bırakılmadı; kanal/program "
            "bulunamadığı için yalnızca <tv> oluşturuldu."
        )


# =====================================================================
# ÇALIŞTIR
# =====================================================================

if __name__ == "__main__":
    main()
```
