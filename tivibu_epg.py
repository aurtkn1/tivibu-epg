#!/usr/bin/env python3

import re
import time
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

CATEGORY_WORDS = (
    "Film",
    "Dizi",
    "Yaşam",
    "Spor",
    "Haber",
    "Belgesel",
    "Çocuk",
    "Diğer",
    "Müzik",
)


def clean_text(text):
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_channel_name(name):
    name = clean_text(name)

    if name in CHANNEL_EXCLUDE:
        return ""

    return name


def is_channel_anchor(item):
    href = item.get("href", "")
    text = clean_text(item.get("text", ""))

    if not href or not text:
        return False

    if "/kanallar/" not in href:
        return False

    if text in CHANNEL_EXCLUDE:
        return False

    if len(text) > 80:
        return False

    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        return False

    if "Canlı" in text:
        return False

    return True


def parse_program_text(text):
    """
    Tivibu program linklerinden örnekler:

    Tivibu'nun Renkli Dünyası Yaşam - 00:00 → 03:00 Canlı
    Sultanların Mirası Yaşam - 23:55 → 00:30 Canlı
    """

    text = clean_text(text)

    if not text:
        return None

    # Önce standart ok işareti
    pattern = re.compile(
        r"^(.*?)\s+"
        r"(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|Çocuk|Diğer|Müzik)"
        r"\s*-\s*"
        r"(\d{1,2}:\d{2})"
        r"\s*(?:→|->|–|-)\s*"
        r"(\d{1,2}:\d{2})"
        r"(?:\s+Canlı)?$",
        re.IGNORECASE,
    )

    match = pattern.match(text)

    if match:
        title = clean_text(match.group(1))
        start_time = match.group(2)
        end_time = match.group(3)

        if title:
            return {
                "title": title,
                "start": start_time,
                "end": end_time,
            }

    # Daha esnek yedek parser
    fallback = re.search(
        r"^(.*?)\s+(?:Film|Dizi|Yaşam|Spor|Haber|Belgesel|Çocuk|Diğer|Müzik)"
        r"\s*-\s*(\d{1,2}:\d{2})\s*(?:→|->|–|-)\s*(\d{1,2}:\d{2})",
        text,
        re.IGNORECASE,
    )

    if fallback:
        title = clean_text(fallback.group(1))

        if title:
            return {
                "title": title,
                "start": fallback.group(2),
                "end": fallback.group(3),
            }

    return None


def make_datetime(day, clock):
    hour, minute = map(int, clock.split(":"))
    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=TIMEZONE,
    )


def build_absolute_programs(channel_programs, target_day):
    """
    Tivibu bazı kanallarda günün ilk programını:

        23:55 → 00:30

    şeklinde gösteriyor.

    Bu durumda 23:55'in bir önceki güne ait olduğunu
    otomatik olarak hesaplıyoruz.
    """

    result = []

    if not channel_programs:
        return result

    previous_stop = None

    for index, program in enumerate(channel_programs):
        start_clock = program["start"]
        end_clock = program["end"]

        start_dt = make_datetime(target_day, start_clock)
        end_dt = make_datetime(target_day, end_clock)

        # Gece yarısını geçiyor
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        # İlk kayıt 18:00+ başlayıp gece yarısını geçiyorsa,
        # büyük ihtimalle bir önceki günün programıdır.
        if index == 0:
            if (
                start_dt.hour >= 18
                and end_dt.date() > start_dt.date()
            ):
                start_dt -= timedelta(days=1)
        else:
            # Önceki programın bitişinden önce görünüyorsa
            # bir sonraki güne taşı.
            while start_dt < previous_stop:
                start_dt += timedelta(days=1)

            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

        previous_stop = end_dt

        result.append(
            {
                "title": program["title"],
                "start": start_dt,
                "stop": end_dt,
            }
        )

    return result


