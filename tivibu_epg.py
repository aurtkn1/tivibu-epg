# tivibu_epg.py

import re
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright


BASE_URL = "https://www.tivibu.com.tr"
SOURCE_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
TIMEZONE = ZoneInfo("Europe/Istanbul")
DAYS = 7


EXTRACT_JS = r"""
() => {
    const clean = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const channels = [];
    const channelMap = new Map();

    document.querySelectorAll(".fullLink[channelcode]").forEach(card => {
        const id = clean(card.getAttribute("channelcode"));
        const name = clean(
            card.querySelector(".channelsTitle")?.innerText ||
            card.querySelector(".channelsTitle a")?.getAttribute("title") ||
            card.getAttribute("title")
        );

        if (!id || !name) return;

        if (!channelMap.has(id)) {
            channelMap.set(id, true);
            channels.push({id, name});
        }
    });

    const programs = [];
    const seen = new Set();

    document.querySelectorAll('a[href*="/rv?"]').forEach(link => {
        const title = clean(link.querySelector(".programTitle")?.innerText);
        const start = clean(link.querySelector(".startTime")?.innerText);
        const finish = clean(link.querySelector(".finishTime")?.innerText);
        const type = clean(link.querySelector(".programType .type")?.innerText);

        const href = link.getAttribute("href") || "";
        const match = href.match(/ch\d+/i);

        if (!match || !title || !start || !finish) return;

        const channelId = match[0];
        const key = [channelId, title, start, finish].join("|");

        if (seen.has(key)) return;
        seen.add(key);

        programs.push({
            channelId,
            title,
            start,
            finish,
            type
        });
    });

    return {
        channelCards: document.querySelectorAll(".fullLink[channelcode]").length,
        channels,
        programLinks: document.querySelectorAll('a[href*="/rv?"]').length,
        programs
    };
}
"""


def log(message):
    print(message, flush=True)


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def close_overlays(page):
    try:
        page.evaluate("""
        () => {
            const selectors = [
                ".ins-preview-wrapper",
                "#ins-frameless-overlay",
                '[id^="ins-frame"]',
                '[class*="ins-preview-wrapper"]',
                '[class*="ins-webpush"]'
            ];

            selectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(el => el.remove());
            });

            document.body.style.overflow = "auto";
            document.documentElement.style.overflow = "auto";
        }
        """)
    except Exception:
        pass


def install_popup_blocker(context):
    context.add_init_script("""
    (() => {
        const removeInsider = () => {
            [
                ".ins-preview-wrapper",
                "#ins-frameless-overlay",
                '[id^="ins-frame"]',
                '[class*="ins-preview-wrapper"]'
            ].forEach(selector => {
                document.querySelectorAll(selector).forEach(el => el.remove());
            });
        };

        const observer = new MutationObserver(removeInsider);

        const start = () => {
            if (!document.documentElement) return;
            observer.observe(document.documentElement, {
                childList: true,
                subtree: true
            });
            removeInsider();
        };

        if (document.documentElement) start();
        else document.addEventListener("DOMContentLoaded", start);
    })();
    """)


def wait_page(page, milliseconds=1500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    page.wait_for_timeout(milliseconds)
    close_overlays(page)


def parse_time(value):
    match = re.search(r"(\d{1,2}):(\d{2})", value or "")
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def make_datetime(day, value):
    parsed = parse_time(value)
    if not parsed:
        return None

    hour, minute = parsed

    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=TIMEZONE
    )


