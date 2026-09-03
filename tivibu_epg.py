import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from http.cookiejar import CookieJar


# ============================================================
# TİVİBU 7 GÜNLÜK EPG
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"
MULTI_PREVUE_URL = BASE_URL + "/Channel/GetMultiPrevueData"

OUTPUT_FILE = "epg.xml"

DAYS = 7
REQUEST_DELAY = 1.0
TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ============================================================
# HTTP
# ============================================================

cookie_jar = CookieJar()

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cookie_jar)
)

opener.addheaders = [
    ("User-Agent", USER_AGENT),
    ("Accept-Language", "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
]


def http_get(url, referer=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    with opener.open(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def http_post(url, data, referer=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if referer:
        headers["Referer"] = referer

    encoded = urllib.parse.urlencode(data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=encoded,
        headers=headers,
        method="POST",
    )

    with opener.open(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


# ============================================================
# YARDIMCI
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_name(value):
    value = clean_text(value).upper()

    replacements = {
        "İ": "I",
        "İ": "I",
        "Ş": "S",
        "Ğ": "G",
        "Ü": "U",
        "Ö": "O",
        "Ç": "C",
    }

    for a, b in replacements.items():
        value = value.replace(a, b)

    value = re.sub(r"[^A-Z0-9]+", "", value)

    return value


def channel_id(name):
    n = normalize_name(name)

    if not n:
        n = "KANAL"

    return "tivibu_" + n.lower()


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ============================================================
# SAYFA
# ============================================================

def get_main_page():
    print("[1] Tivibu canlı TV sayfası alınıyor...")

    page = http_get(LIVE_TV_URL)

    print(f"    Sayfa uzunluğu: {len(page):,}")

    return page


# ============================================================
# CSRF
# ============================================================

def extract_csrf_token(page):
    print("[2] CSRF token aranıyor...")

    patterns = [
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']{80,})["\'][^>]*name=["\']__RequestVerificationToken["\']',
        r'value=["\']([^"\']{80,})["\'][^>]*name=["\']RequestVerificationToken["\']',
        r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'csrf["\']?\s*[:=]\s*["\']([^"\']+)["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, page, re.I)

        if match:
            token = html.unescape(match.group(1))

            if len(token) >= 20:
                print(f"    Token uzunluğu: {len(token)}")
                return token

    print("    CSRF token bulunamadı.")

    return ""


# ============================================================
# HTML ATTR
# ============================================================

def parse_attrs(tag):
    attrs = {}

    for match in re.finditer(
        r'''([:\w-]+)\s*=\s*(["'])(.*?)\2''',
        tag,
        re.S,
    ):
        key = match.group(1).lower()
        value = html.unescape(match.group(3))

        attrs[key] = value

    return attrs


# ============================================================
# TİVİBU TARİHLERİ
# ============================================================

def extract_date_options(page):
    found = {}

    # channeldatebegin / channeldateend aynı tag içerisinde
    for tag_match in re.finditer(r"<[^>]+>", page, re.I | re.S):

        tag = tag_match.group(0)

        if "channeldatebegin" not in tag.lower():
            continue

        attrs = parse_attrs(tag)

        begin = ""
        end = ""

        for key, value in attrs.items():

            if "channeldatebegin" in key:
                begin = clean_text(value)

            elif "channeldateend" in key:
                end = clean_text(value)

        if not begin:
            continue

        # Bazen end değeri aynı elementte olmayabiliyor.
        if not end:
            continue

        date_text = ""

        patterns = [
            r"(\d{4})[./-](\d{2})[./-](\d{2})",
            r"(\d{2})[./-](\d{2})[./-](\d{4})",
        ]

        for pattern in patterns:

            m = re.search(pattern, begin)

            if not m:
                continue

            if len(m.group(1)) == 4:
                y, mo, d = m.groups()
            else:
                d, mo, y = m.groups()

            date_text = f"{d}.{mo}.{y}"
            break

        if not date_text:
            continue

        found[date_text] = (begin, end)

    # Sayfa içerisinde JSON/string olarak geçen tarihler için ikinci yöntem
    if len(found) < 7:

        patterns = [
            r'channeldatebegin["\']?\s*[:=]\s*["\']([^"\']+)',
            r'channelDateBegin["\']?\s*[:=]\s*["\']([^"\']+)',
        ]

        begins = []

        for pattern in patterns:

            for m in re.finditer(pattern, page, re.I):

                begins.append(m.group(1))

        for begin in begins:

            m = re.search(
                r"(\d{4})[./-](\d{2})[./-](\d{2})",
                begin,
            )

            if not m:
                continue

            y, mo, d = m.groups()

            date_text = f"{d}.{mo}.{y}"

            if date_text not in found:

                end = (
                    f"{y}.{mo}.{d} 23:59:59"
                )

                found[date_text] = (
                    begin,
                    end,
                )

    return found


def build_target_dates(page):
    options = extract_date_options(page)

    print("[2] Tivibu tarihleri:")

    parsed = []

    for date_text, values in options.items():

        begin, end = values

        try:
            dt = datetime.strptime(
                date_text,
                "%d.%m.%Y",
            )
        except Exception:
            continue

        parsed.append(
            (
                dt,
                date_text,
                begin,
                end,
            )
        )

    parsed.sort(key=lambda x: x[0])

    for _, date_text, begin, end in parsed:
        print(
            f"    {date_text} -> {begin} / {end}"
        )

    # Öncelik: bugün ve sonraki 6 gün
    today = datetime.now().date()

    target = []

    for item in parsed:

        dt = item[0]

        if dt.date() >= today:

            target.append(item)

        if len(target) >= DAYS:
            break

    # Eğer runner saat/tarih farkı yüzünden bugünü bulamazsak,
    # en son tarihlerden 7 tane değil, sayfadaki ilk 7 tarihi kullan.
    if len(target) < DAYS:

        target = []

        for item in parsed:

            target.append(item)

            if len(target) >= DAYS:
                break

    # Hâlâ yoksa günleri kendimiz oluştur.
    if not target:

        for i in range(DAYS):

            dt = datetime.now().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(days=i)

            date_text = dt.strftime("%d.%m.%Y")

            begin = dt.strftime("%Y.%m.%d 00:00:00")
            end = dt.strftime("%Y.%m.%d 23:59:59")

            target.append(
                (
                    dt,
                    date_text,
                    begin,
                    end,
                )
            )

    print()
    print("[3] Hedef 7 gün:")

    result = []

    for i, item in enumerate(target[:DAYS], 1):

        dt, date_text, begin, end = item

        print(
            f"    {i}. {date_text} -> "
            f"{begin} / {end}"
        )

        result.append(
            {
                "date": dt,
                "date_text": date_text,
                "begin": begin,
                "end": end,
            }
        )

    return result


# ============================================================
# GERÇEK KANALLAR
# ============================================================

# Tivibu canlı TV sayfasında gerçek TV kanalı olarak görünen
# kanal isimleri.
#
# Müzik kategorileri, footer linkleri, "Nereden Nereye",
# "Tivibu Nedir" vb. PROGRAM/UI metinleri burada yoktur.
# ============================================================

REAL_CHANNEL_NAMES = [
    "24",
    "360",
    "A HABER",
    "A PARA",
    "A SPOR",
    "A2",
    "AKİT TV",
    "AL JAZEERA ARABIC",
    "AL JAZEERA INTERNATIONAL",
    "AL SUNNAH",
    "ANEWS",
    "AS TV",
    "ATV",
    "BABYTV",
    "BBC EARTH",
    "BBC FIRST",
    "BBC NEWS",
    "BENGÜTÜRK",
    "BENİM KANALIM",
    "BEYAZ TV",
    "BLOOMBERG",
    "BLOOMBERG HT",
    "CARTOON NETWORK",
    "CARTOONITO",
    "CNBC",
    "CNBC-e",
    "CNN INTERNATIONAL",
    "CNN TÜRK",
    "Cosmo EN",
    "COSMO SPORTS",
    "DA VINCI",
    "DEUTSCHE WELLE",
    "DISCOVERY CHANNEL",
    "DISNEY JUNIOR",
    "DİYANET TV",
    "DMAX",
    "DOST TV",
    "DREAM TÜRK TV",
    "EKOL TV",
    "EKOTÜRK",
    "EPIC DRAMA",
    "EUROSPORT 1",
    "EUROSPORT 2",
    "FB TV",
    "FIGHT NETWORK",
    "FILMSCREEN INTERNATIONAL",
    "FLASH HABER TV",
    "FRANCE 24",
    "FX",
    "GZT TV",
    "HABER GLOBAL",
    "HABERTÜRK",
    "HABİTAT TV",
    "HALK TV",
    "HT SPOR HD",
    "ID",
    "KANAL 7",
    "KANAL D",
    "KON TV",
    "LALEGÜL",
    "LOVE NATURE",
    "MİNİKA GO",
    "MİNİKA ÇOCUK",
    "MOONBUG",
    "NATIONAL GEOGRAPHIC",
    "NATIONAL GEOGRAPHIC WILD",
    "NHK WORLD-JAPAN",
    "NICK JR",
    "NICKELODEON",
    "NICKTOONS",
    "NOW",
    "NTV",
    "NUMBER1 TÜRK",
    "POWER TV",
    "POWER TÜRK",
    "RT ARABIC",
    "SAUDI QURAN",
    "SEMERKAND TV",
    "SHOW TV",
    "SİNEMA 1001",
    "SİNEMA 1002",
    "SİNEMA 2",
    "SİNEMA AİLE",
    "SİNEMA AİLE 2",
    "SİNEMA AKSİYON",
    "SİNEMA AKSİYON 2",
    "SİNEMA KOMEDİ",
    "SİNEMA KOMEDİ 2",
    "SİNEMA TV",
    "SİNEMA YERLİ",
    "SİNEMA YERLİ 2",
    "SPACETOON",
    "STAR TV",
    "SZC TV",
    "TARİH TV",
    "TELE 1",
    "TGRT HABER",
    "TİVİ 6",
    "TİVİBU SPOR",
    "TİVİBU SPOR 1",
    "TİVİBU SPOR 2",
    "TİVİBU SPOR 3",
    "TİVİBU SPOR 4",
    "TİVİBU TANITIM",
    "TİVİBU ÇOCUK",
    "TLC",
    "TRT 1",
    "TRT 2",
    "TRT 3 SPOR",
    "TRT AVAZ",
    "TRT BELGESEL",
    "TRT DİYANET ÇOCUK",
    "TRT EBA",
    "TRT EL ARABIA",
    "TRT GENÇ",
    "TRT HABER",
    "TRT KURDİ",
    "TRT MÜZİK",
    "TRT SPOR",
    "TRT SPOR YILDIZ",
    "TRT TÜRK",
    "TRT WORLD",
    "TRT ÇOCUK",
    "TV 100",
    "TV 5",
    "TV 8,5",
    "TV2",
    "TV4",
    "TV5 MONDE",
    "TV8",
    "TVNET",
    "TYT TÜRK",
    "TÜRKHABER TV",
    "ULUSAL 1",
    "VAV TV",
    "VIASAT HISTORY",
    "ÜLKE TV",
]


def extract_real_channels_from_html(page):
    print("[3] Gerçek Tivibu kanalları çıkarılıyor...")

    # Önce whitelist kullanılır.
    # Böylece /kanallar/ altında bulunan PROGRAMLAR
    # kesinlikle kanal haline gelmez.

    page_lower = page.lower()

    found = []

    for name in REAL_CHANNEL_NAMES:

        if name.lower() in page_lower:
            found.append(name)

    # Sıralamayı Tivibu sayfasındaki ilk görünüşe göre düzelt.
    positions = []

    for name in found:

        pos = page_lower.find(name.lower())

        if pos < 0:
            pos = 999999999

        positions.append(
            (
                pos,
                name,
            )
        )

    positions.sort(key=lambda x: x[0])

    channels = []

    seen = set()

    for _, name in positions:

        key = normalize_name(name)

        if key in seen:
            continue

        seen.add(key)

        channels.append(
            {
                "name": name,
                "id": channel_id(name),
            }
        )

    print(
        f"    Gerçek kanal sayısı: {len(channels)}"
    )

    for ch in channels:
        print(
            f"      + {ch['name']}"
        )

    return channels


# ============================================================
# CHANNEL COLUMN CODE
# ============================================================

def extract_channel_column_code(page):
    patterns = [
        r'channelColumnCode["\']?\s*[:=]\s*["\']([^"\']+)',
        r'ChannelColumnCode["\']?\s*[:=]\s*["\']([^"\']+)',
        r'data-channel-column-code=["\']([^"\']+)',
        r'channel-column-code=["\']([^"\']+)',
    ]

    for pattern in patterns:

        m = re.search(
            pattern,
            page,
            re.I,
        )

        if m:

            value = clean_text(m.group(1))

            if value:
                return value

    # Tivibu'nun canlı TV kategorisi.
    return "020002"


def extract_channel_search_value(page):
    patterns = [
        r'channelSearchValue["\']?\s*[:=]\s*["\']([^"\']*)',
        r'ChannelSearchValue["\']?\s*[:=]\s*["\']([^"\']*)',
        r'data-channel-search-value=["\']([^"\']*)',
        r'channel-search-value=["\']([^"\']*)',
    ]

    for pattern in patterns:

        m = re.search(
            pattern,
            page,
            re.I,
        )

        if m:
            return clean_text(m.group(1))

    return ""


# ============================================================
# API RESPONSE
# ============================================================

def json_load_loose(text):
    text = text.strip()

    # JSON doğrudan
    try:
        return json.loads(text)
    except Exception:
        pass

    # HTML entity
    try:
        return json.loads(
            html.unescape(text)
        )
    except Exception:
        pass

    # JSONP
    m = re.search(
        r"^[^(]+\((.*)\)\s*;?\s*$",
        text,
        re.S,
    )

    if m:

        try:
            return json.loads(
                m.group(1)
            )
        except Exception:
            pass

    return None


def find_lists(obj):
    result = []

    if isinstance(obj, list):

        if obj:
            result.append(obj)

        for item in obj:
            result.extend(
                find_lists(item)
            )

    elif isinstance(obj, dict):

        for value in obj.values():
            result.extend(
                find_lists(value)
            )

    return result


def find_dicts_with_keys(obj, wanted):
    result = []

    if isinstance(obj, dict):

        keys = {
            str(k).lower()
            for k in obj.keys()
        }

        if keys.intersection(wanted):
            result.append(obj)

        for value in obj.values():
            result.extend(
                find_dicts_with_keys(
                    value,
                    wanted,
                )
            )

    elif isinstance(obj, list):

        for item in obj:
            result.extend(
                find_dicts_with_keys(
                    item,
                    wanted,
                )
            )

    return result


def get_value(obj, names):
    if not isinstance(obj, dict):
        return ""

    lower = {
        str(k).lower(): v
        for k, v in obj.items()
    }

    for name in names:

        key = name.lower()

        if key in lower:

            value = lower[key]

            if value is None:
                return ""

            return value

    return ""


# ============================================================
# API PROGRAM PARSER
# ============================================================

def extract_api_records(data):
    channels = []
    programmes = []

    if data is None:
        return channels, programmes

    # --------------------------------------------
    # CHANNEL KAYITLARI
    # --------------------------------------------

    channel_key_names = {
        "channelcode",
        "channelid",
        "channelname",
        "displayname",
    }

    channel_dicts = find_dicts_with_keys(
        data,
        channel_key_names,
    )

    seen_channels = set()

    for obj in channel_dicts:

        code = clean_text(
            get_value(
                obj,
                [
                    "channelCode",
                    "channelcode",
                    "channelId",
                    "channelid",
                    "code",
                ],
            )
        )

        name = clean_text(
            get_value(
                obj,
                [
                    "channelName",
                    "channelname",
                    "displayName",
                    "displayname",
                    "name",
                    "title",
                ],
            )
        )

        if not code and not name:
            continue

        key = (
            str(code),
            normalize_name(name),
        )

        if key in seen_channels:
            continue

        seen_channels.add(key)

        channels.append(
            {
                "code": str(code),
                "name": name,
                "raw": obj,
            }
        )

    # --------------------------------------------
    # PROGRAM KAYITLARI
    # --------------------------------------------

    programme_key_names = {
        "programname",
        "programtitle",
        "title",
        "startdate",
        "starttime",
        "start",
        "enddate",
        "endtime",
        "stop",
    }

    programme_dicts = find_dicts_with_keys(
        data,
        programme_key_names,
    )

    seen_programmes = set()

    for obj in programme_dicts:

        title = clean_text(
            get_value(
                obj,
                [
                    "programName",
                    "programname",
                    "programTitle",
                    "programtitle",
                    "title",
                    "name",
                    "programmeName",
                ],
            )
        )

        channel_code = clean_text(
            get_value(
                obj,
                [
                    "channelCode",
                    "channelcode",
                    "channelId",
                    "channelid",
                    "channel",
                ],
            )
        )

        channel_name = clean_text(
            get_value(
                obj,
                [
                    "channelName",
                    "channelname",
                    "channelTitle",
                    "channeltitle",
                ],
            )
        )

        start = clean_text(
            get_value(
                obj,
                [
                    "start",
                    "startDate",
                    "startdate",
                    "startTime",
                    "starttime",
                    "begin",
                    "beginDate",
                    "begindate",
                ],
            )
        )

        stop = clean_text(
            get_value(
                obj,
                [
                    "stop",
                    "end",
                    "endDate",
                    "enddate",
                    "endTime",
                    "endtime",
                    "finish",
                    "finishDate",
                    "finishdate",
                ],
            )
        )

        # Eğer program kaydı değilse at.
        if not title:
            continue

        if not start:
            continue

        key = (
            channel_code,
            normalize_name(channel_name),
            title,
            start,
            stop,
        )

        if key in seen_programmes:
            continue

        seen_programmes.add(key)

        programmes.append(
            {
                "channel_code": str(channel_code),
                "channel_name": channel_name,
                "title": title,
                "start": start,
                "stop": stop,
                "raw": obj,
            }
        )

    return channels, programmes


# ============================================================
# TARİH FORMATLAMA
# ============================================================

def parse_datetime(value):
    if value is None:
        return None

    value = clean_text(value)

    # Unix timestamp
    if re.fullmatch(r"\d{10}", value):

        try:
            return datetime.fromtimestamp(
                int(value)
            )
        except Exception:
            pass

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",

        "%Y.%m.%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d-%m-%Y %H:%M",

        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]

    # ISO timezone varsa Z kısmını kaldır.
    clean = re.sub(
        r"(Z|[+-]\d{2}:\d{2})$",
        "",
        value,
    )

    for fmt in formats:

        try:
            return datetime.strptime(
                clean,
                fmt,
            )
        except Exception:
            pass

    # İçinden tarih/saat çek
    m = re.search(
        r"(\d{4})[./-](\d{2})[./-](\d{2}).*?"
        r"(\d{2}):(\d{2})(?::(\d{2}))?",
        value,
    )

    if m:

        y, mo, d, h, mi, sec = m.groups()

        try:
            return datetime(
                int(y),
                int(mo),
                int(d),
                int(h),
                int(mi),
                int(sec or 0),
            )
        except Exception:
            pass

    return None


def xml_time(dt):
    return dt.strftime(
        "%Y%m%d%H%M%S"
    ) + " " + TIMEZONE


# ============================================================
# API POST
# ============================================================

def make_api_payload(
    begin,
    end,
    csrf_token,
    channel_column_code,
    channel_search_value,
    page_no=1,
):
    """
    Tivibu endpoint'ine aynı anda birkaç isim varyasyonu
    gönderilir.

    Böylece site tarafında küçük parametre isim değişiklikleri
    olsa bile çalışabilir.
    """

    payload = {
        "channelColumnCode": channel_column_code,
        "channelSearchValue": channel_search_value,

        "channelDateBegin": begin,
        "channelDateEnd": end,

        "pageNo": str(page_no),
        "pageSize": "1000",

        "page": str(page_no),
        "size": "1000",

        "__RequestVerificationToken": csrf_token,
    }

    return payload


def post_multi_prevue(
    begin,
    end,
    csrf_token,
    channel_column_code,
    channel_search_value,
    page_no=1,
):
    payload = make_api_payload(
        begin=begin,
        end=end,
        csrf_token=csrf_token,
        channel_column_code=channel_column_code,
        channel_search_value=channel_search_value,
        page_no=page_no,
    )

    try:

        text = http_post(
            MULTI_PREVUE_URL,
            payload,
            referer=LIVE_TV_URL,
        )

        data = json_load_loose(text)

        if data is None:

            # Bazen response HTML içinde JSON bulunabilir.
            m = re.search(
                r"(\{.*\})",
                text,
                re.S,
            )

            if m:

                try:
                    data = json.loads(
                        m.group(1)
                    )
                except Exception:
                    data = None

        if data is None:
            return None, text

        return data, ""

    except urllib.error.HTTPError as exc:

        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            body = ""

        return None, body

    except Exception as exc:

        return None, str(exc)


# ============================================================
# PROGRAM KANAL EŞLEŞTİRME
# ============================================================

def build_channel_maps(
    html_channels,
    api_channels,
):
    by_code = {}
    by_name = {}

    # HTML'deki gerçek kanallar ana kaynak.
    for ch in html_channels:

        name = ch["name"]
        cid = ch["id"]

        by_name[
            normalize_name(name)
        ] = {
            "name": name,
            "id": cid,
            "api_code": "",
        }

    # API kanal kayıtları da eklenir.
    for ch in api_channels:

        code = str(
            ch.get("code", "")
        ).strip()

        name = clean_text(
            ch.get("name", "")
        )

        if code:
            by_code[code] = {
                "name": name,
                "id": channel_id(name) if name else "",
                "api_code": code,
            }

        if name:

            norm = normalize_name(name)

            if norm in by_name:

                by_name[norm]["api_code"] = code

            elif norm:
                by_name[norm] = {
                    "name": name,
                    "id": channel_id(name),
                    "api_code": code,
                }

    return by_code, by_name


def match_program_channel(
    program,
    by_code,
    by_name,
):
    code = str(
        program.get(
            "channel_code",
            "",
        )
    ).strip()

    name = clean_text(
        program.get(
            "channel_name",
            "",
        )
    )

    # 1 — API channelCode
    if code and code in by_code:

        ch = by_code[code]

        # HTML gerçek kanal listesinde olup olmadığını
        # ayrıca kontrol et.
        if ch.get("name"):

            norm = normalize_name(
                ch["name"]
            )

            if norm in by_name:
                return by_name[norm]

    # 2 — Program channelName
    if name:

        norm = normalize_name(name)

        if norm in by_name:
            return by_name[norm]

    return None


# ============================================================
# PROGRAM BAŞLANGIÇ/BİTİŞ
# ============================================================

def normalize_program_times(
    program,
    target_date,
):
    start = parse_datetime(
        program.get("start")
    )

    stop = parse_datetime(
        program.get("stop")
    )

    if start is None:
        return None, None

    # API sadece saat döndürdüyse hedef gün kullan.
    if start.year < 2000:

        start = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            start.hour,
            start.minute,
            start.second,
        )

    if stop is not None and stop.year < 2000:

        stop = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            stop.hour,
            stop.minute,
            stop.second,
        )

    # Stop yoksa 30 dakika.
    if stop is None:
        stop = start + timedelta(
            minutes=30
        )

    # Ters zamanları düzelt.
    if stop <= start:

        stop = start + timedelta(
            minutes=30
        )

    return start, stop


# ============================================================
# API'DEN TEK GÜN
# ============================================================

def fetch_day(
    target,
    csrf_token,
    channel_column_code,
    channel_search_value,
    html_channels,
):
    begin = target["begin"]
    end = target["end"]

    target_date = target["date"]

    all_api_channels = []
    all_programmes = []

    seen_codes = set()
    seen_programmes = set()

    # --------------------------------------------------------
    # Sayfa 1
    # --------------------------------------------------------

    print(
        f"    İlk API isteği: {begin} -> {end}"
    )

    data, error = post_multi_prevue(
        begin,
        end,
        csrf_token,
        channel_column_code,
        channel_search_value,
        page_no=1,
    )

    if data is None:

        print(
            "    İlk tarih isteği başarısız."
        )

        if error:
            print(
                f"    Cevap: {error[:500]}"
            )

        return [], [], False

    channels, programmes = extract_api_records(
        data
    )

    for ch in channels:

        code = str(
            ch.get("code", "")
        ).strip()

        if code and code not in seen_codes:

            seen_codes.add(code)
            all_api_channels.append(ch)

    for p in programmes:

        key = (
            p.get("channel_code", ""),
            normalize_name(
                p.get("channel_name", "")
            ),
            p.get("title", ""),
            p.get("start", ""),
            p.get("stop", ""),
        )

        if key not in seen_programmes:

            seen_programmes.add(key)
            all_programmes.append(p)

    print(
        f"    API kanal kaydı: {len(all_api_channels)}"
    )

    print(
        f"    API program kaydı: {len(all_programmes)}"
    )

    # --------------------------------------------------------
    # Pagination
    # --------------------------------------------------------

    # Çok yüksek sayfa sayısına çıkmasını engelle.
    for page_no in range(2, 21):

        time.sleep(REQUEST_DELAY)

        data2, error2 = post_multi_prevue(
            begin,
            end,
            csrf_token,
            channel_column_code,
            channel_search_value,
            page_no=page_no,
        )

        if data2 is None:

            # 400 burada genellikle "daha fazla sayfa yok"
            # anlamına geliyor.
            print(
                f"    Sayfa {page_no}: bitti "
                f"(HTTP 400 / veri yok)"
            )

            break

        channels2, programmes2 = extract_api_records(
            data2
        )

        new_count = 0

        for ch in channels2:

            code = str(
                ch.get("code", "")
            ).strip()

            if code and code not in seen_codes:

                seen_codes.add(code)
                all_api_channels.append(ch)

        for p in programmes2:

            key = (
                p.get("channel_code", ""),
                normalize_name(
                    p.get("channel_name", "")
                ),
                p.get("title", ""),
                p.get("start", ""),
                p.get("stop", ""),
            )

            if key not in seen_programmes:

                seen_programmes.add(key)
                all_programmes.append(p)

                new_count += 1

        print(
            f"    Sayfa {page_no}: "
            f"+{new_count} yeni program"
        )

        if not channels2 and not programmes2:
            break

        if new_count == 0:
            break

    # --------------------------------------------------------
    # EŞLEŞTİR
    # --------------------------------------------------------

    by_code, by_name = build_channel_maps(
        html_channels,
        all_api_channels,
    )

    matched = []
    unmatched = 0

    for program in all_programmes:

        ch = match_program_channel(
            program,
            by_code,
            by_name,
        )

        if ch is None:

            unmatched += 1
            continue

        start, stop = normalize_program_times(
            program,
            target_date,
        )

        if start is None or stop is None:
            continue

        # Sadece hedef günün programlarını kabul et.
        # Gece yarısını geçen programlara izin ver.
        day_start = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
        )

        day_end = day_start + timedelta(
            days=1
        )

        if stop <= day_start:
            continue

        if start >= day_end:
            continue

        # Gün sınırlarını kırp.
        if start < day_start:
            start = day_start

        if stop > day_end:
            stop = day_end

        title = clean_text(
            program.get(
                "title",
                "",
            )
        )

        if not title:
            continue

        matched.append(
            {
                "channel_id": ch["id"],
                "channel_name": ch["name"],
                "title": title,
                "start": start,
                "stop": stop,
            }
        )

    print(
        f"    Eşleşen program: {len(matched)}"
    )

    print(
        f"    Eşleşmeyen program: {unmatched}"
    )

    print(
        f"    Kullanılabilir program: {len(matched)}"
    )

    return (
        all_api_channels,
        matched,
        True,
    )


# ============================================================
# XML
# ============================================================

def write_xml(
    channels,
    programmes,
):
    print()
    print("[5] XML oluşturuluyor...")

    root = ET.Element(
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

    for ch in channels:

        element = ET.SubElement(
            root,
            "channel",
            {
                "id": ch["id"],
            },
        )

        display = ET.SubElement(
            element,
            "display-name",
            {
                "lang": "tr",
            },
        )

        display.text = ch["name"]

    # --------------------------------------------------------
    # PROGRAMME
    # --------------------------------------------------------

    programmes.sort(
        key=lambda p: (
            p["start"],
            p["channel_id"],
        )
    )

    for p in programmes:

        element = ET.SubElement(
            root,
            "programme",
            {
                "start": xml_time(
                    p["start"]
                ),
                "stop": xml_time(
                    p["stop"]
                ),
                "channel": p["channel_id"],
            },
        )

        title = ET.SubElement(
            element,
            "title",
            {
                "lang": "tr",
            },
        )

        title.text = p["title"]

    tree = ET.ElementTree(root)

    try:
        ET.indent(
            tree,
            space="  ",
        )
    except Exception:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    print(
        f"    XML yazıldı: {OUTPUT_FILE}"
    )

    print(
        f"    Kanal sayısı: {len(channels)}"
    )

    print(
        f"    Program sayısı: {len(programmes)}"
    )


# ============================================================
# XML KONTROL
# ============================================================

def validate_xml(
    channels,
    programmes,
):
    print()
    print("[6] XML kontrol ediliyor...")

    if not programmes:
        raise RuntimeError(
            "Hiç program alınamadı. "
            "Yanlış XML oluşturulmayacak."
        )

    channel_ids = {
        ch["id"]
        for ch in channels
    }

    invalid = []

    for p in programmes:

        if p["channel_id"] not in channel_ids:

            invalid.append(p)

    if invalid:

        raise RuntimeError(
            f"{len(invalid)} program "
            f"geçersiz kanal ID'sine sahip."
        )

    # XML'i tekrar oku.
    tree = ET.parse(
        OUTPUT_FILE
    )

    root = tree.getroot()

    xml_channels = root.findall(
        "channel"
    )

    xml_programmes = root.findall(
        "programme"
    )

    if not xml_programmes:
        raise RuntimeError(
            "XML içinde programme yok."
        )

    # Program başlıklarının kanal olarak
    # yazılmadığını kontrol et.
    forbidden = {
        normalize_name(
            "Tivibu'nun Renkli Dünyası"
        ),
        normalize_name(
            "Nereden Nereye"
        ),
        normalize_name(
            "Count Me In"
        ),
        normalize_name(
            "Sefiller"
        ),
        normalize_name(
            "Ölü Mevsim"
        ),
        normalize_name(
            "Cebimde Kelimeler"
        ),
        normalize_name(
            "Tivibu Nedir"
        ),
        normalize_name(
            "Favori Kanallarım"
        ),
        normalize_name(
            "Tivibu Canlı TV, Kanal ve Programlar"
        ),
    }

    bad_channels = []

    for ch in xml_channels:

        name = ch.findtext(
            "display-name",
            default="",
        )

        if normalize_name(name) in forbidden:
            bad_channels.append(name)

    if bad_channels:

        raise RuntimeError(
            "XML içinde program/UI metni kanal "
            "olarak bulundu: "
            + ", ".join(bad_channels)
        )

    print(
        f"    XML kanal: {len(xml_channels)}"
    )

    print(
        f"    XML program: {len(xml_programmes)}"
    )

    print(
        "    XML doğrulaması BAŞARILI."
    )


# ============================================================
# KANAL TEMİZLEME
# ============================================================

def finalize_channels(
    html_channels,
    programmes,
):
    used_ids = {
        p["channel_id"]
        for p in programmes
    }

    result = []

    for ch in html_channels:

        # EPG'de programı olmasa bile gerçek Tivibu
        # kanalı olarak XML'de tutulur.
        result.append(ch)

    # Tekrar temizle
    unique = []
    seen = set()

    for ch in result:

        key = ch["id"]

        if key in seen:
            continue

        seen.add(key)
        unique.append(ch)

    return unique


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("TİVİBU 7 GÜNLÜK EPG")
    print("=" * 70)

    # --------------------------------------------------------
    # SAYFA
    # --------------------------------------------------------

    page = get_main_page()

    # --------------------------------------------------------
    # TOKEN
    # --------------------------------------------------------

    csrf_token = extract_csrf_token(
        page
    )

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
        f"{channel_column_code}"
    )

    print(
        f"    channelSearchValue: "
        f"{channel_search_value or '(boş)'}"
    )

    # --------------------------------------------------------
    # TARİHLER
    # --------------------------------------------------------

    targets = build_target_dates(
        page
    )

    if len(targets) != DAYS:
        raise RuntimeError(
            f"{DAYS} gün yerine "
            f"{len(targets)} gün bulundu."
        )

    # --------------------------------------------------------
    # GERÇEK KANALLAR
    # --------------------------------------------------------

    html_channels = (
        extract_real_channels_from_html(
            page
        )
    )

    if not html_channels:
        raise RuntimeError(
            "Gerçek Tivibu kanalları bulunamadı."
        )

    # --------------------------------------------------------
    # EPG
    # --------------------------------------------------------

    print()
    print("[4] 7 günlük EPG çekiliyor...")
    print()

    all_programmes = []

    successful_days = 0
    failed_days = 0

    for index, target in enumerate(
        targets,
        1,
    ):

        print("-" * 70)
        print(
            f"GÜN {index}/{DAYS}"
        )

        print(
            f"GÜN: {target['date_text']}"
        )

        (
            api_channels,
            programmes,
            success,
        ) = fetch_day(
            target=target,
            csrf_token=csrf_token,
            channel_column_code=channel_column_code,
            channel_search_value=channel_search_value,
            html_channels=html_channels,
        )

        if success and programmes:

            successful_days += 1
            all_programmes.extend(
                programmes
            )

        else:

            failed_days += 1

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # DUPLICATE TEMİZLE
    # --------------------------------------------------------

    unique_programmes = []
    seen = set()

    for p in all_programmes:

        key = (
            p["channel_id"],
            p["title"],
            p["start"],
            p["stop"],
        )

        if key in seen:
            continue

        seen.add(key)
        unique_programmes.append(p)

    all_programmes = unique_programmes

    print()
    print("=" * 70)
    print("EPG TOPLAMA SONUCU")
    print("=" * 70)

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
        f"{len(all_programmes)}"
    )

    # --------------------------------------------------------
    # PROGRAM YOKSA XML ÜRETME
    # --------------------------------------------------------

    if not all_programmes:
        raise RuntimeError(
            "Hiç program alınamadı. "
            "Yanlış XML oluşturulmayacak."
        )

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    final_channels = finalize_channels(
        html_channels,
        all_programmes,
    )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    write_xml(
        final_channels,
        all_programmes,
    )

    # --------------------------------------------------------
    # VALIDATE
    # --------------------------------------------------------

    validate_xml(
        final_channels,
        all_programmes,
    )

    print()
    print("=" * 70)
    print("TAMAMLANDI")
    print("=" * 70)
    print(
        f"XML: {OUTPUT_FILE}"
    )
    print(
        f"Kanal: {len(final_channels)}"
    )
    print(
        f"Program: {len(all_programmes)}"
    )


# ============================================================
# ÇALIŞTIR
# ============================================================

if __name__ == "__main__":
    main()
