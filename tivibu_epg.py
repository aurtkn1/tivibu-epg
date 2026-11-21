
import re
import html
import time
import unicodedata
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError
)


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
PAGE_WAIT_MS = 2200
DATE_WAIT_MS = 2500

MIN_CHANNELS = 10
MIN_PROGRAMS = 100
MIN_DAYS = 6

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
}

TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})"
    r"\s*(?:→|➜|[-–])\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)

CATEGORY_RE = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|"
    r"Belgesel|Sinema|Ulusal|Global|Diğer|Aktüalite|"
    r"Spor Programı)\s*[-–]?\s*$",
    re.IGNORECASE
)


# ============================================================
# ESKİ ÇALIŞAN KANAL-PROGRAM EŞLEŞTİRME MANTIĞI
#
# Program bağlantısından başlanır.
# Üst kapsayıcılar taranır.
# En yakın tek /kanallar/ bağlantısı bulunur.
# Kanal adresi üzerinden programlar gruplandırılır.
# ============================================================

EXTRACT_JS = r"""
() => {
    const timeRe =
        /\d{1,2}:\d{2}\s*(?:→|➜|[-–])\s*\d{1,2}:\d{2}/;

    const norm = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const anchors = [
        ...document.querySelectorAll("a")
    ];

    const groups = new Map();
    const unmapped = [];

    const diagnostics = {
        totalAnchors: anchors.length,
        programAnchors: 0,
        mappedPrograms: 0,
        unmappedPrograms: 0,
        mappedChannels: 0,
        samples: []
    };

    function getChannelLinks(container) {
        const links = [
            ...container.querySelectorAll("a[href]")
        ];

        const unique = new Map();

        for (const link of links) {
            try {
                const url = new URL(
                    link.href,
                    location.href
                );

                if (
                    !url.pathname
                        .toLowerCase()
                        .includes("/kanallar/")
                ) {
                    continue;
                }

                const href =
                    url.origin +
                    url.pathname.replace(/\/+$/, "");

                let name = norm(
                    link.innerText ||
                    link.textContent
                );

                if (!name) {
                    const image = link.querySelector(
                        "img[alt]"
                    );

                    if (image) {
                        name = norm(
                            image.getAttribute("alt")
                        );
                    }
                }

                name = name
                    .replace(/^Kanal\s*:\s*/i, "")
                    .trim();

                if (href && name) {
                    unique.set(
                        href,
                        {
                            href,
                            name
                        }
                    );
                }

            } catch (_) {
            }
        }

        return unique;
    }

    for (const programAnchor of anchors) {
        const programText = norm(
            programAnchor.innerText ||
            programAnchor.textContent
        );

        if (
            !programAnchor.href ||
            !timeRe.test(programText)
        ) {
            continue;
        }

        diagnostics.programAnchors++;

        let node = programAnchor;
        let channelHref = "";
        let channelName = "";
        let chosenDepth = -1;

        // ÖNEMLİ:
        // Program bağlantısından yukarı doğru çıkılır.
        // En yakın tek kanal bağlantısı bulunan kapsayıcı seçilir.
        for (
            let depth = 0;
            depth < 35 && node;
            depth++, node = node.parentElement
        ) {
            const unique = getChannelLinks(node);

            if (unique.size === 1) {
                const match = [
                    ...unique.values()
                ][0];

                channelHref = match.href;
                channelName = match.name;
                chosenDepth = depth;

                break;
            }
        }

        if (!channelHref || !channelName) {
            diagnostics.unmappedPrograms++;

            if (
                diagnostics.samples.length < 10
            ) {
                unmapped.push({
                    program: programText,
                    href: programAnchor.href
                });
            }

            continue;
        }

        if (!groups.has(channelHref)) {
            groups.set(
                channelHref,
                {
                    name: channelName,
                    href: channelHref,
                    items: [],
                    seen: new Set(),
                    depth: chosenDepth
                }
            );
        }

        const group = groups.get(channelHref);

        if (!group.seen.has(programText)) {
            group.seen.add(programText);
            group.items.push(programText);

            diagnostics.mappedPrograms++;
        }
    }

    const rows = [];

    for (const group of groups.values()) {
        if (group.items.length) {
            rows.push({
                name: group.name,
                href: group.href,
                items: group.items,
                depth: group.depth
            });
        }
    }

    diagnostics.mappedChannels = rows.length;
    diagnostics.samples = unmapped;

    return {
        rows,
        diagnostics
    };
}
"""


