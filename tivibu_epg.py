import re
import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from xml.etree.ElementTree import Element, SubElement, ElementTree
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

DAYS = 7
WORKERS = 8

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# KANAL KATEGORİLERİ
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

    value = re.sub(
        r"[^a-z0-9]+",
        "",
        value
    )

    return value


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
        tzinfo=TURKEY_TZ,
    )


def xml_datetime(dt):
    return dt.strftime("%Y%m%d%H%M%S") + " +0300"


# ==============================================================
# PROGRAM SATIRI
#
# Örnek:
#
# Kanal D Ana Haber Aktüalite - 19:00 → 20:00 Canlı
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
    re.IGNORECASE,
)


def parse_program_text(text, target_date):
    text = clean_text(text)

    match = PROGRAM_RE.match(text)

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
        int(match.group("sm")),
    )

    end = make_datetime(
        target_date,
        int(match.group("eh")),
        int(match.group("em")),
    )

    if end <= start:
        end += timedelta(
            days=1
        )

    return {
        "title": title,
        "start": start,
        "end": end,
    }


# ==============================================================
# KANALLARI AL
# ==============================================================

def collect_channels(page):

    result = {}

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

            if not valid_channel(text):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            key = normalize_key(
                href
            )

            if not key:
                key = normalize_key(
                    text
                )

            if key in result:
                continue

            result[key] = {
                "name": text,
                "url": href,
                "id": make_xml_id(text),
                "url_key": key,
            }

        except Exception:
            continue

    return result


# ==============================================================
# PROGRAM LINKLERİ
#
# ÖNEMLİ:
#
# Tivibu'nun güncel sayfasında program linklerinin href'i
# doğrudan ilgili /kanallar/... sayfasına gidiyor.
#
# Örneğin:
#
# <a href="/kanallar/kanal-d">
#     Kanal D Ana Haber Aktüalite - 19:00 → 20:00 Canlı
# </a>
#
# Dolayısıyla /rv? ve ch ID bağımlılığı yok.
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

            if "→" not in text:
                continue

            if " - " not in text:
                continue

            if not re.search(
                r"\d{1,2}:\d{2}\s*→\s*\d{1,2}:\d{2}",
                text
            ):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            channel_url = href.split(
                "?",
                1
            )[0].rstrip("/")

            result.append({
                "href": channel_url,
                "text": text,
            })

        except Exception:
            continue

    return result


# ==============================================================
# URL -> KANAL
# ==============================================================

def build_channel_url_map(channels):

    result = {}

    for channel in channels.values():

        url = (
            channel["url"]
            .split("?", 1)[0]
            .rstrip("/")
            .lower()
        )

        result[url] = channel

    return result


# ==============================================================
# PROGRAMLARI ÇIKAR
# ==============================================================

def extract_programs(
    page,
    target_date,
    channel_url_map
):

    programs = []

    links = collect_program_links(
        page
    )

    for item in links:

        channel = channel_url_map.get(
            item["href"].lower()
        )

        if channel is None:
            continue

        parsed = parse_program_text(
            item["text"],
            target_date
        )

        if not parsed:
            continue

        programs.append({
            "channel": channel["name"],
            "title": parsed["title"],
            "start": parsed["start"],
            "end": parsed["end"],
        })

    return programs


# ==============================================================
# PROGRAM SAYFA HASH
#
# Tarih tıklanınca gerçekten programların değiştiğini
# doğrulamak için kullanılır.
# ==============================================================

def schedule_signature(page):

    try:

        texts = page.evaluate(
            """
            () => {

                const result = [];

                const elements = [
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll("div"),
                    ...document.querySelectorAll("span"),
                    ...document.querySelectorAll("li")
                ];

                for (const el of elements) {

                    const text =
                        (el.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (!text) {
                        continue;
                    }

                    if (
                        /\\d{1,2}:\\d{2}\\s*→\\s*\\d{1,2}:\\d{2}/.test(text)
                    ) {

                        if (text.length < 350) {
                            result.push(text);
                        }
                    }
                }

                return result.slice(0, 1000);
            }
            """
        )

        return "|".join(
            texts
        )

    except Exception:

        return ""


