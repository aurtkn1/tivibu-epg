
import os
import re
import html
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = BASE_URL + "/canli-tv"
OUTPUT_FILE = "epg.xml"

TIMEZONE = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
DATE_WAIT_MS = 2500
PAGE_WAIT_MS = 2000

MIN_CHANNELS = 10
MIN_PROGRAMS = 500
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

CHANNEL_ID_RE = re.compile(
    r"(?<![a-z0-9])(ch[a-z0-9_-]{1,})(?![a-z0-9])",
    re.IGNORECASE
)


# ============================================================
# METİN İŞLEMLERİ
# ============================================================

def clean_text(value):
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


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
        value = value.replace(old, new)
        value = value.replace(old.upper(), new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")

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
# KANAL KİMLİĞİ BULMA
# ============================================================

def extract_channel_id(value):
    value = clean_text(value)

    if not value:
        return ""

    match = CHANNEL_ID_RE.search(value)

    if match:
        return match.group(1).lower()

    return ""


def get_anchor_channel_id(anchor):
    """
    Bağlantı adresi ve bağlantı üzerindeki diğer alanlardan
    kanal kimliğini bulur.
    """

    values = []

    for key in (
        "href",
        "title",
        "aria-label",
        "data-id",
        "data-channel",
        "data-channel-id",
        "data-code",
        "id",
    ):
        value = anchor.get(key)

        if value:
            values.append(str(value))

    return extract_channel_id(" ".join(values))


# ============================================================
# SAYFA DOM VERİSİNİ OKUMA
# ============================================================

READ_PAGE_JS = r"""
() => {
    const norm = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const anchors = [...document.querySelectorAll("a")];

    const result = [];

    for (const a of anchors) {
        const img = a.querySelector("img");

        const attributes = {};

        for (const attr of [...a.attributes]) {
            attributes[attr.name] = attr.value;
        }

        result.push({
            href: a.getAttribute("href") || "",
            text: norm(a.innerText || a.textContent || ""),
            title: a.getAttribute("title") || "",
            aria: a.getAttribute("aria-label") || "",
            image_alt: img ? (img.getAttribute("alt") || "") : "",
            attributes: attributes
        });
    }

    return {
        anchors: result,
        page_title: document.title,
        url: location.href
    };
}
"""


def extract_page_data(page):
    """
    Sayfadaki bağlantıları tek seferde okur.

    Kanal bağlantıları ile program bağlantıları ayrı ayrı
    değerlendirilir.

    Program grupları kanal kimliğine göre oluşturulur.
    """

    data = page.evaluate(READ_PAGE_JS)

    anchors = data.get("anchors", [])

    channel_names = {}
    program_groups = {}

    program_anchor_count = 0
    channel_anchor_count = 0

    for anchor in anchors:
        href = clean_text(anchor.get("href"))
        text = clean_text(anchor.get("text"))

        title = clean_text(anchor.get("title"))
        aria = clean_text(anchor.get("aria"))
        image_alt = clean_text(anchor.get("image_alt"))

        attrs = anchor.get("attributes") or {}

        attribute_text = " ".join(
            str(value)
            for value in attrs.values()
            if value
        )

        combined = " ".join([
            href,
            text,
            title,
            aria,
            image_alt,
            attribute_text,
        ])

        channel_id = extract_channel_id(combined)

        if not channel_id:
            continue

        # Program bağlantısı mı?
        is_program = (
            "datatype=2" in combined.lower()
            or "datatype%3d2" in combined.lower()
            or re.search(r"(?:^|[?&|])i\s*=\s*2", combined, re.I)
        )

        time_match = TIME_RE.search(text)

        if is_program and time_match:
            program_anchor_count += 1

            program_groups.setdefault(
                channel_id,
                []
            ).append(text)

            continue

        # Program bağlantısı değilse kanal adı adayıdır.
        name_candidates = [
            text,
            image_alt,
            title,
            aria,
            clean_text(attrs.get("data-name", "")),
            clean_text(attrs.get("data-channel-name", "")),
        ]

        for candidate in name_candidates:
            candidate = clean_text(candidate)

            if not candidate:
                continue

            if TIME_RE.search(candidate):
                continue

            if len(candidate) > 80:
                continue

            if candidate.lower().startswith("http"):
                continue

            if is_bad_channel(candidate):
                continue

            channel_names.setdefault(
                channel_id,
                candidate
            )

            channel_anchor_count += 1
            break

    # Aynı program satırlarını temizle.
    for channel_id, items in list(program_groups.items()):
        unique = []
        seen = set()

        for item in items:
            key = item.casefold()

            if key in seen:
                continue

            seen.add(key)
            unique.append(item)

        program_groups[channel_id] = unique

    return {
        "channel_names": channel_names,
        "program_groups": program_groups,
        "anchor_count": len(anchors),
        "channel_anchor_count": channel_anchor_count,
        "program_anchor_count": program_anchor_count,
    }


# ============================================================
# TARİH İŞLEMLERİ
# ============================================================

def parse_tivibu_date(value):
    value = clean_text(value)

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


def read_available_dates(page):
    """
    Tarih bağlantılarının begin/end değerlerini okur.
    """

    records = page.locator(
        "a[channeldatebegin][channeldateend]"
    ).evaluate_all(
        """elements => elements.map(el => ({
            begin: el.getAttribute("channeldatebegin"),
            end: el.getAttribute("channeldateend"),
            text: (el.innerText || el.textContent || "").trim()
        }))"""
    )

    result = []
    seen = set()

    for item in records:
        begin = clean_text(item.get("begin"))
        end = clean_text(item.get("end"))

        if not begin or not end:
            continue

        key = (begin, end)

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
            "text": clean_text(item.get("text")),
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
        target_day = today + timedelta(days=offset)

        if target_day not in available:
            raise RuntimeError(
                f"Tivibu tarih listesinde "
                f"{target_day.strftime('%d.%m.%Y')} bulunamadı. "
                "Mevcut XML korunuyor."
            )

        targets.append(available[target_day])

    return targets


def date_label(day, today):
    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def open_live_page(page):
    page.goto(
        LIVE_URL,
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

    page.wait_for_timeout(PAGE_WAIT_MS)


def select_date_by_values(page, date_record):
    """
    Tarihi görünen yazıya göre değil, Tivibu'nun gerçek
    channeldatebegin / channeldateend değerlerine göre seçer.
    """

    begin = date_record["begin"]
    end = date_record["end"]

    locator = page.locator(
        "a[channeldatebegin][channeldateend]"
    )

    count = locator.count()

    for index in range(count):
        item = locator.nth(index)

        item_begin = item.get_attribute(
            "channeldatebegin"
        )

        item_end = item.get_attribute(
            "channeldateend"
        )

        if item_begin != begin or item_end != end:
            continue

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

        page.wait_for_timeout(DATE_WAIT_MS)

        return

    raise RuntimeError(
        "Tarih bağlantısı bulunamadı: "
        + begin
    )


# ============================================================
# PROGRAM SAATLERİNİ PARSE ETME
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
    ).strip()

    title = re.sub(
        r"\s+(?:Yaşam|Film|Dizi|Spor|Haber|Çocuk|Müzik|Belgesel|Sinema|Ulusal|Global|Diğer)\s*$",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()

    if not title:
        return None

    sh = int(match.group("sh"))
    sm = int(match.group("sm"))
    eh = int(match.group("eh"))
    em = int(match.group("em"))

    if sh > 23 or eh > 23:
        return None

    if sm > 59 or em > 59:
        return None

    return {
        "title": title,
        "sh": sh,
        "sm": sm,
        "eh": eh,
        "em": em,
    }


def assign_program_times(items, selected_day):
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
        if first_minutes >= 18 * 60 and has_rollover
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
            tzinfo=TIMEZONE
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
            tzinfo=TIMEZONE
        )

        if (
            stop > start
            and (stop - start).total_seconds() <= 86400
        ):
            result.append({
                "title": item["title"],
                "start": start,
                "stop": stop,
            })

        previous_minutes = start_minutes

    return result


# ============================================================
# 7 GÜNLÜK VERİ TOPLAMA
# ============================================================

def collect_seven_days(page, today, date_records):
    all_channels = {}
    all_programs = {}

    # Bugün bulunan kanal kimliklerini ve adlarını saklarız.
    # Sonraki günlerde kanal adı DOM'da görünmese bile
    # aynı kanal kimliği bu eşleşmeden çözümlenir.
    channel_id_to_name = {}

    window_start = datetime(
        today.year,
        today.month,
        today.day,
        0,
        0,
        tzinfo=TIMEZONE
    )

    end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        0,
        0,
        tzinfo=TIMEZONE
    )

    for offset, date_record in enumerate(date_records):
        selected_day = today + timedelta(days=offset)
        label = date_label(selected_day, today)

        print(
            f"[{offset + 1}/{DAYS}] {label} alınıyor...",
            flush=True
        )

        # Her gün için sayfayı yeniden aç.
        # Böylece önceki günün DOM değişiklikleri taşınmaz.
        open_live_page(page)

        if offset > 0:
            select_date_by_values(
                page,
                date_record
            )

            print(
                "  Tarih seçildi:",
                date_record["begin"]
            )

        extracted = extract_page_data(page)

        page_channel_names = extracted["channel_names"]
        program_groups = extracted["program_groups"]

        print(
            "  HTML bağlantısı:",
            extracted["anchor_count"]
        )

        print(
            "  Kanal adı bulunan:",
            len(page_channel_names)
        )

        print(
            "  Program grubu:",
            len(program_groups)
        )

        # Önce o gün sayfada bulunan kanal adlarını eşleştir.
        for channel_id, name in page_channel_names.items():
            name = clean_text(name)

            if not name or is_bad_channel(name):
                continue

            channel_id_to_name[channel_id] = name

        # Program gruplarını kalıcı kanal kimliği tablosuyla eşleştir.
        matched_count = 0
        day_program_count = 0
        unmatched_ids = []

        for channel_id, items in program_groups.items():
            channel_name = channel_id_to_name.get(
                channel_id
            )

            if not channel_name:
                unmatched_ids.append(channel_id)
                continue

            if is_bad_channel(channel_name):
                continue

            programs = assign_program_times(
                items,
                selected_day
            )

            if not programs:
                continue

            matched_count += 1

            all_channels[channel_name] = channel_name

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
                    program["stop"],
                )

                if key in all_programs:
                    continue

                all_programs[key] = {
                    "channel_name": channel_name,
                    **program,
                }

                day_program_count += 1

        print(
            "  Eşleşen kanal:",
            matched_count
        )

        print(
            "  Günlük yeni program:",
            day_program_count
        )

        if unmatched_ids:
            print(
                "  Eşleşmeyen kanal kimliği:",
                ", ".join(unmatched_ids[:10])
            )

        # İlk gün eşleşme tablosu oluşmadıysa sonraki günlere
        # hatalı/eksik isim aktarılmasını önle.
        if offset == 0 and matched_count < MIN_CHANNELS:
            raise RuntimeError(
                "İlk gün kanal eşleşmesi yetersiz. "
                f"{matched_count} kanal bulundu. "
                "Mevcut XML korunuyor."
            )

        if matched_count == 0:
            raise RuntimeError(
                f"{label} için hiçbir kanal eşleşmedi. "
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
            "generator-info-url": LIVE_URL,
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
                ),
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

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

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
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )

    channel_set = set(channel_names)

    invalid = [
        item
        for item in programs
        if item["channel_name"] not in channel_set
    ]

    if invalid:
        raise RuntimeError(
            f"{len(invalid)} programın kanal eşleşmesi geçersiz. "
            "Mevcut XML korunuyor."
        )