def extract_dom_data(page):
    """
    Sayfadaki bütün <a> elemanlarını DOM sırasıyla alır.

    Önemli:
    /kanallar/...  = gerçek kanal bağlantısı
    /rv?...        = program bağlantısı

    Böylece eski parser'daki kanal/program karışıklığı ortadan kalkar.
    """

    items = page.locator("a").evaluate_all(
        """
        els => els.map(a => ({
            href: a.href || "",
            text: (a.innerText || a.textContent || "").trim()
        }))
        """
    )

    channels = []
    channel_seen = set()

    programs_by_channel = {}

    current_channel = None

    for item in items:
        href = item.get("href", "")
        text = clean_text(item.get("text", ""))

        if not text:
            continue

        # -----------------------------------------
        # GERÇEK KANAL
        # -----------------------------------------
        if is_channel_anchor(item):
            channel_name = normalize_channel_name(text)

            if not channel_name:
                continue

            current_channel = channel_name

            if channel_name not in channel_seen:
                channel_seen.add(channel_name)
                channels.append(channel_name)

            if channel_name not in programs_by_channel:
                programs_by_channel[channel_name] = []

            continue

        # -----------------------------------------
        # PROGRAM
        # -----------------------------------------
        if current_channel:
            program = parse_program_text(text)

            if program:
                programs_by_channel.setdefault(
                    current_channel,
                    []
                ).append(program)

    return channels, programs_by_channel


def dismiss_cookie_popup(page):
    possible_texts = [
        "Tümünü Kabul Et",
        "Tümünü kabul et",
        "Kabul Et",
        "Kabul et",
        "Çerezleri Kabul Et",
        "Çerezleri kabul et",
        "Allow all",
        "Accept All",
        "Accept",
    ]

    for text in possible_texts:
        try:
            locator = page.get_by_text(text, exact=True)

            count = locator.count()

            for i in range(count):
                try:
                    element = locator.nth(i)

                    if element.is_visible():
                        element.click(force=True, timeout=1500)
                        page.wait_for_timeout(500)
                        return
                except Exception:
                    pass

        except Exception:
            pass


