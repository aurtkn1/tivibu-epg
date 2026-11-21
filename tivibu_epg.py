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


SELECT_DATE_JS = r"""
(targetDate) => {
    const clean = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const dateKey = date => {
        if (!date || isNaN(date.getTime())) return "";

        return [
            date.getFullYear(),
            String(date.getMonth() + 1).padStart(2, "0"),
            String(date.getDate()).padStart(2, "0")
        ].join("-");
    };

    const parseDate = value => {
        value = clean(value);
        if (!value) return null;

        if (/^\d{8}$/.test(value)) {
            const result = new Date(
                Number(value.slice(0, 4)),
                Number(value.slice(4, 6)) - 1,
                Number(value.slice(6, 8))
            );

            return isNaN(result.getTime()) ? null : result;
        }

        if (/^\d{10,13}$/.test(value)) {
            let number = Number(value);

            if (value.length === 10) number *= 1000;

            const result = new Date(number);
            return isNaN(result.getTime()) ? null : result;
        }

        let result = new Date(value);

        if (!isNaN(result.getTime())) return result;

        const match = value.match(
            /^(\d{1,2})[./-](\d{1,2})[./-](\d{4})/
        );

        if (match) {
            result = new Date(
                Number(match[3]),
                Number(match[2]) - 1,
                Number(match[1])
            );

            return isNaN(result.getTime()) ? null : result;
        }

        return null;
    };

    const wanted = targetDate;
    const candidates = Array.from(
        document.querySelectorAll(
            "a[channeldatebegin][channeldateend], [channeldatebegin][channeldateend]"
        )
    );

    let matchingElement = null;

    for (const element of candidates) {
        const beginValue =
            element.getAttribute("channeldatebegin") || "";

        const endValue =
            element.getAttribute("channeldateend") || "";

        const label = clean(element.innerText);

        const beginDate = parseDate(beginValue);
        const endDate = parseDate(endValue);

        let matches = false;

        if (beginDate && dateKey(beginDate) === wanted) {
            matches = true;
        }

        if (endDate && dateKey(endDate) === wanted) {
            matches = true;
        }

        if (beginDate && endDate) {
            const startKey = dateKey(beginDate);
            const finishKey = dateKey(endDate);

            if (startKey <= wanted && wanted <= finishKey) {
                matches = true;
            }
        }

        const dotDate = wanted.split("-").reverse().join(".");
        const slashDate = wanted.split("-").reverse().join("/");

        if (
            label.includes(dotDate) ||
            label.includes(slashDate)
        ) {
            matches = true;
        }

        if (matches) {
            matchingElement = element;
            break;
        }
    }

    if (matchingElement) {
        const classes = (
            matchingElement.getAttribute("class") || ""
        ).toLowerCase();

        if (
            classes.includes("active") ||
            matchingElement.getAttribute("aria-selected") === "true"
        ) {
            return {
                success: true,
                method: "active"
            };
        }

        matchingElement.scrollIntoView({
            block: "center",
            behavior: "instant"
        });

        matchingElement.click();

        return {
            success: true,
            method: "attribute"
        };
    }

    const dotDate = wanted.split("-").reverse().join(".");
    const slashDate = wanted.split("-").reverse().join("/");

    const textElements = Array.from(
        document.querySelectorAll("a, button, [role='button']")
    );

    for (const element of textElements) {
        const label = clean(element.innerText);

        if (
            label.includes(dotDate) ||
            label.includes(slashDate)
        ) {
            element.scrollIntoView({
                block: "center",
                behavior: "instant"
            });

            element.click();

            return {
                success: true,
                method: "text"
            };
        }
    }

    return {
        success: false,
        candidates: candidates.length,
        details: candidates.slice(0, 15).map(element => ({
            begin: element.getAttribute("channeldatebegin"),
            end: element.getAttribute("channeldateend"),
            text: clean(element.innerText)
        }))
    };
}
"""


OPEN_CHANNEL_JS = r"""
(channelId) => {
    const cards = Array.from(
        document.querySelectorAll(".fullLink[channelcode]")
    ).filter(card =>
        (card.getAttribute("channelcode") || "").toLowerCase() ===
        channelId.toLowerCase()
    );

    if (!cards.length) {
        return {
            success: false,
            reason: "Kanal kartı bulunamadı"
        };
    }

    const visibleCard = cards.find(card => {
        const style = window.getComputedStyle(card);
        const rect = card.getBoundingClientRect();

        return (
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            Number(style.opacity) !== 0 &&
            rect.width > 0 &&
            rect.height > 0
        );
    });

    const card = visibleCard || cards[0];

    card.scrollIntoView({
        block: "center",
        inline: "nearest",
        behavior: "instant"
    });

    const icon = card.querySelector(".openIcon");

    if (icon) {
        icon.dispatchEvent(
            new MouseEvent("click", {
                bubbles: true,
                cancelable: true,
                view: window
            })
        );

        return {
            success: true,
            method: "openIcon",
            visible: Boolean(visibleCard)
        };
    }

    card.dispatchEvent(
        new MouseEvent("click", {
            bubbles: true,
            cancelable: true,
            view: window
        })
    );

    return {
        success: true,
        method: "card",
        visible: Boolean(visibleCard)
    };
}
"""


