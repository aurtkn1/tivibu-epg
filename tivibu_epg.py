
import html
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"
TEMP_FILE = OUTPUT_FILE + ".tmp"

ISTANBUL = ZoneInfo("Europe/Istanbul")

DAYS = 7
PAGE_TIMEOUT = 60000
CHANNEL_WAIT_MS = 1200
DATE_WAIT_MS = 1200

MIN_CHANNELS = 30
MIN_PROGRAMS = 100
MIN_DAYS = 6

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


GET_CHANNELS_JS = r"""
() => {
    const cards = [
        ...document.querySelectorAll(
            '.fullLink[channelcode][title]'
        )
    ];

    const result = [];
    const seen = new Set();

    for (const card of cards) {
        const id = (
            card.getAttribute('channelcode') || ''
        ).trim();

        const name = (
            card.getAttribute('title') || ''
        ).trim();

        if (!id || !name) continue;
        if (!/^ch\d+$/i.test(id)) continue;

        const key = id.toLowerCase();

        if (seen.has(key)) continue;
        seen.add(key);

        result.push({
            channel_id: id,
            name: name
        });
    }

    return result;
}
"""


SELECT_CHANNEL_JS = r"""
(channelId) => {
    const cards = [
        ...document.querySelectorAll(
            '.fullLink[channelcode]'
        )
    ];

    const matches = cards.filter(card =>
        (card.getAttribute('channelcode') || '')
            .toLowerCase() === channelId.toLowerCase()
    );

    if (!matches.length) {
        return {
            success: false,
            reason: 'Kanal kartı DOM içinde bulunamadı'
        };
    }

    const card = matches[0];

    try {
        card.click();

        return {
            success: true,
            method: 'element.click',
            matches: matches.length,
            name: card.getAttribute('title')
        };
    } catch (error) {
        try {
            card.dispatchEvent(
                new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                })
            );

            return {
                success: true,
                method: 'dispatchEvent',
                matches: matches.length,
                name: card.getAttribute('title')
            };
        } catch (secondError) {
            return {
                success: false,
                reason: String(secondError)
            };
        }
    }
}
"""


SELECT_DATE_JS = r"""
(expectedDate) => {
    const links = [
        ...document.querySelectorAll(
            'a[channeldatebegin][channeldateend]'
        )
    ];

    const link = links.find(item => {
        const begin = (
            item.getAttribute('channeldatebegin') || ''
        ).trim();

        return begin.startsWith(expectedDate);
    });

    if (!link) {
        return {
            success: false,
            reason: 'Tarih bağlantısı DOM içinde bulunamadı',
            expected: expectedDate,
            count: links.length
        };
    }

    const before = link.classList.contains('active');

    try {
        link.click();

        return {
            success: true,
            method: 'element.click',
            title: link.getAttribute('title'),
            beforeActive: before
        };
    } catch (error) {
        try {
            link.dispatchEvent(
                new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                })
            );

            return {
                success: true,
                method: 'dispatchEvent',
                title: link.getAttribute('title'),
                beforeActive: before
            };
        } catch (secondError) {
            return {
                success: false,
                reason: String(secondError)
            };
        }
    }
}
"""


GET_DATE_STATE_JS = r"""
(expectedDate) => {
    const links = [
        ...document.querySelectorAll(
            'a[channeldatebegin][channeldateend]'
        )
    ];

    const link = links.find(item => {
        const begin = (
            item.getAttribute('channeldatebegin') || ''
        ).trim();

        return begin.startsWith(expectedDate);
    });

    if (!link) return null;

    return {
        active: link.classList.contains('active'),
        title: link.getAttribute('title'),
        begin: link.getAttribute('channeldatebegin')
    };
}
"""


GET_PROGRAMS_JS = r"""
() => {
    const anchors = [
        ...document.querySelectorAll('a[href*="/rv?i="]')
    ];

    const result = [];
    const seen = new Set();

    for (const anchor of anchors) {
        const href = anchor.getAttribute('href') || '';

        let decoded = href;

        try {
            decoded = decodeURIComponent(href);
        } catch (_) {}

        const match = decoded.match(/\b(ch\d{6,})\b/i);

        if (!match) continue;

        const channelId = match[1].toLowerCase();

        const titleElement = anchor.querySelector(
            '.programTitle'
        );

        const startElement = anchor.querySelector(
            '.startTime'
        );

        const finishElement = anchor.querySelector(
            '.finishTime'
        );

        if (!titleElement || !startElement || !finishElement) {
            continue;
        }

        const title = (
            titleElement.innerText ||
            titleElement.textContent ||
            ''
        ).replace(/\s+/g, ' ').trim();

        const start = (
            startElement.innerText ||
            startElement.textContent ||
            ''
        ).trim();

        const finish = (
            finishElement.innerText ||
            finishElement.textContent ||
            ''
        ).trim();

        if (!title || !start || !finish) continue;

        const categoryElement = anchor.querySelector(
            '.programType .type'
        );

        const category = categoryElement
            ? (
                categoryElement.innerText ||
                categoryElement.textContent ||
                ''
              ).trim()
            : '';

        const key = [
            channelId,
            title,
            start,
            finish
        ].join('|');

        if (seen.has(key)) continue;
        seen.add(key);

        result.push({
            channel_id: channelId,
            title: title,
            start: start,
            finish: finish,
            category: category
        });
    }

    return result;
}
"""


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_name(value):
    return clean_text(value).upper()