# ==============================================================
# TARİH BUTONUNU BUL
# ==============================================================

def find_date_element(page, target):

    candidates = page.locator(
        "button, a, [role='button']"
    )

    count = candidates.count()

    for i in range(count):

        try:

            element = candidates.nth(i)

            text = clean_text(
                element.inner_text()
            )

            if text != target:
                continue

            box = element.bounding_box()

            if box is None:
                continue

            return element

        except Exception:
            continue

    return None


# ==============================================================
# TARİH DEĞİŞTİR
# ==============================================================

def select_date(page, target_date):

    target = target_date.strftime(
        "%d.%m.%Y"
    )

    before = schedule_signature(
        page
    )

    element = find_date_element(
        page,
        target
    )

    if element is None:
        return False

    # ----------------------------------------------------------
    # 1. Normal click
    # ----------------------------------------------------------

    try:

        element.scroll_into_view_if_needed(
            timeout=2000
        )

        element.click(
            timeout=3000
        )

    except Exception:

        try:

            element.click(
                force=True,
                timeout=3000
            )

        except Exception:
            pass

    # ----------------------------------------------------------
    # Gerçek değişikliği bekle
    # ----------------------------------------------------------

    for _ in range(20):

        time.sleep(
            0.25
        )

        after = schedule_signature(
            page
        )

        if (
            after
            and after != before
        ):
            return True

    # ----------------------------------------------------------
    # 2. JS click
    # ----------------------------------------------------------

    try:

        page.evaluate(
            """
            target => {

                const elements = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll(
                        "[role='button']"
                    )
                ];

                for (const el of elements) {

                    const text =
                        (el.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (text === target) {

                        el.scrollIntoView({
                            block: "center"
                        });

                        el.dispatchEvent(
                            new MouseEvent(
                                "click",
                                {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                }
                            )
                        );

                        return true;
                    }
                }

                return false;
            }
            """,
            target
        )

    except Exception:
        pass

    for _ in range(20):

        time.sleep(
            0.25
        )

        after = schedule_signature(
            page
        )

        if (
            after
            and after != before
        ):
            return True

    # ----------------------------------------------------------
    # 3. Enter
    # ----------------------------------------------------------

    try:

        element.focus()

        page.keyboard.press(
            "Enter"
        )

    except Exception:
        pass

    for _ in range(20):

        time.sleep(
            0.25
        )

        after = schedule_signature(
            page
        )

        if (
            after
            and after != before
        ):
            return True

    return False


# ==============================================================
# TEK KATEGORİ
# ==============================================================

def process_category(
    category_url,
    channels
):

    local_programs = []

    with sync_playwright() as p:

        browser = None

        try:

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
                8000
            )

            print()
            print("=" * 70)
            print(
                f"KATEGORİ: {category_url}"
            )
            print("=" * 70)

            try:

                page.goto(
                    category_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

            except PlaywrightTimeoutError:

                print(
                    "Sayfa yükleme zaman aşımı."
                )

            page.wait_for_timeout(
                1800
            )

            # --------------------------------------------------
            # URL -> KANAL
            # --------------------------------------------------

            channel_url_map = (
                build_channel_url_map(
                    channels
                )
            )

            # --------------------------------------------------
            # Program var mı?
            # --------------------------------------------------

            first_programs = extract_programs(
                page,
                TODAY,
                channel_url_map
            )

            print(
                f"İlk gün programı: "
                f"{len(first_programs)}"
            )

            local_programs.extend(
                first_programs
            )

            # --------------------------------------------------
            # Kalan 6 gün
            # --------------------------------------------------

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
                    f"  Tarih: "
                    f"{target_date.strftime('%d.%m.%Y')}"
                )

                selected = select_date(
                    page,
                    target_date
                )

                if not selected:

                    print(
                        "    Tarih değişmedi, "
                        "ikinci yöntem deneniyor..."
                    )

                    # Sayfa yenile ve tekrar dene.
                    try:

                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=60000
                        )

                        page.wait_for_timeout(
                            1500
                        )

                        selected = select_date(
                            page,
                            target_date
                        )

                    except Exception:
                        selected = False

                if not selected:

                    print(
                        "    GÜN ALINAMADI"
                    )

                    continue

                programs = extract_programs(
                    page,
                    target_date,
                    channel_url_map
                )

                print(
                    f"    Program: "
                    f"{len(programs)}"
                )

                local_programs.extend(
                    programs
                )

        except Exception as e:

            print(
                f"KATEGORİ HATASI: "
                f"{e}"
            )

        finally:

            if browser:

                try:
                    browser.close()
                except Exception:
                    pass

    return local_programs


