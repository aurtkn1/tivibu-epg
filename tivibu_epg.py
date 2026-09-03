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

REQUEST_DELAY = 0.7

TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ============================================================
# COOKIE / HTTP
# ============================================================

COOKIE_JAR = CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(
        COOKIE_JAR
    )
)


# ============================================================
# METİN TEMİZLEME
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = value.replace(
        "\xa0",
        " "
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


# ============================================================
# KARŞILAŞTIRMA İÇİN İSİM NORMALİZASYONU
# ============================================================

def normalize_name(value):

    value = clean_text(
        value
    )

    value = value.upper()

    value = value.replace(
        "İ",
        "I"
    )

    value = value.replace(
        "Ğ",
        "G"
    )

    value = value.replace(
        "Ü",
        "U"
    )

    value = value.replace(
        "Ş",
        "S"
    )

    value = value.replace(
        "Ö",
        "O"
    )

    value = value.replace(
        "Ç",
        "C"
    )

    value = re.sub(
        r"[^A-Z0-9]+",
        "",
        value
    )

    return value


# ============================================================
# HTTP İSTEĞİ
# ============================================================

def http_request(
    url,
    data=None,
    headers=None
):

    request_headers = {
        "User-Agent": USER_AGENT,

        "Accept":
            "*/*",

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
        )
    )

    try:

        with OPENER.open(
            request,
            timeout=60
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

    except urllib.error.HTTPError as exc:

        try:

            error_body = exc.read().decode(
                "utf-8",
                errors="replace"
            )

        except Exception:

            error_body = ""

        raise RuntimeError(
            f"HTTP {exc.code} hatası\n"
            f"URL: {url}\n"
            f"Cevap: {error_body[:1000]}"
        )

    except Exception as exc:

        raise RuntimeError(
            f"HTTP isteği başarısız: {exc}"
        )


# ============================================================
# ANA TİVİBU SAYFASI
# ============================================================

def get_main_page():

    print(
        "[1] Tivibu canlı TV sayfası alınıyor..."
    )

    page = http_request(
        LIVE_TV_URL,
        headers={
            "Referer":
                BASE_URL + "/"
        }
    )

    print(
        f"    Sayfa uzunluğu: "
        f"{len(page):,}"
    )

    return page


# ============================================================
# CSRF TOKEN
# ============================================================

def extract_csrf_token(page):

    patterns = [

        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']'
        r'[^>]*value=["\']([^"\']+)["\']',

        r'value=["\']([^"\']+)["\']'
        r'[^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM'
        r'.{0,500}?'
        r'value=["\']([^"\']+)["\']',

        r'"CSRF-TOKEN-TVBUDNBX!-FORM"'
        r'\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            page,
            re.IGNORECASE | re.DOTALL
        )

        if match:

            token = clean_text(
                match.group(1)
            )

            if token:
                return token

    raise RuntimeError(
        "CSRF token bulunamadı."
    )


# ============================================================
# GERÇEK KANALLARI TİVİBU SAYFASINDAN ÇIKAR
# ============================================================
#
# KRİTİK KISIM
#
# Tivibu'nun gerçek kanal sayfaları:
#
# /kanallar/tivibu-tanitim
# /kanallar/tarih-tv
# /kanallar/sinema-tv
# ...
#
# Programlar ise bu bağlantıların yanında listeleniyor.
#
# Dolayısıyla artık:
#
# API'deki her kayıt = kanal
#
# YAPILMIYOR.
#
# Sadece /kanallar/ bağlantısına sahip kayıtlar
# gerçek kanal olarak kabul ediliyor.
# ============================================================

def extract_real_channels_from_html(page):

    print()
    print(
        "[2] Gerçek Tivibu kanalları HTML'den çıkarılıyor..."
    )

    channels = {}

    # Tüm anchor etiketlerini bul.
    anchor_pattern = re.compile(
        r"<a\b([^>]*)>(.*?)</a>",
        re.IGNORECASE | re.DOTALL
    )

    for match in anchor_pattern.finditer(
        page
    ):

        attributes = match.group(1)

        inner_html = match.group(2)

        # Sadece /kanallar/ bağlantıları.
        href_match = re.search(
            r'href\s*=\s*["\']([^"\']*/kanallar/[^"\']+)["\']',
            attributes,
            re.IGNORECASE
        )

        if not href_match:
            continue

        href = html.unescape(
            href_match.group(1)
        )

        # Anchor içindeki HTML'i temizle.
        text = re.sub(
            r"<[^>]+>",
            " ",
            inner_html
        )

        text = clean_text(
            text
        )

        if not text:
            continue

        # Görsel alt yazıları vs.
        text = re.sub(
            r"^Image:\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = clean_text(
            text
        )

        if not text:
            continue

        # Program saatleri kanal adı değildir.
        if re.search(
            r"\b\d{1,2}:\d{2}\b",
            text
        ):
            continue

        if "→" in text:
            continue

        normalized = normalize_name(
            text
        )

        if not normalized:
            continue

        # URL'yi benzersiz anahtar olarak kullan.
        parsed = urllib.parse.urlparse(
            href
        )

        path = parsed.path.rstrip(
            "/"
        )

        slug = path.split(
            "/"
        )[-1]

        if not slug:
            continue

        # Aynı kanalın tekrarlarını birleştir.
        if normalized not in channels:

            channels[normalized] = {
                "name": text,
                "slug": slug,
                "href": (
                    href
                    if href.startswith("http")
                    else BASE_URL + href
                ),
            }

    print(
        f"    HTML'den bulunan gerçek kanal: "
        f"{len(channels)}"
    )

    for channel in sorted(
        channels.values(),
        key=lambda x:
            x["name"].lower()
    ):

        print(
            f"      + {channel['name']}"
        )

    if not channels:

        raise RuntimeError(
            "Tivibu HTML'sinden gerçek kanal bulunamadı."
        )

    return channels


# ============================================================
# TİVİBU TARİHLERİ
# ============================================================

def extract_date_options(page):

    dates = []

    patterns = [

        re.compile(
            r'channeldatebegin\s*=\s*["\']([^"\']+)["\']'
            r'[^>]*'
            r'channeldateend\s*=\s*["\']([^"\']+)["\']',
            re.IGNORECASE
        ),

        re.compile(
            r'channeldatebegin\s*=\s*["\']([^"\']+)["\']'
            r'[^>]*'
            r'channeldateend\s*=\s*["\']([^"\']+)["\']',
            re.IGNORECASE
        ),
    ]

    for pattern in patterns:

        for match in pattern.finditer(
            page
        ):

            begin = clean_text(
                match.group(1)
            )

            end = clean_text(
                match.group(2)
            )

            dates.append(
                {
                    "begin": begin,
                    "end": end,
                }
            )

    unique = []

    seen = set()

    for item in dates:

        key = (
            item["begin"],
            item["end"]
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
                fmt
            )

        except ValueError:
            pass

    try:

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        ).replace(
            tzinfo=None
        )

    except Exception:
        return None


