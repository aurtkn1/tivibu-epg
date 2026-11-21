# tivibu_epg.py
import re
import html
import time
import os
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.tivibu.com.tr"
SOURCE_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
TIMEZONE = ZoneInfo("Europe/Istanbul")
DAYS = 7

CATEGORIES = [
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

EXTRACT_JS = r"""
() => {
    const clean = (value) =>
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
            const item = { id, name };
            channelMap.set(id, item);
            channels.push(item);
        }
    });

    const programs = [];
    const seen = new Set();

    document.querySelectorAll('a[href*="/rv?"]').forEach(link => {
        const titleElement = link.querySelector(".programTitle");
        const startElement = link.querySelector(".startTime");
        const finishElement = link.querySelector(".finishTime");

        if (!titleElement || !startElement || !finishElement) return;

        const title = clean(titleElement.innerText);
        const start = clean(startElement.innerText);
        const finish = clean(finishElement.innerText);
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
        programs,
        pageTitle: document.title
    };
}
"""


def log(message):
    print(message, flush=True)


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def parse_time(value):
    match = re.search(r"(\d{1,2}):(\d{2})", value or "")
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def close_overlays(page):
    """
    Tivibu sayfasındaki Insider bildirim/reklam pencerelerini kapatır.
    Kapatma düğmesi bulunamazsa engelleyici katmanları etkisizleştirir.
    """

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
                document.querySelectorAll(selector).forEach(element => {
                    element.remove();
                });
            });

            document.querySelectorAll("body *").forEach(element => {
                const style = window.getComputedStyle(element);

                if (
                    style.position === "fixed" &&
                    Number(style.zIndex) > 1000
                ) {
                    const text = (
                        element.innerText ||
                        element.getAttribute("aria-label") ||
                        ""
                    ).toLowerCase();

                    if (
                        text.includes("bildirim") ||
                        text.includes("notification") ||
                        element.className?.toString().toLowerCase().includes("ins-")
                    ) {
                        element.remove();
                    }
                }
            });

            document.body.style.overflow = "auto";
            document.documentElement.style.overflow = "auto";
        }
        """)
    except Exception:
        pass


def install_popup_blocker(context):
    """
    Insider katmanlarının sayfaya yeniden eklenmesini önlemeye yardımcı olur.
    """

    context.add_init_script("""
    (() => {
        const removeInsider = () => {
            const selectors = [
                ".ins-preview-wrapper",
                "#ins-frameless-overlay",
                '[id^="ins-frame"]',
                '[class*="ins-preview-wrapper"]'
            ];

            selectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(element => {
                    element.remove();
                });
            });
        };

        const observer = new MutationObserver(removeInsider);

        const startObserver = () => {
            if (document.documentElement) {
                observer.observe(document.documentElement, {
                    childList: true,
                    subtree: true
                });
                removeInsider();
            }
        };

        if (document.documentElement) {
            startObserver();
        } else {
            document.addEventListener("DOMContentLoaded", startObserver);
        }
    })();
    """)


def safe_click(page, locator, timeout=10000):
    """
    Önce normal tıklamayı dener.
    Üst katman tıklamayı engellerse DOM click kullanır.
    """

    close_overlays(page)

    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
        locator.click(timeout=3000)
        return True
    except Exception:
        pass

    close_overlays(page)

    try:
        locator.evaluate("(element) => element.click()")
        return True
    except Exception:
        return False


def wait_page(page, milliseconds=1200):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    page.wait_for_timeout(milliseconds)
    close_overlays(page)


def get_visible_date_links(page):
    try:
        return page.locator(
            "a[channeldatebegin][channeldateend]:visible"
        ).all()
    except Exception:
        return []


def select_date(page, target_date):
    date_text = target_date.strftime("%d.%m.%Y")

    close_overlays(page)

    links = page.locator(
        "a[channeldatebegin][channeldateend]:visible"
    )

    try:
        count = links.count()
    except Exception:
        count = 0

    for index in range(count):
        link = links.nth(index)

        try:
            begin = link.get_attribute("channeldatebegin") or ""
            end = link.get_attribute("channeldateend") or ""
            label = normalize(link.inner_text())

            date_value = begin or end

            if target_date.strftime("%Y-%m-%d") in date_value:
                if "active" in (link.get_attribute("class") or "").lower():
                    return True

                safe_click(page, link)
                wait_page(page, 1800)
                return True

            if date_text in label:
                if "active" in (link.get_attribute("class") or "").lower():
                    return True

                safe_click(page, link)
                wait_page(page, 1800)
                return True

        except Exception:
            continue

    return False


def extract_page(page):
    close_overlays(page)

    try:
        return page.evaluate(EXTRACT_JS)
    except Exception as error:
        log(f"Sayfa verisi okunamadı: {error}")
        return {
            "channelCards": 0,
            "channels": [],
            "programLinks": 0,
            "programs": [],
            "pageTitle": ""
        }


def make_datetime(day, time_value):
    parsed = parse_time(time_value)
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


def collect_category(page, category_name, category_path, channels, programs):
    url = urljoin(BASE_URL, category_path)

    log("")
    log("=" * 65)
    log(f"KATEGORİ: {category_name}")
    log(f"Kaynak: {url}")

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )
    except Exception as error:
        log(f"Sayfa açma hatası: {error}")
        return

    wait_page(page, 2500)

    try:
        page.locator(".fullLink[channelcode]").first.wait_for(
            state="attached",
            timeout=15000
        )
    except Exception:
        pass

    close_overlays(page)

    # Sayfadaki kanalların yüklenmesi için aşağı kaydır.
    previous_count = -1
    stable_count = 0

    for _ in range(15):
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

        if stable_count >= 2:
            break

        previous_count = current_count

        try:
            page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception:
            pass

        page.wait_for_timeout(500)
        close_overlays(page)

    data = extract_page(page)

    log(f"Kanal kartı: {data['channelCards']}")
    log(f"Kanal bağlantısı: {data['programLinks']}")

    for channel in data["channels"]:
        channel_id = normalize(channel["id"])
        channel_name = normalize(channel["name"])

        if channel_id and channel_name:
            if channel_id not in channels:
                channels[channel_id] = channel_name
                log(f"Yeni kanal: {channel_name} ({channel_id})")

    for program in data["programs"]:
        channel_id = normalize(program["channelId"])

        if channel_id not in channels:
            continue

        programs.append(program)

    log(f"Bu kategoride okunan program: {len(data['programs'])}")


def collect_day(page, day, channels, all_programs):
    log("")
    log("-" * 65)
    log(f"TARİH: {day.strftime('%d.%m.%Y')}")

    for category_name, category_path in CATEGORIES:
        url = urljoin(BASE_URL, category_path)

        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000
            )
        except Exception as error:
            log(f"{category_name}: Sayfa açılamadı: {error}")
            continue

        wait_page(page, 1500)
        close_overlays(page)

        selected = select_date(page, day)

        if not selected:
            log(f"{category_name}: Tarih seçilemedi.")
            continue

        data = extract_page(page)

        for channel in data["channels"]:
            channel_id = normalize(channel["id"])
            channel_name = normalize(channel["name"])

            if channel_id and channel_name:
                channels.setdefault(channel_id, channel_name)

        added = 0

        for program in data["programs"]:
            channel_id = normalize(program["channelId"])

            if channel_id not in channels:
                continue

            item = dict(program)
            item["date"] = day
            all_programs.append(item)
            added += 1

        log(
            f"{category_name}: "
            f"{len(data['channels'])} kanal kartı, "
            f"{len(data['programs'])} program, "
            f"{added} kayıt eklendi."
        )


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
    valid_programs = 0

    for program in programs:
        channel_id = normalize(program.get("channelId"))
        title = normalize(program.get("title"))
        start_text = normalize(program.get("start"))
        finish_text = normalize(program.get("finish"))
        day = program.get("date")

        if not channel_id or channel_id not in channels:
            continue

        if not title or not day:
            continue

        start_dt = make_datetime(day, start_text)
        finish_dt = make_datetime(day, finish_text)

        if not start_dt or not finish_dt:
            continue

        if finish_dt <= start_dt:
            finish_dt += timedelta(days=1)

        start_xml = start_dt.strftime("%Y%m%d%H%M%S %z")
        stop_xml = finish_dt.strftime("%Y%m%d%H%M%S %z")

        key = (
            channel_id,
            start_xml,
            stop_xml,
            title
        )

        if key in seen:
            continue

        seen.add(key)

        programme = ET.SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": start_xml,
                "stop": stop_xml
            }
        )

        title_element = ET.SubElement(
            programme,
            "title",
            {"lang": "tr"}
        )
        title_element.text = title

        program_type = normalize(program.get("type"))

        if program_type:
            category = ET.SubElement(
                programme,
                "category",
                {"lang": "tr"}
            )
            category.text = program_type

        valid_programs += 1

    log("")
    log(f"XML kanal sayısı: {len(channels)}")
    log(f"XML program sayısı: {valid_programs}")

    return tv, valid_programs


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

    # Dosya tamamlanmadan mevcut XML'in üzerine yazma.
    ET.parse(temporary_file)
    os.replace(temporary_file, OUTPUT_FILE)

    log(f"XML kaydedildi: {OUTPUT_FILE}")


def main():
    log("=" * 65)
    log("TIVIBU 7 GÜNLÜK EPG")
    log("Kanal ID: .fullLink[channelcode]")
    log("Kanal adı: .channelsTitle")
    log("Program: .programTitle / .startTime / .finishTime")
    log("Insider reklam engelleyici düzeltmesi etkin.")
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

        page.on(
            "dialog",
            lambda dialog: dialog.dismiss()
        )

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

    if len(channels) < 30:
        raise RuntimeError(
            f"Kanal sayısı yetersiz: {len(channels)}"
        )

    if program_count < 100:
        raise RuntimeError(
            f"Program sayısı yetersiz: {program_count}"
        )

    save_xml(root)

    log("")
    log("=" * 65)
    log("İŞLEM TAMAMLANDI")
    log(f"Toplam kanal: {len(channels)}")
    log(f"Toplam program: {program_count}")
    log(f"Gün sayısı: {DAYS}")
    log("=" * 65)


if __name__ == "__main__":
    main()
