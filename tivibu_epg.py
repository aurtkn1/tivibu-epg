import re
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree.ElementTree import Element, SubElement, ElementTree

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# TEMEL YARDIMCILAR
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_name(value):
    return clean_text(value)


def normalize_key(value):
    value = clean_text(value).lower()

    replacements = {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
        "İ": "i",
        "Ş": "s",
        "Ğ": "g",
        "Ü": "u",
        "Ö": "o",
        "Ç": "c",
        "’": "",
        "'": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return re.sub(
        r"[^a-z0-9]+",
        "",
        value
    )


def make_xml_id(name):
    value = clean_text(name).lower()

    replacements = {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
        "’": "_",
        "'": "_",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    )

    return value.strip("_")


def valid_channel_name(name):
    name = clean_text(name)

    if not name:
        return False

    bad = {
        "BENİM KANALIM",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TİVİBU CANLI TV",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "TİVİBU SPOR CANLI İZLE",
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "GİRİŞ YAP",
        "ÜYE OL",
    }

    if name.upper() in bad:
        return False

    if len(name) > 100:
        return False

    return True


def valid_program_title(title):
    title = clean_text(title)

    if not title:
        return False

    if len(title) > 300:
        return False

    bad = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "GİRİŞ YAP",
        "ÜYE OL",
    }

    if title.upper() in bad:
        return False

    return True


def make_datetime(date_obj, hour, minute):
    return datetime(
        date_obj.year,
        date_obj.month,
        date_obj.day,
        hour,
        minute,
        tzinfo=TURKEY_TZ
    )


def xml_datetime(dt):
    return (
        dt.strftime("%Y%m%d%H%M%S")
        + " +0300"
    )


# ==============================================================
# PROGRAM SATIRINI PARSE ET
#
# Örnek:
#
# Kanal D Ana Haber Aktüalite - 19:00 → 20:00 Canlı
#
# Sonuç:
# başlık      = Kanal D Ana Haber
# başlangıç   = 19:00
# bitiş       = 20:00
# ==============================================================

PROGRAM_RE = re.compile(
    r"^(?P<title>.*?)"
    r"\s+"
    r"(?P<category>"
    r"Film|"
    r"Dizi|"
    r"Yaşam|"
    r"Spor Programı|"
    r"Spor|"
    r"Haber|"
    r"Belgesel|"
    r"Çocuk|"
    r"Müzik|"
    r"Eğlence|"
    r"Aktüalite|"
    r"Diğer"
    r")"
    r"\s*-\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})"
    r"\s*→\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
    r"(?:\s+Canlı)?$",
    re.IGNORECASE
)


def parse_program_line(text, base_date):

    text = clean_text(text)

    match = PROGRAM_RE.match(text)

    if not match:
        return None

    title = clean_text(
        match.group("title")
    )

    if not valid_program_title(title):
        return None

    start_hour = int(
        match.group("sh")
    )

    start_minute = int(
        match.group("sm")
    )

    end_hour = int(
        match.group("eh")
    )

    end_minute = int(
        match.group("em")
    )

    start = make_datetime(
        base_date,
        start_hour,
        start_minute
    )

    end = make_datetime(
        base_date,
        end_hour,
        end_minute
    )

    # Örneğin:
    # 23:45 -> 00:35
    if end <= start:
        end += timedelta(days=1)

    return {
        "title": title,
        "start": start,
        "end": end,
    }


# ==============================================================
# KANALLARI BUL
# ==============================================================

def collect_channels(page):

    channels = {}

    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

    print(
        f"Kanal linki sayısı: {count}"
    )

    for i in range(count):

        try:

            element = locator.nth(i)

            href = element.get_attribute(
                "href"
            )

            text = clean_text(
                element.inner_text()
            )

            if not href or not text:
                continue

            # Program linklerini kanal listesine alma.
            if "→" in text:
                continue

            if not valid_channel_name(text):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            href = (
                href
                .split("?", 1)[0]
                .rstrip("/")
            )

            if not re.search(
                r"/kanallar/[^/?#]+$",
                href
            ):
                continue

            key = normalize_key(
                text
            )

            if key in channels:
                continue

            channels[key] = {
                "name": text,
                "url": href,
                "id": make_xml_id(text),
            }

        except Exception:
            continue

    return channels


# ==============================================================
# PROGRAM LİNKLERİNİ BUL
#
# ÖNEMLİ:
#
# Tivibu canlı-TV sayfasındaki programlar da
# /kanallar/... URL'sine bağlı.
#
# Bu yüzden /rv veya ch ID kullanmıyoruz.
# ==============================================================

def collect_program_links(page):

    programs = []

    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

    print(
        f"Toplam kanal/program linki: {count}"
    )

    for i in range(count):

        try:

            element = locator.nth(i)

            href = element.get_attribute(
                "href"
            )

            text = clean_text(
                element.inner_text()
            )

            if not href or not text:
                continue

            if "→" not in text:
                continue

            if not re.search(
                r"\d{1,2}:\d{2}\s*→\s*\d{1,2}:\d{2}",
                text
            ):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            href = (
                href
                .split("?", 1)[0]
                .rstrip("/")
                .lower()
            )

            programs.append({
                "url": href,
                "text": text,
            })

        except Exception:
            continue

    return programs


# ==============================================================
# PROGRAMLARI KANALLARA BAĞLA
# ==============================================================

