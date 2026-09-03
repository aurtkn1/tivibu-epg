#!/usr/bin/env python3

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree.ElementTree import Element, SubElement, ElementTree

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


TIVIBU_URL = "https://www.tivibu.com.tr/canli-tv"
OUTPUT_FILE = "epg.xml"

TIMEZONE = ZoneInfo("Europe/Istanbul")

CHANNEL_EXCLUDE = {
    "Nereden Nereye",
    "Count Me In",
    "Sefiller",
    "Ölü Mevsim",
    "Cebimde Kelimeler",
    "Tivibu Canlı TV, Kanal ve Programlar",
    "Tivibu Nedir?",
    "Favori Kanallarım",
    "Tivibu Spor Canlı İzle",
    "TRT1 Canlı İzle",
}


def clean_text(text):
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def channel_id(name):
    value = clean_text(name).lower()

    replacements = {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def is_real_channel(item):
    href = clean_text(item.get("href", ""))
    text = clean_text(item.get("text", ""))

    if not href or not text:
        return False

    if "/kanallar/" not in href:
        return False

    if text in CHANNEL_EXCLUDE:
        return False

    if len(text) > 70:
        return False

    if re.search(r"\d{1,2}:\d{2}", text):
        return False

    if "Canlı" in text:
        return False

    return True


def parse_program(text):
    text = clean_text(text)

    if not text:
        return None

    # Örnek:
    # Tivibu'nun Renkli Dünyası Yaşam - 00:00 → 03:00 Canlı
    # Sultanların Mirası Yaşam - 23:55 → 00:30 Canlı

    match = re.search(
        r"^(.*?)\s+"
        r"(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|Çocuk|Diğer|Müzik)"
        r"\s*-\s*"
        r"(\d{1,2}:\d{2})"
        r"\s*(?:→|->|–|-)\s*"
        r"(\d{1,2}:\d{2})"
        r"(?:\s+Canlı)?$",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    title = clean_text(match.group(1))

    if not title:
        return None

    return {
        "title": title,
        "start": match.group(2),
        "end": match.group(3),
    }


def make_dt(day, clock):
    hour, minute = map(int, clock.split(":"))

    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=TIMEZONE,
    )


def convert_program_times(programs, target_day):
    result = []
    previous_stop = None

    for index, program in enumerate(programs):
        start = make_dt(target_day, program["start"])
        stop = make_dt(target_day, program["end"])

        # Program gece yarısını geçiyor.
        if stop <= start:
            stop += timedelta(days=1)

        # Günün ilk programı 18:00 sonrası başlayıp gece yarısını
        # geçiyorsa bu program aslında önceki güne aittir.
        if index == 0:
            if start.hour >= 18 and stop > start:
                start -= timedelta(days=1)

        else:
            # Programlar kronolojik sırada ilerlemeli.
            while start < previous_stop:
                start += timedelta(days=1)

            if stop <= start:
                stop += timedelta(days=1)

        previous_stop = stop

        result.append(
            {
                "title": program["title"],
                "start": start,
                "stop": stop,
            }
        )

    return result


def extract_page(page):
    """
    Tivibu DOM yapısı:

    /kanallar/...  -> kanal
    /rv?...        -> program

    Bütün linkler DOM sırasıyla okunur.
    Son görülen gerçek kanal, sonraki programların kanalıdır.
    """

    items = page.locator("a").evaluate_all(
        """
        elements => elements.map(a => ({
            href: a.href || "",
            text: (a.innerText || a.textContent || "").trim()
        }))
        """
    )

    channels = []
    seen_channels = set()

    programs = {}
    current_channel = None

    for item in items:
        text = clean_text(item.get("text", ""))

        if not text:
            continue

        # Kanal
        if is_real_channel(item):
            current_channel = text

            if current_channel not in seen_channels:
                seen_channels.add(current_channel)
                channels.append(current_channel)

            programs.setdefault(current_channel, [])

            continue

        # Program
        if current_channel:
            parsed = parse_program(text)

            if parsed:
                programs[current_channel].append(parsed)

    # Aynı programları temizle
    for name in programs:
        unique = []
        seen = set()

        for program in programs[name]:
            key = (
                program["title"],
                program["start"],
                program["end"],
            )

            if key not in seen:
                seen.add(key)
                unique.append(program)

        programs[name] = unique

    return channels, programs


def close_cookie_popup(page):
    texts = [
        "Tümünü Kabul Et",
        "Tümünü kabul et",
        "Kabul Et",
        "Kabul et",
        "Çerezleri Kabul Et",
        "Çerezleri kabul et",
        "Accept All",
        "Accept",
    ]

    for text in texts:
        try:
            locator = page.get_by_text(
                text,
                exact=True,
            )

            for i in range(locator.count() - 1, -1, -1):
                try:
                    element = locator.nth(i)

                    if element.is_visible():
                        element.click(
                            force=True,
                            timeout=1500,
                        )

                        page.wait_for_timeout(500)
                        return

                except Exception:
                    pass

        except Exception:
            pass


def click_visible_date(page, label):
    try:
        locator = page.get_by_text(
            label,
            exact=True,
        )

        for i in range(locator.count() - 1, -1, -1):
            try:
                element = locator.nth(i)

                if not element.is_visible():
                    continue

                element.scroll_into_view_if_needed(
                    timeout=3000
                )

                element.click(
                    force=True,
                    timeout=3000,
                )

                page.wait_for_timeout(3500)

                return True

            except Exception:
                pass

    except Exception:
        pass

    # JavaScript yedek yöntem
    try:
        return page.evaluate(
            """
            label => {
                const all = [...document.querySelectorAll("*")];

                for (const el of all) {
                    if (
                        el.children.length === 0 &&
                        (el.textContent || "").trim() === label
                    ) {
                        let target = el;

                        for (let i = 0; i < 6 && target; i++) {
                            const tag = target.tagName;

                            if (
                                tag === "BUTTON" ||
                                tag === "A" ||
                                tag === "LI" ||
                                target.getAttribute("role") === "button" ||
                                target.onclick
                            ) {
                                target.click();
                                return true;
                            }

                            target = target.parentElement;
                        }

                        el.click();
                        return true;
                    }
                }

                return false;
            }
            """,
            label,
        )

    except Exception:
        return False


def select_day(page, target_day, index):
    if index == 0:
        # Bugün
        click_visible_date(page, "Bugün")
        page.wait_for_timeout(2500)
        return

    if index == 1:
        # Yarın
        if not click_visible_date(page, "Yarın"):
            # Tarih doğrudan görünüyorsa onu dene.
            click_visible_date(
                page,
                target_day.strftime("%d.%m.%Y"),
            )

        page.wait_for_timeout(3500)
        return

    # 2. günden itibaren doğrudan tarih.
    label = target_day.strftime("%d.%m.%Y")

    if not click_visible_date(page, label):
        print(
            f"UYARI: {label} tarih düğmesi bulunamadı."
        )

    page.wait_for_timeout(3500)


def wait_for_programs(page):
    for _ in range(6):
        try:
            count = page.locator(
                "a"
            ).evaluate_all(
                """
                elements => elements.filter(a => {
                    const t = (a.innerText || "").trim();
                    return /\\d{1,2}:\\d{2}/.test(t)
                        && /→|->|–/.test(t);
                }).length
                """
            )

            if count > 0:
                return count

        except Exception:
            pass

        page.wait_for_timeout(1500)

    return 0


def get_schedule(page, target_day, index):
    print()
    print("=" * 60)
    print(
        "Tarih:",
        target_day.strftime("%d.%m.%Y"),
    )
    print("=" * 60)

    select_day(
        page,
        target_day,
        index,
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pass

    count = wait_for_programs(page)

    print(
        "DOM program bağlantısı:",
        count,
    )

    channels, programs = extract_page(page)

    total = sum(
        len(v)
        for v in programs.values()
    )

    print(
        "Kanal:",
        len(channels),
    )

    print(
        "Program:",
        total,
    )

    return channels, programs


def build_xml(channels, programs):
    tv = Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": "https://www.tivibu.com.tr/",
        },
    )

    # CHANNEL
    for name in channels:
        if name in CHANNEL_EXCLUDE:
            continue

        cid = channel_id(name)

        if not cid:
            continue

        channel = SubElement(
            tv,
            "channel",
            {
                "id": cid,
            },
        )

        display = SubElement(
            channel,
            "display-name",
            {
                "lang": "tr",
            },
        )

        display.text = name

    # PROGRAM
    for name in channels:
        if name in CHANNEL_EXCLUDE:
            continue

        cid = channel_id(name)

        if not cid:
            continue

        for program in programs.get(name, []):
            programme = SubElement(
                tv,
                "programme",
                {
                    "channel": cid,
                    "start": program["start"].strftime(
                        "%Y%m%d%H%M%S %z"
                    ),
                    "stop": program["stop"].strftime(
                        "%Y%m%d%H%M%S %z"
                    ),
                },
            )

            title = SubElement(
                programme,
                "title",
                {
                    "lang": "tr",
                },
            )

            title.text = program["title"]

    try:
        import xml.etree.ElementTree as ET
        ET.indent(tv, space="  ")
    except Exception:
        pass

    ElementTree(tv).write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    now = datetime.now(TIMEZONE)

    first_day = now.date()

    days = [
        first_day + timedelta(days=i)
        for i in range(7)
    ]

    print()
    print("=" * 70)
    print("TIVIBU EPG")
    print("=" * 70)
    print(
        "Dönem:",
        days[0].strftime("%d.%m.%Y"),
        "->",
        days[-1].strftime("%d.%m.%Y"),
    )
    print("=" * 70)

    all_channels = []
    all_programs = {}

    successful = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080,
            },
        )

        page = context.new_page()

        print("Tivibu açılıyor...")

        page.goto(
            TIVIBU_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(4000)

        close_cookie_popup(page)

        try:
            page.wait_for_selector(
                'a[href*="/kanallar/"]',
                timeout=30000,
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3000)

        for index, day in enumerate(days):
            try:
                channels, raw_programs = get_schedule(
                    page,
                    day,
                    index,
                )

                # Kanal listesini birleştir.
                for name in channels:
                    if (
                        name not in CHANNEL_EXCLUDE
                        and name not in all_channels
                    ):
                        all_channels.append(name)

                # Günün programlarını gerçek tarih/saatlere çevir.
                day_start = datetime(
                    day.year,
                    day.month,
                    day.day,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                day_end = day_start + timedelta(days=1)

                added_today = 0

                for name, items in raw_programs.items():
                    absolute = convert_program_times(
                        items,
                        day,
                    )

                    for program in absolute:
                        # Bu güne değmeyen programı alma.
                        if program["stop"] <= day_start:
                            continue

                        if program["start"] >= day_end:
                            continue

                        all_programs.setdefault(
                            name,
                            [],
                        )

                        duplicate = any(
                            x["title"] == program["title"]
                            and x["start"] == program["start"]
                            and x["stop"] == program["stop"]
                            for x in all_programs[name]
                        )

                        if not duplicate:
                            all_programs[name].append(
                                program
                            )
                            added_today += 1

                if added_today > 0:
                    successful += 1

                print(
                    f"{day.strftime('%d.%m.%Y')}: "
                    f"{added_today} program eklendi"
                )

            except Exception as exc:
                print(
                    f"{day.strftime('%d.%m.%Y')} HATA:"
                )
                print(
                    repr(exc)
                )

        browser.close()

    # Programları sırala.
    for name in all_programs:
        all_programs[name].sort(
            key=lambda x: x["start"]
        )

    total_programs = sum(
        len(v)
        for v in all_programs.values()
    )

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)
    print(
        "Başarılı gün:",
        f"{successful}/7",
    )
    print(
        "Kanal:",
        len(all_channels),
    )
    print(
        "Toplam program:",
        total_programs,
    )
    print("=" * 70)

    if total_programs == 0:
        raise RuntimeError(
            "0 program bulundu. Tivibu DOM yapısı veya tarih seçici değişmiş."
        )

    build_xml(
        all_channels,
        all_programs,
    )

    print()
    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )


if __name__ == "__main__":
    main()
