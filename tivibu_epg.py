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
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
MULTI_PREVUE_URL = f"{BASE_URL}/Channel/GetMultiPrevueData"

OUTPUT_FILE = "epg.xml"

DAYS = 7
REQUEST_DELAY = 0.50
TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

COOKIE_JAR = CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)


# ============================================================
# GERÇEK TİVİBU KANALLARI
# ============================================================
#
# Program isimlerinin yanlışlıkla kanal olarak yazılmasını
# engellemek için gerçek kanal isimlerini beyaz liste olarak
# kullanıyoruz.
#
# Tivibu sayfası değişirse buraya yeni gerçek kanallar
# eklenebilir.
# ============================================================

KNOWN_CHANNELS = {
    "TİVİBU TANITIM",
    "BENİM KANALIM",

    "TARİH TV",

    "SİNEMA TV",
    "SİNEMA 2",
    "SİNEMA YERLİ",
    "SİNEMA YERLİ 2",
    "SİNEMA AKSİYON",
    "SİNEMA AKSİYON 2",

    "TLC",
    "DMAX",
    "DISCOVERY CHANNEL",
    "DISCOVERY SCIENCE",
    "DISCOVERY ID",
    "NAT GEO",
    "NAT GEO WILD",
    "ANIMAL PLANET",

    "TRT 1",
    "TRT 2",
    "TRT BELGESEL",
    "TRT SPOR",
    "TRT SPOR YILDIZ",
    "TRT HABER",
    "TRT MÜZİK",
    "TRT ÇOCUK",
    "TRT TÜRK",
    "TRT AVAZ",
    "TRT WORLD",

    "ATV",
    "KANAL D",
    "SHOW TV",
    "STAR TV",
    "NOW",
    "TV8",
    "TV8,5",
    "CNBC-E",
    "A HABER",
    "A SPOR",
    "HABERTÜRK",
    "BLOOMBERG HT",

    "NTV",
    "CNN TÜRK",
    "HABER GLOBAL",
    "TGRT HABER",
    "TV100",
    "24 TV",

    "360",
    "BEYAZ TV",
    "KANAL 7",
    "TV4",
    "TEVE2",
    "360 TV",

    "EUROSPORT 1",
    "EUROSPORT 2",

    "S SPORT",
    "S SPORT 2",
    "TİVİBU SPOR",
    "TİVİBU SPOR 1",
    "TİVİBU SPOR 2",
    "TİVİBU SPOR 3",
    "TİVİBU SPOR 4",

    "CARTOON NETWORK",
    "MINIKA GO",
    "MINIKA ÇOCUK",
    "DISNEY CHANNEL",
    "NICKTOONS",
    "NICKELODEON",

    "BABY TV",
    "CARTOONITO",

    "FOX CRIME",
    "FX",
    "AXN",
    "WARNER TV",
    "AMC",
    "EPIC DRAMA",

    "DREAM TÜRK",
    "POWER TV",
    "POWER TÜRK",
    "NR1",
    "NR1 TÜRK",

    "FASHION TV",
    "LOVE NATURE",
    "VİASAT NATURE",

    "CNN",
    "BBC WORLD NEWS",
    "EURONEWS",
    "AL JAZEERA",
    "FRANCE 24",

    "HISTORY",
    "HISTORY 2",

    "KIDSCO",
}


# ============================================================
# PROGRAM OLAMAYACAK / KANAL OLAMAYACAK İSİMLER
# ============================================================

BLOCKED_CHANNEL_NAMES = {
    "TİVİBU CANLI TV",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
    "FAVORİ KANALLARIM",
}


# ============================================================
# HTTP
# ============================================================

def http_request(url, data=None, headers=None):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
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
        with OPENER.open(request, timeout=60) as response:
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
        body = ""

        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            pass

        raise RuntimeError(
            f"HTTP {exc.code}\n"
            f"URL: {url}\n"
            f"Cevap: {body[:1000]}"
        ) from exc

    except Exception as exc:
        raise RuntimeError(
            f"HTTP isteği başarısız:\n"
            f"URL: {url}\n"
            f"Hata: {exc}"
        ) from exc


