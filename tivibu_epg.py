
import re
import os
import sys
import html
import tempfile
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from html.parser import HTMLParser

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# TIVIBU 7 GÜNLÜK EPG
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS_TO_FETCH = 7
MIN_CHANNELS = 30
MIN_PROGRAMS = 100

TIMEOUT = 60000
DATE_WAIT_MS = 2500

TIME_PATTERN = re.compile(
    r"(?P<start>\d{1,2}:\d{2})\s*(?:→|->|–|-)\s*(?P<end>\d{1,2}:\d{2})"
)

CHANNEL_ID_PATTERN = re.compile(
    r"ch[0-9A-Za-z]+",
    re.IGNORECASE
)

CATEGORY_PATTERN = re.compile(
    r"\s+(?:Yaşam|Film|Dizi|Haber|Spor|Çocuk|Müzik|Belgesel|"
    r"Ulusal|Global|Diğer)\s*$",
    re.IGNORECASE
)


# ============================================================
# HTML AYRIŞTIRICI
# ============================================================

class TivibuHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.anchors = []
        self.current_anchor = None
        self.images = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        if tag == "a":

            self.current_anchor = {
                "href": html.unescape(attrs.get("href", "")),
                "text": "",
                "images": []
            }

        elif tag == "img":

            alt = html.unescape(
                attrs.get("alt", "")
            ).strip()

            if alt:

                self.images.append(alt)

                if self.current_anchor is not None:
                    self.current_anchor["images"].append(alt)

    def handle_data(self, data):

        if self.current_anchor is not None:
            self.current_anchor["text"] += " " + data

    def handle_endtag(self, tag):

        if tag == "a" and self.current_anchor is not None:

            self.current_anchor["text"] = normalize_text(
                self.current_anchor["text"]
            )

            self.anchors.append(self.current_anchor)
            self.current_anchor = None


# ============================================================
# GENEL YARDIMCILAR
# ============================================================

def normalize_text(value):

    if not value:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_channel_id(href):

    if not href:
        return None

    decoded = html.unescape(str(href))

    match = CHANNEL_ID_PATTERN.search(decoded)

    if not match:
        return None

    return match.group(0).lower()


def parse_html(source):

    parser = TivibuHTMLParser()
    parser.feed(source)

    return parser


def extract_channel_name(anchor):

    candidates = []

    for image in anchor.get("images", []):

        image = normalize_text(image)

        image = re.sub(
            r"^Kanal\s*:\s*",
            "",
            image,
            flags=re.IGNORECASE
        )

        if image:
            candidates.append(image)

    text = normalize_text(
        anchor.get("text", "")
    )

    if text:
        candidates.append(text)

    for candidate in candidates:

        candidate = normalize_text(candidate)

        if not candidate:
            continue

        if TIME_PATTERN.search(candidate):
            continue

        if candidate.lower() in (
            "canlı",
            "izle",
            "program akışı"
        ):
            continue

        return candidate

    return None


def parse_program_text(text):

    text = normalize_text(text)

    match = TIME_PATTERN.search(text)

    if not match:
        return None

    start = match.group("start")
    end = match.group("end")

    title_part = text[:match.start()].strip()

    title_part = re.sub(
        r"\s+Canlı$",
        "",
        title_part,
        flags=re.IGNORECASE
    ).strip()

    title_part = re.sub(
        r"\s*[-–]\s*$",
        "",
        title_part
    ).strip()

    title_part = CATEGORY_PATTERN.sub(
        "",
        title_part
    ).strip()

    if not title_part:
        return None

    return {
        "title": title_part,
        "start": start,
        "end": end
    }


# ============================================================
# KANAL VE PROGRAM VERİLERİNİ ÇIKAR
# ============================================================

