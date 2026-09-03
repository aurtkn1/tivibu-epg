import re
import html
import json
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

# Tivibu'nun bilinen kategori sayfaları.
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
# YARDIMCI
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n+", "\n", value)

    return value.strip()


def one_line(value):
    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(value or ""))
    ).strip()


def normalize_key(value):
    value = one_line(value).lower()

    replacements = {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
        "’": "",
        "'": "",
        "-": "",
        "_": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return re.sub(
        r"[^a-z0-9]+",
        "",
        value
    )


def make_xml_id(name):
    value = one_line(name).lower()

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
    name = one_line(name)

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
    title = one_line(title)

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
#
# Örnek:
#
# Kanal D Ana Haber Aktüalite - 19:00 → 20:00 Canlı
# =============================================================

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

    text = one_line(text)

    match = PROGRAM_RE.match(
        text
    )

    if not match:
        return None

    title = one_line(
        match.group("title")
    )

    if not valid_program_title(title):
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
# TARİH / PROGRAM VERİSİNİ JAVASCRIPT'TEN AL
#
# Buradaki amaç:
# - Tarih butonunun exact yazısını varsaymamak
# - Program verisini mümkün olan en küçük DOM elemanından almak
# - Kanal linki ile aynı container içinden kanal adını çözmek
# ==============================================================

def extract_dom_records(
    page,
    target_date,
    channels
):
    allowed = {
        normalize_key(c["name"]): c["name"]
        for c in channels.values()
    }

    result = page.evaluate(
        """
        (allowed, targetText) => {

            const normalize = (value) => {
                return (value || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase()
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .replace(/ı/g, "i")
                    .replace(/[^a-z0-9]+/g, "");
            };

            const programPattern =
                /\\d{1,2}:\\d{2}\\s*→\\s*\\d{1,2}:\\d{2}/;

            const records = [];
            const seen = new Set();

            // --------------------------------------------------
            // Önce gerçek kanal linklerini bul.
            // --------------------------------------------------

            const channelLinks = [
                ...document.querySelectorAll(
                    'a[href*="/kanallar/"]'
                )
            ];

            const channelMap = new Map();

            for (const link of channelLinks) {

                const rawName =
                    (link.innerText || "")
                    .replace(/\\s+/g, " ")
                    .trim();

                if (!rawName) {
                    continue;
                }

                const key =
                    normalize(rawName);

                if (!allowed[key]) {
                    continue;
                }

                const href =
                    link.getAttribute("href") || "";

                const url =
                    href.startsWith("/")
                        ? location.origin + href
                        : href;

                channelMap.set(
                    url.split("?")[0].replace(/\\/$/, "").toLowerCase(),
                    allowed[key]
                );
            }

            // --------------------------------------------------
            // Tüm elementleri tara.
            // --------------------------------------------------

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

                if (!programPattern.test(text)) {
                    continue;
                }

                if (text.length > 400) {
                    continue;
                }

                // İçinde program bulunan daha küçük child varsa
                // parent'ı kullanma.
                let hasProgramChild = false;

                for (const child of element.children) {

                    const childText =
                        (child.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (
                        childText &&
                        childText.length < text.length &&
                        programPattern.test(childText)
                    ) {
                        hasProgramChild = true;
                        break;
                    }
                }

                if (hasProgramChild) {
                    continue;
                }

                // ------------------------------------------------
                // Önce elementin kendi href'i
                // ------------------------------------------------

                let foundChannel = null;

                const ownHref =
                    element.getAttribute("href");

                if (ownHref) {

                    const url =
                        ownHref.startsWith("/")
                            ? location.origin + ownHref
                            : ownHref;

                    const cleanUrl =
                        url.split("?")[0]
                           .replace(/\\/$/, "")
                           .toLowerCase();

                    foundChannel =
                        channelMap.get(cleanUrl) || null;
                }

                // ------------------------------------------------
                // Sonra parent zinciri
                // ------------------------------------------------

                let node = element;

                for (
                    let depth = 0;
                    !foundChannel &&
                    node &&
                    depth < 12;
                    depth++
                ) {

                    const links = [
                        ...node.querySelectorAll(
                            'a[href*="/kanallar/"]'
                        )
                    ];

                    const found = [];

                    for (const link of links) {

                        const rawName =
                            (link.innerText || "")
                            .replace(/\\s+/g, " ")
                            .trim();

                        if (!rawName) {
                            continue;
                        }

                        const key =
                            normalize(rawName);

                        if (allowed[key]) {

                            if (
                                !found.includes(
                                    allowed[key]
                                )
                            ) {
                                found.push(
                                    allowed[key]
                                );
                            }
                        }
                    }

                    if (found.length === 1) {
                        foundChannel = found[0];
                        break;
                    }

                    node = node.parentElement;
                }

                if (!foundChannel) {
                    continue;
                }

                // ------------------------------------------------
                // Tarih metnini istemiyoruz.
                // Program satırı zaten saatli.
                // ------------------------------------------------

                const key =
                    foundChannel +
                    "|" +
                    text;

                if (seen.has(key)) {
                    continue;
                }

                seen.add(key);

                records.push({
                    channel: foundChannel,
                    text: text
                });
            }

            return records;
        }
        """,
        allowed,
        target_date.strftime("%d.%m.%Y")
    )

    programs = []

    for item in result:

        parsed = parse_program_line(
            item["text"],
            target_date
        )

        if not parsed:
            continue

        programs.append({
            "channel": item["channel"],
            "title": parsed["title"],
            "start": parsed["start"],
            "end": parsed["end"]
        })

    return programs


# ==============================================================
# TARİH KONTROLÜ
# ==============================================================

def current_page_date_state(page):

    try:

        state = page.evaluate(
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
                        "select option:checked"
                    ),
                    ...document.querySelectorAll(
                        "[aria-selected='true']"
                    )
                ];

                for (const element of elements) {

                    const text =
                        (element.innerText ||
                         element.textContent ||
                         "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (text) {
                        result.push(text);
                    }
                }

                return result;
            }
            """
        )

        return state

    except Exception:

        return []


# ==============================================================
# TARİH ELEMENTİNİ BUL
# ==============================================================

def find_date_target(page, target_date):

    target = target_date.strftime(
        "%d.%m.%Y"
    )

    # ----------------------------------------------------------
    # Önce tüm DOM attribute'larını kontrol et.
    # ----------------------------------------------------------

    try:

        candidate = page.evaluate(
            """
            target => {

                const elements = [
                    ...document.querySelectorAll("*")
                ];

                for (const el of elements) {

                    const attrs = [
                        "data-date",
                        "data-day",
                        "data-value",
                        "value",
                        "aria-label"
                    ];

                    for (const attr of attrs) {

                        const value =
                            el.getAttribute(attr);

                        if (
                            value &&
                            value.includes(target)
                        ) {
                            return {
                                type: "attribute",
                                attr: attr,
                                value: value
                            };
                        }
                    }

                    const text =
                        (el.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (text === target) {

                        return {
                            type: "text",
                            value: target
                        };
                    }
                }

                return null;
            }
            """,
            target
        )

        if candidate:
            return candidate

    except Exception:
        pass

    return None


# ==============================================================
# TARİH DEĞİŞTİRME
#
# Tarihler sayfada "05.09.2026" olarak görünmese bile
# data-date/value/aria-label üzerinden arıyoruz.
#
# Ayrıca tıklamadan sonra eski program listesiyle karşılaştırma
# yapıyoruz.
# ==============================================================

def change_date(page, target_date):

    target = target_date.strftime(
        "%d.%m.%Y"
    )

    before = extract_visible_program_text(
        page
    )

    candidate = find_date_target(
        page,
        target_date
    )

    if not candidate:
        return False

    # ----------------------------------------------------------
    # Native JS event
    # ----------------------------------------------------------

    try:

        clicked = page.evaluate(
            """
            target => {

                const elements = [
                    ...document.querySelectorAll("*")
                ];

                for (const el of elements) {

                    const attrs = [
                        "data-date",
                        "data-day",
                        "data-value",
                        "value",
                        "aria-label"
                    ];

                    for (const attr of attrs) {

                        const value =
                            el.getAttribute(attr);

                        if (
                            value &&
                            value.includes(target)
                        ) {

                            el.scrollIntoView({
                                block: "center"
                            });

                            el.dispatchEvent(
                                new MouseEvent(
                                    "mousedown",
                                    {
                                        bubbles: true,
                                        cancelable: true,
                                        view: window
                                    }
                                )
                            );

                            el.dispatchEvent(
                                new MouseEvent(
                                    "mouseup",
                                    {
                                        bubbles: true,
                                        cancelable: true,
                                        view: window
                                    }
                                )
                            );

                            el.click();

                            return true;
                        }
                    }

                    const text =
                        (el.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (text === target) {

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
            target
        )

        if not clicked:
            return False

    except Exception:
        return False

    # ----------------------------------------------------------
    # Yeni programlar gelmesini bekle
    # ----------------------------------------------------------

    for _ in range(30):

        time.sleep(
            0.25
        )

        after = extract_visible_program_text(
            page
        )

        if (
            after
            and after != before
        ):
            return True

    return False


# ==============================================================
# GÖRÜNÜR PROGRAM TEXT İMZASI
# ==============================================================

def extract_visible_program_text(page):

    try:

        result = page.evaluate(
            """
            () => {

                const output = [];

                const elements = [
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll("li"),
                    ...document.querySelectorAll("div"),
                    ...document.querySelectorAll("span")
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

                        if (
                            text.length >= 5 &&
                            text.length <= 400
                        ) {
                            output.push(text);
                        }
                    }
                }

                return output.slice(
                    0,
                    1000
                );
            }
            """
        )

        return "|".join(
            result
        )

    except Exception:
        return ""


# ==============================================================
# SAYFAYI YÜKLE
# ==============================================================

def load_page(page, url):

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except PlaywrightTimeoutError:

        print(
            "  Sayfa yükleme zaman aşımı."
        )

    except Exception as e:

        print(
            f"  Sayfa yükleme hatası: {e}"
        )

    page.wait_for_timeout(
        2000
    )


# ==============================================================
# BİR KATEGORİYİ 7 GÜN AL
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

    load_page(
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
    # Her gün açıkça seçiliyor.
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
        # İlk günde sayfanın mevcut gününü kullan.
        #
        # Günün programlarının tarihi bugüne aitse doğrudan al.
        # ------------------------------------------------------

        if day_index == 0:

            programs = extract_programs(
                page,
                target_date,
                channel_url_map
            )

            # Eğer program yoksa tarih seçmeyi dene.
            if not programs:

                print(
                    "    İlk gün programı bulunamadı, "
                    "tarih seçimi deneniyor..."
                )

                selected = change_date(
                    page,
                    target_date
                )

                if selected:

                    programs = extract_programs(
                        page,
                        target_date,
                        channel_url_map
                    )

        else:

            selected = change_date(
                page,
                target_date
            )

            if not selected:

                print(
                    "    Tarih değiştirilemedi."
                )

                # ------------------------------------------------
                # Sayfa yenile + tekrar
                # ------------------------------------------------

                load_page(
                    page,
                    category_url
                )

                selected = change_date(
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

        result.extend(
            programs
        )

    return result


# ==============================================================
# PROGRAM BİRLEŞTİR
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
    # Aynı kanal içinde mantıksız çakışmaları temizle.
    # ----------------------------------------------------------

    grouped = defaultdict(
        list
    )

    for program in unique:

        grouped[
            normalize_key(
                program["channel"]
            )
        ].append(
            program
        )

    final = []

    for channel_key, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        previous_end = None

        for program in items:

            start = program["start"]
            end = program.get("end")

            if end is None:

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

            if previous_end is not None:

                # Aynı başlangıçta ikinci kayıt varsa ilkini koru.
                if (
                    start ==
                    final[-1]["start"]
                    and
                    program["channel"]
                    ==
                    final[-1]["channel"]
                ):
                    continue

            if end - start > timedelta(
                hours=12
            ):

                end = (
                    start
                    + timedelta(
                        hours=3
                    )
                )

            item = {
                "channel": program["channel"],
                "title": program["title"],
                "start": start,
                "end": end,
            }

            final.append(
                item
            )

            previous_end = end

    final.sort(
        key=lambda x: (
            x["start"],
            normalize_key(
                x["channel"]
            )
        )
    )

    return final


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

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = channel["name"]

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

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Dönem: "
        f"{TODAY.strftime('%d.%m.%Y')} -> "
        f"{(
            TODAY
            + timedelta(
                days=DAYS - 1
            )
        ).strftime('%d.%m.%Y')}"
    )

    print("=" * 70)

    # ----------------------------------------------------------
    # KANALLARI BİR KEZ AL
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
                "height": 1080,
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            10000
        )

        print(
            "Kanal listesi alınıyor..."
        )

        load_page(
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
    # SADECE ANA SAYFA + KATEGORİLER
    # SIRALI
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
    # TEKILLEŞTİR
    # ----------------------------------------------------------

    all_programs = finalize_programs(
        all_programs
    )

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    xml_channel_count = write_xml(
        channels,
        all_programs
    )

    # ----------------------------------------------------------
    # ÖZET
    # ----------------------------------------------------------

    counts = defaultdict(
        int
    )

    dates = defaultdict(
        int
    )

    for program in all_programs:

        counts[
            program["channel"]
        ] += 1

        dates[
            program["start"].strftime(
                "%d.%m.%Y"
            )
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
        "GÜN BAZINDA:"
    )

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
            f"  {date}: "
            f"{dates.get(date, 0)} program"
        )

    print()
    print(
        "KANAL BAZINDA:"
    )

    for name in sorted(
        counts
    ):

        print(
            f"  {name}: "
            f"{counts[name]}"
        )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
