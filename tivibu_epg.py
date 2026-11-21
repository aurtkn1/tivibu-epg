
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tivibu 7 Günlük EPG Oluşturucu

Eski ve doğrulanmamış GetMultiPrevueData servisi kullanılmaz.
Tivibu canlı TV sayfası Chromium ile açılır.
Kanal kodları bağlantılardan ve kanal kartlarının HTML yapısından
alınır. Programlar kanal kodları üzerinden eşleştirilir.

Bütün doğrulamalar tamamlanmadan mevcut epg.xml değiştirilmez.
"""

import os
import re
import time
import html
import tempfile
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, unquote
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS = 7
TIMEZONE = ZoneInfo("Europe/Istanbul")

MIN_CHANNELS = 20
MIN_PROGRAMS = 100

PAGE_TIMEOUT = 90000
WAIT_AFTER_DATE_CLICK = 2500

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# GENEL YARDIMCILAR
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")

    return re.sub(r"\s+", " ", value).strip()


def normalize_code(value):
    if not value:
        return ""

    match = re.search(
        r"ch\d{6,}",
        unquote(str(value)),
        re.IGNORECASE
    )

    return match.group(0) if match else ""


def parse_clock(value):
    value = clean_text(value)

    match = re.search(
        r"(\d{1,2}):(\d{2})",
        value
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def make_datetime(day, clock):
    if clock is None:
        return None

    hour, minute = clock

    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=TIMEZONE
    )


def parse_date(value):
    value = clean_text(value)

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


def get_program_code(url):
    if not url:
        return ""

    decoded = unquote(url)

    code = normalize_code(decoded)

    if code:
        return code

    parsed = urlparse(url)

    for value in parse_qs(parsed.query).values():
        for item in value:
            code = normalize_code(item)

            if code:
                return code

    return ""


# ============================================================
# XML
# ============================================================

def add_text(parent, tag, value, lang=None):
    attrs = {}

    if lang:
        attrs["lang"] = lang

    node = ET.SubElement(parent, tag, attrs)
    node.text = clean_text(value)

    return node


def build_xml(channels, programs):

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL,
        }
    )

    for code in sorted(
        channels,
        key=lambda x: channels[x]["name"].casefold()
    ):

        info = channels[code]

        channel = ET.SubElement(
            root,
            "channel",
            {"id": code}
        )

        add_text(
            channel,
            "display-name",
            info["name"],
            "tr"
        )

        icon = info.get("icon", "")

        if icon.startswith(("http://", "https://")):
            ET.SubElement(
                channel,
                "icon",
                {"src": icon}
            )

    for program in sorted(
        programs,
        key=lambda x: (
            x["begin"],
            x["channel"]
        )
    ):

        node = ET.SubElement(
            root,
            "programme",
            {
                "start": program["begin"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "stop": program["end"].strftime(
                    "%Y%m%d%H%M%S %z"
                ),
                "channel": program["channel"],
            }
        )

        add_text(
            node,
            "title",
            program["title"],
            "tr"
        )

        if program.get("description"):
            add_text(
                node,
                "desc",
                program["description"],
                "tr"
            )

        if program.get("category"):
            add_text(
                node,
                "category",
                program["category"],
                "tr"
            )

    ET.indent(root, space="  ")

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True
    )


# ============================================================
# SAYFA TARİHLERİ
# ============================================================

def read_date_options(page):

    result = page.locator("a").evaluate_all("""
        elements => elements.map(a => ({
            text: (a.innerText || '').trim(),
            begin: a.getAttribute('channeldatebegin') || '',
            end: a.getAttribute('channeldateend') || '',
            html: a.outerHTML
        }))
    """)

    dates = {}

    for item in result:

        begin = clean_text(item.get("begin"))
        end = clean_text(item.get("end"))

        if not begin or not end:
            continue

        day = parse_date(begin)

        if day is None:
            continue

        dates.setdefault(
            day,
            {
                "begin": begin,
                "end": end,
                "text": item.get("text", "")
            }
        )

    return dates


def select_date(page, date_info):

    begin = date_info["begin"]

    locator = page.locator(
        "a[channeldatebegin]"
    ).filter(
        has=page.locator("span")
    )

    try:

        target = page.locator(
            "a[channeldatebegin]"
        ).evaluate_all(
            """
            (elements, begin) => {
                const found = elements.find(
                    el => el.getAttribute('channeldatebegin') === begin
                );
                return found ? true : false;
            }
            """,
            begin
        )

        if not target:
            raise RuntimeError(
                "Tarih bağlantısı sayfada bulunamadı: " + begin
            )

        page.locator(
            "a[channeldatebegin]"
        ).filter(
            has_text=re.compile(r".*")
        ).evaluate_all(
            """
            (elements, begin) => {
                const el = elements.find(
                    x => x.getAttribute('channeldatebegin') === begin
                );
                if (el) el.click();
            }
            """,
            begin
        )

        page.wait_for_timeout(WAIT_AFTER_DATE_CLICK)

    except Exception as exc:

        raise RuntimeError(
            f"Tarih seçilemedi: {begin}. Hata: {exc}"
        ) from exc


# ============================================================
# KANALLAR
# ============================================================

def extract_channels(page):

    records = page.locator("a").evaluate_all("""
        elements => elements.map(a => {
            let parent = a;
            let combined = '';

            for (let i = 0; i < 9 && parent; i++) {
                combined += ' ' + (parent.outerHTML || '');
                parent = parent.parentElement;
            }

            const image = a.querySelector('img');

            return {
                text: (a.innerText || '').trim(),
                href: a.href || '',
                html: combined,
                image: image ? (
                    image.getAttribute('src') ||
                    image.getAttribute('data-src') ||
                    ''
                ) : ''
            };
        })
    """)

    channels = {}

    for item in records:

        href = item.get("href", "")

        if "/kanallar/" not in href:
            continue

        name = clean_text(item.get("text"))

        if not name:
            continue

        name = name.split("\n")[0].strip()

        if not name:
            continue

        code = normalize_code(
            item.get("html", "")
        )

        if not code:
            continue

        icon = item.get("image", "")

        if icon:
            icon = urljoin(BASE_URL, icon)

        channels[code] = {
            "name": name,
            "icon": icon
        }

    return channels


# ============================================================
# PROGRAMLAR
# ============================================================

def extract_programs(page, target_day, channels):

    records = page.locator("a").evaluate_all("""
        elements => elements.map(a => ({
            text: (a.innerText || '').trim(),
            href: a.href || '',
            title: a.getAttribute('title') || ''
        }))
    """)

    programs = {}

    for item in records:

        code = get_program_code(
            item.get("href", "")
        )

        if not code:
            continue

        text = clean_text(
            item.get("text") or item.get("title")
        )

        if not text:
            continue

        # Program metni örneği:
        # Belgelerle Tarih Yaşam - 00:00 → 00:45 Canlı

        match = re.search(
            r"^(.*?)\s+"
            r"(?:Yaşam|Film|Dizi|Haber|Spor|Çocuk|Müzik|Belgesel)"
            r"\s*[-–]\s*"
            r"(\d{1,2}:\d{2})"
            r"\s*(?:→|->|-)\s*"
            r"(\d{1,2}:\d{2})",
            text,
            re.IGNORECASE
        )

        if not match:

            # Bazı bağlantılarda kategori bulunmayabilir.
            match = re.search(
                r"^(.*?)\s*[-–]\s*"
                r"(\d{1,2}:\d{2})"
                r"\s*(?:→|->)\s*"
                r"(\d{1,2}:\d{2})",
                text
            )

        if not match:
            continue

        title = clean_text(match.group(1))

        begin_clock = parse_clock(match.group(2))
        end_clock = parse_clock(match.group(3))

        if not title or not begin_clock or not end_clock:
            continue

        begin_dt = make_datetime(
            target_day,
            begin_clock
        )

        end_dt = make_datetime(
            target_day,
            end_clock
        )

        if end_dt <= begin_dt:
            end_dt += timedelta(days=1)

        if code not in channels:
            continue

        key = (
            code,
            begin_dt,
            end_dt,
            title.casefold()
        )

        programs[key] = {
            "channel": code,
            "begin": begin_dt,
            "end": end_dt,
            "title": title,
            "description": "",
            "category": ""
        }

    return programs


# ============================================================
# TARİH DEĞİŞİMİNDE KANAL KODU KORUMA
# ============================================================

def merge_channels(target, source):

    for code, info in source.items():

        if code not in target:
            target[code] = info
            continue

        if (
            not target[code].get("icon")
            and info.get("icon")
        ):
            target[code]["icon"] = info["icon"]


# ============================================================
# ANA İŞLEM
# ============================================================

def main():

    started = time.time()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Chromium sayfa taraması")
    print("Kanal koduyla eşleştirme")
    print("=" * 68)

    channels = {}
    all_programs = {}

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )

        page = context.new_page()

        page.set_default_timeout(PAGE_TIMEOUT)

        print("[1/4] Tivibu sayfası açılıyor...")

        response = page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        if response is None or response.status >= 400:
            raise RuntimeError(
                "Tivibu sayfası açılamadı."
            )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=20000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3000)

        print("[2/4] Tarih seçenekleri okunuyor...")

        date_options = read_date_options(page)

        today = datetime.now(TIMEZONE).date()

        target_dates = [
            date_options[day]
            for day in sorted(date_options)
            if day >= today
        ][:DAYS]

        if len(target_dates) != DAYS:

            raise RuntimeError(
                f"{DAYS} gün bekleniyordu, "
                f"{len(target_dates)} tarih bulundu. "
                "Mevcut XML korunuyor."
            )

        print(
            f"Bulunan tarih seçeneği: {len(date_options)}"
        )

        print(
            f"Alınacak gün: {len(target_dates)}"
        )

        print("[3/4] Kanal ve programlar alınıyor...")

        successful_days = 0

        for index, date_info in enumerate(
            target_dates,
            1
        ):

            target_day = parse_date(
                date_info["begin"]
            )

            label = target_day.strftime(
                "%d.%m.%Y"
            )

            print(
                f"[{index}/{DAYS}] {label} alınıyor...",
                end=" ",
                flush=True
            )

            if index > 1:
                select_date(
                    page,
                    date_info
                )

            day_channels = extract_channels(page)

            if not day_channels:
                print("Kanal kodu bulunamadı.")
            else:
                merge_channels(
                    channels,
                    day_channels
                )

            day_programs = extract_programs(
                page,
                target_day,
                channels
            )

            for key, program in day_programs.items():
                all_programs[key] = program

            successful_days += 1

            print(
                f"{len(day_channels)} kanal kartı, "
                f"{len(day_programs)} program"
            )

            time.sleep(0.5)

        browser.close()

    # ========================================================
    # SON KONTROLLER
    # ========================================================

    programs = list(all_programs.values())

    named_channels = {
        code: info
        for code, info in channels.items()
        if info.get("name")
    }

    if successful_days != DAYS:
        raise RuntimeError(
            "Yedi günlük veri tamamlanamadı. "
            "Mevcut XML korunuyor."
        )

    if len(named_channels) < MIN_CHANNELS:
        raise RuntimeError(
            f"Yalnızca {len(named_channels)} kanal bulundu. "
            f"En az {MIN_CHANNELS} bekleniyordu. "
            "Kanal kodu eşleştirmesi doğrulanamadı. "
            "Mevcut XML korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:
        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            f"En az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut XML korunuyor."
        )

    invalid = [
        item
        for item in programs
        if item["channel"] not in named_channels
    ]

    if invalid:
        raise RuntimeError(
            f"{len(invalid)} programın kanalı bulunamadı. "
            "Mevcut XML korunuyor."
        )

    days_found = sorted({
        item["begin"].date()
        for item in programs
    })

    if len(days_found) < 6:
        raise RuntimeError(
            f"Yalnızca {len(days_found)} yayın günü bulundu. "
            "Mevcut XML korunuyor."
        )

    # ========================================================
    # XML OLUŞTUR VE GÜVENLİ KAYDET
    # ========================================================

    print("[4/4] XML oluşturuluyor...")

    xml_bytes = build_xml(
        named_channels,
        programs
    )

    # XML dosyasını değiştirmeden önce geçerliliğini kontrol et.
    ET.fromstring(xml_bytes)

    temp_file = OUTPUT_FILE + ".tmp"

    try:

        with open(temp_file, "wb") as file:

            file.write(xml_bytes)
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temp_file,
            OUTPUT_FILE
        )

    finally:

        if os.path.exists(temp_file):
            os.remove(temp_file)

    # ========================================================
    # SONUÇ
    # ========================================================

    print()
    print("=" * 68)
    print("TIVIBU EPG İŞLEMİ TAMAMLANDI")
    print("=" * 68)

    print(f"Başarılı gün: {successful_days}/{DAYS}")
    print(f"Toplam kanal: {len(named_channels)}")
    print(f"Toplam program: {len(programs)}")
    print(f"Program bulunan gün: {len(days_found)}")

    if days_found:
        print(
            "İlk gün: "
            + days_found[0].strftime("%d.%m.%Y")
        )

        print(
            "Son gün: "
            + days_found[-1].strftime("%d.%m.%Y")
        )

    print(f"Dosya: {OUTPUT_FILE}")

    print(
        f"Süre: {time.time() - started:.1f} saniye"
    )

    print("=" * 68)


if __name__ == "__main__":
    main()
