
import html
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 1500

MIN_CHANNELS = 30
MIN_PROGRAMS = 100
MIN_DAYS = 6


CATEGORY_PAGES = [
    ("Tüm Kanallar", "/canli-tv"),
    ("Ulusal", "/canli-tv/ulusal"),
    ("Haber", "/canli-tv/haber"),
    ("Spor", "/canli-tv/spor"),
    ("Sinema", "/canli-tv/sinema"),
    ("Dizi", "/canli-tv/dizi"),
    ("Çocuk", "/canli-tv/cocuk"),
    ("Belgesel", "/canli-tv/belgesel"),
    ("Yaşam-Stil", "/canli-tv/yasam-stil"),
    ("Müzik", "/canli-tv/muzik"),
    ("Global", "/canli-tv/global"),
    ("Diğer", "/canli-tv/diger"),
]


BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
}


# ============================================================
# TIVIBU GERÇEK HTML YAPISINDAN VERİ ÇEKME
#
# Kanal kartı:
# .fullLink[channelcode]
# .channelsTitle
#
# Program kartı:
# a[href*="/rv?"]
# .programTitle
# .type
# .startTime
# .finishTime
# ============================================================

EXTRACT_JS = r"""() => {

    const clean = value => {
        return (value || '')
            .replace(/\u00a0/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    };

    const extractCode = value => {
        if (!value) return '';

        let decoded = value;

        try {
            decoded = decodeURIComponent(value);
        } catch (e) {}

        const match = decoded.match(/\b(ch\d{6,})\b/i);

        return match ? match[1].toLowerCase() : '';
    };

    const channelNames = new Map();

    const channelCards = [
        ...document.querySelectorAll('.fullLink[channelcode]')
    ];

    for (const card of channelCards) {

        const code = extractCode(
            card.getAttribute('channelcode')
        );

        if (!code) continue;

        const nameElement = card.querySelector(
            '.channelsTitle'
        );

        if (!nameElement) continue;

        const name = clean(
            nameElement.innerText ||
            nameElement.textContent
        );

        if (!name) continue;

        channelNames.set(code, name);
    }


    const programsByChannel = new Map();

    const programLinks = [
        ...document.querySelectorAll('a[href*="/rv?"]')
    ];

    let programLinksFound = 0;
    let programLinksParsed = 0;

    for (const link of programLinks) {

        const programText = link.querySelector(
            '.programText'
        );

        if (!programText) continue;

        programLinksFound++;

        const code = extractCode(
            link.getAttribute('href')
        );

        if (!code) continue;

        const titleElement = programText.querySelector(
            '.programTitle'
        );

        const typeElement = programText.querySelector(
            '.programType .type'
        );

        const startElement = programText.querySelector(
            '.programType .startTime'
        );

        const finishElement = programText.querySelector(
            '.programType .finishTime'
        );

        if (
            !titleElement ||
            !startElement ||
            !finishElement
        ) {
            continue;
        }

        const title = clean(
            titleElement.innerText ||
            titleElement.textContent
        );

        const category = typeElement
            ? clean(
                typeElement.innerText ||
                typeElement.textContent
              )
            : '';

        const start = clean(
            startElement.innerText ||
            startElement.textContent
        );

        const finish = clean(
            finishElement.innerText ||
            finishElement.textContent
        );

        const timePattern = /^\d{1,2}:\d{2}$/;

        if (
            !title ||
            !timePattern.test(start) ||
            !timePattern.test(finish)
        ) {
            continue;
        }

        const startParts = start.split(':').map(Number);
        const finishParts = finish.split(':').map(Number);

        if (
            startParts[0] > 23 ||
            finishParts[0] > 23 ||
            startParts[1] > 59 ||
            finishParts[1] > 59
        ) {
            continue;
        }

        if (!programsByChannel.has(code)) {
            programsByChannel.set(code, []);
        }

        programsByChannel.get(code).push({
            title: title,
            category: category,
            sh: startParts[0],
            sm: startParts[1],
            eh: finishParts[0],
            em: finishParts[1]
        });

        programLinksParsed++;
    }


    const rows = [];

    for (const [code, name] of channelNames.entries()) {

        const items = programsByChannel.get(code) || [];

        rows.push({
            code: code,
            name: name,
            items: items
        });
    }


    return {
        rows: rows,
        diagnostics: {
            channelCards: channelCards.length,
            channelsFound: channelNames.size,
            programLinks: programLinks.length,
            programCardsFound: programLinksFound,
            programsParsed: programLinksParsed,
            channelsWithPrograms: programsByChannel.size
        }
    };
}"""