# ============================================================
# METİN İŞLEMLERİ
# ============================================================

def clean_text(value):
    value = html.unescape(
        str(value or "")
    ).replace("\xa0", " ")

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def normalize_name(value):
    return clean_text(value).upper()


def is_bad_channel(name):
    return normalize_name(name) in BAD_CHANNELS


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
        value = value.replace(
            old,
            new
        ).replace(
            old.upper(),
            new
        )

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    ).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    ).strip("_")

    return value or "channel"


def unique_id(name, used):
    base = slugify(name)
    result = base
    suffix = 2

    while result in used:
        result = f"{base}_{suffix}"
        suffix += 1

    used.add(result)

    return result


# ============================================================
# PROGRAM SATIRI PARSE
# ============================================================

def parse_program_line(line):
    line = clean_text(line)

    match = TIME_RE.search(line)

    if not match:
        return None

    title = clean_text(
        line[:match.start()]
    )

    title = re.sub(
        r"\s*[-–]\s*$",
        "",
        title
    )

    title = CATEGORY_RE.sub(
        "",
        title
    ).strip(" -–")

    if not title:
        return None

    sh = int(match.group("sh"))
    sm = int(match.group("sm"))
    eh = int(match.group("eh"))
    em = int(match.group("em"))

    if (
        sh > 23
        or eh > 23
        or sm > 59
        or em > 59
    ):
        return None

    return {
        "title": title,
        "sh": sh,
        "sm": sm,
        "eh": eh,
        "em": em
    }


# ============================================================
# TARİH İŞLEMLERİ
# ============================================================

def parse_tivibu_date(value):
    value = clean_text(value)

    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt
            )
        except ValueError:
            continue

    return None


def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def read_available_dates(page):
    records = page.locator(
        "a[channeldatebegin][channeldateend]"
    ).evaluate_all(
        """elements => elements.map(element => ({
            begin: element.getAttribute("channeldatebegin"),
            end: element.getAttribute("channeldateend"),
            text: (element.innerText || element.textContent || "").trim()
        }))"""
    )

    result = []
    seen = set()

    for record in records:
        begin = clean_text(
            record.get("begin")
        )

        end = clean_text(
            record.get("end")
        )

        if not begin or not end:
            continue

        key = (
            begin,
            end
        )

        if key in seen:
            continue

        seen.add(key)

        parsed = parse_tivibu_date(begin)

        if not parsed:
            continue

        result.append({
            "begin": begin,
            "end": end,
            "date": parsed.date(),
            "text": clean_text(
                record.get("text")
            )
        })

    return sorted(
        result,
        key=lambda item: item["date"]
    )


def build_target_dates(records, today):
    available = {
        item["date"]: item
        for item in records
        if item["date"] >= today
    }

    targets = []

    for offset in range(DAYS):
        target_day = today + timedelta(
            days=offset
        )

        if target_day not in available:
            raise RuntimeError(
                f"Tivibu tarih listesinde "
                f"{target_day.strftime('%d.%m.%Y')} bulunamadı. "
                "Mevcut XML korunuyor."
            )

        targets.append(
            available[target_day]
        )

    return targets


# ============================================================
# SAYFA AÇMA VE TARİH SEÇME
# ============================================================

