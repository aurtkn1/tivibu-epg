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

PROGRAM_CATEGORIES = {
    "Film",
    "Dizi",
    "Yaşam",
    "Spor",
    "Haber",
    "Belgesel",
    "Çocuk",
    "Diğer",
    "Müzik",
    "Eğlence",
    "Aktüalite",
    "Kültür",
    "Magazin",
    "Sinema",
    "Program",
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

    if "Program Akışı" in text:
        return False

    return True


def parse_program(text):
    text = clean_text(text)

    if not text:
        return None

    category_pattern = "|".join(
        re.escape(x)
        for x in sorted(
            PROGRAM_CATEGORIES,
            key=len,
            reverse=True,
        )
    )

    match = re.match(
        rf"^(.*?)\s+"
        rf"(?:{category_pattern})"
        rf"\s*-\s*"
        rf"(\d{{1,2}}:\d{{2}})"
        rf"\s*(?:→|->|–|-)\s*"
        rf"(\d{{1,2}}:\d{{2}})"
        rf"(?:\s+Canlı)?$",
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


def extract_channel_key(href):
    """
    Program URL örneği:

    /rv?datatype=2&i=2%7Cch00000000000000001358

    Buradan:

    ch00000000000000001358

    değerini çıkarır.
    """

    if not href:
        return None

    match = re.search(
        r"(ch[a-zA-Z0-9]+)",
        href,
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1).lower()


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

        start = make_dt(
            target_day,
            program["start"],
        )

        stop = make_dt(
            target_day,
            program["end"],
        )

        if stop <= start:
            stop += timedelta(days=1)

        if index == 0:

            if start.hour >= 18 and stop > start:
                start -= timedelta(days=1)

        else:

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


def collect_channels(page):
    """
    Ana sayfadaki gerçek kanal linklerini toplar.

    Kanal linklerinin yanında program linkleri de bulunduğu
    için programları burada eşleştirmiyoruz.
    """

    channels = []
    seen = set()

    elements = page.locator(
        'a[href*="/kanallar/"]'
    )

    count = elements.count()

    for i in range(count):

        try:
            element = elements.nth(i)

            href = element.get_attribute(
                "href"
            ) or ""

            text = clean_text(
                element.inner_text()
            )

            item = {
                "href": href,
                "text": text,
            }

            if not is_real_channel(item):
                continue

            if text in seen:
                continue

            seen.add(text)

            if href.startswith("/"):
                href = (
                    "https://www.tivibu.com.tr"
                    + href
                )

            channels.append(
                {
                    "name": text,
                    "url": href,
                }
            )

        except Exception:
            continue

    return channels


def build_channel_key_map(page, channels):
    """
    EN ÖNEMLİ KISIM:

    Her kanalın kendi Tivibu sayfasına giriyoruz.

    Kanal sayfasındaki program URL'lerinden chXXXXXXXX
    değerini buluyoruz.

    Böylece:

        ch000001 -> SİNEMA TV
        ch000002 -> SİNEMA 2
        ch000003 -> TRT 1

    gibi gerçek Tivibu kanal eşleştirmesi oluşturuluyor.

    Programların hepsinin TİVİBU TANITIM'a gitmesinin
    sebebi olan DOM sırası problemi burada tamamen
    ortadan kalkıyor.
    """

    channel_keys = {}

    print()
    print("=" * 70)
    print("KANAL ID EŞLEŞTİRMESİ")
    print("=" * 70)

    for channel in channels:

        name = channel["name"]
        url = channel["url"]

        print(
            f"{name} -> aranıyor..."
        )

        try:

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(
                2000
            )

            close_cookie_popup(
                page
            )

            program_links = page.locator(
                'a[href*="/rv"]'
            )

            count = program_links.count()

            found_key = None

            for i in range(
                min(count, 100)
            ):

                try:

                    href = (
                        program_links
                        .nth(i)
                        .get_attribute(
                            "href"
                        )
                        or ""
                    )

                    key = extract_channel_key(
                        href
                    )

                    if key:
                        found_key = key
                        break

                except Exception:
                    continue

            if found_key:

                channel_keys[found_key] = name

                print(
                    f"  OK: {found_key}"
                )

            else:

                print(
                    "  UYARI: kanal ID bulunamadı"
                )

        except Exception as exc:

            print(
                f"  HATA: {repr(exc)}"
            )

    print()
    print(
        "Eşleşen kanal ID:",
        len(channel_keys),
    )

    print("=" * 70)

    return channel_keys


def extract_programs(page, channel_keys):
    """
    Ana canlı TV sayfasındaki bütün program linklerini
    okur.

    Her programın URL'sindeki chXXXXXXXX değeri alınır
    ve channel_keys üzerinden gerçek kanal bulunur.
    """

    programs = {}

    links = page.locator(
        'a[href*="/rv"]'
    )

    count = links.count()

    print(
        "Program linki:",
        count,
    )

    matched = 0
    unmatched = 0

    for i in range(count):

        try:

            element = links.nth(i)

            href = (
                element.get_attribute(
                    "href"
                )
                or ""
            )

            key = extract_channel_key(
                href
            )

            if not key:
                unmatched += 1
                continue

            channel = channel_keys.get(
                key
            )

            if not channel:
                unmatched += 1
                continue

            text = clean_text(
                element.inner_text()
            )

            if not text:
                text = clean_text(
                    element.text_content()
                    or ""
                )

            parsed = parse_program(
                text
            )

            if not parsed:
                continue

            programs.setdefault(
                channel,
                [],
            )

            duplicate = any(
                x["title"]
                == parsed["title"]
                and x["start"]
                == parsed["start"]
                and x["end"]
                == parsed["end"]
                for x in programs[channel]
            )

            if not duplicate:

                programs[channel].append(
                    parsed
                )

                matched += 1

        except Exception:
            continue

    print(
        "Eşleşen program:",
        matched,
    )

    print(
        "Eşleşmeyen program:",
        unmatched,
    )

    for name in sorted(
        programs.keys()
    ):

        print(
            f"  {name}: "
            f"{len(programs[name])} program"
        )

    return programs


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

            for i in range(
                locator.count() - 1,
                -1,
                -1,
            ):

                try:

                    element = locator.nth(i)

                    if not element.is_visible():
                        continue

                    element.click(
                        force=True,
                        timeout=1500,
                    )

                    page.wait_for_timeout(
                        500
                    )

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

        for i in range(
            locator.count() - 1,
            -1,
            -1,
        ):

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

                page.wait_for_timeout(
                    3500
                )

                return True

            except Exception:
                pass

    except Exception:
        pass

    try:

        return page.evaluate(
            """
            label => {

                const all =
                    [...document.querySelectorAll("*")];

                for (const el of all) {

                    if (
                        el.children.length === 0 &&
                        (el.textContent || "").trim()
                            === label
                    ) {

                        let target = el;

                        for (
                            let i = 0;
                            i < 8 && target;
                            i++
                        ) {

                            const tag =
                                target.tagName;

                            if (
                                tag === "BUTTON" ||
                                tag === "A" ||
                                tag === "LI" ||
                                target.getAttribute(
                                    "role"
                                ) === "button" ||
                                target.onclick
                            ) {

                                target.click();

                                return true;
                            }

                            target =
                                target.parentElement;
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

        click_visible_date(
            page,
            "Bugün",
        )

        page.wait_for_timeout(
            2500
        )

        return

    if index == 1:

        if not click_visible_date(
            page,
            "Yarın",
        ):

            click_visible_date(
                page,
                target_day.strftime(
                    "%d.%m.%Y"
                ),
            )

        page.wait_for_timeout(
            3500
        )

        return

    label = target_day.strftime(
        "%d.%m.%Y"
    )

    if not click_visible_date(
        page,
        label,
    ):

        print(
            f"UYARI: {label} bulunamadı"
        )

    page.wait_for_timeout(
        3500
    )


def wait_for_programs(page):

    for _ in range(8):

        try:

            count = page.locator(
                'a[href*="/rv"]'
            ).count()

            if count > 0:
                return count

        except Exception:
            pass

        page.wait_for_timeout(
            1500
        )

    return 0


def get_schedule(
    page,
    target_day,
    index,
    channel_keys,
):
    print()
    print("=" * 60)

    print(
        "Tarih:",
        target_day.strftime(
            "%d.%m.%Y"
        ),
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

    count = wait_for_programs(
        page
    )

    print(
        "DOM program bağlantısı:",
        count,
    )

    programs = extract_programs(
        page,
        channel_keys,
    )

    total = sum(
        len(v)
        for v in programs.values()
    )

    print(
        "Kanal:",
        len(programs),
    )

    print(
        "Program:",
        total,
    )

    return programs


def build_xml(
    channels,
    programs,
):
    tv = Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu 7 Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        },
    )

    written_channels = set()

    for channel in channels:

        name = channel["name"]

        if name in CHANNEL_EXCLUDE:
            continue

        cid = channel_id(name)

        if not cid:
            continue

        if cid in written_channels:
            continue

        written_channels.add(cid)

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": cid,
            },
        )

        display = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr",
            },
        )

        display.text = name

    written_programs = set()

    for channel in channels:

        name = channel["name"]

        if name in CHANNEL_EXCLUDE:
            continue

        cid = channel_id(name)

        if not cid:
            continue

        for program in programs.get(
            name,
            [],
        ):

            key = (
                cid,
                program["start"],
                program["stop"],
                program["title"],
            )

            if key in written_programs:
                continue

            written_programs.add(
                key
            )

            programme = SubElement(
                tv,
                "programme",
                {
                    "channel": cid,
                    "start":
                        program["start"].strftime(
                            "%Y%m%d%H%M%S %z"
                        ),
                    "stop":
                        program["stop"].strftime(
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

        ET.indent(
            tv,
            space="  ",
        )

    except Exception:
        pass

    ElementTree(tv).write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():

    now = datetime.now(
        TIMEZONE
    )

    first_day = now.date()

    days = [
        first_day
        + timedelta(days=i)
        for i in range(7)
    ]

    print()
    print("=" * 70)
    print("TIVIBU EPG")
    print("=" * 70)

    print(
        "Dönem:",
        days[0].strftime(
            "%d.%m.%Y"
        ),
        "->",
        days[-1].strftime(
            "%d.%m.%Y"
        ),
    )

    print("=" * 70)

    all_channels = []
    all_programs = {}

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

        print(
            "Tivibu açılıyor..."
        )

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

        page.wait_for_timeout(
            5000
        )

        close_cookie_popup(
            page
        )

        try:

            page.wait_for_selector(
                'a[href*="/kanallar/"]',
                timeout=30000,
            )

        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(
            3000
        )

        # -----------------------------------------------------
        # KANALLARI AL
        # -----------------------------------------------------

        channel_list = collect_channels(
            page
        )

        print()
        print(
            "Bulunan gerçek kanal:",
            len(channel_list),
        )

        for channel in channel_list:
            print(
                " ",
                channel["name"],
            )

        all_channels = channel_list.copy()

        # -----------------------------------------------------
        # KANAL ID'LERİNİ BUL
        # -----------------------------------------------------

        channel_keys = build_channel_key_map(
            page,
            channel_list,
        )

        if not channel_keys:

            browser.close()

            raise RuntimeError(
                "Hiçbir Tivibu kanal ID'si bulunamadı."
            )

        # -----------------------------------------------------
        # 7 GÜN
        # -----------------------------------------------------

        for index, day in enumerate(
            days
        ):

            try:

                raw_programs = get_schedule(
                    page,
                    day,
                    index,
                    channel_keys,
                )

                day_start = datetime(
                    day.year,
                    day.month,
                    day.day,
                    0,
                    0,
                    tzinfo=TIMEZONE,
                )

                day_end = (
                    day_start
                    + timedelta(days=1)
                )

                added_today = 0

                for name, items in raw_programs.items():

                    absolute = convert_program_times(
                        items,
                        day,
                    )

                    for program in absolute:

                        if (
                            program["stop"]
                            <= day_start
                        ):
                            continue

                        if (
                            program["start"]
                            >= day_end
                        ):
                            continue

                        all_programs.setdefault(
                            name,
                            [],
                        )

                        duplicate = any(
                            x["title"]
                            == program["title"]
                            and x["start"]
                            == program["start"]
                            and x["stop"]
                            == program["stop"]
                            for x in all_programs[
                                name
                            ]
                        )

                        if not duplicate:

                            all_programs[
                                name
                            ].append(
                                program
                            )

                            added_today += 1

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

    # ---------------------------------------------------------
    # SIRALA
    # ---------------------------------------------------------

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
        "Kanal:",
        len(all_channels),
    )

    print(
        "Toplam program:",
        total_programs,
    )

    print("=" * 70)

    for channel in all_channels:

        name = channel["name"]

        print(
            f"{name}: "
            f"{len(all_programs.get(name, []))} program"
        )

    if total_programs == 0:

        raise RuntimeError(
            "0 program bulundu."
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
