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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

TURKEY_TZ = ZoneInfo("Europe/Istanbul")


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
        tzinfo=TURKEY_TZ,
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


def parse_program_line(text, target_date):
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
        int(match.group("sm"))
    )

    end = make_datetime(
        target_date,
        int(match.group("eh")),
        int(match.group("em"))
    )

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

            if not valid_channel(name):
                continue

            if href.startswith("/"):
                href = BASE_URL + href

            clean_url = (
                href
                .split("?", 1)[0]
                .rstrip("/")
            )

            if not re.search(
                r"/kanallar/[^/?#]+$",
                clean_url
            ):
                continue

            key = normalize_key(
                name
            )

            if key in channels:
                continue

            channels[key] = {
                "name": name,
                "url": clean_url,
                "id": make_xml_id(name),
            }

        except Exception:
            continue

    return channels


def build_channel_url_map(channels):
    result = {}

    for channel in channels.values():

        result[
            channel["url"].lower().rstrip("/")
        ] = channel

    return result


# ==============================================================
# PROGRAM LİNKLERİ
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


def extract_programs(
    page,
    target_date,
    channel_url_map
):
    programs = []

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
# SAYFANIN MEVCUT PROGRAM İMZASI
# ==============================================================

def schedule_signature(page):

    try:

        values = page.evaluate(
            """
            () => {

                const result = [];

                const elements = [
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll("li"),
                    ...document.querySelectorAll("div"),
                    ...document.querySelectorAll("span")
                ];

                for (const element of elements) {

                    const text =
                        (element.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (!text) {
                        continue;
                    }

                    if (
                        /\\d{1,2}:\\d{2}\\s*→\\s*\\d{1,2}:\\d{2}/
                        .test(text)
                    ) {

                        if (
                            text.length >= 5 &&
                            text.length <= 350
                        ) {
                            result.push(text);
                        }
                    }
                }

                return result.slice(0, 1200);
            }
            """
        )

        return "|".join(
            values
        )

    except Exception:
        return ""


# ==============================================================
# TARİH SEÇİM MEKANİZMASI
# ==============================================================

def find_date_candidates(page):

    try:

        return page.evaluate(
            """
            () => {

                const result = [];

                const elements = [
                    ...document.querySelectorAll(
                        "button"
                    ),
                    ...document.querySelectorAll(
                        "[role='button']"
                    ),
                    ...document.querySelectorAll(
                        "a"
                    ),
                    ...document.querySelectorAll(
                        "[data-date]"
                    ),
                    ...document.querySelectorAll(
                        "[data-value]"
                    ),
                    ...document.querySelectorAll(
                        "option"
                    )
                ];

                for (const el of elements) {

                    const text =
                        (el.innerText ||
                         el.textContent ||
                         "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    const dataDate =
                        el.getAttribute(
                            "data-date"
                        );

                    const dataValue =
                        el.getAttribute(
                            "data-value"
                        );

                    const value =
                        el.getAttribute(
                            "value"
                        );

                    const aria =
                        el.getAttribute(
                            "aria-label"
                        );

                    result.push({
                        text: text,
                        dataDate: dataDate || "",
                        dataValue: dataValue || "",
                        value: value || "",
                        aria: aria || "",
                        tag: el.tagName
                    });
                }

                return result;
            }
            """
        )

    except Exception:
        return []


