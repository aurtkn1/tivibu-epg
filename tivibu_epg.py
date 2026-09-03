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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

CHANNEL_COLUMN_CODE = "020002"


# ============================================================
# COOKIE
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
# GENEL YARDIMCILAR
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
    }

    for old, new in replacements.items():
        name = name.replace(old, new)

    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")

    return name[:100] or "channel"


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value[:26], fmt)
        except ValueError:
            pass

    return None


def parse_time(value):
    if value is None:
        return None

    text = str(value).strip()

    match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def make_datetime(date_obj, time_value):
    parsed = parse_time(time_value)

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
    return dt.strftime("%Y%m%d%H%M%S +0300")


# ============================================================
# HTML PARSER
# ============================================================

class TivibuHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.anchors = []

        self.current_href = None
        self.current_text = []
        self.in_anchor = False

    def handle_starttag(self, tag, attrs):

        if tag.lower() == "a":

            attrs_dict = dict(attrs)

            self.current_href = attrs_dict.get("href", "")
            self.current_text = []
            self.in_anchor = True

    def handle_data(self, data):

        if self.in_anchor:
            self.current_text.append(data)

    def handle_endtag(self, tag):

        if tag.lower() == "a" and self.in_anchor:

            text = clean_text(" ".join(self.current_text))

            self.anchors.append({
                "href": self.current_href or "",
                "text": text
            })

            self.current_href = None
            self.current_text = []
            self.in_anchor = False


# ============================================================
# PROGRAM METNİ PARSE
# ============================================================

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2})"
    r"\s*(?:→|->|–|—|-)"
    r"\s*"
    r"(?P<stop>\d{1,2}:\d{2})"
)


def parse_program_text(text):

    text = clean_text(text)

    match = TIME_RANGE_RE.search(text)

    if not match:
        return None

    start = match.group("start")
    stop = match.group("stop")

    before = text[:match.start()].strip()

    # Sondaki kategori ayırıcılarını temizle.
    before = re.sub(r"\s+-\s*$", "", before).strip()

    if not before:
        return None

    # "Canlı" ve benzeri ifadeleri temizle.
    before = re.sub(
        r"\s+Canlı\s*$",
        "",
        before,
        flags=re.IGNORECASE
    ).strip()

    # Bazı Tivibu kayıtlarında başlık + kategori bulunuyor.
    title = before
    category = ""

    # Son bölüm yaygın kategori ise ayır.
    category_match = re.search(
        r"\s+(Film|Dizi|Spor|Haber|Belgesel|Çocuk|Müzik|"
        r"Yaşam|Yaşam-Stil|Sinema|Ulusal|Global|Diğer)\s*$",
        before,
        flags=re.IGNORECASE
    )

    if category_match:

        category = category_match.group(1).strip()

        title = before[:category_match.start()].strip()

    if not title:
        title = before

    return {
        "title": title,
        "category": category,
        "start": start,
        "stop": stop
    }


# ============================================================
# HTML KANAL + PROGRAMLAR
# ============================================================

def parse_html(page, target_date):

    parser = TivibuHTMLParser()

    try:
        parser.feed(page)
    except Exception as exc:
        print("HTML parser hatası:", exc)
        return [], []

    anchors = parser.anchors

    channels_by_href = OrderedDict()

    programs = []

    # --------------------------------------------------------
    # 1. GERÇEK KANAL LİNKLERİNİ BUL
    # --------------------------------------------------------

    for item in anchors:

        href = item["href"]
        text = clean_text(item["text"])

        if not href or not text:
            continue

        # Tarih sekmeleri kesinlikle kanal değildir.
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
            continue

        if text.lower() in {
            "dün",
            "bugün",
            "yarın",
            "önceki",
            "sonraki",
            "tümü",
        }:
            continue

        # Programlarda saat aralığı bulunur.
        if TIME_RANGE_RE.search(text):
            continue

        # Kanal URL'si olması bekleniyor.
        href_lower = href.lower()

        if (
            "/kanal" not in href_lower
            and "/channel" not in href_lower
        ):
            continue

        # Çok uzun metinleri kanal kabul etme.
        if len(text) > 100:
            continue

        # Menü / genel site bağlantılarını ele.
        blocked = {
            "canlı tv",
            "ana sayfa",
            "programlar",
            "paketler",
            "kampanyalar",
            "giriş yap",
            "üye ol",
            "arama",
        }

        if text.lower() in blocked:
            continue

        if href not in channels_by_href:

            channels_by_href[href] = {
                "id": normalize_channel_id(text),
                "name": text
            }

    # --------------------------------------------------------
    # 2. PROGRAMLARI BUL
    # --------------------------------------------------------

    for item in anchors:

        href = item["href"]
        text = clean_text(item["text"])

        if not href or not text:
            continue

        parsed = parse_program_text(text)

        if parsed is None:
            continue

        # Programın bağlı olduğu kanal anchor'dan bulunuyor.
        channel = channels_by_href.get(href)

        if channel is None:
            continue

        start_dt = make_datetime(
            target_date,
            parsed["start"]
        )

        stop_dt = make_datetime(
            target_date,
            parsed["stop"]
        )

        if start_dt is None or stop_dt is None:
            continue

        # Gece yarısını geçiyorsa.
        if stop_dt <= start_dt:
            stop_dt += timedelta(days=1)

        programs.append({
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "title": parsed["title"],
            "category": parsed["category"],
            "start": start_dt,
            "stop": stop_dt
        })

    # --------------------------------------------------------
    # DUPLICATE TEMİZLE
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
        unique_programs.append(program)

    channels = list(channels_by_href.values())

    return channels, unique_programs


