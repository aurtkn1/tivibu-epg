#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
MULTI_PREVUE_URL = f"{BASE_URL}/Channel/GetMultiPrevueData"

OUTPUT_FILE = "epg.xml"

# Kaç günlük EPG üretilecek?
DAYS = 7

# İstekler arasında bekleme.
REQUEST_DELAY = 0.50

TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------

def http_request(url, data=None, headers=None):
    """
    GET veya POST HTTP isteği gönderir.
    Cookie'leri aynı oturumda tutmak için CookieJar kullanılır.
    """

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
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()

            charset = (
                response.headers.get_content_charset()
                or "utf-8"
            )

            return raw.decode(
                charset,
                errors="replace",
            )

    except Exception as exc:
        raise RuntimeError(
            f"HTTP isteği başarısız:\n"
            f"URL: {url}\n"
            f"Hata: {exc}"
        ) from exc


# ---------------------------------------------------------------------
# Tivibu ana sayfa
# ---------------------------------------------------------------------

def get_main_page():
    print("[1/5] Tivibu canlı TV sayfası alınıyor...")

    return http_request(
        LIVE_TV_URL,
        headers={
            "Referer": BASE_URL + "/",
        },
    )


# ---------------------------------------------------------------------
# CSRF TOKEN
# ---------------------------------------------------------------------

def extract_csrf_token(page):
    """
    Tivibu sayfasındaki CSRF token'ı bulur.
    """

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
        "CSRF token bulunamadı.\n"
        "Tivibu HTML yapısını değiştirmiş olabilir."
    )


# ---------------------------------------------------------------------
# Tarihler
# ---------------------------------------------------------------------

def extract_date_options(page):
    """
    Sayfadaki channelsDates tarihlerini çıkarır.

    Örnek HTML:

    <a
        class="active"
        channeldatebegin="2026.09.03 00:00:00"
        channeldateend="2026.09.03 23:59:59">
        Bugün
    </a>
    """

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

        text = html.unescape(
            re.sub(
                r"<[^>]+>",
                "",
                match.group(3),
            )
        )

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

    # Aynı tarih tekrar bulunursa sil.
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


def parse_tivibu_date(value):
    """
    Tivibu tarih formatı:

    2026.09.03 00:00:00
    """

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]

    value = value.strip()

    for fmt in formats:

        try:
            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:
            pass

    return None


def build_target_dates(page):
    """
    Sayfadaki tarih listesinden sonraki 7 günü seçer.

    Öncelik:
    1. Bugün / ileri tarihler
    2. Sayfada bulunan gerçek tarih aralıkları

    Sayfa tarihleri yetmezse otomatik tarih üretir.
    """

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

    # Tekilleştir
    unique = {}

    for item in target:
        unique[item["date"]] = item

    target = [
        unique[d]
        for d in sorted(unique)
    ]

    # Sayfada 7 gün yoksa üret.
    if len(target) < DAYS:

        print(
            "      Sayfadaki tarih sayısı 7 günden az. "
            "Eksik günler otomatik oluşturulacak."
        )

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


# ---------------------------------------------------------------------
# channelColumnCode / search
# ---------------------------------------------------------------------

def extract_channel_column_code(page):
    """
    Tivibu'nun channelColumnCode değerini HTML/JS içinden yakalamaya çalışır.

    Bulunamazsa boş string döndürür.

    GetMultiPrevueData bazı sayfalarda boş değerle de çalışabilir.
    """

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

    return ""


def extract_channel_search_value(page):
    """
    channelSearchValue için başlangıç değeri.

    Tivibu sayfasında sabit olarak tanımlıysa yakalar.
    Bulamazsa boş string kullanılır.
    """

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


# ---------------------------------------------------------------------
# GetMultiPrevueData
# ---------------------------------------------------------------------

def post_multi_prevue(
    csrf_token,
    channel_column_code,
    channel_search_value,
    begin_date,
    end_date,
    page_no=1,
):
    """
    Tivibu'nun ana çoklu EPG endpoint'i.
    """

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


# ---------------------------------------------------------------------
# Yardımcı alanlar
# ---------------------------------------------------------------------

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


def first_nonempty(item, keys):
    for key in keys:

        value = item.get(key)

        if value is None:
            continue

        value = clean_text(value)

        if value:
            return value

    return ""


# ---------------------------------------------------------------------
# Programları normalize et
# ---------------------------------------------------------------------

def normalize_channels(data):
    """
    channelListViewModel'den kanal sözlüğü çıkarır.
    """

    channels = {}

    items = data.get(
        "channelListViewModel"
    ) or []

    for item in items:

        code = first_nonempty(
            item,
            [
                "channelCode",
                "code",
            ],
        )

        if not code:
            continue

        name = first_nonempty(
            item,
            [
                "channelName",
                "name",
                "displayName",
            ],
        )

        if not name:
            name = code

        icon = first_nonempty(
            item,
            [
                "channelImage",
                "image",
                "channelIcon",
            ],
        )

        channels[code] = {
            "name": name,
            "icon": icon,
        }

    return channels


def normalize_programs(data):
    """
    prevueListViewModel içindeki programları çıkarır.
    """

    programs = data.get(
        "prevueListViewModel"
    ) or []

    # Bazı response'larda farklı alan gelebilir.
    if not programs:

        programs = data.get(
            "mobilPrevueViewModel"
        ) or []

    return programs


# ---------------------------------------------------------------------
# Tarih parse
# ---------------------------------------------------------------------

def parse_datetime(value):
    if not value:
        return None

    value = clean_text(value)

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
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


# ---------------------------------------------------------------------
# XMLTV
# ---------------------------------------------------------------------