def click_date(
    page,
    target_date
):

    target_full = target_date.strftime(
        "%d.%m.%Y"
    )

    target_short = target_date.strftime(
        "%d.%m"
    )

    target_iso = target_date.strftime(
        "%Y-%m-%d"
    )

    target_day = target_date.strftime(
        "%d"
    )

    target_month = target_date.strftime(
        "%m"
    )

    before = schedule_signature(
        page
    )

    candidates = find_date_candidates(
        page
    )

    # ----------------------------------------------------------
    # Öncelik: tam tarih eşleşmesi
    # ----------------------------------------------------------

    for candidate in candidates:

        fields = [
            candidate.get("text", ""),
            candidate.get("dataDate", ""),
            candidate.get("dataValue", ""),
            candidate.get("value", ""),
            candidate.get("aria", ""),
        ]

        if any(
            value in (
                target_full,
                target_iso
            )
            for value in fields
            if value
        ):

            try:

                result = page.evaluate(
                    """
                    target => {

                        const elements = [
                            ...document.querySelectorAll(
                                "button"
                            ),
                            ...document.querySelectorAll(
                                "[role='button']"
                            ),
                            ...document.querySelectorAll(
                                "a"
                            ),
                            ...document.querySelectorAll(
                                "[data-date]"
                            ),
                            ...document.querySelectorAll(
                                "[data-value]"
                            ),
                            ...document.querySelectorAll(
                                "option"
                            )
                        ];

                        for (const el of elements) {

                            const text =
                                (el.innerText ||
                                 el.textContent ||
                                 "")
                                .replace(/\\s+/g, " ")
                                .trim();

                            const attrs = [
                                el.getAttribute(
                                    "data-date"
                                ),
                                el.getAttribute(
                                    "data-value"
                                ),
                                el.getAttribute(
                                    "value"
                                ),
                                el.getAttribute(
                                    "aria-label"
                                )
                            ];

                            if (
                                text === target ||
                                attrs.includes(target)
                            ) {

                                el.scrollIntoView({
                                    block: "center"
                                });

                                el.click();

                                return true;
                            }
                        }

                        return false;
                    }
                    """,
                    target_full
                )

                if result:
                    break

            except Exception:
                continue

    # ----------------------------------------------------------
    # Native SELECT
    # ----------------------------------------------------------

    if not candidates:

        try:

            selects = page.locator(
                "select"
            )

            for i in range(
                selects.count()
            ):

                select = selects.nth(i)

                option_count = select.locator(
                    "option"
                ).count()

                for j in range(
                    option_count
                ):

                    option = select.locator(
                        "option"
                    ).nth(j)

                    text = clean_text(
                        option.inner_text()
                    )

                    value = (
                        option.get_attribute(
                            "value"
                        )
                        or ""
                    )

                    if (
                        target_full in text
                        or target_iso in text
                        or target_full == value
                        or target_iso == value
                    ):

                        select.select_option(
                            value=value
                        )

                        break

        except Exception:
            pass

    # ----------------------------------------------------------
    # Eğer görünür tarih sadece gün adı / gün sayısı ise
    # 04, 05, 06... üzerinden kontrollü deneme.
    # ----------------------------------------------------------

    if not candidates:

        try:

            result = page.evaluate(
                """
                args => {

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

                        if (
                            text === args.day
                            ||
                            text === args.dayNoZero
                        ) {

                            el.scrollIntoView({
                                block: "center"
                            });

                            el.click();

                            return true;
                        }
                    }

                    return false;
                }
                """,
                {
                    "day": target_day,
                    "dayNoZero": str(
                        int(target_day)
                    )
                }
            )

        except Exception:
            result = False

    # ----------------------------------------------------------
    # Değişikliği bekle
    # ----------------------------------------------------------

    if result is not False:

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
    # Son fallback:
    # page'de datepicker kullanan elementleri tarayıp
    # custom JS event oluştur.
    # ----------------------------------------------------------

    try:

        changed = page.evaluate(
            """
            args => {

                const all = [
                    ...document.querySelectorAll("*")
                ];

                for (const el of all) {

                    const text =
                        (el.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    const attributes = [
                        el.getAttribute("data-date"),
                        el.getAttribute("data-day"),
                        el.getAttribute("data-value"),
                        el.getAttribute("value"),
                        el.getAttribute("aria-label")
                    ];

                    const found =
                        attributes.some(
                            value =>
                                value &&
                                (
                                    value.includes(
                                        args.full
                                    )
                                    ||
                                    value.includes(
                                        args.iso
                                    )
                                )
                        )
                        ||
                        text === args.full;

                    if (found) {

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
            {
                "full": target_full,
                "iso": target_iso
            }
        )

        if changed:

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

    except Exception:
        pass

    return False


# ==============================================================
# SAYFA YENİDEN AÇMA
# ==============================================================

def reload_page(
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
# BİR KATEGORİDE 7 GÜN
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

    reload_page(
        page,
        category_url
    )

    channel_url_map = (
        build_channel_url_map(
            channels
        )
    )

    results = []

    # ----------------------------------------------------------
    # 7 GÜN
    # ----------------------------------------------------------

    for day_index in range(
        DAYS
    ):

        target_date = (
            TODAY
            + timedelta(
                days=day_index
            )
        )

        print()
        print(
            f"  TARİH: "
            f"{target_date.strftime('%d.%m.%Y')}"
        )

        # ------------------------------------------------------
        # İlk gün:
        # Sayfanın açılış akışını doğrudan al.
        # ------------------------------------------------------

        if day_index == 0:

            programs = extract_programs(
                page,
                target_date,
                channel_url_map
            )

            # İlk gün boşsa tarih seçimini dene.
            if not programs:

                print(
                    "    İlk gün programı boş."
                )

                selected = click_date(
                    page,
                    target_date
                )

                if selected:

                    programs = extract_programs(
                        page,
                        target_date,
                        channel_url_map
                    )

        # ------------------------------------------------------
        # Sonraki günler
        # ------------------------------------------------------

        else:

            selected = click_date(
                page,
                target_date
            )

            if not selected:

                print(
                    "    Tarih seçimi başarısız, "
                    "sayfa yeniden açılıyor."
                )

                reload_page(
                    page,
                    category_url
                )

                selected = click_date(
                    page,
                    target_date
                )

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

        results.extend(
            programs
        )

    return results


# ==============================================================
# DUPLICATE + END TIME
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

    for _, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for i, program in enumerate(items):

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
            program["channel"]
        )
        for program in programs
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

    # ----------------------------------------------------------
    # DOSYAYI YAZ
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

    # ----------------------------------------------------------
    # KANALLARI TEK SEFER AL
    # ----------------------------------------------------------

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

        reload_page(
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

    # ----------------------------------------------------------
    # BÜTÜN KATEGORİLER
    #
    # SIRALI.
    #
    # Paralel yok.
    # Tarihler karışmıyor.
    # ----------------------------------------------------------

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

    # ----------------------------------------------------------
    # PROGRAMLARI BİRLEŞTİR
    # ----------------------------------------------------------

    all_programs = finalize_programs(
        all_programs
    )

    # ----------------------------------------------------------
    # GÜN KONTROLÜ
    # ----------------------------------------------------------

    date_counts = defaultdict(int)

    for program in all_programs:

        date_counts[
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
            f"{date_counts.get(date, 0)} program"
        )

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    xml_channel_count = write_xml(
        channels,
        all_programs
    )

    # ----------------------------------------------------------
    # KANAL SAYILARI
    # ----------------------------------------------------------

    channel_counts = defaultdict(
        int
    )

    for program in all_programs:

        channel_counts[
            program["channel"]
        ] += 1

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

    print()
    print(
        "KANAL PROGRAM SAYILARI:"
    )

    for channel_name in sorted(
        channel_counts
    ):

        print(
            f"  {channel_name}: "
            f"{channel_counts[channel_name]}"
        )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