def extract_page_data(source):

    parser = parse_html(source)

    channel_names = {}
    ordered_channel_ids = []

    program_groups = {}

    # Kanal adlarını bul
    for anchor in parser.anchors:

        channel_id = normalize_channel_id(
            anchor.get("href", "")
        )

        if not channel_id:
            continue

        name = extract_channel_name(anchor)

        if not name:
            continue

        if channel_id not in channel_names:

            channel_names[channel_id] = name
            ordered_channel_ids.append(channel_id)

    # Program bağlantılarını bul
    for anchor in parser.anchors:

        href = anchor.get("href", "")

        decoded_href = html.unescape(href)

        if "datatype=2" not in decoded_href:
            continue

        if not re.search(
            r"i\s*=\s*2",
            decoded_href
        ):
            continue

        channel_id = normalize_channel_id(
            decoded_href
        )

        if not channel_id:
            continue

        program = parse_program_text(
            anchor.get("text", "")
        )

        if not program:
            continue

        if channel_id not in program_groups:
            program_groups[channel_id] = []

        program_groups[channel_id].append(program)

    # Kanal adı bulunamazsa görsel alt metinlerini kullan
    if not channel_names and program_groups:

        image_names = []

        for image in parser.images:

            image = normalize_text(image)

            image = re.sub(
                r"^Kanal\s*:\s*",
                "",
                image,
                flags=re.IGNORECASE
            )

            if image and image not in image_names:
                image_names.append(image)

        for index, channel_id in enumerate(
            program_groups.keys()
        ):

            if index < len(image_names):
                channel_names[channel_id] = image_names[index]

    # Kanal ve programları birleştir
    result = {}

    for channel_id, programs in program_groups.items():

        name = channel_names.get(channel_id)

        if not name:
            continue

        if not programs:
            continue

        result[channel_id] = {
            "name": name,
            "programs": programs
        }

    diagnostics = {
        "anchors": len(parser.anchors),
        "channel_names": len(channel_names),
        "program_groups": len(program_groups),
        "matched_channels": len(result),
        "program_count": sum(
            len(item["programs"])
            for item in result.values()
        )
    }

    return result, diagnostics


# ============================================================
# TARİH SEÇİMİ
# ESKİ ÇALIŞAN SÜRÜMDEKİ SELECTOR KULLANILIYOR
# ============================================================

def date_label(day, today):

    if day == today:
        return "Bugün"

    if day == today + timedelta(days=1):
        return "Yarın"

    return day.strftime("%d.%m.%Y")


def build_seven_date_labels(today):

    return [
        date_label(
            today + timedelta(days=offset),
            today
        )
        for offset in range(DAYS_TO_FETCH)
    ]


def find_date_link(page, label):

    links = page.locator(
        "a[channeldatebegin][channeldateend]"
    )

    count = links.count()

    for index in range(count):

        link = links.nth(index)

        try:

            text = normalize_text(
                link.inner_text(timeout=1500)
            )

            if text == label:
                return link

        except Exception:
            continue

    return None


def select_date(page, label):

    link = find_date_link(
        page,
        label
    )

    if link is None:

        available = []

        links = page.locator(
            "a[channeldatebegin][channeldateend]"
        )

        for index in range(links.count()):

            try:
                available.append(
                    normalize_text(
                        links.nth(index).inner_text(timeout=1000)
                    )
                )
            except Exception:
                pass

        raise RuntimeError(
            "Tarih bağlantısı bulunamadı: "
            + label
            + ". Bulunan tarih bağlantıları: "
            + ", ".join(available)
        )

    old_url = page.url

    try:

        link.click(
            timeout=15000,
            force=True
        )

    except PlaywrightTimeoutError:

        # Tıklama sonrası sayfa geçişi gecikirse
        # yüklenme kontrolü aşağıda yapılır.
        pass

    try:

        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000
        )

    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(
        DATE_WAIT_MS
    )

    print(
        "  Tarih bağlantısı seçildi:",
        label,
        flush=True
    )

    if page.url != old_url:

        print(
            "  Yeni sayfa adresi:",
            page.url,
            flush=True
        )