# ==============================================================
# DUPLICATE / PROGRAM BİTİŞLERİ
# ==============================================================

def finalize_programs(programs):

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

    # ----------------------------------------------------------
    # Kanal bazında sırala
    # ----------------------------------------------------------

    grouped = defaultdict(list)

    for program in unique:

        grouped[
            normalize_key(
                program["channel"]
            )
        ].append(
            program
        )

    result = []

    for channel_key, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(items):

            start = program["start"]
            end = program.get("end")

            if end is None:

                if i + 1 < len(items):

                    end = items[
                        i + 1
                    ]["start"]

                else:

                    end = start + timedelta(
                        minutes=30
                    )

            if end <= start:

                end = start + timedelta(
                    minutes=30
                )

            if end - start > timedelta(
                hours=12
            ):

                end = start + timedelta(
                    hours=3
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
            normalize_key(
                x["channel"]
            )
        )
    )

    return result


# ==============================================================
# XML YAZ
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
                "Tivibu 7 Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # CHANNEL
    # ----------------------------------------------------------

    for channel in final_channels:

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display_name = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display_name.text = channel["name"]

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

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

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

    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{TODAY.strftime('%d.%m.%Y')} -> "
        f"{LAST_DAY.strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    # ----------------------------------------------------------
    # Kanal listesini bir kez oluştur
    # ----------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
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

        print(
            "Tivibu kanal listesi alınıyor..."
        )

        try:

            page.goto(
                LIVE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(
            2000
        )

        channels = collect_channels(
            page
        )

        browser.close()

    print()
    print(
        f"Toplam kanal: "
        f"{len(channels)}"
    )

    # ----------------------------------------------------------
    # Kategori sayfalarını paralel işle
    # ----------------------------------------------------------

    all_programs = []

    print()
    print(
        f"{len(CATEGORY_URLS)} kategori taranıyor..."
    )

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                process_category,
                category_url,
                channels
            ):
                category_url
            for category_url in CATEGORY_URLS
        }

        completed = 0

        for future in as_completed(
            futures
        ):

            category_url = futures[
                future
            ]

            try:

                programs = future.result()

                all_programs.extend(
                    programs
                )

                completed += 1

                print()
                print(
                    f"[{completed}/{len(CATEGORY_URLS)}] "
                    f"TAMAMLANDI: "
                    f"{category_url}"
                )

            except Exception as e:

                completed += 1

                print()
                print(
                    f"[{completed}/{len(CATEGORY_URLS)}] "
                    f"HATA: "
                    f"{category_url} -> {e}"
                )

    # ----------------------------------------------------------
    # PROGRAMLARI TEMİZLE
    # ----------------------------------------------------------

    programs = finalize_programs(
        all_programs
    )

    # ----------------------------------------------------------
    # PROGRAM SAYILARI
    # ----------------------------------------------------------

    counts = defaultdict(
        int
    )

    for program in programs:

        counts[
            program["channel"]
        ] += 1

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    channel_count = write_xml(
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
        f"{channel_count}"
    )

    print(
        f"XML program: "
        f"{len(programs)}"
    )

    print()
    print(
        "Kanal program sayıları:"
    )

    for channel_name in sorted(
        counts
    ):

        print(
            f"  {channel_name}: "
            f"{counts[channel_name]}"
        )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