def select_date(page, target_date):
    """
    Tarih bağlantılarının farklı HTML biçimlerini kontrol eder.
    Önce tarih değerlerini arar, ardından görünen tarih metinlerini dener.
    """

    target_iso = target_date.strftime("%Y-%m-%d")
    target_dot = target_date.strftime("%d.%m.%Y")
    target_slash = target_date.strftime("%d/%m/%Y")

    close_overlays(page)

    # Sayfadaki tarih bağlantılarının mevcut HTML bilgilerini al.
    try:
        date_elements = page.locator(
            "a[channeldatebegin], a[channeldateend], "
            "[channeldatebegin], [channeldateend]"
        )

        count = date_elements.count()

        for index in range(count):
            element = date_elements.nth(index)

            try:
                begin = element.get_attribute("channeldatebegin") or ""
                end = element.get_attribute("channeldateend") or ""
                label = normalize(element.inner_text())
                value = begin + " " + end

                if (
                    target_iso in value or
                    target_dot in value or
                    target_slash in value or
                    target_dot in label or
                    target_slash in label
                ):
                    if "active" in (element.get_attribute("class") or "").lower():
                        return True

                    try:
                        element.evaluate("(el) => el.click()")
                        wait_page(page, 1800)
                        return True
                    except Exception:
                        continue

            except Exception:
                continue

    except Exception:
        pass

    # Alternatif: tarih butonları veya bağlantılarının metninden seç.
    try:
        candidates = page.locator("a, button, [role='button']")

        count = candidates.count()

        for index in range(count):
            element = candidates.nth(index)

            try:
                if not element.is_visible():
                    continue

                label = normalize(element.inner_text())

                if target_dot in label or target_slash in label:
                    element.evaluate("(el) => el.click()")
                    wait_page(page, 1800)
                    return True

            except Exception:
                continue

    except Exception:
        pass

    # Tarih seçicinin HTML yapısını logla.
    try:
        diagnostic = page.evaluate("""
        () => Array.from(document.querySelectorAll(
            '[channeldatebegin], [channeldateend], a, button'
        )).slice(0, 50).map(el => ({
            tag: el.tagName,
            text: (el.innerText || '').trim().slice(0, 80),
            begin: el.getAttribute('channeldatebegin'),
            end: el.getAttribute('channeldateend'),
            cls: el.className?.toString().slice(0, 100)
        }))
        """)
        log(f"Tarih seçici bulunamadı. Sayfa elemanları: {diagnostic}")
    except Exception:
        pass

    return False


def extract_page(page):
    close_overlays(page)

    try:
        return page.evaluate(EXTRACT_JS)
    except Exception as error:
        log(f"Veri okuma hatası: {error}")
        return {
            "channelCards": 0,
            "channels": [],
            "programLinks": 0,
            "programs": []
        }


def collect_day(page, day, channels, all_programs):
    log("")
    log("-" * 65)
    log(f"TARİH: {day.strftime('%d.%m.%Y')}")

    close_overlays(page)

    try:
        page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )
    except Exception as error:
        log(f"Sayfa açılamadı: {error}")
        return

    wait_page(page, 2500)

    # Kanalların ve sayfa içeriğinin yüklenmesini bekle.
    try:
        page.locator(".fullLink[channelcode]").first.wait_for(
            state="attached",
            timeout=20000
        )
    except Exception:
        pass

    close_overlays(page)

    # Sayfa aşağı kaydırılarak tüm kanal kartları yüklenir.
    previous_count = -1
    stable_count = 0

    for _ in range(20):
        close_overlays(page)

        try:
            current_count = page.locator(
                ".fullLink[channelcode]"
            ).count()
        except Exception:
            current_count = 0

        if current_count == previous_count:
            stable_count += 1
        else:
            stable_count = 0

        if stable_count >= 3:
            break

        previous_count = current_count

        try:
            page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception:
            pass

        page.wait_for_timeout(500)

    # Kanal bilgilerini al.
    data = extract_page(page)

    log(f"Kanal kartı: {data['channelCards']}")
    log(f"Program bağlantısı: {data['programLinks']}")

    for channel in data["channels"]:
        channel_id = normalize(channel["id"])
        channel_name = normalize(channel["name"])

        if channel_id and channel_name:
            if channel_id not in channels:
                channels[channel_id] = channel_name
                log(f"Yeni kanal: {channel_name} ({channel_id})")

    # Tarihi seç.
    if not select_date(page, day):
        log("Tarih seçilemedi; bu günün programları atlandı.")
        return

    # Tarih değiştikten sonra programları tekrar oku.
    data = extract_page(page)

    added = 0

    for program in data["programs"]:
        channel_id = normalize(program["channelId"])

        if channel_id not in channels:
            continue

        item = dict(program)
        item["date"] = day
        all_programs.append(item)
        added += 1

    log(f"Okunan program: {len(data['programs'])}")
    log(f"Eklenen program: {added}")