def click_date(page, target_day, day_index):
    """
    0 = Bugün
    1 = Yarın
    2+ = dd.mm.yyyy
    """

    if day_index == 0:
        labels = ["Bugün", "Bugün "]
    elif day_index == 1:
        labels = ["Yarın", "Yarın "]
    else:
        labels = [
            target_day.strftime("%d.%m.%Y"),
            target_day.strftime("%d.%m.%Y ").strip(),
        ]

    for label in labels:
        try:
            locator = page.get_by_text(label, exact=True)

            count = locator.count()

            for i in range(count - 1, -1, -1):
                try:
                    element = locator.nth(i)

                    if not element.is_visible():
                        continue

                    element.scroll_into_view_if_needed(timeout=2000)
                    element.click(force=True, timeout=3000)

                    page.wait_for_timeout(2500)

                    return True

                except Exception:
                    continue

        except Exception:
            continue

    # JavaScript yedek yöntem
    try:
        clicked = page.evaluate(
            """
            label => {
                const elements = Array.from(document.querySelectorAll("*"));

                for (const el of elements) {
                    if (!el.children.length &&
                        (el.textContent || "").trim() === label) {

                        let target = el;

                        for (let i = 0; i < 5 && target; i++) {
                            if (
                                target.tagName === "BUTTON" ||
                                target.tagName === "A" ||
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
            labels[0],
        )

        if clicked:
            page.wait_for_timeout(2500)
            return True

    except Exception:
        pass

    return False


def fingerprint_programs(programs_by_channel):
    values = []

    for channel in sorted(programs_by_channel):
        programs = programs_by_channel[channel]

        for program in programs[:5]:
            values.append(
                (
                    channel,
                    program["title"],
                    program["start"],
                    program["end"],
                )
            )

    return tuple(values)


def get_day_schedule(page, target_day, day_index, previous_fingerprint=None):
    print()
    print("=" * 70)
    print(
        f"PROGRAM ALINIYOR: "
        f"{target_day.strftime('%d.%m.%Y')}"
    )
    print("=" * 70)

    # İlk gün zaten açık olabilir.
    if day_index == 0:
        page.wait_for_timeout(1500)

    else:
        clicked = click_date(
            page,
            target_day,
            day_index,
        )

        if not clicked:
            print(
                f"UYARI: {target_day.strftime('%d.%m.%Y')} "
                f"tarih butonu bulunamadı."
            )

        page.wait_for_timeout(1500)

    # AJAX'ın tamamlanmasını bekle
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=8000,
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(2000)

    channels, programs = extract_dom_data(page)

    count = sum(
        len(value)
        for value in programs.values()
    )

    current_fingerprint = fingerprint_programs(programs)

    print(f"Kanal: {len(channels)}")
    print(f"Program: {count}")

    # Eğer tarih tıklaması AJAX'ı henüz tamamlamadıysa tekrar dene.
    if (
        day_index > 0
        and previous_fingerprint
        and current_fingerprint == previous_fingerprint
    ):
        print("Program listesi değişmedi, tekrar bekleniyor...")

        page.wait_for_timeout(4000)

        channels, programs = extract_dom_data(page)

        count = sum(
            len(value)
            for value in programs.values()
        )

        current_fingerprint = fingerprint_programs(programs)

        print(f"Tekrar kontrol: {count} program")

    return (
        channels,
        programs,
        current_fingerprint,
    )


def xmltv_time(dt):
    """
    XMLTV timezone formatı:
    20260903000000 +0300
    """

    return dt.strftime("%Y%m%d%H%M%S %z")


def safe_xml_text(text):
    return clean_text(text)


def build_xml(all_channels, all_programs):
    tv = Element(
        "tv",
        {
            "generator-info-name": "Tivibu EPG",
            "generator-info-url": "https://www.tivibu.com.tr/",
        },
    )

    # -----------------------------------------
    # CHANNEL
    # -----------------------------------------
    for channel_name in all_channels:
        if not channel_name:
            continue

        if channel_name in CHANNEL_EXCLUDE:
            continue

        channel_id = (
            re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                channel_name.lower(),
            )
            .strip("_")
        )

        channel = SubElement(
            tv,
            "channel",
            {
                "id": channel_id,
            },
        )

        display = SubElement(
            channel,
            "display-name",
            {
                "lang": "tr",
            },
        )

        display.text = safe_xml_text(channel_name)

    # -----------------------------------------
    # PROGRAM
    # -----------------------------------------
    for channel_name in all_channels:
        channel_id = (
            re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                channel_name.lower(),
            )
            .strip("_")
        )

        programs = all_programs.get(
            channel_name,
            [],
        )

        for program in programs:
            programme = SubElement(
                tv,
                "programme",
                {
                    "channel": channel_id,
                    "start": xmltv_time(program["start"]),
                    "stop": xmltv_time(program["stop"]),
                },
            )

            title = SubElement(
                programme,
                "title",
                {
                    "lang": "tr",
                },
            )

            title.text = safe_xml_text(
                program["title"]
            )

    tree = ElementTree(tv)

    try:
        import xml.etree.ElementTree as ET

        ET.indent(
            tree,
            space="  ",
        )
    except Exception:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    now = datetime.now(TIMEZONE)

    target_days = [
        (now.date() + timedelta(days=i))
        for i in range(7)
    ]

    print()
    print("=" * 70)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("=" * 70)
    print(
        "Başlangıç:",
        now.strftime("%d.%m.%Y %H:%M:%S"),
    )
    print(
        "Dönem:",
        target_days[0].strftime("%d.%m.%Y"),
        "->",
        target_days[-1].strftime("%d.%m.%Y"),
    )
    print("=" * 70)

    all_channels = []
    all_programs = {}

    successful_days = 0
    failed_days = 0

    previous_fingerprint = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        print()
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

        page.wait_for_timeout(3000)

        dismiss_cookie_popup(page)

        # Kanal bağlantılarının gelmesini bekle
        try:
            page.wait_for_selector(
                'a[href*="/kanallar/"]',
                timeout=30000,
            )
        except PlaywrightTimeoutError:
            print(
                "UYARI: Kanal bağlantıları beklenen sürede gelmedi."
            )

        page.wait_for_timeout(3000)

        # -----------------------------------------
        # 7 GÜN
        # -----------------------------------------
        for index, target_day in enumerate(target_days):
            try:
                (
                    channels,
                    day_programs,
                    current_fingerprint,
                ) = get_day_schedule(
                    page,
                    target_day,
                    index,
                    previous_fingerprint,
                )

                if not all_channels:
                    all_channels = list(channels)

                else:
                    for channel in channels:
                        if channel not in all_channels:
                            all_channels.append(channel)

                # Günün başlangıç/bitişi
                day_start = datetime(
                    target_day.year,
                    target_day.month,
                    target_day.day,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                day_end = day_start + timedelta(days=1)

                day_count = 0

                for channel_name, raw_programs in day_programs.items():
                    absolute_programs = build_absolute_programs(
                        raw_programs,
                        target_day,
                    )

                    for program in absolute_programs:
                        # Sadece bu güne temas eden programları al.
                        if (
                            program["stop"] <= day_start
                            or program["start"] >= day_end
                        ):
                            continue

                        if channel_name not in all_programs:
                            all_programs[channel_name] = []

                        # Aynı programı tekrar ekleme
                        duplicate = False

                        for existing in all_programs[channel_name]:
                            if (
                                existing["title"]
                                == program["title"]
                                and existing["start"]
                                == program["start"]
                                and existing["stop"]
                                == program["stop"]
                            ):
                                duplicate = True
                                break

                        if not duplicate:
                            all_programs[channel_name].append(
                                program
                            )
                            day_count += 1

                if day_count > 0:
                    successful_days += 1
                    print(
                        f"BAŞARILI: "
                        f"{target_day.strftime('%d.%m.%Y')} "
                        f"-> {day_count} program"
                    )
                else:
                    failed_days += 1
                    print(
                        f"HATA: "
                        f"{target_day.strftime('%d.%m.%Y')} "
                        f"-> 0 program"
                    )

                previous_fingerprint = current_fingerprint

            except Exception as exc:
                failed_days += 1

                print(
                    f"HATA: "
                    f"{target_day.strftime('%d.%m.%Y')}"
                )

                print(
                    "Detay:",
                    repr(exc),
                )

        browser.close()

    # Programları zamana göre sırala
    for channel_name in all_programs:
        all_programs[channel_name].sort(
            key=lambda x: x["start"]
        )

    total_programs = sum(
        len(value)
        for value in all_programs.values()
    )

    # -----------------------------------------
    # SONUÇ
    # -----------------------------------------
    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)
    print(
        f"Başarılı gün: {successful_days}/7"
    )
    print(
        f"Başarısız gün: {failed_days}/7"
    )
    print(
        f"Kanal: {len(all_channels)}"
    )
    print(
        f"Toplam program: {total_programs}"
    )
    print("=" * 70)

    if total_programs == 0:
        raise RuntimeError(
            "Hiç program alınamadı. Tivibu sayfasının DOM yapısı değişmiş olabilir."
        )

    # En azından 1 kanal ve program varsa XML üret.
    build_xml(
        all_channels,
        all_programs,
    )

    print()
    print(
        f"EPG oluşturuldu: {OUTPUT_FILE}"
    )

    # İlk birkaç kanalı ve programı göster
    print()
    print("ÖRNEK PROGRAMLAR:")

    shown = 0

    for channel_name in all_channels:
        programs = all_programs.get(
            channel_name,
            [],
        )

        for program in programs[:2]:
            print(
                f"{channel_name} | "
                f"{program['start'].strftime('%d.%m %H:%M')} - "
                f"{program['stop'].strftime('%d.%m %H:%M')} | "
                f"{program['title']}"
            )

            shown += 1

            if shown >= 10:
                break

        if shown >= 10:
            break


if __name__ == "__main__":
    main()
