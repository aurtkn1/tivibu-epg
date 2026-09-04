import re
import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from xml.etree.ElementTree import Element, SubElement, ElementTree

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS = 7

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# KATEGORİLER
# ==============================================================

CATEGORY_URLS = [
    f"{BASE_URL}/canli-tv",
    f"{BASE_URL}/canli-tv/ulusal",
    f"{BASE_URL}/canli-tv/muzik",
    f"{BASE_URL}/canli-tv/yasam-stil",
    f"{BASE_URL}/canli-tv/dizi",
    f"{BASE_URL}/canli-tv/spor",
    f"{BASE_URL}/canli-tv/haber",
    f"{BASE_URL}/canli-tv/belgesel",
    f"{BASE_URL}/canli-tv/cocuk",
    f"{BASE_URL}/canli-tv/sinema",
    f"{BASE_URL}/canli-tv/global",
    f"{BASE_URL}/canli-tv/diger",
]


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


def normalize_key(value):
    value = clean_text(value).lower()

    replacements = {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
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


def valid_channel(name):
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


def valid_title(title):
    title = clean_text(title)

    if not title:
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

    if len(title) > 300:
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
# PROGRAM SATIRI
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


def parse_program_line(
    text,
    target_date
):

    text = clean_text(
        text
    )

    match = PROGRAM_RE.match(
        text
    )

    if not match:
        return None

    title = clean_text(
        match.group("title")
    )

    if not valid_title(title):
        return None

    start = make_datetime(
        target_date,
        int(match.group("sh")),
        int(match.group("sm"))
    )

    end = make_datetime(
        target_date,
        int(match.group("eh")),
        int(match.group("em"))
    )

    if end <= start:
        end += timedelta(
            days=1
        )

    return {
        "title": title,
        "start": start,
        "end": end
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

    for i in range(count):

        try:

            element = locator.nth(i)

            href = element.get_attribute(
                "href"
            )

            name = clean_text(
                element.inner_text()
            )

            if not href or not name:
                continue

            # Sadece kanal linkleri
            if not re.search(
                r"/kanallar/[^/?#]+/?$",
                href.split("?", 1)[0]
            ):
                continue

            if not valid_channel(name):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            href = href.split(
                "?",
                1
            )[0].rstrip("/")

            key = normalize_key(
                name
            )

            if key not in channels:

                channels[key] = {
                    "name": name,
                    "url": href,
                    "id": make_xml_id(name),
                }

        except Exception:
            continue

    return channels


# ==============================================================
# KANAL URL MAP
# ==============================================================

def build_channel_url_map(channels):

    result = {}

    for channel in channels.values():

        result[
            channel["url"].lower().rstrip("/")
        ] = channel

    return result


# ==============================================================
# PROGRAM LİNKLERİNİ BUL
# ==============================================================

def collect_program_links(page):

    result = []

    locator = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = locator.count()

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

            # Program satırı işareti
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

            result.append({
                "url": href,
                "text": text,
            })

        except Exception:
            continue

    return result


# ==============================================================
# PROGRAMLARI ÇEK
# ==============================================================

def extract_programs(
    page,
    target_date,
    channel_url_map
):

    result = []

    links = collect_program_links(
        page
    )

    seen = set()

    for item in links:

        channel = channel_url_map.get(
            item["url"]
        )

        if channel is None:
            continue

        parsed = parse_program_line(
            item["text"],
            target_date
        )

        if parsed is None:
            continue

        # ------------------------------------------------------
        # Sadece SEÇİLEN GÜNÜN başlangıçlarını al.
        #
        # 04.09 seçiliyse 05.09 00:00-02:30 gibi taşan
        # programlar 04.09 gününe yanlışlıkla yazılmaz.
        # ------------------------------------------------------

        if parsed["start"].date() != target_date.date():
            continue

        key = (
            channel["name"].upper(),
            parsed["start"],
            parsed["title"].upper(),
        )

        if key in seen:
            continue

        seen.add(key)

        result.append({
            "channel": channel["name"],
            "title": parsed["title"],
            "start": parsed["start"],
            "end": parsed["end"],
        })

    result.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return result


# ==============================================================
# YARIN BUTONUNU BUL
# ==============================================================

def find_yarin_button(page):

    candidates = []

    # Butonlar
    try:

        locator = page.locator(
            "button, [role='button'], a"
        )

        count = locator.count()

        for i in range(count):

            try:

                element = locator.nth(i)

                text = clean_text(
                    element.inner_text()
                )

                if text == "Yarın":

                    if element.is_visible():

                        candidates.append(
                            element
                        )

            except Exception:
                continue

    except Exception:
        pass

    if candidates:
        return candidates[0]

    return None


# ==============================================================
# YARIN'A GEÇ
# ==============================================================

def click_tomorrow(page):

    before = collect_program_links(
        page
    )

    before_signature = "|".join(
        item["text"]
        for item in before[:150]
    )

    button = find_yarin_button(
        page
    )

    if button is None:

        # JS fallback
        try:

            clicked = page.evaluate(
                """
                () => {

                    const elements = [
                        ...document.querySelectorAll(
                            "button"
                        ),
                        ...document.querySelectorAll(
                            "[role='button']"
                        ),
                        ...document.querySelectorAll(
                            "a"
                        )
                    ];

                    for (const el of elements) {

                        const text =
                            (el.innerText || "")
                            .replace(/\\s+/g, " ")
                            .trim();

                        if (text === "Yarın") {

                            el.scrollIntoView({
                                block: "center"
                            });

                            el.click();

                            return true;
                        }
                    }

                    return false;
                }
                """
            )

            if not clicked:
                return False

        except Exception:
            return False

    else:

        try:

            button.scroll_into_view_if_needed(
                timeout=2000
            )

            button.click(
                timeout=3000
            )

        except Exception:

            try:

                button.click(
                    force=True,
                    timeout=3000
                )

            except Exception:
                return False

    # ----------------------------------------------------------
    # Program listesinin değişmesini bekle.
    # ----------------------------------------------------------

    for _ in range(30):

        time.sleep(
            0.25
        )

        current = collect_program_links(
            page
        )

        current_signature = "|".join(
            item["text"]
            for item in current[:150]
        )

        if (
            current_signature
            and
            current_signature != before_signature
        ):
            return True

    # Buton çalışmış olabilir; yine de kısa kontrol.
    time.sleep(
        1
    )

    return True


# ==============================================================
# SAYFAYI AÇ
# ==============================================================

def open_page(
    page,
    url
):

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except PlaywrightTimeoutError:
        pass

    except Exception:
        pass

    page.wait_for_timeout(
        1800
    )


# ==============================================================
# TEK KATEGORİ 7 GÜN
# ==============================================================

def scrape_category(
    page,
    category_url,
    channels
):

    print()
    print("=" * 70)
    print(
        f"SAYFA: {category_url}"
    )
    print("=" * 70)

    open_page(
        page,
        category_url
    )

    channel_url_map = (
        build_channel_url_map(
            channels
        )
    )

    result = []

    # ----------------------------------------------------------
    # BUGÜN
    # ----------------------------------------------------------

    today_programs = extract_programs(
        page,
        TODAY,
        channel_url_map
    )

    print(
        f"  {TODAY.strftime('%d.%m.%Y')}: "
        f"{len(today_programs)} program"
    )

    result.extend(
        today_programs
    )

    # ----------------------------------------------------------
    # SONRAKİ 6 GÜN
    #
    # ÖNEMLİ:
    # Exact "07.09.2026" DOM elementi aranmıyor.
    #
    # Her seferinde "Yarın" butonuna basılıyor.
    #
    # Tivibu sayfasında "Dün / Bugün / Yarın" ve ilerleyen
    # tarihler aynı tarih çubuğunda bulunuyor.
    # ----------------------------------------------------------

    for day_index in range(
        1,
        DAYS
    ):

        target_date = (
            TODAY
            + timedelta(
                days=day_index
            )
        )

        print(
            f"  {target_date.strftime('%d.%m.%Y')}: "
            "Yarın'a geçiliyor..."
        )

        changed = click_tomorrow(
            page
        )

        if not changed:

            print(
                "    Yarın butonuyla geçilemedi."
            )

            # Sayfayı temizden aç.
            open_page(
                page,
                category_url
            )

            # Gerekirse birden fazla Yarın.
            for _ in range(
                day_index
            ):

                if not click_tomorrow(
                    page
                ):
                    break

        programs = extract_programs(
            page,
            target_date,
            channel_url_map
        )

        print(
            f"    Program: "
            f"{len(programs)}"
        )

        result.extend(
            programs
        )

    return result


# ==============================================================
# DUPLICATE
# ==============================================================

def deduplicate_programs(
    programs
):

    result = []
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

        seen.add(
            key
        )

        result.append(
            program
        )

    result.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return result


# ==============================================================
# END TIME
# ==============================================================

def finalize_end_times(
    programs
):

    grouped = defaultdict(list)

    for program in programs:

        grouped[
            normalize_key(
                program["channel"]
            )
        ].append(
            program
        )

    result = []

    for _, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(
            items
        ):

            start = program["start"]
            end = program.get(
                "end"
            )

            if end is None:

                if i + 1 < len(items):

                    end = items[
                        i + 1
                    ]["start"]

                else:

                    end = (
                        start
                        + timedelta(
                            minutes=30
                        )
                    )

            if end <= start:

                end = (
                    start
                    + timedelta(
                        minutes=30
                    )
                )

            # Aşırı uzun kayıtları koruma.
            if (
                end - start
                > timedelta(hours=12)
            ):

                end = (
                    start
                    + timedelta(
                        hours=3
                    )
                )

            result.append({
                "channel": program["channel"],
                "title": program["title"],
                "start": start,
                "end": end,
            })

    result.sort(
        key=lambda x: (
            x["start"],
            x["channel"]
        )
    )

    return result


# ==============================================================
# XML
# ==============================================================

def write_xml(
    channels,
    programs
):

    used = {
        normalize_key(
            p["channel"]
        )
        for p in programs
    }

    final_channels = []

    for key, channel in channels.items():

        if key not in used:
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
                "Tivibu 7 Günlük EPG",
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

        display_name = SubElement(
            element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display_name.text = channel["name"]

    channel_ids = {
        normalize_key(
            c["name"]
        ): c["id"]
        for c in final_channels
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

        element = SubElement(
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
            element,
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

    global TODAY

    TODAY = datetime.now(
        TURKEY_TZ
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    LAST_DAY = (
        TODAY
        + timedelta(
            days=DAYS - 1
        )
    )

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{TODAY.strftime('%d.%m.%Y')} -> "
        f"{LAST_DAY.strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    # ==========================================================
    # KANALLARI TEK SEFER AL
    # ==========================================================

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
                "height": 1080
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            10000
        )

        print(
            "Kanal listesi alınıyor..."
        )

        open_page(
            page,
            LIVE_URL
        )

        channels = collect_channels(
            page
        )

        browser.close()

    print()
    print(
        f"Bulunan kanal: "
        f"{len(channels)}"
    )

    # ==========================================================
    # KATEGORİLERİ SIRALI TARA
    # ==============================================================

    all_programs = []

    for category_url in CATEGORY_URLS:

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
                    "height": 1080
                }
            )

            page = context.new_page()

            page.set_default_timeout(
                10000
            )

            programs = scrape_category(
                page,
                category_url,
                channels
            )

            all_programs.extend(
                programs
            )

            browser.close()

    # ==========================================================
    # DUPLICATE
    # ==============================================================

    all_programs = deduplicate_programs(
        all_programs
    )

    # ==========================================================
    # END TIME
    # ==============================================================

    all_programs = finalize_end_times(
        all_programs
    )

    # ==========================================================
    # GÜN KONTROLÜ
    # ==============================================================

    day_counts = defaultdict(
        int
    )

    for program in all_programs:

        day_counts[
            program["start"].strftime(
                "%d.%m.%Y"
            )
        ] += 1

    print()
    print("=" * 70)
    print("GÜN KONTROLÜ")
    print("=" * 70)

    for day_index in range(
        DAYS
    ):

        date = (
            TODAY
            + timedelta(
                days=day_index
            )
        ).strftime(
            "%d.%m.%Y"
        )

        print(
            f"{date}: "
            f"{day_counts.get(date, 0)} program"
        )

    # ==========================================================
    # KANAL KONTROLÜ
    # ==============================================================

    channel_counts = defaultdict(
        int
    )

    for program in all_programs:

        channel_counts[
            program["channel"]
        ] += 1

    print()
    print("=" * 70)
    print("KANAL KONTROLÜ")
    print("=" * 70)

    for channel_name in sorted(
        channel_counts
    ):

        print(
            f"{channel_name}: "
            f"{channel_counts[channel_name]}"
        )

    # ==========================================================
    # XML
    # ==============================================================

    xml_channel_count = write_xml(
        channels,
        all_programs
    )

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
        f"{len(all_programs)}"
    )

    print(
        f"Dosya: "
        f"{OUTPUT_FILE}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