# ============================================================
# 7 GÜNLÜK TARİHLER
# ============================================================

def build_target_dates(page):

    today = datetime.now().date()

    source_dates = extract_date_options(
        page
    )

    result = {}

    for item in source_dates:

        dt = parse_datetime(
            item["begin"]
        )

        if not dt:
            continue

        day = dt.date()

        if day < today:
            continue

        result[day] = {
            "date": day,
            "begin": item["begin"],
            "end": item["end"],
        }

    # Sayfada yeterli tarih yoksa oluştur.
    for offset in range(DAYS):

        day = today + timedelta(
            days=offset
        )

        if day not in result:

            result[day] = {
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

    dates = [
        result[d]
        for d in sorted(result)
    ]

    return dates[:DAYS]


# ============================================================
# CHANNEL COLUMN CODE
# ============================================================

def extract_channel_column_code(page):

    patterns = [

        r'channelColumnCode\s*=\s*["\']([^"\']+)["\']',

        r'channelColumnCode\s*:\s*["\']([^"\']+)["\']',

        r'"channelColumnCode"\s*:\s*"([^"]+)"',

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            page,
            re.IGNORECASE
        )

        for value in matches:

            value = clean_text(
                value
            )

            if value:
                return value

    # Tivibu tarafında kullanılan varsayılan.
    return "020002"


# ============================================================
# CHANNEL SEARCH VALUE
# ============================================================

def extract_channel_search_value(page):

    patterns = [

        r'channelSearchValue\s*=\s*["\']([^"\']*)["\']',

        r'channelSearchValue\s*:\s*["\']([^"\']*)["\']',

        r'"channelSearchValue"\s*:\s*"([^"]*)"',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            page,
            re.IGNORECASE
        )

        for value in matches:

            value = clean_text(
                value
            )

            if value:
                return value

    return ""


# ============================================================
# GET MULTI PREVUE
# ============================================================

def post_multi_prevue(
    csrf_token,
    channel_column_code,
    channel_search_value,
    begin_date,
    end_date,
    page_no=1
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
        headers=headers
    )

    try:

        return json.loads(
            response
        )

    except json.JSONDecodeError:

        raise RuntimeError(
            "Tivibu API JSON döndürmedi.\n"
            f"Cevap:\n{response[:1500]}"
        )


# ============================================================
# JSON ALANINDAN İLK DOLU DEĞER
# ============================================================

def first_nonempty(
    item,
    keys
):

    if not isinstance(
        item,
        dict
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
# API KANALLARINI AL
# ============================================================

def extract_api_channels(data):

    if not isinstance(
        data,
        dict
    ):
        return []

    candidates = [

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

    for items in candidates:

        if isinstance(
            items,
            list
        ):
            return items

        if isinstance(
            items,
            dict
        ):

            for value in items.values():

                if isinstance(
                    value,
                    list
                ):
                    return value

    return []


# ============================================================
# API PROGRAMLARINI AL
# ============================================================

def extract_api_programs(data):

    if not isinstance(
        data,
        dict
    ):
        return []

    candidates = [

        data.get(
            "prevueListViewModel"
        ),

        data.get(
            "mobilPrevueViewModel"
        ),

        data.get(
            "programListViewModel"
        ),

        data.get(
            "programs"
        ),
    ]

    for items in candidates:

        if isinstance(
            items,
            list
        ):
            return items

        if isinstance(
            items,
            dict
        ):

            for value in items.values():

                if isinstance(
                    value,
                    list
                ):
                    return value

    return []


# ============================================================
# API KANAL ADI
# ============================================================

def get_channel_name_from_api(
    item
):

    return first_nonempty(
        item,
        [
            "channelName",
            "displayName",
            "name",
            "title",
        ]
    )


# ============================================================
# GERÇEK KANAL EŞLEŞTİRME
# ============================================================
#
# API'deki channelCode ile Tivibu HTML'deki gerçek kanal
# ismini eşleştiriyoruz.
#
# Eşleşmeyen hiçbir API kaydı kanal yapılmaz.
# ============================================================

def build_channel_map(
    real_channels,
    api_items
):

    channel_map = {}

    real_names = {
        key: value
        for key, value
        in real_channels.items()
    }

    for item in api_items:

        if not isinstance(
            item,
            dict
        ):
            continue

        code = first_nonempty(
            item,
            [
                "channelCode",
                "code",
                "channelId",
                "id",
            ]
        )

        name = get_channel_name_from_api(
            item
        )

        if not code or not name:
            continue

        normalized = normalize_name(
            name
        )

        # API kaydı gerçek Tivibu kanal listesinde
        # yoksa KESİNLİKLE kanal olarak ekleme.
        if normalized not in real_names:

            continue

        real_channel = real_names[
            normalized
        ]

        icon = first_nonempty(
            item,
            [
                "channelImage",
                "channelIcon",
                "image",
                "icon",
            ]
        )

        channel_map[
            code
        ] = {
            "name":
                real_channel["name"],

            "icon":
                icon,

            "slug":
                real_channel["slug"],

            "href":
                real_channel["href"],
        }

    return channel_map


# ============================================================
# PROGRAM BAŞLIĞI
# ============================================================

def get_program_title(
    program
):

    return first_nonempty(
        program,
        [
            "prevueName",
            "programName",
            "programmeName",
            "title",
            "name",
        ]
    )


# ============================================================
# PROGRAM KANAL KODU
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
        ]
    )


# ============================================================
# PROGRAM BAŞLANGIÇ
# ============================================================

def get_program_start(
    program
):

    return first_nonempty(
        program,
        [
            "beginTime",
            "startTime",
            "startDate",
            "start",
            "dateBegin",
        ]
    )


# ============================================================
# PROGRAM BİTİŞ
# ============================================================

def get_program_end(
    program
):

    return first_nonempty(
        program,
        [
            "endTime",
            "stopTime",
            "endDate",
            "end",
            "dateEnd",
        ]
    )


# ============================================================
# PROGRAM GEÇERLİ Mİ?
# ============================================================

def valid_program(
    program
):

    title = get_program_title(
        program
    )

    if not title:
        return False

    if len(title) < 2:
        return False

    start = parse_datetime(
        get_program_start(
            program
        )
    )

    end = parse_datetime(
        get_program_end(
            program
        )
    )

    if not start or not end:
        return False

    if end <= start:
        return False

    return True


# ============================================================
# XMLTV
# ============================================================

def create_xmltv(
    channels,
    programs
):

    print()
    print(
        "[5] XML oluşturuluyor..."
    )

    tv = ET.Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",

            "generator-info-url":
                LIVE_TV_URL,
        }
    )

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    for code, info in sorted(
        channels.items(),
        key=lambda x:
            x[1]["name"].lower()
    ):

        channel = ET.SubElement(
            tv,
            "channel",
            {
                "id":
                    str(code)
            }
        )

        display_name = ET.SubElement(
            channel,
            "display-name",
            {
                "lang":
                    "tr"
            }
        )

        display_name.text = info[
            "name"
        ]

        icon = clean_text(
            info.get(
                "icon",
                ""
            )
        )

        if icon.startswith(
            "http://"
        ) or icon.startswith(
            "https://"
        ):

            ET.SubElement(
                channel,
                "icon",
                {
                    "src":
                        icon
                }
            )

    # --------------------------------------------------------
    # PROGRAMME
    # --------------------------------------------------------

    written = 0

    seen = set()

    for program in sorted(
        programs,
        key=lambda p: (
            parse_datetime(
                get_program_start(
                    p
                )
            )
            or datetime.min
        )
    ):

        if not valid_program(
            program
        ):
            continue

        channel_code = (
            get_program_channel_code(
                program
            )
        )

        # Gerçek kanal yoksa programı da yazma.
        if channel_code not in channels:
            continue

        title = get_program_title(
            program
        )

        start = parse_datetime(
            get_program_start(
                program
            )
        )

        end = parse_datetime(
            get_program_end(
                program
            )
        )

        key = (
            channel_code,
            start,
            end,
            title
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        programme = ET.SubElement(
            tv,
            "programme",
            {
                "start":
                    start.strftime(
                        "%Y%m%d%H%M%S"
                    ) + " " + TIMEZONE,

                "stop":
                    end.strftime(
                        "%Y%m%d%H%M%S"
                    ) + " " + TIMEZONE,

                "channel":
                    str(channel_code),
            }
        )

        title_element = ET.SubElement(
            programme,
            "title",
            {
                "lang":
                    "tr"
            }
        )

        title_element.text = title

        description = first_nonempty(
            program,
            [
                "description",
                "desc",
                "summary",
            ]
        )

        if description:

            desc_element = ET.SubElement(
                programme,
                "desc",
                {
                    "lang":
                        "tr"
                }
            )

            desc_element.text = description

        category = first_nonempty(
            program,
            [
                "category",
                "genre",
            ]
        )

        if category:

            category_element = ET.SubElement(
                programme,
                "category",
                {
                    "lang":
                        "tr"
                }
            )

            category_element.text = category

        written += 1

    try:

        ET.indent(
            tv,
            space="  "
        )

    except AttributeError:
        pass

    tree = ET.ElementTree(
        tv
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    return written


# ============================================================
# XML KONTROL
# ============================================================

def validate_xml():

    print()
    print(
        "[6] XML kontrol ediliyor..."
    )

    tree = ET.parse(
        OUTPUT_FILE
    )

    root = tree.getroot()

    channels = root.findall(
        "channel"
    )

    programmes = root.findall(
        "programme"
    )

    channel_ids = {
        channel.get("id")
        for channel in channels
    }

    print(
        f"    Kanal sayısı   : "
        f"{len(channels)}"
    )

    print(
        f"    Program sayısı : "
        f"{len(programmes)}"
    )

    # --------------------------------------------------------
    # CHANNEL KONTROL
    # --------------------------------------------------------

    bad_channels = []

    for channel in channels:

        display = channel.find(
            "display-name"
        )

        if display is None:
            bad_channels.append(
                "display-name yok"
            )
            continue

        name = clean_text(
            display.text
        )

        # Program gibi görünen kanal varsa hata.
        if re.search(
            r"\b\d{1,2}:\d{2}\b",
            name
        ):

            bad_channels.append(
                name
            )

        if "→" in name:

            bad_channels.append(
                name
            )

    # --------------------------------------------------------
    # PROGRAM KONTROL
    # --------------------------------------------------------

    bad_programmes = []

    for programme in programmes:

        channel_id = programme.get(
            "channel"
        )

        if channel_id not in channel_ids:

            bad_programmes.append(
                f"Geçersiz kanal: {channel_id}"
            )

        title = programme.find(
            "title"
        )

        if (
            title is None
            or not clean_text(
                title.text
            )
        ):

            bad_programmes.append(
                "Başlıksız program"
            )

    if bad_channels:

        print()
        print(
            "HATALI CHANNEL KAYITLARI:"
        )

        for item in bad_channels:

            print(
                f"    {item}"
            )

        raise RuntimeError(
            "XML içinde geçersiz kanal bulundu."
        )

    if bad_programmes:

        print()
        print(
            "HATALI PROGRAM KAYITLARI:"
        )

        for item in bad_programmes[:20]:

            print(
                f"    {item}"
            )

        raise RuntimeError(
            "XML içinde geçersiz program bulundu."
        )

    print(
        "    XML kontrolü BAŞARILI."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    print()
    print("=" * 70)
    print(
        "TİVİBU 7 GÜNLÜK EPG"
    )
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # 1 - ANA SAYFA
    # --------------------------------------------------------

    page = get_main_page()

    # --------------------------------------------------------
    # 2 - GERÇEK KANALLAR
    # --------------------------------------------------------

    real_channels = (
        extract_real_channels_from_html(
            page
        )
    )

    # --------------------------------------------------------
    # CSRF
    # --------------------------------------------------------

    csrf_token = extract_csrf_token(
        page
    )

    print()
    print(
        f"    CSRF token: "
        f"{len(csrf_token)} karakter"
    )

    # --------------------------------------------------------
    # API DEĞERLERİ
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
        f"    channelColumnCode : "
        f"{channel_column_code}"
    )

    print(
        f"    channelSearchValue: "
        f"{channel_search_value or '(boş)'}"
    )

    # --------------------------------------------------------
    # 3 - TARİHLER
    # --------------------------------------------------------

    target_dates = build_target_dates(
        page
    )

    print()
    print(
        "[3] 7 günlük tarih listesi:"
    )

    for index, item in enumerate(
        target_dates,
        start=1
    ):

        print(
            f"    {index}. "
            f"{item['date'].strftime('%d.%m.%Y')}"
        )

    # --------------------------------------------------------
    # 4 - API
    # --------------------------------------------------------

    print()
    print(
        "[4] Tivibu EPG verileri çekiliyor..."
    )

    all_channels = {}

    all_programs = {}

    successful_days = 0

    failed_days = 0

    for index, date_info in enumerate(
        target_dates,
        start=1
    ):

        print()
        print(
            "-" * 60
        )

        print(
            f"GÜN {index}/{len(target_dates)}"
        )

        print(
            date_info[
                "date"
            ].strftime(
                "%d.%m.%Y"
            )
        )

        try:

            data = post_multi_prevue(
                csrf_token=
                    csrf_token,

                channel_column_code=
                    channel_column_code,

                channel_search_value=
                    channel_search_value,

                begin_date=
                    date_info["begin"],

                end_date=
                    date_info["end"],

                page_no=1
            )

            # ------------------------------------------------
            # API KANALLARI
            # ------------------------------------------------

            api_channels = (
                extract_api_channels(
                    data
                )
            )

            # SADECE gerçek HTML kanallarını eşleştir.
            matched_channels = (
                build_channel_map(
                    real_channels,
                    api_channels
                )
            )

            for code, info in (
                matched_channels.items()
            ):

                if code not in all_channels:

                    all_channels[
                        code
                    ] = info

            # ------------------------------------------------
            # PROGRAMLAR
            # ------------------------------------------------

            programs = (
                extract_api_programs(
                    data
                )
            )

            new_programs = 0

            for program in programs:

                if not isinstance(
                    program,
                    dict
                ):
                    continue

                channel_code = (
                    get_program_channel_code(
                        program
                    )
                )

                # PROGRAMIN KANALI GERÇEK KANAL DEĞİLSE
                # PROGRAMI DA YAZMA.
                if channel_code not in matched_channels:

                    continue

                if not valid_program(
                    program
                ):
                    continue

                title = get_program_title(
                    program
                )

                start = parse_datetime(
                    get_program_start(
                        program
                    )
                )

                end = parse_datetime(
                    get_program_end(
                        program
                    )
                )

                key = (
                    channel_code,
                    start,
                    end,
                    title
                )

                if key in all_programs:

                    continue

                all_programs[
                    key
                ] = program

                new_programs += 1

            print(
                f"    API kanal kaydı     : "
                f"{len(api_channels)}"
            )

            print(
                f"    Gerçek kanal eşleşme: "
                f"{len(matched_channels)}"
            )

            print(
                f"    Program              : "
                f"{len(programs)}"
            )

            print(
                f"    Yeni program         : "
                f"{new_programs}"
            )

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
    # KRİTİK KONTROL
    # --------------------------------------------------------

    if not all_channels:

        raise RuntimeError(
            "Hiçbir gerçek kanal API ile eşleşmedi. "
            "Yanlış XML oluşturulmayacak."
        )

    if not all_programs:

        raise RuntimeError(
            "Hiç program alınamadı. "
            "Yanlış XML oluşturulmayacak."
        )

    # --------------------------------------------------------
    # 5 - XML
    # --------------------------------------------------------

    written = create_xmltv(
        channels=
            all_channels,

        programs=
            list(
                all_programs.values()
            )
    )

    # --------------------------------------------------------
    # 6 - KONTROL
    # --------------------------------------------------------

    validate_xml()

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    elapsed = time.time() - started

    print()
    print("=" * 70)
    print(
        "TAMAMLANDI"
    )
    print("=" * 70)

    print(
        f"Gerçek kanal       : "
        f"{len(all_channels)}"
    )

    print(
        f"Program            : "
        f"{written}"
    )

    print(
        f"Başarılı gün       : "
        f"{successful_days}"
    )

    print(
        f"Hatalı gün         : "
        f"{failed_days}"
    )

    print(
        f"Dosya              : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Süre               : "
        f"{elapsed:.1f} saniye"
    )

    print()
    print(
        "EPG hazır."
    )

    print(
        "TiviMate URL:"
    )

    print(
        "https://aurtkn1.github.io/tivibu-epg/epg.xml"
    )

    print("=" * 70)
    print()


# ============================================================
# ÇALIŞTIR
# ============================================================

if __name__ == "__main__":

    main()