def build_xml(channels, programs):
    tv = ET.Element("tv")
    tv.set("source-info-name", "Tivibu")
    tv.set("generator-info-name", "Tivibu 7 Günlük EPG")

    for channel_id, channel_name in sorted(channels.items()):
        channel_element = ET.SubElement(
            tv,
            "channel",
            {"id": channel_id}
        )

        display_name = ET.SubElement(
            channel_element,
            "display-name"
        )
        display_name.text = channel_name

    seen = set()
    valid_count = 0

    for program in programs:
        channel_id = normalize(program.get("channelId"))
        title = normalize(program.get("title"))
        day = program.get("date")

        if not channel_id or channel_id not in channels:
            continue

        if not title or not day:
            continue

        start_dt = make_datetime(day, program.get("start"))
        finish_dt = make_datetime(day, program.get("finish"))

        if not start_dt or not finish_dt:
            continue

        if finish_dt <= start_dt:
            finish_dt += timedelta(days=1)

        start_xml = start_dt.strftime("%Y%m%d%H%M%S %z")
        stop_xml = finish_dt.strftime("%Y%m%d%H%M%S %z")

        key = (channel_id, start_xml, stop_xml, title)

        if key in seen:
            continue

        seen.add(key)

        element = ET.SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": start_xml,
                "stop": stop_xml
            }
        )

        title_element = ET.SubElement(
            element,
            "title",
            {"lang": "tr"}
        )
        title_element.text = title

        program_type = normalize(program.get("type"))

        if program_type:
            category = ET.SubElement(
                element,
                "category",
                {"lang": "tr"}
            )
            category.text = program_type

        valid_count += 1

    return tv, valid_count


def save_xml(root):
    tree = ET.ElementTree(root)

    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass

    temporary_file = OUTPUT_FILE + ".tmp"

    tree.write(
        temporary_file,
        encoding="utf-8",
        xml_declaration=True
    )

    ET.parse(temporary_file)
    os.replace(temporary_file, OUTPUT_FILE)

    log(f"XML kaydedildi: {OUTPUT_FILE}")


def main():
    log("=" * 65)
    log("TIVIBU 7 GÜNLÜK EPG")
    log("Yalnızca TÜM KANALLAR kategorisi kullanılacak.")
    log("Kanal ID: .fullLink[channelcode]")
    log("Kanal adı: .channelsTitle")
    log("Program: .programTitle / .startTime / .finishTime")
    log("=" * 65)

    channels = {}
    programs = []

    today = datetime.now(TIMEZONE).date()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--disable-popup-blocking"
            ]
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080
            },
            permissions=[]
        )

        install_popup_blocker(context)

        page = context.new_page()
        page.set_default_timeout(15000)

        page.on("dialog", lambda dialog: dialog.dismiss())

        for offset in range(DAYS):
            day = today + timedelta(days=offset)

            collect_day(
                page,
                day,
                channels,
                programs
            )

        browser.close()

    root, program_count = build_xml(
        channels,
        programs
    )

    log("")
    log("=" * 65)
    log(f"Toplam kanal: {len(channels)}")
    log(f"Toplam program: {program_count}")

    if len(channels) < 30:
        raise RuntimeError(
            f"Kanal sayısı yetersiz: {len(channels)}"
        )

    if program_count < 100:
        raise RuntimeError(
            f"Program sayısı yetersiz: {program_count}"
        )

    save_xml(root)

    log("İŞLEM TAMAMLANDI")


if __name__ == "__main__":
    main()