# ============================================================
# HTTP GET
# ============================================================

def http_get(url, headers=None):

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

    if headers:

        for key, value in headers.items():
            request.add_header(key, value)

    with OPENER.open(request, timeout=30) as response:

        data = response.read()

        charset = response.headers.get_content_charset()

        if not charset:
            charset = "utf-8"

        return (
            response.status,
            response.headers.get("Content-Type", ""),
            data.decode(charset, errors="replace")
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
            return html.unescape(match.group(1))

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

    base_data = {
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

    if mode == "form":

        data = dict(base_data)

        if csrf_token:
            data["CSRF-TOKEN-TVBUDNBX!-FORM"] = csrf_token

        encoded = urllib.parse.urlencode(
            data
        ).encode("utf-8")

        request = urllib.request.Request(
            API_URL,
            data=encoded,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/x-www-form-urlencoded; charset=UTF-8"
        )

    elif mode == "json":

        data = dict(base_data)

        payload = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        request = urllib.request.Request(
            API_URL,
            data=payload,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/json; charset=UTF-8"
        )

    elif mode == "token-form":

        data = dict(base_data)

        data["__RequestVerificationToken"] = csrf_token or ""

        if csrf_token:
            data["CSRF-TOKEN-TVBUDNBX!-FORM"] = csrf_token

        encoded = urllib.parse.urlencode(
            data
        ).encode("utf-8")

        request = urllib.request.Request(
            API_URL,
            data=encoded,
            method="POST"
        )

        request.add_header(
            "Content-Type",
            "application/x-www-form-urlencoded; charset=UTF-8"
        )

    elif mode == "get":

        query = urllib.parse.urlencode(
            base_data
        )

        request = urllib.request.Request(
            API_URL + "?" + query,
            method="GET"
        )

    else:
        return None

    for key, value in headers.items():
        request.add_header(key, value)

    if csrf_token:

        request.add_header(
            "X-CSRF-TOKEN",
            csrf_token
        )

    try:

        with OPENER.open(
            request,
            timeout=30
        ) as response:

            raw = response.read()

            charset = response.headers.get_content_charset()

            if not charset:
                charset = "utf-8"

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
                clean_text(body)[:500]
            )

        return {
            "status": exc.code,
            "content_type": "",
            "text": body
        }

    except Exception as exc:

        print(
            f"      API {mode}: {exc}"
        )

        return None


# ============================================================
# JSON İÇİNDE PROGRAMLARI RECURSIVE BUL
# ============================================================

def normalize_key(key):

    return re.sub(
        r"[^a-z0-9]",
        "",
        str(key).lower()
    )


def find_value(obj, possible_keys):

    normalized_targets = {
        normalize_key(x)
        for x in possible_keys
    }

    if isinstance(obj, dict):

        for key, value in obj.items():

            if normalize_key(key) in normalized_targets:

                if value is not None:
                    return value

        for value in obj.values():

            result = find_value(
                value,
                possible_keys
            )

            if result is not None:
                return result

    elif isinstance(obj, list):

        for value in obj:

            result = find_value(
                value,
                possible_keys
            )

            if result is not None:
                return result

    return None


def extract_json_records(obj):

    records = []

    def walk(value):

        if isinstance(value, dict):

            keys = {
                normalize_key(k): k
                for k in value.keys()
            }

            title_key = None
            start_key = None
            stop_key = None
            channel_key = None

            title_candidates = [
                "title",
                "programName",
                "programmeName",
                "programTitle",
                "prevueName",
                "name",
                "eventName",
            ]

            start_candidates = [
                "start",
                "startTime",
                "begin",
                "beginTime",
                "dateBegin",
                "startDate",
                "programmeStart",
            ]

            stop_candidates = [
                "end",
                "endTime",
                "stop",
                "stopTime",
                "dateEnd",
                "endDate",
                "programmeEnd",
            ]

            channel_candidates = [
                "channelName",
                "channelTitle",
                "channel",
                "channelDisplayName",
                "displayName",
            ]

            for candidate in title_candidates:

                normalized = normalize_key(candidate)

                if normalized in keys:
                    title_key = keys[normalized]
                    break

            for candidate in start_candidates:

                normalized = normalize_key(candidate)

                if normalized in keys:
                    start_key = keys[normalized]
                    break

            for candidate in stop_candidates:

                normalized = normalize_key(candidate)

                if normalized in keys:
                    stop_key = keys[normalized]
                    break

            for candidate in channel_candidates:

                normalized = normalize_key(candidate)

                if normalized in keys:
                    channel_key = keys[normalized]
                    break

            if (
                title_key
                and start_key
                and stop_key
            ):

                title = clean_text(
                    value.get(title_key)
                )

                start = value.get(start_key)
                stop = value.get(stop_key)

                channel = ""

                if channel_key:
                    channel = clean_text(
                        value.get(channel_key)
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

        elif isinstance(value, list):

            for child in value:
                walk(child)

    walk(obj)

    return records


# ============================================================
# API JSON PARSE
# ============================================================

def parse_api_response(text, target_date):

    try:
        data = json.loads(text)

    except Exception:

        return []

    records = extract_json_records(data)

    result = []

    for record in records:

        start_dt = None
        stop_dt = None

        # Tam tarih formatı varsa.
        parsed_start_date = parse_date(
            record["start"]
        )

        parsed_stop_date = parse_date(
            record["stop"]
        )

        if parsed_start_date:
            start_dt = parsed_start_date
        else:
            start_dt = make_datetime(
                target_date,
                record["start"]
            )

        if parsed_stop_date:
            stop_dt = parsed_stop_date
        else:
            stop_dt = make_datetime(
                target_date,
                record["stop"]
            )

        if not start_dt or not stop_dt:
            continue

        if stop_dt <= start_dt:
            stop_dt += timedelta(days=1)

        result.append({
            "channel_name": record["channel"] or "Bilinmeyen Kanal",
            "channel_id": normalize_channel_id(
                record["channel"] or "Bilinmeyen Kanal"
            ),
            "title": record["title"],
            "category": "",
            "start": start_dt,
            "stop": stop_dt
        })

    # duplicate
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
        unique.append(item)

    return unique


# ============================================================
# GÜN İŞLE
# ============================================================

def process_day(target_date, first_page=None):

    date_string = target_date.strftime(
        "%Y-%m-%d"
    )

    date_begin = (
        target_date.strftime("%Y-%m-%d")
        + " 00:00:00"
    )

    date_end = (
        target_date.strftime("%Y-%m-%d")
        + " 23:59:59"
    )

    print()
    print(
        f"[{target_date.strftime('%d.%m.%Y')}]"
    )

    # --------------------------------------------------------
    # ANA SAYFA
    # --------------------------------------------------------

    page = first_page

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

    csrf = get_csrf_token(page)

    if csrf:
        print("      CSRF token bulundu.")
    else:
        print("      CSRF token bulunamadı.")

    # --------------------------------------------------------
    # API DENEMELERİ
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
            csrf,
            mode
        )

        if not response:
            continue

        status = response["status"]

        print(
            f"      API HTTP: {status}"
        )

        if status != 200:
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

            return [], programs, True, False

        # JSON olup olmadığını kontrol et.
        try:

            parsed_json = json.loads(text)

            if isinstance(parsed_json, dict):

                print(
                    "      API JSON alındı fakat program kaydı bulunamadı."
                )

                print(
                    "      JSON anahtarları:",
                    list(parsed_json.keys())[:20]
                )

        except Exception:

            print(
                "      API cevabı JSON değil."
            )

    # --------------------------------------------------------
    # HTML FALLBACK
    # --------------------------------------------------------

    print(
        "      HTML programları deneniyor..."
    )

    if not page:

        return [], [], False, False

    channels, programs = parse_html(
        page,
        target_date
    )

    print(
        f"      HTML kanal: {len(channels)}"
    )

    print(
        f"      HTML program: {len(programs)}"
    )

    return channels, programs, False, bool(programs)


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
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL
        }
    )

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    channel_names = OrderedDict()

    for channel in channels:

        channel_id = channel.get("id")
        channel_name = channel.get("name")

        if not channel_id or not channel_name:
            continue

        channel_names[channel_id] = channel_name

    # Programlardan da kanal üret.
    for program in programs:

        channel_id = program.get(
            "channel_id"
        )

        channel_name = program.get(
            "channel_name"
        )

        if (
            channel_id
            and channel_name
            and channel_id not in channel_names
        ):

            channel_names[channel_id] = channel_name

    for channel_id, channel_name in channel_names.items():

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

        display.text = channel_name

    # --------------------------------------------------------
    # PROGRAMLAR
    # --------------------------------------------------------

    programs_sorted = sorted(
        programs,
        key=lambda x: (
            x["channel_id"],
            x["start"]
        )
    )

    for program in programs_sorted:

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
                "channel": program["channel_id"]
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program["title"]

        category = program.get(
            "category",
            ""
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

    # --------------------------------------------------------
    # XML YAZ
    # --------------------------------------------------------

    tree = ET.ElementTree(tv)

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        output_file,
        encoding="utf-8",
        xml_declaration=True
    )

    return len(channel_names), len(programs_sorted)


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    start_time = datetime.now()

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print()
    print("[1/5] Tivibu ana sayfası alınıyor...")

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
    # BUGÜN
    # --------------------------------------------------------

    today = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    print()
    print("[2/5] Tarihler hazırlanıyor...")

    target_dates = [
        today + timedelta(days=i)
        for i in range(DAYS)
    ]

    for date in target_dates:

        print(
            " -",
            date.strftime("%d.%m.%Y")
        )

    # --------------------------------------------------------
    # GÜNLER
    # --------------------------------------------------------

    print()
    print("[3/5] EPG verileri toplanıyor...")

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

        first_page = (
            main_page
            if target_date == today
            else None
        )

        channels, programs, api_ok, html_ok = process_day(
            target_date,
            first_page
        )

        if api_ok or html_ok:
            successful_days += 1
        else:
            failed_days += 1

        if api_ok:
            api_success_days += 1

        if html_ok:
            html_success_days += 1

        for channel in channels:

            channel_id = channel["id"]

            if channel_id not in all_channels:

                all_channels[channel_id] = channel

        for program in programs:

            all_programs.append(
                program
            )

            channel_id = program[
                "channel_id"
            ]

            if channel_id not in all_channels:

                all_channels[channel_id] = {
                    "id": channel_id,
                    "name": program[
                        "channel_name"
                    ]
                }

    # --------------------------------------------------------
    # DUPLICATE PROGRAMLAR
    # --------------------------------------------------------

    unique_programs = []

    seen_programs = set()

    for program in all_programs:

        key = (
            program["channel_id"],
            program["title"],
            program["start"],
            program["stop"]
        )

        if key in seen_programs:
            continue

        seen_programs.add(key)

        unique_programs.append(
            program
        )

    all_programs = unique_programs

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    print()
    print("[4/5] XMLTV oluşturuluyor...")

    channel_count, program_count = create_xml(
        list(all_channels.values()),
        all_programs,
        OUTPUT_FILE
    )

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    elapsed = (
        datetime.now() - start_time
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

        print(
            "API hâlâ veri döndürmüyorsa GitHub Actions"
        )

        print(
            "çıktısındaki API HTTP ve API cevap satırlarını"
        )

        print(
            "kullanarak API isteğini ayrıca teşhis edeceğiz."
        )

    elif program_count < 20:

        print()
        print(
            "UYARI: Program sayısı beklenenden düşük."
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