def save_xml_safely(channel_names, programs):
    xml_bytes = build_xml_bytes(
        channel_names,
        programs
    )

    # XML'i önce geçici dosyaya yaz.
    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "wb") as file:
        file.write(xml_bytes)
        file.flush()
        os.fsync(file.fileno())

    # Geçici XML'i tekrar açıp doğrula.
    try:
        ET.parse(temp_file)
    except ET.ParseError as exc:
        if os.path.exists(temp_file):
            os.remove(temp_file)

        raise RuntimeError(
            f"Oluşturulan XML geçersiz: {exc}. "
            "Mevcut XML korunuyor."
        )

    # Doğrulama başarılıysa eski XML'in yerine geçir.
    os.replace(
        temp_file,
        OUTPUT_FILE
    )


# ============================================================
# ANA PROGRAM
# ============================================================

def main():
    started = time.time()

    today = datetime.now(TIMEZONE).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Her gün ayrı sayfada, tarih değerleriyle seçim")
    print(f"Kaynak: {LIVE_URL}")
    print(f"Başlangıç: {today.strftime('%d.%m.%Y')}")
    print("=" * 68)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1440,
                "height": 1100,
            }
        )

        page.set_default_timeout(
            PAGE_TIMEOUT
        )

        print("[1/4] Tivibu sayfası açılıyor...")

        open_live_page(page)

        print("[2/4] Tarih bağlantıları okunuyor...")

        date_records = read_available_dates(page)

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

        print("[3/4] Kanal ve programlar alınıyor...")

        try:
            channel_names, programs = collect_seven_days(
                page,
                today,
                targets
            )
        finally:
            browser.close()

    print("[4/4] Veriler doğrulanıyor...")

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
    print(f"İlk tarih: {days[0].strftime('%d.%m.%Y')}")
    print(f"Son tarih: {days[-1].strftime('%d.%m.%Y')}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(f"Süre: {time.time() - started:.1f} saniye")
    print("=" * 68)


if __name__ == "__main__":
    main()