# ============================================================
# METİN İŞLEME
# ============================================================

def clean_text(value):
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value):
    return clean_text(value).upper()


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)
        value = value.replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    ).strip("_")

    return value or "channel"


# ============================================================
# TARİH SEÇİMİ
# ============================================================

def date_label(day, today):

    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def select_date(page, selected_day, today):

    expected = selected_day.strftime("%Y.%m.%d")

    date_links = page.locator(
        "a[channeldatebegin][channeldateend]:visible"
    )

    for index in range(date_links.count()):

        link = date_links.nth(index)

        begin = (
            link.get_attribute("channeldatebegin") or ""
        )

        if not begin.startswith(expected):
            continue

        classes = (
            link.get_attribute("class") or ""
        ).lower().split()

        if "active" not in classes:

            link.click(timeout=15000)

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=5000
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(DATE_WAIT_MS)

        return


    label = date_label(selected_day, today)

    visible_labels = page.locator(
        "a:visible"
    ).filter(
        has_text=re.compile(
            r"^\s*" + re.escape(label) + r"\s*$",
            re.IGNORECASE
        )
    )

    if visible_labels.count():

        visible_labels.first.click(timeout=15000)

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=5000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(DATE_WAIT_MS)

        return


    raise RuntimeError(
        f"{expected} tarihi için görünür tarih bağlantısı bulunamadı."
    )


# ============================================================
# PROGRAM SAATLERİNİ TARİHE DÖNÜŞTÜRME
# ============================================================

def assign_times(items, selected_day):

    parsed = []

    for item in items:

        try:
            title = clean_text(item.get("title"))
            category = clean_text(item.get("category"))

            sh = int(item["sh"])
            sm = int(item["sm"])
            eh = int(item["eh"])
            em = int(item["em"])

        except (KeyError, TypeError, ValueError):
            continue

        if not title:
            continue

        if (
            sh > 23 or eh > 23
            or sm > 59 or em > 59
            or sh < 0 or eh < 0
            or sm < 0 or em < 0
        ):
            continue

        parsed.append({
            "title": title,
            "category": category,
            "sh": sh,
            "sm": sm,
            "eh": eh,
            "em": em,
        })


    parsed.sort(
        key=lambda item: (
            item["sh"] * 60 + item["sm"]
        )
    )

    result = []

    for item in parsed:

        start_minutes = (
            item["sh"] * 60 + item["sm"]
        )

        end_minutes = (
            item["eh"] * 60 + item["em"]
        )

        start_day = selected_day

        # Bitiş saati başlangıçtan küçük veya eşitse
        # program gece yarısını geçiyor demektir.
        end_day = (
            selected_day + timedelta(days=1)
            if end_minutes <= start_minutes
            else selected_day
        )

        start = datetime(
            start_day.year,
            start_day.month,
            start_day.day,
            item["sh"],
            item["sm"],
            tzinfo=ISTANBUL
        )

        stop = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            item["eh"],
            item["em"],
            tzinfo=ISTANBUL
        )

        if stop <= start:
            continue

        if (
            stop - start
        ).total_seconds() > 24 * 3600:
            continue

        result.append({
            "title": item["title"],
            "category": item["category"],
            "start": start,
            "stop": stop,
        })

    return result


# ============================================================
# SAYFA BEKLEME
# ============================================================

def wait_page(page):

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=12000
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(1800)


# ============================================================
# TÜM KATEGORİLERİ VE 7 GÜNÜ TARA
# ============================================================

