
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
# AYARLAR
# ============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = BASE_URL + "/canli-tv"

OUTPUT_FILE = "epg.xml"

DAYS_TO_FETCH = 7
MIN_CHANNELS = 10
MIN_PROGRAMS = 100

TIMEOUT = 60000
PAGE_WAIT_MS = 2500

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
# HTML PARSER
# ============================================================

class TivibuHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.anchors = []
        self.images = []
        self.current_anchor = None

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        if tag == "a":

            self.current_anchor = {
                "href": html.unescape(attrs.get("href", "")),
                "text": "",
                "images": []
            }

        elif tag == "img":

            alt = normalize_text(attrs.get("alt", ""))

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
# METİN İŞLEME
# ============================================================

def normalize_text(value):

    if not value:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")

    return re.sub(r"\s+", " ", value).strip()


def normalize_channel_id(href):

    if not href:
        return None

    match = CHANNEL_ID_PATTERN.search(
        html.unescape(str(href))
    )

    if match:
        return match.group(0).lower()

    return None


def parse_program_text(text):

    text = normalize_text(text)

    match = TIME_PATTERN.search(text)

    if not match:
        return None

    title = text[:match.start()].strip()

    title = re.sub(
        r"\s+Canlı$",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()

    title = re.sub(r"\s*[-–]\s*$", "", title).strip()

    title = CATEGORY_PATTERN.sub("", title).strip()

    if not title:
        return None

    return {
        "title": title,
        "start": match.group("start"),
        "end": match.group("end")
    }


def extract_channel_name(anchor):

    for image in anchor.get("images", []):

        name = normalize_text(image)

        name = re.sub(
            r"^Kanal\s*:\s*",
            "",
            name,
            flags=re.IGNORECASE
        )

        if name:
            return name

    text = normalize_text(anchor.get("text", ""))

    if not text or TIME_PATTERN.search(text):
        return None

    if text.lower() in ("canlı", "izle", "program akışı"):
        return None

    return text


# ============================================================
# SAYFADAN KANAL VE PROGRAMLARI OKU
# ============================================================

def extract_page_data(source):

    parser = TivibuHTMLParser()
    parser.feed(source)

    channel_names = {}
    program_groups = {}

    for anchor in parser.anchors:

        channel_id = normalize_channel_id(
            anchor.get("href", "")
        )

        if not channel_id:
            continue

        name = extract_channel_name(anchor)

        if name and channel_id not in channel_names:
            channel_names[channel_id] = name

    for anchor in parser.anchors:

        href = html.unescape(
            anchor.get("href", "")
        )

        if "datatype=2" not in href:
            continue

        if not re.search(r"i\s*=\s*2", href):
            continue

        channel_id = normalize_channel_id(href)

        if not channel_id:
            continue

        program = parse_program_text(
            anchor.get("text", "")
        )

        if not program:
            continue

        program_groups.setdefault(
            channel_id,
            []
        ).append(program)

    result = {}

    for channel_id, programs in program_groups.items():

        name = channel_names.get(channel_id)

        if not name:
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
# TARİH BAĞLANTILARINI İLK SAYFADAN KAYDET
# ============================================================

def read_available_dates(page):

    links = page.locator(
        "a[channeldatebegin][channeldateend]"
    )

    count = links.count()

    dates = {}

    for index in range(count):

        link = links.nth(index)

        try:

            begin = link.get_attribute(
                "channeldatebegin"
            )

            end = link.get_attribute(
                "channeldateend"
            )

            if not begin or not end:
                continue

            date_value = datetime.strptime(
                begin,
                "%Y.%m.%d %H:%M:%S"
            ).date()

            label = normalize_text(
                link.get_attribute("title") or ""
            )

            if not label:
                label = normalize_text(
                    link.inner_text(timeout=1000)
                )

            dates[date_value] = {
                "begin": begin,
                "end": end,
                "label": label
            }

        except Exception:
            continue

    return dates


def build_date_list(available_dates, today):

    selected = []

    for offset in range(DAYS_TO_FETCH):

        target_day = today + timedelta(days=offset)

        record = available_dates.get(target_day)

        if record is None:

            raise RuntimeError(
                "Tivibu tarih listesinde gün bulunamadı: "
                + target_day.strftime("%d.%m.%Y")
            )

        if offset == 0:
            label = "Bugün"

        elif offset == 1:
            label = "Yarın"

        else:
            label = target_day.strftime("%d.%m.%Y")

        selected.append({
            "date": target_day,
            "label": label,
            "begin": record["begin"],
            "end": record["end"]
        })

    return selected


# ============================================================
# HER GÜN İÇİN SAYFAYI YENİDEN AÇ
# ============================================================

def open_live_page(page):

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

    page.wait_for_timeout(PAGE_WAIT_MS)


def select_date_by_attributes(page, date_record):

    begin = date_record["begin"]
    end = date_record["end"]

    links = page.locator(
        "a[channeldatebegin][channeldateend]"
    )

    selected_link = None

    for index in range(links.count()):

        link = links.nth(index)

        try:

            current_begin = link.get_attribute(
                "channeldatebegin"
            )

            current_end = link.get_attribute(
                "channeldateend"
            )

            if current_begin == begin and current_end == end:

                selected_link = link
                break

        except Exception:
            continue

    if selected_link is None:

        raise RuntimeError(
            "Tarih bağlantısı bulunamadı: "
            + date_record["label"]
            + " | "
            + begin
        )

    # Görünürlük kontrolüne takılmadan doğru bağlantıya tıkla.
    selected_link.evaluate(
        "(element) => element.click()"
    )

    page.wait_for_timeout(PAGE_WAIT_MS)

    print(
        "  Tarih seçildi:",
        date_record["label"],
        "|",
        begin,
        flush=True
    )


# ============================================================
# TARİH VE SAAT DÖNÜŞÜMÜ
# ============================================================

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


def xmltv_time(value):

    return value.strftime(
        "%Y%m%d%H%M%S"
    ) + " +0300"


# ============================================================
# 7 GÜNLÜK EPG TOPLAMA
# ============================================================

def collect_seven_days(page, date_records):

    all_channels = {}

    for day_index, record in enumerate(
        date_records,
        1
    ):

        label = record["label"]
        current_day = record["date"]

        print(
            f"[{day_index}/{DAYS_TO_FETCH}] "
            f"{label} alınıyor...",
            flush=True
        )

        # Her gün temiz sayfa aç.
        open_live_page(page)

        # Bugün zaten seçili gelir.
        # Diğer günlerde kayıtlı tarih değerleriyle seçim yap.
        if day_index > 1:

            select_date_by_attributes(
                page,
                record
            )

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

    # Yinelenen programları temizle.
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
# XMLTV OLUŞTUR
# ============================================================

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
            days.add(program["start"].date())

    if len(days) < 6:

        raise RuntimeError(
            f"Yalnızca {len(days)} farklı yayın günü bulundu. "
            "XML korunuyor."
        )


# ============================================================
# GÜVENLİ XML KAYDI
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

        # Kaydedilen XML'i tekrar oku.
        ET.parse(temp_path)

        # Kontrol başarılıysa mevcut dosyanın yerine koy.
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
    print("Her gün ayrı sayfada, tarih değerleriyle seçim")
    print("Kaynak:", LIVE_URL)

    today = datetime.now().date()

    print(
        "Başlangıç:",
        today.strftime("%d.%m.%Y")
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

        page.set_default_timeout(TIMEOUT)

        print(
            "[1/4] Tivibu sayfası açılıyor...",
            flush=True
        )

        open_live_page(page)

        print(
            "[2/4] Tarih bağlantıları okunuyor...",
            flush=True
        )

        available_dates = read_available_dates(page)

        print(
            "Bulunan tarih bağlantısı:",
            len(available_dates),
            flush=True
        )

        date_records = build_date_list(
            available_dates,
            today
        )

        print(
            "Alınacak günler:",
            ", ".join(
                item["label"]
                for item in date_records
            ),
            flush=True
        )

        print(
            "[3/4] Kanal ve programlar alınıyor...",
            flush=True
        )

        channels, program_count = collect_seven_days(
            page,
            date_records
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

    tree = build_xml(channels)

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