def select_date(page, selected_day, today):

    expected = selected_day.strftime("%Y.%m.%d")

    result = page.evaluate(
        SELECT_DATE_JS,
        expected
    )

    if not result or not result.get("success"):

        reason = (
            result.get("reason")
            if result
            else "Bilinmeyen JavaScript hatası"
        )

        raise RuntimeError(
            f"Tarih seçilemedi: {expected} - {reason}"
        )

    page.wait_for_timeout(DATE_WAIT_MS)

    state = page.evaluate(
        GET_DATE_STATE_JS,
        expected
    )

    if not state:
        raise RuntimeError(
            f"Tarih bağlantısı tıklama sonrası bulunamadı: {expected}"
        )

    if not state.get("active"):
        raise RuntimeError(
            f"Tarih tıklandı fakat active olmadı: "
            f"{state.get('title')}"
        )

    return True


def select_channel(page, channel):

    channel_id = channel["channel_id"]

    result = page.evaluate(
        SELECT_CHANNEL_JS,
        channel_id
    )

    if not result or not result.get("success"):

        reason = (
            result.get("reason")
            if result
            else "Bilinmeyen JavaScript hatası"
        )

        raise RuntimeError(
            f"Kanal tıklanamadı: {reason}"
        )

    page.wait_for_timeout(CHANNEL_WAIT_MS)

    return result


def parse_clock(value):

    value = clean_text(value)

    if not TIME_RE.match(value):
        return None

    hour, minute = map(
        int,
        value.split(":")
    )

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def assign_program_times(items, selected_day):

    result = []

    for item in items:

        title = clean_text(item.get("title"))

        start_clock = parse_clock(
            item.get("start")
        )

        finish_clock = parse_clock(
            item.get("finish")
        )

        if not title:
            continue

        if not start_clock or not finish_clock:
            continue

        sh, sm = start_clock
        eh, em = finish_clock

        start = datetime(
            selected_day.year,
            selected_day.month,
            selected_day.day,
            sh,
            sm,
            tzinfo=ISTANBUL
        )

        finish = datetime(
            selected_day.year,
            selected_day.month,
            selected_day.day,
            eh,
            em,
            tzinfo=ISTANBUL
        )

        if finish <= start:
            finish += timedelta(days=1)

        if finish <= start:
            continue

        if (finish - start).total_seconds() > 24 * 3600:
            continue

        result.append({
            "title": title,
            "category": clean_text(
                item.get("category")
            ),
            "start": start,
            "stop": finish
        })

    return result