# ============================================================
# TARİH DÖNÜŞÜMÜ
# ============================================================

def date_from_label(label, base_date):

    if label == "Bugün":
        return base_date

    if label == "Yarın":
        return base_date + timedelta(days=1)

    try:

        return datetime.strptime(
            label,
            "%d.%m.%Y"
        ).date()

    except ValueError:

        return None


def make_datetime(day, clock):

    hour, minute = map(
        int,
        clock.split(":")
    )

    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute
    )


# ============================================================
# 7 GÜNLÜK VERİ TOPLAMA
# ============================================================

def collect_seven_days(page, date_labels, base_date):

    all_channels = {}

    # Bugün sayfa açıldığında zaten seçili.
    # Bugün için tıklama yapılmıyor.

    for day_index, label in enumerate(
        date_labels,
        1
    ):

        print(
            f"[{day_index}/{DAYS_TO_FETCH}] "
            f"{label} alınıyor...",
            flush=True
        )

        if label != "Bugün":

            select_date(
                page,
                label
            )

        else:

            page.wait_for_timeout(1000)

        source = page.content()

        day_data, diagnostics = extract_page_data(
            source
        )

        print(
            "  HTML bağlantısı:",
            diagnostics["anchors"],
            flush=True
        )

        print(
            "  Kanal adı bulunan:",
            diagnostics["channel_names"],
            flush=True
        )

        print(
            "  Program grubu:",
            diagnostics["program_groups"],
            flush=True
        )

        print(
            "  Eşleşen kanal:",
            diagnostics["matched_channels"],
            flush=True
        )

        if not day_data:

            raise RuntimeError(
                label
                + " için kanal-program verisi bulunamadı. "
                + "Mevcut XML korunuyor."
            )

        current_day = date_from_label(
            label,
            base_date
        )

        if current_day is None:

            raise RuntimeError(
                "Tarih dönüştürülemedi: "
                + label
            )

        daily_program_count = 0

        for channel_id, channel in day_data.items():

            if channel_id not in all_channels:

                all_channels[channel_id] = {
                    "name": channel["name"],
                    "programs": []
                }

            for program in channel["programs"]:

                start_dt = make_datetime(
                    current_day,
                    program["start"]
                )

                end_dt = make_datetime(
                    current_day,
                    program["end"]
                )

                # Gece yarısını geçen program
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)

                all_channels[channel_id]["programs"].append({
                    "title": program["title"],
                    "start": start_dt,
                    "end": end_dt
                })

                daily_program_count += 1

        print(
            "  Günlük program sayısı:",
            daily_program_count,
            flush=True
        )

    # Yinelenen kayıtları temizle
    for channel in all_channels.values():

        unique = {}

        for program in channel["programs"]:

            key = (
                program["title"],
                program["start"],
                program["end"]
            )

            unique[key] = program

        channel["programs"] = sorted(
            unique.values(),
            key=lambda item: item["start"]
        )

    final_program_count = sum(
        len(channel["programs"])
        for channel in all_channels.values()
    )

    return all_channels, final_program_count


# ============================================================
# XMLTV OLUŞTURMA
# ============================================================

def xmltv_time(value):

    return value.strftime(
        "%Y%m%d%H%M%S"
    ) + " +0300"


def build_xml(channels):

    tv = ET.Element(
        "tv",
        attrib={
            "generator-info-name": "Tivibu 7 Günlük EPG",
            "generator-info-url": LIVE_URL
        }
    )

    for channel_id, channel in sorted(
        channels.items(),
        key=lambda item: item[1]["name"].casefold()
    ):

        channel_element = ET.SubElement(
            tv,
            "channel",
            id=channel_id
        )

        display_name = ET.SubElement(
            channel_element,
            "display-name"
        )

        display_name.text = channel["name"]

    for channel_id, channel in channels.items():

        for program in channel["programs"]:

            programme = ET.SubElement(
                tv,
                "programme",
                attrib={
                    "start": xmltv_time(program["start"]),
                    "stop": xmltv_time(program["end"]),
                    "channel": channel_id
                }
            )

            title = ET.SubElement(
                programme,
                "title",
                lang="tr"
            )

            title.text = program["title"]

    return ET.ElementTree(tv)


