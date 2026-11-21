import re
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright


BASE_URL = "https://www.tivibu.com.tr"
SOURCE_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"
TIMEZONE = ZoneInfo("Europe/Istanbul")
DAYS = 7


EXTRACT_CHANNEL_JS = r"""
(channelId) => {
    const clean = value =>
        (value || "").replace(/\s+/g, " ").trim();

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
        if (match[0].toLowerCase() !== channelId.toLowerCase()) return;

        const key = [title, start, finish].join("|");
        if (seen.has(key)) return;

        seen.add(key);

        programs.push({
            channelId: match[0],
            title,
            start,
            finish,
            type
        });
    });

    return programs;
}
"""


EXTRACT_CHANNELS_JS = r"""
() => {
    const clean = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const channels = [];
    const seen = new Set();

    document.querySelectorAll(".fullLink[channelcode]").forEach(card => {
        const id = clean(card.getAttribute("channelcode"));
        const name = clean(
            card.querySelector(".channelsTitle")?.innerText ||
            card.querySelector(".channelsTitle a")?.getAttribute("title") ||
            card.getAttribute("title")
        );

        if (!id || !name || seen.has(id)) return;

        seen.add(id);
        channels.push({id, name});
    });

    return channels;
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
            [
                ".ins-preview-wrapper",
                "#ins-frameless-overlay",
                '[id^="ins-frame"]',
                '[class*="ins-preview-wrapper"]',
                '[class*="ins-webpush"]'
            ].forEach(selector => {
                document.querySelectorAll(selector).forEach(el => el.remove());
            });

            if (document.body) document.body.style.overflow = "auto";
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
                '[class*="ins-preview-wrapper"]',
                '[class*="ins-webpush"]'
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


def wait_page(page, milliseconds=1000):
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
    target_iso = target_date.strftime("%Y-%m-%d")
    target_dot = target_date.strftime("%d.%m.%Y")
    target_slash = target_date.strftime("%d/%m/%Y")

    close_overlays(page)

    try:
        elements = page.locator(
            "[channeldatebegin], [channeldateend]"
        )

        for index in range(elements.count()):
            element = elements.nth(index)

            try:
                begin = element.get_attribute("channeldatebegin") or ""
                end = element.get_attribute("channeldateend") or ""
                label = normalize(element.inner_text())
                value = begin + " " + end

                matches = (
                    target_iso in value
                    or target_dot in value
                    or target_slash in value
                    or target_dot in label
                    or target_slash in label
                )

                if not matches:
                    continue

                classes = (
                    element.get_attribute("class") or ""
                ).lower()

                if "active" in classes:
                    return True

                element.evaluate("(el) => el.click()")
                wait_page(page, 1200)
                return True

            except Exception:
                continue

    except Exception:
        pass

    try:
        elements = page.locator("a, button, [role='button']")

        for index in range(elements.count()):
            element = elements.nth(index)

            try:
                if not element.is_visible():
                    continue

                label = normalize(element.inner_text())

                if target_dot in label or target_slash in label:
                    element.evaluate("(el) => el.click()")
                    wait_page(page, 1200)
                    return True

            except Exception:
                continue

    except Exception:
        pass

    return False


def get_channels(page):
    close_overlays(page)

    try:
        return page.evaluate(EXTRACT_CHANNELS_JS)
    except Exception as error:
        log(f"Kanal listesi okunamadı: {error}")
        return []


def open_channel_and_read_programs(page, channel):
    channel_id = normalize(channel["id"])
    channel_name = normalize(channel["name"])

    close_overlays(page)

    try:
        # Görünür olmayan kopya kartlar yerine görünür kartı seç.
        card = page.locator(
            f'.fullLink[channelcode="{channel_id}"]:visible'
        ).first

        if card.count() == 0:
            # Kart DOM'da var ama görünmüyorsa doğrudan DOM üzerinden aç.
            opened = page.locator(
                f'.fullLink[channelcode="{channel_id}"]'
            ).first.evaluate("""
            card => {
                const icon = card.querySelector(".openIcon");
                if (icon) {
                    icon.click();
                    return true;
                }
                return false;
            }
            """)
        else:
            opened = card.evaluate("""
            card => {
                const icon = card.querySelector(".openIcon");
                if (icon) {
                    icon.click();
                    return true;
                }
                return false;
            }
            """)

        if not opened:
            log(f"  Program açma düğmesi bulunamadı: {channel_name}")
            return []

    except Exception as error:
        log(f"  Kanal açılamadı: {channel_name}: {error}")
        return []

    close_overlays(page)
    page.wait_for_timeout(500)

    try:
        programs = page.evaluate(EXTRACT_CHANNEL_JS, channel_id)

        if not programs:
            page.wait_for_timeout(1000)
            programs = page.evaluate(EXTRACT_CHANNEL_JS, channel_id)

        log(f"  Program sayısı: {len(programs)}")
        return programs

    except Exception as error:
        log(f"  Program okunamadı: {channel_name}: {error}")
        return []


def collect_day(page, day, channels, all_programs):
    log("")
    log("=" * 65)
    log(f"TARİH: {day.strftime('%d.%m.%Y')}")

    try:
        page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )
    except Exception as error:
        log(f"Sayfa açılamadı: {error}")
        return

    wait_page(page, 2000)

    try:
        page.locator(
            ".fullLink[channelcode]"
        ).first.wait_for(
            state="attached",
            timeout=20000
        )
    except Exception:
        pass

    close_overlays(page)

    # Lazy-load kanal kartlarını yükle.
    previous_count = -1
    stable_count = 0

    for _ in range(30):
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

        page.wait_for_timeout(350)

    page.evaluate("window.scrollTo(0, 0)")
    wait_page(page, 500)

    day_channels = get_channels(page)
    log(f"Bulunan kanal kartı: {len(day_channels)}")

    for channel in day_channels:
        channel_id = normalize(channel["id"])
        channel_name = normalize(channel["name"])

        if channel_id and channel_name:
            channels[channel_id] = channel_name

    if not day_channels:
        log("Kanal kartı bulunamadı.")
        return

    if not select_date(page, day):
        log("Tarih seçilemedi; bu gün atlandı.")
        return

    log("Tarih seçildi. Kanallar okunuyor.")

    day_count = 0

    for index, channel in enumerate(day_channels, start=1):
        channel_name = normalize(channel["name"])

        log(f"[{index}/{len(day_channels)}] {channel_name}")

        programs = open_channel_and_read_programs(
            page,
            channel
        )

        for program in programs:
            program["date"] = day
            all_programs.append(program)
            day_count += 1

    log(f"Günlük toplam program: {day_count}")


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
    channel_program_counts = {}

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
        channel_program_counts[channel_id] = (
            channel_program_counts.get(channel_id, 0) + 1
        )

    log("")
    log("KANAL BAZLI PROGRAM KONTROLÜ")
    log("-" * 65)

    for channel_id, channel_name in sorted(channels.items()):
        count = channel_program_counts.get(channel_id, 0)
        log(f"{channel_name}: {count} program")

    log("-" * 65)
    log(f"Programı olan kanal: {len(channel_program_counts)}")
    log(f"Programı olmayan kanal: {len(channels) - len(channel_program_counts)}")

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
    log("Yalnızca TÜM KANALLAR kategorisi")
    log("Görünürlük engeli kaldırıldı; kanallar DOM üzerinden açılıyor.")
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
    log(f"Toplam gün: {DAYS}")
    log("=" * 65)

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