def collect_all(page, today):

    print("Tivibu Tüm Kanallar sayfası açılıyor...")

    page.goto(
        LIVE_TV_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    page.wait_for_timeout(3000)

    page.wait_for_selector(
        ".fullLink[channelcode][title]",
        state="attached",
        timeout=30000
    )

    channels = page.evaluate(GET_CHANNELS_JS)

    print(f"Kanal kartı: {len(channels)}")

    unique_ids = {
        item["channel_id"].lower()
        for item in channels
    }

    print(f"Benzersiz kanal ID: {len(unique_ids)}")

    if not channels:
        raise RuntimeError(
            "Tüm Kanallar sayfasından kanal bulunamadı."
        )

    print("===== KANAL LİSTESİ =====")

    for channel in channels:
        print(
            f'{channel["name"]}: '
            f'{channel["channel_id"]}'
        )

    print("=" * 68)
    print("KANALLAR TEK TEK SEÇİLEREK YAYIN AKIŞLARI OKUNUYOR")
    print("=" * 68)

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

    window_end_day = today + timedelta(days=DAYS)

    window_end = datetime(
        window_end_day.year,
        window_end_day.month,
        window_end_day.day,
        0,
        0,
        tzinfo=ISTANBUL
    )

    for index, channel in enumerate(channels, 1):

        name = clean_text(channel["name"])
        channel_id = channel["channel_id"]

        print(
            f"\n[{index}/{len(channels)}] {name}"
        )

        print(
            f"KANAL: {name} | {channel_id}"
        )

        if normalize_name(name) in BAD_CHANNELS:
            print("  Bilgilendirme kanalı atlandı.")
            continue

        try:

            click_result = select_channel(
                page,
                channel
            )

            print(
                "  Kanal tıklama yöntemi: "
                + click_result.get("method", "bilinmiyor")
            )

        except Exception as exc:

            print(
                f"  Kanal seçilemedi: {exc}"
            )

            continue

        channel_program_count = 0

        for offset in range(DAYS):

            selected_day = today + timedelta(
                days=offset
            )

            label = (
                "Bugün"
                if offset == 0
                else "Yarın"
                if offset == 1
                else selected_day.strftime("%d.%m.%Y")
            )

            try:

                select_date(
                    page,
                    selected_day,
                    today
                )

                raw_programs = page.evaluate(
                    GET_PROGRAMS_JS
                )

                # DEBUG 1: Sayfadaki toplam program bağlantısı
                print(
                    f"    DEBUG: Sayfadaki toplam program: "
                    f"{len(raw_programs)}"
                )

                # DEBUG 2: Program bağlantılarında bulunan kanal ID'leri
                found_ids = list(dict.fromkeys(
                    item["channel_id"]
                    for item in raw_programs
                ))

                print(
                    "    DEBUG: Bulunan kanal ID'leri: "
                    + str(found_ids)
                )

                matched = [
                    item
                    for item in raw_programs
                    if item["channel_id"].lower()
                    == channel_id.lower()
                ]

                print(
                    f"    DEBUG: Seçilen kanal ID eşleşmesi: "
                    f"{len(matched)}"
                )

                programs = assign_program_times(
                    matched,
                    selected_day
                )

                new_count = 0

                for program in programs:

                    if (
                        program["stop"] <= window_start
                        or program["start"] >= window_end
                    ):
                        continue

                    key = (
                        channel_id.lower(),
                        program["title"].casefold(),
                        program["start"],
                        program["stop"]
                    )

                    if key in all_programs:
                        continue

                    all_programs[key] = {
                        "channel_id": channel_id,
                        "channel_name": name,
                        **program
                    }

                    new_count += 1

                if programs:
                    all_channels[channel_id] = {
                        "name": name
                    }

                channel_program_count += len(programs)

                print(
                    f"  {label}: "
                    f"{len(programs)} program / "
                    f"{new_count} yeni kayıt"
                )

            except Exception as exc:

                print(
                    f"  {label}: HATA - {exc}"
                )

        print(
            f"  KANAL TOPLAMI: "
            f"{channel_program_count} program"
        )

    return (
        all_channels,
        list(all_programs.values())
    )


def build_xml(channels, programs):

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_TV_URL
        }
    )

    for channel_id, info in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):

        node = ET.SubElement(
            root,
            "channel",
            {"id": channel_id}
        )

        display = ET.SubElement(
            node,
            "display-name",
            {"lang": "tr"}
        )

        display.text = info["name"]

    for item in sorted(
        programs,
        key=lambda value: (
            value["start"],
            value["channel_id"],
            value["stop"]
        )
    ):

        node = ET.SubElement(
            root,
            "programme",
            {
                "channel": item["channel_id"],
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
            {"lang": "tr"}
        )

        title.text = item["title"]

        if item.get("category"):

            category = ET.SubElement(
                node,
                "category",
                {"lang": "tr"}
            )

            category.text = item["category"]

    tree = ET.ElementTree(root)

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        TEMP_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    ET.parse(TEMP_FILE)

    os.replace(
        TEMP_FILE,
        OUTPUT_FILE
    )


def validate_data(channels, programs):

    if len(channels) < MIN_CHANNELS:

        raise RuntimeError(
            f"Yalnızca {len(channels)} kanal bulundu. "
            f"En az {MIN_CHANNELS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )

    if len(programs) < MIN_PROGRAMS:

        raise RuntimeError(
            f"Yalnızca {len(programs)} program bulundu. "
            f"En az {MIN_PROGRAMS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )

    days = {
        item["start"].date()
        for item in programs
    }

    if len(days) < MIN_DAYS:

        raise RuntimeError(
            f"Yalnızca {len(days)} yayın günü bulundu. "
            f"En az {MIN_DAYS} bekleniyordu. "
            "Mevcut epg.xml korunuyor."
        )


def main():

    started = time.time()

    today = datetime.now(
        ISTANBUL
    ).date()

    print("=" * 68)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Yalnızca TÜM KANALLAR")
    print("Kanal kartları tek tek seçilecek.")
    print("Tarihler JavaScript element.click() ile seçilecek.")
    print("DEBUG: Program bağlantıları ve kanal ID'leri gösterilecek.")
    print("Programlar: programTitle / startTime / finishTime")
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

    print("\n" + "=" * 68)
    print("VERİ KONTROLÜ")
    print("=" * 68)

    print(
        f"Toplam kanal: {len(channels)}"
    )

    print(
        f"Toplam program: {len(programs)}"
    )

    days = sorted({
        item["start"].date()
        for item in programs
    })

    print(
        f"Yayın günü: {len(days)}"
    )

    validate_data(
        channels,
        programs
    )

    build_xml(
        channels,
        programs
    )

    print("\n" + "=" * 68)
    print("7 GÜNLÜK EPG BAŞARIYLA OLUŞTURULDU")
    print(f"Toplam kanal: {len(channels)}")
    print(f"Toplam program: {len(programs)}")
    print(f"İlk gün: {days[0]}")
    print(f"Son gün: {days[-1]}")
    print(f"Dosya: {OUTPUT_FILE}")
    print(
        f"İşlem süresi: {time.time() - started:.1f} saniye"
    )
    print("=" * 68)


if __name__ == "__main__":
    main()