def add_text(parent, tag, text, lang=None):

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


def programme_key(program):
    """
    Aynı programın birden fazla tarih çağrısında
    tekrar yazılmasını engeller.
    """

    code = first_nonempty(
        program,
        [
            "channelCode",
        ],
    )

    begin = first_nonempty(
        program,
        [
            "beginTime",
        ],
    )

    end = first_nonempty(
        program,
        [
            "endTime",
        ],
    )

    title = first_nonempty(
        program,
        [
            "prevueName",
            "name",
        ],
    )

    return (
        code,
        begin,
        end,
        title,
    )


def create_xmltv(
    channels,
    programs,
):
    print("[5/5] XMLTV oluşturuluyor...")

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
    # Channels
    # -------------------------------------------------------------

    for code in sorted(
        channels.keys(),
        key=lambda x: channels[x]["name"].lower(),
    ):

        info = channels[code]

        channel_element = ET.SubElement(
            tv,
            "channel",
            {
                "id": code,
            },
        )

        add_text(
            channel_element,
            "display-name",
            info["name"],
            "tr",
        )

        icon = info.get("icon")

        if icon and icon.startswith(
            "http"
        ):

            ET.SubElement(
                channel_element,
                "icon",
                {
                    "src": icon,
                },
            )

    # -------------------------------------------------------------
    # Programlar
    # -------------------------------------------------------------

    count = 0

    for program in programs:

        channel_code = first_nonempty(
            program,
            [
                "channelCode",
            ],
        )

        if not channel_code:
            continue

        begin_value = first_nonempty(
            program,
            [
                "beginTime",
            ],
        )

        end_value = first_nonempty(
            program,
            [
                "endTime",
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

        title = first_nonempty(
            program,
            [
                "prevueName",
                "name",
            ],
        )

        if not title:
            title = "Bilinmeyen Program"

        description = first_nonempty(
            program,
            [
                "description",
                "desc",
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
            ],
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
                "channel": channel_code,
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


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main():

    start = time.time()

    print()
    print("=" * 70)
    print("TİVİBU 7 GÜNLÜK EPG OLUŞTURUCU")
    print("=" * 70)
    print()

    # -------------------------------------------------------------
    # 1) Sayfa
    # -------------------------------------------------------------

    page = get_main_page()

    print()

    # -------------------------------------------------------------
    # 2) CSRF
    # -------------------------------------------------------------

    print("[2/5] CSRF token aranıyor...")

    csrf_token = extract_csrf_token(
        page
    )

    print(
        f"      Token bulundu. "
        f"Uzunluk: {len(csrf_token)}"
    )

    # -------------------------------------------------------------
    # 3) Başlangıç değişkenleri
    # -------------------------------------------------------------

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
        f"channelColumnCode   : "
        f"{channel_column_code or '(boş)'}"
    )

    print(
        f"channelSearchValue  : "
        f"{channel_search_value or '(boş)'}"
    )

    # -------------------------------------------------------------
    # 4) Tarihler
    # -------------------------------------------------------------

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
            f"{item['date'].strftime('%d.%m.%Y')} "
            f"-> "
            f"{item['begin']} / {item['end']}"
        )

    # -------------------------------------------------------------
    # 5) EPG verisini çek
    # -------------------------------------------------------------

    print()
    print(
        "[4/5] GetMultiPrevueData çağrıları başlıyor..."
    )
    print()

    channels = {}

    all_programs = {}

    total_programs = 0

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

            # -----------------------------------------------------
            # Kanal bilgileri
            # -----------------------------------------------------

            day_channels = normalize_channels(
                data
            )

            for code, info in day_channels.items():

                if code not in channels:

                    channels[code] = info

                else:

                    # İlk isim boş/garipse yenisini kullan.
                    old_name = channels[code]["name"]

                    if (
                        not old_name
                        or old_name == code
                    ):
                        channels[code]["name"] = (
                            info["name"]
                        )

                    if (
                        not channels[code].get("icon")
                        and info.get("icon")
                    ):
                        channels[code]["icon"] = (
                            info["icon"]
                        )

            # -----------------------------------------------------
            # Programlar
            # -----------------------------------------------------

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

            total_programs += new_count

            print(
                f"      Kanal: "
                f"{len(day_channels)}"
            )

            print(
                f"      Program: "
                f"{len(day_programs)}"
            )

            print(
                f"      Yeni program: "
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

    # -------------------------------------------------------------
    # Eğer channelListViewModel boş ama programlarda kanal kodları
    # varsa yine de XML için kanal oluştur.
    # -------------------------------------------------------------

    for program in all_programs.values():

        code = first_nonempty(
            program,
            [
                "channelCode",
            ],
        )

        if not code:
            continue

        if code not in channels:

            channel_name = first_nonempty(
                program,
                [
                    "channelName",
                ],
            )

            if not channel_name:
                channel_name = code

            channels[code] = {
                "name": channel_name,
                "icon": "",
            }

    # -------------------------------------------------------------
    # Programları tarihe göre sırala
    # -------------------------------------------------------------

    sorted_programs = sorted(
        all_programs.values(),
        key=lambda p: (
            p.get(
                "channelCode",
                ""
            ),
            p.get(
                "beginTime",
                ""
            ),
        ),
    )

    # -------------------------------------------------------------
    # XML oluştur
    # -------------------------------------------------------------

    xml_program_count = create_xmltv(
        channels=channels,
        programs=sorted_programs,
    )

    # -------------------------------------------------------------
    # Sonuç
    # -------------------------------------------------------------

    elapsed = time.time() - start

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


if __name__ == "__main__":
    main()