# ============================================================
# XML DOĞRULAMA
# ============================================================

def validate_data(channels, program_count):

    channel_count = len(channels)

    if channel_count < MIN_CHANNELS:

        raise RuntimeError(
            f"Kanal sayısı yetersiz: {channel_count}. "
            f"Minimum: {MIN_CHANNELS}. XML korunuyor."
        )

    if program_count < MIN_PROGRAMS:

        raise RuntimeError(
            f"Program sayısı yetersiz: {program_count}. "
            f"Minimum: {MIN_PROGRAMS}. XML korunuyor."
        )

    days = set()

    for channel in channels.values():

        for program in channel["programs"]:

            days.add(
                program["start"].date()
            )

    if len(days) < 6:

        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "XML korunuyor."
        )


# ============================================================
# XML GÜVENLİ KAYDETME
# ============================================================

def save_xml(tree, output_file):

    directory = os.path.dirname(
        os.path.abspath(output_file)
    )

    fd, temp_path = tempfile.mkstemp(
        prefix="tivibu_",
        suffix=".xml",
        dir=directory
    )

    os.close(fd)

    try:

        tree.write(
            temp_path,
            encoding="utf-8",
            xml_declaration=True
        )

        # XML tekrar okunarak kontrol edilir.
        ET.parse(temp_path)

        # Doğrulama başarılıysa eski dosyanın yerine geçer.
        os.replace(
            temp_path,
            output_file
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


# ============================================================
# ANA PROGRAM
# ============================================================

def main():

    print("=" * 64)
    print("TIVIBU 7 GÜNLÜK EPG")
    print("Tarih bağlantıları: channeldatebegin/channeldateend")
    print("Kaynak:", LIVE_URL)

    base_date = datetime.now().date()

    print(
        "Başlangıç:",
        base_date.strftime("%d.%m.%Y")
    )

    print("=" * 64)

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={
                "width": 1920,
                "height": 1080
            }
        )

        page = context.new_page()

        page.set_default_timeout(
            TIMEOUT
        )

        print(
            "[1/4] Tivibu sayfası açılıyor...",
            flush=True
        )

        page.goto(
            LIVE_URL,
            wait_until="domcontentloaded",
            timeout=TIMEOUT
        )

        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=15000
            )

        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3000)

        print(
            "[2/4] Tarih seçenekleri okunuyor...",
            flush=True
        )

        date_links = page.locator(
            "a[channeldatebegin][channeldateend]"
        )

        print(
            "Bulunan tarih bağlantısı:",
            date_links.count(),
            flush=True
        )

        date_labels = build_seven_date_labels(
            base_date
        )

        print(
            "Alınacak günler:",
            ", ".join(date_labels),
            flush=True
        )

        print(
            "[3/4] Kanal ve programlar alınıyor...",
            flush=True
        )

        channels, program_count = collect_seven_days(
            page,
            date_labels,
            base_date
        )

        browser.close()

    print(
        "[4/4] XML doğrulanıyor ve kaydediliyor...",
        flush=True
    )

    validate_data(
        channels,
        program_count
    )

    tree = build_xml(
        channels
    )

    save_xml(
        tree,
        OUTPUT_FILE
    )

    print("=" * 64)
    print("TIVIBU EPG TAMAMLANDI")
    print("Toplam kanal:", len(channels))
    print("Toplam program:", program_count)
    print("Alınan gün:", DAYS_TO_FETCH)
    print("Çıktı:", OUTPUT_FILE)
    print("=" * 64)


if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        print(
            "\nHATA:",
            str(error),
            file=sys.stderr,
            flush=True
        )

        raise