# ============================================================
# TİVİBU ANA SAYFASI
# ============================================================

def get_main_page():
    print("[1/5] Tivibu canlı TV sayfası alınıyor...")

    return http_request(
        LIVE_TV_URL,
        headers={
            "Referer": BASE_URL + "/",
        },
    )


# ============================================================
# CSRF TOKEN
# ============================================================

def extract_csrf_token(page):

    patterns = [
        r'name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\'][^>]*value=["\']([^"\']+)["\']',

        r'value=["\']([^"\']+)["\'][^>]*name=["\']CSRF-TOKEN-TVBUDNBX!-FORM["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM["\']?\s*[:=]\s*["\']([^"\']+)["\']',

        r'CSRF-TOKEN-TVBUDNBX!-FORM.{0,500}?value=["\']([^"\']+)["\']',
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

    raise RuntimeError(
        "CSRF token bulunamadı."
    )


# ============================================================
# TARİH SEÇENEKLERİ
# ============================================================

def extract_date_options(page):

    results = []

    pattern = re.compile(
        r'<a[^>]*'
        r'channeldatebegin=["\']([^"\']+)["\'][^>]*'
        r'channeldateend=["\']([^"\']+)["\'][^>]*'
        r'>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(page):

        begin = html.unescape(
            match.group(1)
        ).strip()

        end = html.unescape(
            match.group(2)
        ).strip()

        text = re.sub(
            r"<[^>]+>",
            "",
            match.group(3),
        )

        text = html.unescape(text)

        text = re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

        results.append(
            {
                "text": text,
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


# ============================================================
# TARİH PARSE
# ============================================================

def parse_tivibu_date(value):

    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
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


# ============================================================
# 7 GÜNÜ BELİRLE
# ============================================================

def build_target_dates(page):

    today = datetime.now().date()

    page_dates = extract_date_options(page)

    target = []

    for item in page_dates:

        dt = parse_tivibu_date(
            item["begin"]
        )

        if not dt:
            continue

        day = dt.date()

        if day < today:
            continue

        target.append(
            {
                "date": day,
                "begin": item["begin"],
                "end": item["end"],
            }
        )

    unique = {}

    for item in target:
        unique[item["date"]] = item

    target = [
        unique[d]
        for d in sorted(unique)
    ]

    if len(target) < DAYS:

        existing_dates = {
            item["date"]
            for item in target
        }

        for offset in range(DAYS):

            day = today + timedelta(
                days=offset
            )

            if day in existing_dates:
                continue

            target.append(
                {
                    "date": day,
                    "begin": day.strftime(
                        "%Y.%m.%d 00:00:00"
                    ),
                    "end": day.strftime(
                        "%Y.%m.%d 23:59:59"
                    ),
                }
            )

        target.sort(
            key=lambda x: x["date"]
        )

    return target[:DAYS]


# ============================================================
# CHANNEL COLUMN CODE
# ============================================================

def extract_channel_column_code(page):

    patterns = [
        r'channelColumnCode\s*=\s*["\']([^"\']*)["\']',

        r'channelColumnCode\s*:\s*["\']([^"\']*)["\']',

        r'channelColumnCode["\']?\s*,\s*["\']([^"\']+)["\']',

        r'"channelColumnCode"\s*:\s*"([^"]*)"',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            page,
            re.IGNORECASE,
        )

        for value in matches:

            value = html.unescape(
                value
            ).strip()

            if value:
                return value

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
            re.IGNORECASE,
        )

        for value in matches:

            value = html.unescape(
                value
            ).strip()

            if value:
                return value

    return ""


# ============================================================
# GET MULTI PREVUE DATA
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
        "channelColumnCode": channel_column_code,
        "channelDateBegin": begin_date,
        "channelDateEnd": end_date,
        "channelSearchValue": channel_search_value,
        "pageNo": str(page_no),
    }

    body = urllib.parse.urlencode(
        form
    ).encode("utf-8")

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

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "GetMultiPrevueData JSON döndürmedi.\n"
            f"İlk 1000 karakter:\n"
            f"{response[:1000]}"
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
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ============================================================
# JSON'DAN İLK DOLU ALAN
# ============================================================

def first_nonempty(item, keys):

    if not isinstance(item, dict):
        return ""

    for key in keys:

        value = item.get(key)

        if value is None:
            continue

        value = clean_text(value)

        if value:
            return value

    return ""


# ============================================================
# KANAL İSMİ NORMALİZE
# ============================================================

def normalize_channel_name(name):

    name = clean_text(name)

    if not name:
        return ""

    name = name.upper()

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    return name


# ============================================================
# GERÇEK KANAL KONTROLÜ
# ============================================================

def is_real_channel_name(name):

    name = normalize_channel_name(name)

    if not name:
        return False

    if name in BLOCKED_CHANNEL_NAMES:
        return False

    # Saat içeren şeyler kesinlikle programdır.
    if re.search(
        r"\b\d{1,2}:\d{2}\b",
        name,
    ):
        return False

    # Ok işareti içerenler programdır.
    if "→" in name:
        return False

    if " - " in name:
        # Bazı gerçek kanallarda tire olabilir;
        # ancak program isimlerinde çok yaygın olduğundan
        # beyaz liste dışında kabul etmiyoruz.
        if name not in KNOWN_CHANNELS:
            return False

    if name in KNOWN_CHANNELS:
        return True

    return False


# ============================================================
# PROGRAM BAŞLIĞI KONTROL
# ============================================================

def is_valid_program_title(title):

    title = clean_text(title)

    if not title:
        return False

    if len(title) < 2:
        return False

    # Navigasyon metinleri
    blocked = {
        "DÜN",
        "BUGÜN",
        "YARIN",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
    }

    if title.upper() in blocked:
        return False

    return True


# ============================================================
# KANALLARI NORMALİZE ET
# ============================================================

def normalize_channels(data):

    channels = {}

    if not isinstance(data, dict):
        return channels

    items = (
        data.get("channelListViewModel")
        or []
    )

    if isinstance(items, dict):
        items = list(
            items.values()
        )

    for item in items:

        if not isinstance(item, dict):
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

        if not code or not name:
            continue

        normalized_name = normalize_channel_name(
            name
        )

        # EN ÖNEMLİ FİLTRE:
        # Program adı burada kanal olamaz.
        if not is_real_channel_name(
            normalized_name
        ):
            continue

        icon = first_nonempty(
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
            "icon": icon,
        }

    return channels


# ============================================================
# PROGRAMLARI NORMALİZE ET
# ============================================================

def normalize_programs(data):

    if not isinstance(data, dict):
        return []

    programs = (
        data.get("prevueListViewModel")
        or []
    )

    if not programs:

        programs = (
            data.get("mobilPrevueViewModel")
            or []
        )

    if isinstance(programs, dict):

        # Bazı Tivibu cevaplarında liste
        # farklı bir alanın altında olabilir.
        possible_lists = []

        for value in programs.values():

            if isinstance(value, list):
                possible_lists.extend(
                    value
                )

        if possible_lists:
            programs = possible_lists
        else:
            programs = []

    if not isinstance(programs, list):
        return []

    result = []

    for program in programs:

        if not isinstance(program, dict):
            continue

        title = first_nonempty(
            program,
            [
                "prevueName",
                "programName",
                "title",
                "name",
            ],
        )

        if not is_valid_program_title(
            title
        ):
            continue

        result.append(
            program
        )

    return result


# ============================================================
# TARİH PARSE
# ============================================================

def parse_datetime(value):

    if not value:
        return None

    value = clean_text(value)

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

    # ISO tarih denemesi
    try:

        value2 = value.replace(
            "Z",
            "+00:00",
        )

        return datetime.fromisoformat(
            value2
        ).replace(
            tzinfo=None
        )

    except Exception:
        return None


# ============================================================
# PROGRAM ANAHTARI
# ============================================================

def programme_key(program):

    code = first_nonempty(
        program,
        [
            "channelCode",
            "channelId",
        ],
    )

    begin = first_nonempty(
        program,
        [
            "beginTime",
            "startTime",
            "start",
        ],
    )

    end = first_nonempty(
        program,
        [
            "endTime",
            "stopTime",
            "end",
        ],
    )

    title = first_nonempty(
        program,
        [
            "prevueName",
            "programName",
            "title",
            "name",
        ],
    )

    return (
        code,
        begin,
        end,
        title,
    )


# ============================================================
# XML TEXT
# ============================================================

def add_text(
    parent,
    tag,
    text,
    lang=None,
):

    if lang:

        element = ET.SubElement(
            parent,
            tag,
            {
                "lang": lang
            },
        )

    else:

        element = ET.SubElement(
            parent,
            tag,
        )

    element.text = clean_text(
        text
    )

    return element


# ============================================================
# XMLTV OLUŞTUR
# ============================================================

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

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    valid_channels = {}

    for code, info in channels.items():

        name = clean_text(
            info.get("name", "")
        )

        if not is_real_channel_name(
            name
        ):
            continue

        valid_channels[code] = info

    for code in sorted(
        valid_channels.keys(),
        key=lambda x:
            valid_channels[x]["name"].lower(),
    ):

        info = valid_channels[code]

        channel_element = ET.SubElement(
            tv,
            "channel",
            {
                "id": str(code),
            },
        )

        add_text(
            channel_element,
            "display-name",
            info["name"],
            "tr",
        )

        icon = clean_text(
            info.get("icon", "")
        )

        if (
            icon
            and icon.startswith("http")
        ):

            ET.SubElement(
                channel_element,
                "icon",
                {
                    "src": icon,
                },
            )

    # --------------------------------------------------------
    # PROGRAMLAR
    # --------------------------------------------------------

    count = 0

    valid_programs = []

    for program in programs:

        channel_code = first_nonempty(
            program,
            [
                "channelCode",
                "channelId",
            ],
        )

        if not channel_code:
            continue

        # Kanal XML'de yoksa programı yazma.
        if channel_code not in valid_channels:
            continue

        begin_value = first_nonempty(
            program,
            [
                "beginTime",
                "startTime",
                "start",
            ],
        )

        end_value = first_nonempty(
            program,
            [
                "endTime",
                "stopTime",
                "end",
            ],
        )

        begin_dt = parse_datetime(
            begin_value
        )

        end_dt = parse_datetime(
            end_value
        )

        if not begin_dt or not end_dt:
            continue

        if end_dt <= begin_dt:
            continue

        title = first_nonempty(
            program,
            [
                "prevueName",
                "programName",
                "title",
                "name",
            ],
        )

        if not is_valid_program_title(
            title
        ):
            continue

        description = first_nonempty(
            program,
            [
                "description",
                "desc",
                "summary",
            ],
        )

        genre = first_nonempty(
            program,
            [
                "genre",
                "category",
            ],
        )

        rating = first_nonempty(
            program,
            [
                "ratingId",
                "rating",
            ],
        )

        valid_programs.append(
            (
                begin_dt,
                end_dt,
                channel_code,
                title,
                description,
                genre,
                rating,
            )
        )

    # Tarih + kanal sıralaması
    valid_programs.sort(
        key=lambda x: (
            x[0],
            str(x[2]),
        )
    )

    seen_programs = set()

    for (
        begin_dt,
        end_dt,
        channel_code,
        title,
        description,
        genre,
        rating,
    ) in valid_programs:

        key = (
            channel_code,
            begin_dt,
            end_dt,
            title,
        )

        if key in seen_programs:
            continue

        seen_programs.add(
            key
        )

        start_attr = (
            begin_dt.strftime(
                "%Y%m%d%H%M%S"
            )
            + " "
            + TIMEZONE
        )

        stop_attr = (
            end_dt.strftime(
                "%Y%m%d%H%M%S"
            )
            + " "
            + TIMEZONE
        )

        programme_element = ET.SubElement(
            tv,
            "programme",
            {
                "start": start_attr,
                "stop": stop_attr,
                "channel": str(
                    channel_code
                ),
            },
        )

        add_text(
            programme_element,
            "title",
            title,
            "tr",
        )

        if description:

            add_text(
                programme_element,
                "desc",
                description,
                "tr",
            )

        if genre:

            add_text(
                programme_element,
                "category",
                genre,
                "tr",
            )

        if rating:

            add_text(
                programme_element,
                "rating",
                rating,
            )

        count += 1

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
# XML KONTROLÜ
# ============================================================

def validate_xml():

    print()
    print("[KONTROL] XML kontrol ediliyor...")

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

    invalid_channels = []

    for channel in channels:

        display = channel.find(
            "display-name"
        )

        if display is None:
            continue

        name = clean_text(
            display.text
        )

        if not is_real_channel_name(
            name
        ):
            invalid_channels.append(
                name
            )

    invalid_programmes = []

    for programme in programmes:

        channel_id = programme.get(
            "channel"
        )

        title_element = programme.find(
            "title"
        )

        if channel_id not in channel_ids:

            invalid_programmes.append(
                f"Kanal yok: {channel_id}"
            )

        if (
            title_element is None
            or not clean_text(
                title_element.text
            )
        ):

            invalid_programmes.append(
                "Başlıksız program"
            )

    print(
        f"      Kanal sayısı   : {len(channels)}"
    )

    print(
        f"      Program sayısı : {len(programmes)}"
    )

    print(
        f"      Hatalı kanal   : {len(invalid_channels)}"
    )

    print(
        f"      Hatalı program : {len(invalid_programmes)}"
    )

    if invalid_channels:

        print()
        print(
            "HATALI KANALLAR:"
        )

        for name in invalid_channels[:30]:
            print(
                f"  - {name}"
            )

    if invalid_channels:
        raise RuntimeError(
            "XML içinde gerçek kanal olmayan kayıt bulundu."
        )

    if invalid_programmes:
        raise RuntimeError(
            "XML içinde geçersiz program bulundu."
        )

    print(
        "      XML kontrolü BAŞARILI."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    start = time.time()

    print()
    print("=" * 70)
    print(
        "TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU"
    )
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # 1 - SAYFA
    # --------------------------------------------------------

    page = get_main_page()

    print(
        f"      Sayfa alındı: {len(page):,} karakter"
    )

    # --------------------------------------------------------
    # 2 - CSRF
    # --------------------------------------------------------

    print()
    print(
        "[2/5] CSRF token aranıyor..."
    )

    csrf_token = extract_csrf_token(
        page
    )

    print(
        f"      Token bulundu. Uzunluk: "
        f"{len(csrf_token)}"
    )

    # --------------------------------------------------------
    # BAŞLANGIÇ DEĞERLERİ
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

    print()
    print(
        f"channelColumnCode  : "
        f"{channel_column_code}"
    )

    print(
        f"channelSearchValue : "
        f"{channel_search_value or '(boş)'}"
    )

    # --------------------------------------------------------
    # 3 - TARİHLER
    # --------------------------------------------------------

    target_dates = build_target_dates(
        page
    )

    if not target_dates:

        raise RuntimeError(
            "Hiç tarih bulunamadı."
        )

    print()
    print(
        f"[3/5] Hedef tarih sayısı: "
        f"{len(target_dates)}"
    )

    for index, item in enumerate(
        target_dates,
        start=1,
    ):

        print(
            f"      {index}. "
            f"{item['date'].strftime('%d.%m.%Y')}"
        )

    # --------------------------------------------------------
    # 4 - EPG
    # --------------------------------------------------------

    print()
    print(
        "[4/5] Tivibu EPG verileri çekiliyor..."
    )
    print()

    channels = {}

    all_programs = {}

    successful_days = 0
    failed_days = 0

    for index, date_info in enumerate(
        target_dates,
        start=1,
    ):

        print(
            f"--- GÜN {index}/{len(target_dates)} ---"
        )

        print(
            f"Tarih: "
            f"{date_info['date'].strftime('%d.%m.%Y')}"
        )

        try:

            data = post_multi_prevue(
                csrf_token=csrf_token,
                channel_column_code=channel_column_code,
                channel_search_value=channel_search_value,
                begin_date=date_info["begin"],
                end_date=date_info["end"],
                page_no=1,
            )

            # ------------------------------------------------
            # KANALLAR
            # ------------------------------------------------

            day_channels = normalize_channels(
                data
            )

            for code, info in day_channels.items():

                if code not in channels:

                    channels[code] = info

                else:

                    if (
                        not channels[code].get("icon")
                        and info.get("icon")
                    ):

                        channels[code]["icon"] = (
                            info["icon"]
                        )

            # ------------------------------------------------
            # PROGRAMLAR
            # ------------------------------------------------

            day_programs = normalize_programs(
                data
            )

            new_count = 0

            for program in day_programs:

                key = programme_key(
                    program
                )

                if key in all_programs:
                    continue

                all_programs[key] = program

                new_count += 1

            print(
                f"      Gerçek kanal : "
                f"{len(day_channels)}"
            )

            print(
                f"      Program       : "
                f"{len(day_programs)}"
            )

            print(
                f"      Yeni program  : "
                f"{new_count}"
            )

            successful_days += 1

        except Exception as exc:

            failed_days += 1

            print(
                f"      HATA: {exc}"
            )

        print()

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # PROGRAMLARDAN KANAL ÜRETME YOK
    # --------------------------------------------------------
    #
    # BURASI ÖNEMLİ:
    #
    # Eski sürümde programın channelCode'u bulunamadığında
    # veya kanal listesi boş geldiğinde programdan kanal
    # oluşturulabiliyordu.
    #
    # BU SÜRÜMDE BU YAPILMIYOR.
    #
    # Böylece:
    #
    # Nereden Nereye
    # Count Me In
    # Sefiller
    # Ölü Mevsim
    #
    # gibi program isimleri kanal olamaz.
    # --------------------------------------------------------

    sorted_programs = sorted(
        all_programs.values(),
        key=lambda p: (
            first_nonempty(
                p,
                [
                    "channelCode",
                    "channelId",
                ],
            ),
            first_nonempty(
                p,
                [
                    "beginTime",
                    "startTime",
                    "start",
                ],
            ),
        ),
    )

    # --------------------------------------------------------
    # KANAL SAYISINI KONTROL
    # --------------------------------------------------------

    if not channels:

        raise RuntimeError(
            "Tivibu'dan hiç gerçek kanal alınamadı. "
            "Yanlış XML oluşturulmaması için işlem durduruldu."
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    xml_program_count = create_xmltv(
        channels=channels,
        programs=sorted_programs,
    )

    # --------------------------------------------------------
    # XML DOĞRULAMA
    # --------------------------------------------------------

    validate_xml()

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    elapsed = time.time() - start

    print()
    print("=" * 70)
    print(
        "TAMAMLANDI"
    )
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
        f"Gerçek kanal sayısı   : "
        f"{len(channels)}"
    )

    print(
        f"Toplanan program      : "
        f"{len(sorted_programs)}"
    )

    print(
        f"XML program sayısı    : "
        f"{xml_program_count}"
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
    print()


if __name__ == "__main__":
    main()