EXTRACT_PROGRAMS_JS = r"""
(channelId) => {
    const clean = value =>
        (value || "").replace(/\s+/g, " ").trim();

    const programs = [];
    const seen = new Set();

    document.querySelectorAll('a[href*="/rv?"]').forEach(link => {
        const href = link.getAttribute("href") || "";
        const match = href.match(/ch\d+/i);

        if (!match) return;

        if (match[0].toLowerCase() !== channelId.toLowerCase()) {
            return;
        }

        const title = clean(
            link.querySelector(".programTitle")?.innerText
        );

        const start = clean(
            link.querySelector(".startTime")?.innerText
        );

        const finish = clean(
            link.querySelector(".finishTime")?.innerText
        );

        const type = clean(
            link.querySelector(".programType .type")?.innerText
        );

        if (!title || !start || !finish) return;

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


DIAGNOSE_PROGRAMS_JS = r"""
(channelId) => {
    const links = Array.from(
        document.querySelectorAll('a[href*="/rv?"]')
    );

    const ids = new Set();

    links.forEach(link => {
        const href = link.getAttribute("href") || "";
        const match = href.match(/ch\d+/i);

        if (match) ids.add(match[0]);
    });

    return {
        totalLinks: links.length,
        matchingId: Array.from(ids).some(
            id => id.toLowerCase() === channelId.toLowerCase()
        ),
        sampleIds: Array.from(ids).slice(0, 12)
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
            [
                ".ins-preview-wrapper",
                "#ins-frameless-overlay",
                '[id^="ins-frame"]',
                '[class*="ins-preview-wrapper"]',
                '[class*="ins-webpush"]'
            ].forEach(selector => {
                document.querySelectorAll(selector).forEach(el => el.remove());
            });

            if (document.body) {
                document.body.style.overflow = "auto";
            }

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

        if (document.documentElement) {
            start();
        } else {
            document.addEventListener("DOMContentLoaded", start);
        }
    })();
    """)


def wait_page(page, milliseconds=1000):
    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=5000
        )
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

    close_overlays(page)

    try:
        result = page.evaluate(
            SELECT_DATE_JS,
            target_iso
        )
    except Exception as error:
        log(f"Tarih seçme hatası: {error}")
        return False

    if not result.get("success"):
        log(
            "Tarih bağlantısı bulunamadı. "
            f"Kontrol edilen bağlantı: {result.get('candidates', 0)}"
        )

        for item in result.get("details", [])[:10]:
            log(
                "  Tarih: "
                f"başlangıç={item.get('begin')} "
                f"bitiş={item.get('end')} "
                f"metin={item.get('text')}"
            )

        return False

    wait_page(page, 1500)

    log(
        f"Tarih seçildi: {target_date.strftime('%d.%m.%Y')} "
        f"({result.get('method')})"
    )

    return True


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
        result = page.evaluate(
            OPEN_CHANNEL_JS,
            channel_id
        )

        if not result.get("success"):
            log(
                f"  Kanal açılamadı: {channel_name}: "
                f"{result.get('reason', 'Bilinmeyen hata')}"
            )
            return []

    except Exception as error:
        log(f"  Kanal açılamadı: {channel_name}: {error}")
        return []

    # Her kanal açılışından sonra programların yüklenmesini bekle.
    # İkinci kez tıklama yapılmaz; bu, açık paneli kapatabilir.
    programs = []

    for attempt in range(10):
        close_overlays(page)

        try:
            programs = page.evaluate(
                EXTRACT_PROGRAMS_JS,
                channel_id
            )
        except Exception:
            programs = []

        if programs:
            break

        page.wait_for_timeout(500)

    if not programs:
        try:
            diagnostic = page.evaluate(
                DIAGNOSE_PROGRAMS_JS,
                channel_id
            )

            if diagnostic["totalLinks"] == 0:
                log("  Program listesi sayfaya yüklenmedi.")
            elif not diagnostic["matchingId"]:
                log(
                    "  Program bağlantıları var ancak kanal ID'si "
                    f"eşleşmedi. Bulunan ID örnekleri: "
                    f"{', '.join(diagnostic['sampleIds'])}"
                )
            else:
                log("  Kanal ID'si bulundu ancak program bilgileri okunamadı.")

        except Exception:
            pass

    log(f"  Program sayısı: {len(programs)}")

    return programs


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

    previous_count = -1
    stable_count = 0

    for _ in range(40):
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

    log("Kanallar okunuyor.")

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

        start_dt = make_datetime(
            day,
            program.get("start")
        )

        finish_dt = make_datetime(
            day,
            program.get("finish")
        )

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

        program_type = normalize(
            program.get("type")
        )

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
        count = channel_program_counts.get(
            channel_id,
            0
        )

        log(f"{channel_name}: {count} program")

    log("-" * 65)
    log(
        f"Programı olan kanal: "
        f"{len(channel_program_counts)}"
    )

    log(
        f"Programı olmayan kanal: "
        f"{len(channels) - len(channel_program_counts)}"
    )

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
    log("Tarih seçimi ve kanal program yüklemesi kontrol ediliyor.")
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