def open_live_page(page):
    page.goto(
        LIVE_TV_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=12000
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(
        PAGE_WAIT_MS
    )


def select_date_by_attributes(page, record):
    begin = record["begin"]
    end = record["end"]

    locator = page.locator(
        "a[channeldatebegin][channeldateend]"
    )

    count = locator.count()

    for index in range(count):
        item = locator.nth(index)

        if (
            item.get_attribute("channeldatebegin") == begin
            and item.get_attribute("channeldateend") == end
        ):
            item.evaluate(
                "(element) => element.click()"
            )

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=5000
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(
                DATE_WAIT_MS
            )

            return

    raise RuntimeError(
        "Tarih bağlantısı bulunamadı: "
        + begin
    )


# ============================================================
# PROGRAM SAATLERİNİ TARİHE DÖNÜŞTÜRME
# ============================================================

def assign_times(items, selected_day):
    parsed = [
        item
        for item in (
            parse_program_line(line)
            for line in items
        )
        if item
    ]

    if not parsed:
        return []

    first_minutes = (
        parsed[0]["sh"] * 60
        + parsed[0]["sm"]
    )

    has_rollover = any(
        (
            parsed[index]["sh"] * 60
            + parsed[index]["sm"]
        ) < (
            parsed[index - 1]["sh"] * 60
            + parsed[index - 1]["sm"]
        )
        for index in range(1, len(parsed))
    )

    current_day = (
        selected_day - timedelta(days=1)
        if (
            first_minutes >= 18 * 60
            and has_rollover
        )
        else selected_day
    )

    previous_minutes = None
    result = []

    for item in parsed:
        start_minutes = (
            item["sh"] * 60
            + item["sm"]
        )

        if (
            previous_minutes is not None
            and start_minutes < previous_minutes
        ):
            current_day += timedelta(days=1)

        start = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            item["sh"],
            item["sm"],
            tzinfo=ISTANBUL
        )

        end_day = (
            current_day + timedelta(days=1)
            if (
                item["eh"] * 60 + item["em"]
            ) <= start_minutes
            else current_day
        )

        stop = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            item["eh"],
            item["em"],
            tzinfo=ISTANBUL
        )

        if (
            stop > start
            and (stop - start).total_seconds() <= 86400
        ):
            result.append({
                "title": item["title"],
                "start": start,
                "stop": stop
            })

        previous_minutes = start_minutes

    return result


# ============================================================
# 7 GÜNLÜK VERİ TOPLAMA
# ============================================================

def collect_seven_days(page, today, date_records):
    all_channels = {}
    all_programs = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    for offset, date_record in enumerate(date_records):
        selected_day = today + timedelta(
            days=offset
        )

        label = date_label(
            selected_day,
            today
        )

        print(
            f"[{offset + 1}/{DAYS}] {label} alınıyor...",
            flush=True
        )

        # Her gün sayfa baştan açılır.
        open_live_page(page)

        if offset > 0:
            select_date_by_attributes(
                page,
                date_record
            )

            print(
                "  Tarih seçildi:",
                date_record["begin"]
            )

        extracted = page.evaluate(
            EXTRACT_JS
        )

        rows = extracted.get(
            "rows",
            []
        )

        diagnostics = extracted.get(
            "diagnostics",
            {}
        )

        print(
            "  HTML bağlantısı:",
            diagnostics.get("totalAnchors", 0)
        )

        print(
            "  Program bağlantısı:",
            diagnostics.get("programAnchors", 0)
        )

        print(
            "  Program grubu:",
            len(rows)
        )

        print(
            "  Eşleşen program:",
            diagnostics.get("mappedPrograms", 0)
        )

        print(
            "  Eşleşmeyen program:",
            diagnostics.get("unmappedPrograms", 0)
        )

        if not rows:
            for sample in diagnostics.get(
                "samples",
                []
            )[:5]:
                print(
                    "  Eşleşmeyen örnek:",
                    sample.get("program", "")
                )

            raise RuntimeError(
                f"{label} için kanal/program eşleşmesi bulunamadı. "
                "Mevcut XML korunuyor."
            )

        day_channel_count = 0
        day_program_count = 0

        for row in rows:
            channel_name = clean_text(
                row.get("name")
            )

            if (
                not channel_name
                or is_bad_channel(channel_name)
            ):
                continue

            programs = assign_times(
                row.get("items", []),
                selected_day
            )

            if not programs:
                continue

            if channel_name not in all_channels:
                all_channels[channel_name] = channel_name
                day_channel_count += 1

            for program in programs:
                if (
                    program["stop"] <= window_start
                    or program["start"] >= window_end
                ):
                    continue

                key = (
                    channel_name.casefold(),
                    program["title"].casefold(),
                    program["start"],
                    program["stop"]
                )

                if key not in all_programs:
                    all_programs[key] = {
                        "channel_name": channel_name,
                        **program
                    }

                    day_program_count += 1

        print(
            f"  Günlük yeni kanal: {day_channel_count}"
        )

        print(
            f"  Günlük yeni program: {day_program_count}"
        )

        if (
            offset == 0
            and len(rows) < MIN_CHANNELS
        ):
            raise RuntimeError(
                f"İlk gün yalnızca {len(rows)} program grubu eşleşti. "
                f"En az {MIN_CHANNELS} bekleniyordu. "
                "Mevcut XML korunuyor."
            )

        if day_program_count == 0:
            raise RuntimeError(
                f"{label} için yeni program bulunamadı. "
                "Mevcut XML korunuyor."
            )

    return (
        sorted(
            all_channels.values(),
            key=str.casefold
        ),
        list(all_programs.values())
    )