def collect_all(page, today):

    channels = {}
    programs = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    last_day = today + timedelta(days=DAYS)

    window_end = datetime(
        last_day.year,
        last_day.month,
        last_day.day,
        0,
        0,
        tzinfo=ISTANBUL
    )


    for category_name, category_path in CATEGORY_PAGES:

        category_url = BASE_URL + category_path

        print(
            f"\nKATEGORİ: {category_name}",
            flush=True
        )

        page.goto(
            category_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        wait_page(page)

        category_channels = set()


        for offset in range(DAYS):

            selected_day = today + timedelta(days=offset)

            label = date_label(
                selected_day,
                today
            )

            print(
                f"  [{offset + 1}/{DAYS}] {label} taranıyor...",
                flush=True
            )

            select_date(
                page,
                selected_day,
                today
            )


            extracted = page.evaluate(EXTRACT_JS)

            rows = extracted.get("rows", [])

            diagnostics = extracted.get(
                "diagnostics",
                {}
            )

            day_programs = 0


            for row in rows:

                code = clean_text(
                    row.get("code")
                ).lower()

                name = clean_text(
                    row.get("name")
                )

                if not code or not name:
                    continue

                if normalize_name(name) in BAD_CHANNELS:
                    continue


                if code not in channels:

                    channels[code] = {
                        "name": name
                    }

                elif len(name) > len(channels[code]["name"]):

                    channels[code]["name"] = name


                channel_name = channels[code]["name"]

                category_channels.add(code)


                parsed_programs = assign_times(
                    row.get("items", []),
                    selected_day
                )


                for program in parsed_programs:

                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue


                    key = (
                        code,
                        program["start"],
                        program["stop"],
                        program["title"].casefold()
                    )


                    if key not in programs:

                        programs[key] = {
                            "channel_code": code,
                            "channel_name": channel_name,
                            **program
                        }

                        day_programs += 1


            print(
                f"    Kanal kartı: "
                f"{diagnostics.get('channelCards', 0)} | "
                f"Kanal adı: "
                f"{diagnostics.get('channelsFound', 0)} | "
                f"Program bağlantısı: "
                f"{diagnostics.get('programCardsFound', 0)} | "
                f"Okunan program: "
                f"{diagnostics.get('programsParsed', 0)} | "
                f"Yeni program: {day_programs}",
                flush=True
            )


        print(
            f"  {category_name}: "
            f"{len(category_channels)} kanal bulundu.",
            flush=True
        )


    return channels, list(programs.values())


# ============================================================
# XMLTV OLUŞTUR
# ============================================================

def build_xml(channels, programs):

    used_ids = set()
    channel_ids = {}


    for code, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):

        base = slugify(info["name"])

        channel_id = base
        suffix = 2

        while channel_id in used_ids:

            channel_id = f"{base}_{suffix}"
            suffix += 1

        used_ids.add(channel_id)

        channel_ids[code] = channel_id


    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL
        }
    )


    for code, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):

        node = ET.SubElement(
            root,
            "channel",
            {
                "id": channel_ids[code]
            }
        )

        display = ET.SubElement(
            node,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = info["name"]


    for item in sorted(
        programs,
        key=lambda value: (
            value["start"],
            value["channel_code"],
            value["stop"]
        )
    ):

        node = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[item["channel_code"]],
                "start": item["start"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": item["stop"].strftime(
                    "%Y%m%d%H%M%S %z"
                )
            }
        )


        title = ET.SubElement(
            node,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = item["title"]


        if item.get("category"):

            category = ET.SubElement(
                node,
                "category",
                {
                    "lang": "tr"
                }
            )

            category.text = item["category"]


    tree = ET.ElementTree(root)

    ET.indent(
        tree,
        space="  "
    )


    temp_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temp_file,
        encoding="utf-8",
        xml_declaration=True
    )

    os.replace(
        temp_file,
        OUTPUT_FILE
    )


# ============================================================
# VERİ KONTROLÜ
# ============================================================

def validate_data(channels, programs):

    if len(channels) < MIN_CHANNELS:

        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal eşleşti. "
            f"En az {MIN_CHANNELS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )


    if len(programs) < MIN_PROGRAMS:

        raise RuntimeError(
            f"Yalnızca {len(programs)} program alındı. "
            f"En az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )


    days = {
        program["start"].date()
        for program in programs
    }


    if len(days) < MIN_DAYS:

        raise RuntimeError(
            f"Yalnızca {len(days)} yayın günü bulundu. "
            f"En az {MIN_DAYS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    started = time.time()

    today = datetime.now(
        ISTANBUL
    ).date()


    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Kanal: .fullLink[channelcode] / .channelsTitle")
    print("Program: .programTitle / .startTime / .finishTime")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print(f"Kategori sayısı: {len(CATEGORY_PAGES)}")
    print("=" * 68)


    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )


        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1440,
                "height": 1100
            }
        )


        page.set_default_timeout(
            PAGE_TIMEOUT
        )


        try:

            channels, programs = collect_all(
                page,
                today
            )

        finally:

            browser.close()


    validate_data(
        channels,
        programs
    )


    build_xml(
        channels,
        programs
    )


    days = sorted({
        program["start"].date()
        for program in programs
    })


    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Kanal: {len(channels)}")
    print(f"Program: {len(programs)}")
    print(f"Yayın günü: {len(days)}")
    print(
        f"İlk gün: {days[0].strftime('%d.%m.%Y')}"
    )
    print(
        f"Son gün: {days[-1].strftime('%d.%m.%Y')}"
    )
    print(f"Dosya: {OUTPUT_FILE}")
    print(
        f"Süre: {time.time() - started:.1f} saniye"
    )
    print("=" * 68)


if __name__ == "__main__":
    main()