def extract_programs(
    page,
    base_date,
    channels
):

    channel_url_map = {}

    for channel in channels.values():

        channel_url_map[
            channel["url"]
            .lower()
            .rstrip("/")
        ] = channel

    raw_programs = (
        collect_program_links(
            page
        )
    )

    programs = []

    seen = set()

    for item in raw_programs:

        channel = channel_url_map.get(
            item["url"]
        )

        if channel is None:
            continue

        parsed = parse_program_line(
            item["text"],
            base_date
        )

        if parsed is None:
            continue

        key = (
            channel["name"].upper(),
            parsed["start"],
            parsed["title"].upper()
        )

        if key in seen:
            continue

        seen.add(key)

        programs.append({
            "channel": channel["name"],
            "title": parsed["title"],
            "start": parsed["start"],
            "end": parsed["end"],
        })

    programs.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return programs


# ==============================================================
# AYNI KANALDA ÜST ÜSTE PROGRAMLARIN KONTROLÜ
# ==============================================================

def clean_programs(programs):

    unique = []

    seen = set()

    for program in programs:

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            normalize_key(
                program["title"]
            )
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            program
        )

    unique.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return unique


# ==============================================================
# XML
# ==============================================================

def write_xml(
    channels,
    programs
):

    used_channels = {
        normalize_key(
            program["channel"]
        )
        for program in programs
    }

    final_channels = []

    for key, channel in channels.items():

        if key not in used_channels:
            continue

        if channel["name"].upper() == "BENİM KANALIM":
            continue

        if (
            channel["name"].upper()
            == "TİVİBU SPOR CANLI İZLE"
        ):
            continue

        final_channels.append(
            channel
        )

    final_channels.sort(
        key=lambda x: x["name"].upper()
    )

    tv = Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # CHANNEL
    # ----------------------------------------------------------

    for channel in final_channels:

        element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display = SubElement(
            element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = channel["name"]

    channel_ids = {
        normalize_key(
            channel["name"]
        ): channel["id"]
        for channel in final_channels
    }

    # ----------------------------------------------------------
    # PROGRAM
    # ----------------------------------------------------------

    for program in programs:

        channel_id = channel_ids.get(
            normalize_key(
                program["channel"]
            )
        )

        if not channel_id:
            continue

        programme = SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": xml_datetime(
                    program["start"]
                ),
                "stop": xml_datetime(
                    program["end"]
                ),
            }
        )

        title = SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program["title"]

    tree = ElementTree(
        tv
    )

    try:

        import xml.etree.ElementTree as ET

        ET.indent(
            tree,
            space="  "
        )

    except Exception:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    return len(final_channels)


# ==============================================================
# ANA
# ==============================================================

def main():

    # ----------------------------------------------------------
    # TÜRKİYE TARİHİ
    # ----------------------------------------------------------

    today = datetime.now(
        TURKEY_TZ
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    print()
    print("=" * 70)
    print("TIVIBU GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Tarih: "
        f"{today.strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080,
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            10000
        )

        # ------------------------------------------------------
        # TEK SAYFA
        # ------------------------------------------------------

        print(
            "Tivibu açılıyor..."
        )

        try:

            page.goto(
                LIVE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except PlaywrightTimeoutError:

            print(
                "Sayfa yükleme zaman aşımı."
            )

        except Exception as e:

            print(
                f"Sayfa açma hatası: {e}"
            )

        page.wait_for_timeout(
            2500
        )

        # ------------------------------------------------------
        # 1. KANALLAR
        # ------------------------------------------------------

        channels = collect_channels(
            page
        )

        print()
        print(
            f"GERÇEK KANAL: "
            f"{len(channels)}"
        )

        # ------------------------------------------------------
        # 2. BUGÜNÜN PROGRAMLARI
        # ------------------------------------------------------

        programs = extract_programs(
            page,
            today,
            channels
        )

        print()
        print(
            f"ÇEKİLEN PROGRAM: "
            f"{len(programs)}"
        )

        browser.close()

    # ----------------------------------------------------------
    # DUPLICATE
    # ----------------------------------------------------------

    programs = clean_programs(
        programs
    )

    # ----------------------------------------------------------
    # PROGRAM BULUNAN KANALLAR
    # ----------------------------------------------------------

    used_channels = {
        normalize_key(
            program["channel"]
        )
        for program in programs
    }

    # ----------------------------------------------------------
    # KANAL PROGRAM SAYILARI
    # ----------------------------------------------------------

    counts = defaultdict(
        int
    )

    for program in programs:

        counts[
            program["channel"]
        ] += 1

    print()
    print("=" * 70)
    print("KANAL PROGRAM SAYILARI")
    print("=" * 70)

    for channel_name in sorted(
        counts
    ):

        print(
            f"{channel_name}: "
            f"{counts[channel_name]}"
        )

    # ----------------------------------------------------------
    # ÖRNEK KANAL KONTROLÜ
    # ----------------------------------------------------------

    print()
    print("=" * 70)
    print("ÖRNEK KONTROL")
    print("=" * 70)

    for test_channel in [
        "KANAL D",
        "ATV",
        "TRT 1",
        "TRT SPOR",
        "TRT 3 SPOR",
        "CNN TÜRK",
        "TİVİBU SPOR",
        "SİNEMA TV",
    ]:

        matching = [
            p
            for p in programs
            if normalize_key(
                p["channel"]
            )
            == normalize_key(
                test_channel
            )
        ]

        print(
            f"{test_channel}: "
            f"{len(matching)} program"
        )

        for program in matching[:5]:

            print(
                f"    "
                f"{program['start'].strftime('%H:%M')}"
                f" - "
                f"{program['end'].strftime('%H:%M')}"
                f"  "
                f"{program['title']}"
            )

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    xml_channel_count = write_xml(
        channels,
        programs
    )

    # ----------------------------------------------------------
    # SONUÇ
    # ----------------------------------------------------------

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"XML kanal: "
        f"{xml_channel_count}"
    )

    print(
        f"XML program: "
        f"{len(programs)}"
    )

    print(
        f"Dosya: "
        f"{OUTPUT_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
