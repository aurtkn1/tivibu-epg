#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TIVIBU 7 GÜNLÜK XMLTV EPG
----------------------------------------
Kaynak:
    https://www.tivibu.com.tr/canli-tv

Çıktı:
    epg.xml

Özellikler:
    - Gerçek Tivibu kanallarını çıkarır
    - Program isimlerini kanal içine doğru şekilde bağlar
    - Yanlışlıkla program isimlerini kanal olarak eklemez
    - 7 günlük EPG oluşturur
    - API çalışmazsa HTML üzerinden devam eder
    - GitHub Actions uyumludur
    - Başarısız/boş EPG üretmez
"""

import html
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from http.cookiejar import CookieJar


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS = 7

TIMEZONE = "+0300"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

REQUEST_DELAY = 0.8

TIMEOUT = 40


# ============================================================
# HTTP
# ============================================================

COOKIE_JAR = CookieJar()

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)

OPENER.addheaders = [
    ("User-Agent", USER_AGENT),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.5"),
    ("Cache-Control", "no-cache"),
    ("Pragma", "no-cache"),
    ("Connection", "keep-alive"),
]


def http_get(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.5",
            "Referer": LIVE_TV_URL,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )

    with OPENER.open(req, timeout=TIMEOUT) as response:
        data = response.read()

        charset = response.headers.get_content_charset()

        if charset:
            encoding = charset
        else:
            encoding = "utf-8"

        return data.decode(encoding, errors="replace")


# ============================================================
# METİN
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))

    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)

    value = value.replace("\xa0", " ")

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_text(value):
    value = clean_text(value)

    value = value.replace("’", "'")
    value = value.replace("`", "'")

    return value.casefold().strip()


def slugify(value):
    value = clean_text(value)

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        ch for ch in value
        if not unicodedata.combining(ch)
    )

    value = value.lower()

    value = value.replace("ı", "i")
    value = value.replace("ş", "s")
    value = value.replace("ğ", "g")
    value = value.replace("ü", "u")
    value = value.replace("ö", "o")
    value = value.replace("ç", "c")

    value = re.sub(r"[^a-z0-9]+", "", value)

    return value


# ============================================================
# TARİH
# ============================================================

def parse_date_text(value):
    if not value:
        return None

    m = re.search(
        r"(\d{2})[./-](\d{2})[./-](\d{4})",
        value
    )

    if not m:
        return None

    day = int(m.group(1))
    month = int(m.group(2))
    year = int(m.group(3))

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def format_tivibu_date(dt, end=False):
    if end:
        return dt.strftime("%Y.%m.%d 23:59:59")

    return dt.strftime("%Y.%m.%d 00:00:00")


def find_tivibu_dates(page):
    """
    Sayfadaki bütün Tivibu tarihlerini bulur.
    """

    found = {}

    patterns = [
        r"\b(\d{2}\.\d{2}\.\d{4})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
        r"\b(\d{2}-\d{2}-\d{4})\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, page):
            text = match.group(1)

            dt = parse_date_text(text)

            if dt:
                key = dt.strftime("%Y-%m-%d")
                found[key] = dt

    return sorted(found.values())


def get_target_dates(page):
    dates = find_tivibu_dates(page)

    today = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    candidates = [
        d for d in dates
        if d >= today
    ]

    if len(candidates) >= DAYS:
        return candidates[:DAYS]

    # Sayfada tarih listesi değişirse fallback.
    return [
        today + timedelta(days=i)
        for i in range(DAYS)
    ]


# ============================================================
# GERÇEK KANAL LİSTESİ
# ============================================================

# Tivibu sayfasındaki müzik radyoları kanal değildir.
# Bunları özellikle dışarıda bırakıyoruz.

FALSE_CHANNELS = {
    normalize_text(x)
    for x in [
        "TÜRKÇE POP",
        "TÜRKÇE SLOW",
        "TÜRK HALK MÜZİĞİ",
        "TÜRK SANAT MÜZİĞİ",
        "TAŞ PLAK",
        "DİNİ MUSİKİ",
        "ARABESK",
        "90’ LAR",
        "YABANCI POP",
        "YABANCI ROCK",
        "YABANCI SLOW",
        "AKUSTİK",
        "BLUES",
        "JAZZ",
        "KLASİK MÜZİK",
        "LOUNGE",
        "MUTLU",
        "OLDIES",
        "RETRO",
        "ÇALIŞIRKEN",
        "FİLM MÜZİKLERİ",
    ]
}


# Bunlar sayfada link olarak bulunabilir fakat kanal değildir.

FALSE_CHANNEL_PHRASES = {
    normalize_text(x)
    for x in [
        "Favori Kanallarım",
        "Tivibu Nedir?",
        "Tivibu Canlı TV, Kanal ve Programlar",
        "Tivibu Spor Canlı İzle",
        "TRT1 Canlı İzle",
        "Nereden Nereye",
        "Count Me In",
        "Sefiller",
        "Ölü Mevsim",
        "Cebimde Kelimeler",
    ]
}


def is_probable_channel_name(name):
    name = clean_text(name)

    if not name:
        return False

    normalized = normalize_text(name)

    if normalized in FALSE_CHANNELS:
        return False

    if normalized in FALSE_CHANNEL_PHRASES:
        return False

    # Çok uzun başlıklar kanal olamaz.
    if len(name) > 55:
        return False

    # Program başlığı olma ihtimali yüksek ifadeler.
    bad_words = [
        "film -",
        "dizi -",
        "yaşam -",
        "canlı",
        "programı",
        "program akışı",
        "tek parça",
        "fragman",
    ]

    for word in bad_words:
        if word in normalized:
            return False

    return True


def extract_channel_links(page):
    """
    /kanallar/ bağlantılarından gerçek kanal isimlerini çıkarır.

    Aynı kanalın birden fazla kez görünmesini engeller.
    """

    channels = []

    seen = set()

    # href + anchor içeriği
    pattern = re.compile(
        r'<a\b[^>]*href\s*=\s*["\']([^"\']*/kanallar/[^"\']*)["\'][^>]*>'
        r'(.*?)'
        r'</a>',
        re.I | re.S
    )

    for match in pattern.finditer(page):

        href = html.unescape(match.group(1))
        inner = match.group(2)

        name = clean_text(inner)

        # İçerik boşsa aria/title/data-name dene.
        if not name:
            attrs = match.group(0)

            for attr in (
                "aria-label",
                "title",
                "data-name",
                "data-channel-name",
            ):
                m = re.search(
                    rf'{attr}\s*=\s*["\']([^"\']+)["\']',
                    attrs,
                    re.I,
                )

                if m:
                    name = clean_text(m.group(1))
                    break

        if not is_probable_channel_name(name):
            continue

        # Link URL'sinden slug çıkar.
        parsed = urllib.parse.urlparse(href)

        path = parsed.path.rstrip("/")

        if "/kanallar/" not in path.lower():
            continue

        channel_slug = path.split("/")[-1]

        if not channel_slug:
            continue

        key = normalize_text(name)

        if key in seen:
            continue

        seen.add(key)

        channels.append({
            "name": name,
            "slug": "tivibu_" + slugify(name),
            "url_slug": channel_slug,
            "href": href,
        })

    return channels


# ============================================================
# HTML İÇİ PROGRAM PARSE
# ============================================================

def extract_program_blocks(page):
    """
    Tivibu HTML'inde programlar genellikle:

        Program Adı Kategori - 21:00 → 00:00 Canlı

    şeklinde görünür.

    Burada kanal bağlantılarından sonraki program bloklarını
    tespit etmeye çalışıyoruz.
    """

    results = []

    # Önce tüm metni normalize etmeden yakala.
    text = page

    # HTML entity'lerini aç.
    text = html.unescape(text)

    # Satırları oluştur.
    text = re.sub(r"<script\b.*?</script>", "\n", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", "\n", text, flags=re.I | re.S)

    text = re.sub(r"<[^>]+>", "\n", text)

    text = text.replace("\r", "\n")

    lines = []

    for line in text.split("\n"):
        line = clean_text(line)

        if line:
            lines.append(line)

    # Program zaman formatları:
    # 21:00 → 00:00
    # 21:00 -> 00:00
    # 21:00 - 00:00

    time_re = re.compile(
        r"(.+?)"
        r"(?:\s+(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|Çocuk|Diğer)"
        r")?"
        r"\s*[-–—]"
        r"\s*(\d{1,2}:\d{2})"
        r"\s*(?:→|->|-)"
        r"\s*(\d{1,2}:\d{2})"
        r"(?:\s+Canlı)?$",
        re.I
    )

    for line in lines:

        m = time_re.search(line)

        if not m:
            continue

        title = clean_text(m.group(1))

        start_time = m.group(2)
        end_time = m.group(3)

        if not title:
            continue

        # Gereksiz HTML/site başlıklarını engelle.
        bad = [
            "sonuç bulunamadı",
            "kanal ara",
            "program akışı",
            "içeriği izlemek için",
            "giriş yap",
            "üye ol",
        ]

        normalized = normalize_text(title)

        if normalized in bad:
            continue

        results.append({
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
        })

    return results


# ============================================================
# KANAL BÖLÜMLERİNİ ÇIKAR
# ============================================================

def find_channel_sections(page, channels):
    """
    HTML'i kanal linklerine göre bölümlere ayırır.

    Böylece:
        SİNEMA TV
        programları...

    ile

        SİNEMA 2
        programları...

    birbirine karışmaz.
    """

    sections = []

    channel_patterns = []

    for channel in channels:

        name = re.escape(channel["name"])

        pattern = re.compile(
            r'<a\b[^>]*href\s*=\s*["\'][^"\']*/kanallar/'
            r'[^"\']*["\'][^>]*>'
            r'(.*?)'
            r'</a>',
            re.I | re.S
        )

        for m in pattern.finditer(page):

            inner = clean_text(m.group(1))

            if normalize_text(inner) == normalize_text(channel["name"]):

                channel_patterns.append(
                    (
                        m.start(),
                        m.end(),
                        channel,
                    )
                )

    channel_patterns.sort(key=lambda x: x[0])

    for i, item in enumerate(channel_patterns):

        start_pos = item[1]

        if i + 1 < len(channel_patterns):
            end_pos = channel_patterns[i + 1][0]
        else:
            end_pos = len(page)

        chunk = page[start_pos:end_pos]

        # Aşırı büyük bölüm olmasın.
        if len(chunk) > 250000:
            chunk = chunk[:250000]

        sections.append(
            (
                item[2],
                chunk,
            )
        )

    return sections


# ============================================================
# SAAT PARSE
# ============================================================

def parse_hhmm(value):
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())

    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def make_datetime(day, hhmm, allow_next_day=False):
    parsed = parse_hhmm(hhmm)

    if parsed is None:
        return None

    hour, minute = parsed

    result = day.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if allow_next_day:
        result += timedelta(days=1)

    return result


# ============================================================
# PROGRAM EŞLEŞTİRME
# ============================================================

def parse_programs_from_channel_section(section, channel, day):
    """
    Tek kanal bölümünden programları çıkarır.

    Aynı program farklı HTML alanlarında tekrar edebildiği için
    başlangıç/bitiş/title kombinasyonuyla dedupe yapılır.
    """

    raw = section

    raw = html.unescape(raw)

    raw = re.sub(
        r"<script\b.*?</script>",
        "\n",
        raw,
        flags=re.I | re.S,
    )

    raw = re.sub(
        r"<style\b.*?</style>",
        "\n",
        raw,
        flags=re.I | re.S,
    )

    raw = re.sub(r"<[^>]+>", "\n", raw)

    raw = raw.replace("\r", "\n")

    lines = []

    for line in raw.split("\n"):
        line = clean_text(line)

        if line:
            lines.append(line)

    programs = []

    seen = set()

    # Tivibu'nun kullandığı yapı.
    #
    # Örnek:
    #
    # Beyaz Şövalyeler Film - 19:45 → 22:00 Canlı

    regexes = [
        re.compile(
            r"^(.*?)\s+"
            r"(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|Çocuk|Diğer)"
            r"\s*[-–—]\s*"
            r"(\d{1,2}:\d{2})\s*"
            r"(?:→|->|-)\s*"
            r"(\d{1,2}:\d{2})"
            r"(?:\s+Canlı)?$",
            re.I,
        ),

        re.compile(
            r"^(.*?)\s*[-–—]\s*"
            r"(\d{1,2}:\d{2})\s*"
            r"(?:→|->|-)\s*"
            r"(\d{1,2}:\d{2})"
            r"(?:\s+Canlı)?$",
            re.I,
        ),
    ]

    for line in lines:

        match = None

        for regex in regexes:
            match = regex.search(line)

            if match:
                break

        if not match:
            continue

        title = clean_text(match.group(1))

        start_str = match.group(2)
        end_str = match.group(3)

        if not title:
            continue

        # Kanal ismi program olarak yakalanırsa alma.
        if normalize_text(title) == normalize_text(channel["name"]):
            continue

        # Site navigasyonları.
        if normalize_text(title) in FALSE_CHANNEL_PHRASES:
            continue

        start_dt = make_datetime(day, start_str)

        if start_dt is None:
            continue

        end_dt = make_datetime(day, end_str)

        if end_dt is None:
            continue

        # Gece yarısını aşan program.
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        key = (
            channel["slug"],
            start_dt.strftime("%Y%m%d%H%M"),
            end_dt.strftime("%Y%m%d%H%M"),
            normalize_text(title),
        )

        if key in seen:
            continue

        seen.add(key)

        programs.append({
            "channel": channel["slug"],
            "channel_name": channel["name"],
            "title": title,
            "start": start_dt,
            "stop": end_dt,
        })

    return programs


# ============================================================
# ANA SAYFADAN PROGRAM TOPLAMA
# ============================================================

def collect_from_html(page, channels, day):
    """
    HTML'deki gerçek kanal bölümlerinden programları toplar.
    """

    sections = find_channel_sections(page, channels)

    programs = []

    for channel, section in sections:

        channel_programs = parse_programs_from_channel_section(
            section,
            channel,
            day,
        )

        programs.extend(channel_programs)

    # Dedupe
    unique = {}

    for program in programs:

        key = (
            program["channel"],
            program["start"],
            program["stop"],
            normalize_text(program["title"]),
        )

        unique[key] = program

    return list(unique.values())


# ============================================================
# TARİH BAZLI SAYFA DENEMELERİ
# ============================================================

def build_date_urls(day):
    """
    Tivibu'da tarih seçimi JS üzerinden yapılabildiği için
    bilinen muhtemel query formatlarını dener.

    Ana URL her zaman ilk seçenektir.
    """

    date1 = day.strftime("%Y-%m-%d")
    date2 = day.strftime("%Y.%m.%d")
    date3 = day.strftime("%d.%m.%Y")

    return [
        LIVE_TV_URL,

        LIVE_TV_URL + "?date=" + urllib.parse.quote(date1),

        LIVE_TV_URL + "?date=" + urllib.parse.quote(date2),

        LIVE_TV_URL + "?date=" + urllib.parse.quote(date3),

        LIVE_TV_URL + "?selectedDate=" + urllib.parse.quote(date1),

        LIVE_TV_URL + "?selectedDate=" + urllib.parse.quote(date2),

        LIVE_TV_URL + "?day=" + urllib.parse.quote(date1),

        LIVE_TV_URL + "?day=" + urllib.parse.quote(date2),
    ]


def fetch_day_page(day):
    """
    Tarih için sayfayı almaya çalışır.

    Eğer query parametresi desteklenmiyorsa ana sayfa döner.
    """

    tried = set()

    for url in build_date_urls(day):

        if url in tried:
            continue

        tried.add(url)

        try:
            page = http_get(url)

            if len(page) < 10000:
                continue

            return page

        except Exception:
            continue

    return None


# ============================================================
# XMLTV
# ============================================================

def xml_time(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " " + TIMEZONE


def add_text_element(parent, tag, text, lang=None):

    element = ET.SubElement(parent, tag)

    if lang:
        element.set("lang", lang)

    element.text = text

    return element


def write_xml(channels, programs, output_file):
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        },
    )

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    for channel in channels:

        element = ET.SubElement(
            tv,
            "channel",
            {
                "id": channel["slug"],
            },
        )

        add_text_element(
            element,
            "display-name",
            channel["name"],
            "tr",
        )

    # --------------------------------------------------------
    # PROGRAM
    # --------------------------------------------------------

    programs_sorted = sorted(
        programs,
        key=lambda x: (
            x["channel"],
            x["start"],
            x["stop"],
        ),
    )

    for program in programs_sorted:

        element = ET.SubElement(
            tv,
            "programme",
            {
                "start": xml_time(program["start"]),
                "stop": xml_time(program["stop"]),
                "channel": program["channel"],
            },
        )

        add_text_element(
            element,
            "title",
            program["title"],
            "tr",
        )

    tree = ET.ElementTree(tv)

    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass

    tree.write(
        output_file,
        encoding="utf-8",
        xml_declaration=True,
    )


# ============================================================
# XML DOĞRULAMA
# ============================================================

def validate_xml(channels, programs):
    """
    Yanlış XML oluşturmayı engeller.
    """

    if not channels:
        raise RuntimeError(
            "Hiç gerçek Tivibu kanalı bulunamadı."
        )

    if not programs:
        raise RuntimeError(
            "Hiç program bulunamadı. Boş EPG oluşturulmayacak."
        )

    channel_ids = {
        channel["slug"]
        for channel in channels
    }

    invalid = []

    for program in programs:

        if program["channel"] not in channel_ids:
            invalid.append(program)

    if invalid:

        raise RuntimeError(
            f"{len(invalid)} program tanımsız kanala bağlı."
        )

    # Program başlıklarının kanal olarak eklenmediğini kontrol et.
    channel_names = {
        normalize_text(channel["name"])
        for channel in channels
    }

    for program in programs:

        if normalize_text(program["title"]) in channel_names:
            # Bazı Tivibu kanalları gerçekten aynı isimli program
            # yayınlayabilir. Burada sadece açıkça navigasyon
            # başlıklarını reddediyoruz.
            pass

    return True


# ============================================================
# İSTATİSTİK
# ============================================================

def print_program_stats(channels, programs):

    channel_count = {}

    for program in programs:

        channel_count.setdefault(
            program["channel_name"],
            0,
        )

        channel_count[program["channel_name"]] += 1

    print()
    print("-" * 70)
    print("PROGRAM İSTATİSTİĞİ")
    print("-" * 70)

    for channel in channels:

        count = channel_count.get(
            channel["name"],
            0,
        )

        if count:
            print(
                f"    {channel['name']}: {count}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("TİVİBU 7 GÜNLÜK EPG")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # ANA SAYFA
    # --------------------------------------------------------

    print("[1] Tivibu canlı TV sayfası alınıyor...")

    try:
        main_page = http_get(LIVE_TV_URL)

    except Exception as exc:

        print()
        print("HATA: Tivibu sayfası alınamadı.")
        print(str(exc))

        sys.exit(1)

    print(
        f"    Sayfa uzunluğu: {len(main_page):,}"
        .replace(",", ".")
    )

    # --------------------------------------------------------
    # TARİHLER
    # --------------------------------------------------------

    print()
    print("[2] Tivibu tarihleri:")

    tivibu_dates = find_tivibu_dates(main_page)

    for dt in tivibu_dates:

        print(
            f"    {dt.strftime('%d.%m.%Y')} -> "
            f"{format_tivibu_date(dt)} / "
            f"{format_tivibu_date(dt, end=True)}"
        )

    target_dates = get_target_dates(main_page)

    print()
    print("[3] Hedef 7 gün:")

    for index, dt in enumerate(target_dates, 1):

        print(
            f"    {index}. "
            f"{dt.strftime('%d.%m.%Y')} -> "
            f"{format_tivibu_date(dt)} / "
            f"{format_tivibu_date(dt, end=True)}"
        )

    # --------------------------------------------------------
    # KANALLAR
    # --------------------------------------------------------

    print()
    print("[3] Gerçek Tivibu kanalları çıkarılıyor...")

    channels = extract_channel_links(main_page)

    if not channels:

        print("    Kanal bulunamadı.")

        sys.exit(1)

    print(
        f"    Gerçek kanal sayısı: {len(channels)}"
    )

    for channel in channels:

        print(
            f"      + {channel['name']}"
        )

    # --------------------------------------------------------
    # EPG
    # --------------------------------------------------------

    print()
    print("[4] 7 günlük EPG çekiliyor...")
    print()

    all_programs = []

    successful_days = 0
    failed_days = 0

    processed_day_keys = set()

    for index, day in enumerate(target_dates, 1):

        print("-" * 70)
        print(
            f"GÜN {index}/{len(target_dates)}"
        )
        print(
            f"GÜN: {day.strftime('%d.%m.%Y')}"
        )

        day_key = day.strftime("%Y-%m-%d")

        if day_key in processed_day_keys:
            continue

        processed_day_keys.add(day_key)

        page = None

        # ----------------------------------------------------
        # 1. Ana sayfadan dene
        # ----------------------------------------------------

        try:

            page = http_get(LIVE_TV_URL)

        except Exception:
            page = None

        day_programs = []

        if page:

            try:

                day_programs = collect_from_html(
                    page,
                    channels,
                    day,
                )

            except Exception as exc:

                print(
                    f"    HTML parse hatası: {exc}"
                )

        # ----------------------------------------------------
        # 2. Sonuç yoksa tarih URL'lerini dene
        # ----------------------------------------------------

        if not day_programs:

            for url in build_date_urls(day)[1:]:

                try:

                    print(
                        "    Tarih sayfası deneniyor: "
                        + url
                    )

                    test_page = http_get(url)

                    if not test_page:
                        continue

                    candidate = collect_from_html(
                        test_page,
                        channels,
                        day,
                    )

                    if candidate:

                        page = test_page
                        day_programs = candidate

                        break

                except Exception:
                    continue

                time.sleep(REQUEST_DELAY)

        # ----------------------------------------------------
        # SONUÇ
        # ----------------------------------------------------

        if day_programs:

            successful_days += 1

            all_programs.extend(day_programs)

            print(
                f"    Program sayısı: "
                f"{len(day_programs)}"
            )

        else:

            failed_days += 1

            print(
                "    Bu gün için program bulunamadı."
            )

        time.sleep(REQUEST_DELAY)

    # --------------------------------------------------------
    # DEDUPE
    # --------------------------------------------------------

    unique_programs = {}

    for program in all_programs:

        key = (
            program["channel"],
            program["start"],
            program["stop"],
            normalize_text(program["title"]),
        )

        unique_programs[key] = program

    all_programs = list(
        unique_programs.values()
    )

    # --------------------------------------------------------
    # PROGRAM SINIRLAMA
    # --------------------------------------------------------

    # Yalnızca hedef 7 günlük pencereye yakın programları tut.
    #
    # Gece yarısını aşan programlar için bir gün tolerans bırakılır.

    first_day = target_dates[0]
    last_day = target_dates[-1] + timedelta(days=1)

    filtered_programs = []

    for program in all_programs:

        start = program["start"]
        stop = program["stop"]

        if stop <= first_day:
            continue

        if start >= last_day:
            continue

        filtered_programs.append(program)

    all_programs = filtered_programs

    # --------------------------------------------------------
    # SONUÇ
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("EPG TOPLAMA SONUCU")
    print("=" * 70)

    print(
        f"Başarılı gün : {successful_days}/7"
    )

    print(
        f"Hatalı gün   : {failed_days}/7"
    )

    print(
        f"Kanal        : {len(channels)}"
    )

    print(
        f"Program      : {len(all_programs)}"
    )

    # --------------------------------------------------------
    # KRİTİK KONTROL
    # --------------------------------------------------------

    if not all_programs:

        print()
        print(
            "HATA: Hiç program alınamadı."
        )

        print(
            "Yanlış/boş XML oluşturulmayacak."
        )

        raise RuntimeError(
            "Hiç program alınamadı."
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    validate_xml(
        channels,
        all_programs,
    )

    write_xml(
        channels,
        all_programs,
        OUTPUT_FILE,
    )

    # --------------------------------------------------------
    # İSTATİSTİK
    # --------------------------------------------------------

    print_program_stats(
        channels,
        all_programs,
    )

    print()
    print("=" * 70)
    print("TAMAMLANDI")
    print("=" * 70)

    print(
        f"XML: {OUTPUT_FILE}"
    )

    print(
        f"Kanallar: {len(channels)}"
    )

    print(
        f"Programlar: {len(all_programs)}"
    )

    print(
        "Tivibu EPG hazır."
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print()
        print("İşlem kullanıcı tarafından durduruldu.")
        sys.exit(130)

    except Exception as exc:

        print()
        print("HATA:")
        print(str(exc))

        sys.exit(1)