# ============================================================
# XML OLUŞTURMA
# ============================================================

def build_xml_bytes(channel_names, programs):
    used_ids = set()

    channel_ids = {
        name: unique_id(name, used_ids)
        for name in sorted(
            channel_names,
            key=str.casefold
        )
    }

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL
        }
    )

    for name in sorted(
        channel_names,
        key=str.casefold
    ):
        channel = ET.SubElement(
            root,
            "channel",
            {
                "id": channel_ids[name]
            }
        )

        display = ET.SubElement(
            channel,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display.text = name

    for item in sorted(
        programs,
        key=lambda value: (
            value["start"],
            value["channel_name"].casefold(),
            value["stop"]
        )
    ):
        programme = ET.SubElement(
            root,
            "programme",
            {
                "channel": channel_ids[item["channel_name"]],
                "start": item["start"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": item["stop"].strftime(
                    "%Y%m%d%H%M%S %z"
                )
            }
        )

        title = ET.SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = item["title"]

    ET.indent(
        root,
        space="  "
    )

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True
    )


# ============================================================
# VERİ DOĞRULAMA
# ============================================================

def validate_data(channel_names, programs):
    if len(channel_names) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(channel_names)} kanal bulundu. "
            f"En az {MIN_CHANNELS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            f"En az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    days = {
        item["start"].date()
        for item in programs
    }

    if len(days) < MIN_DAYS:
        raise RuntimeError(
            f"Yalnızca {len(days)} yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )

    channel_set = set(channel_names)

    for item in programs:
        if item["channel_name"] not in channel_set:
            raise RuntimeError(
                "Kanal eşleşmesi geçersiz. "
                "Mevcut XML korunuyor."
            )


# ============================================================
# GÜVENLİ XML KAYDI
# ============================================================

def save_xml_safely(channel_names, programs):
    xml_bytes = build_xml_bytes(
        channel_names,
        programs
    )

    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "wb") as file:
        file.write(xml_bytes)
        file.flush()
        os.fsync(file.fileno())

    try:
        ET.parse(temp_file)

    except ET.ParseError as error:
        if os.path.exists(temp_file):
            os.remove(temp_file)

        raise RuntimeError(
            f"XML doğrulaması başarısız: {error}. "
            "Mevcut XML korunuyor."
        )

    os.replace(
        temp_file,
        OUTPUT_FILE
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
    print("Program bağlantısından kanal bağlantısına eşleştirme")
    print("Her gün ayrı sayfada, tarih değerleriyle seçim")
    print(f"Kaynak: {LIVE_TV_URL}")
    print(
        "Başlangıç:",
        today.strftime("%d.%m.%Y")
    )
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
            print(
                "[1/4] Tivibu sayfası açılıyor...",
                flush=True
            )

            open_live_page(page)

            print(
                "[2/4] Tarih bağlantıları okunuyor...",
                flush=True
            )

            date_records = read_available_dates(
                page
            )

            print(
                "Bulunan tarih bağlantısı:",
                len(date_records)
            )

            targets = build_target_dates(
                date_records,
                today
            )

            print(
                "Alınacak günler:",
                ", ".join(
                    date_label(
                        item["date"],
                        today
                    )
                    for item in targets
                )
            )

            print(
                "[3/4] Kanal ve programlar alınıyor...",
                flush=True
            )

            channel_names, programs = collect_seven_days(
                page,
                today,
                targets
            )

        finally:
            browser.close()

    print(
        "[4/4] Veriler doğrulanıyor...",
        flush=True
    )

    validate_data(
        channel_names,
        programs
    )

    save_xml_safely(
        channel_names,
        programs
    )

    days = sorted({
        item["start"].date()
        for item in programs
    })

    print()
    print("=" * 68)
    print("7 GÜNLÜK EPG OLUŞTURULDU")
    print(f"Toplam kanal: {len(channel_names)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program tarihi sayısı: {len(days)}")
    print(
        "İlk tarih:",
        days[0].strftime("%d.%m.%Y")
    )
    print(
        "Son tarih:",
        days[-1].strftime("%d.%m.%Y")
    )
    print(f"Dosya: {OUTPUT_FILE}")
    print(
        f"Süre: {time.time() - started:.1f} saniye"
    )
    print("=" * 68)


if __name__ == "__main__":
    main()
