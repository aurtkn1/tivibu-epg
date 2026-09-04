import re
import html
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from collections import OrderedDict
from xml.etree.ElementTree import Element, SubElement, ElementTree


# ==============================================================
# AYARLAR
# ==============================================================

BASE_URL = "https://www.tivibu.com.tr"
LIVE_URL = f"{BASE_URL}/canli-tv"

OUTPUT_FILE = "epg.xml"

TURKEY_TZ = ZoneInfo("Europe/Istanbul")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ==============================================================
# TARİH
# ==============================================================

NOW = datetime.now(
    TURKEY_TZ
)

TODAY = NOW.date()


# ==============================================================
# YARDIMCILAR
# ==============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_key(value):
    value = clean_text(value).lower()

    replacements = {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
        "’": "",
        "'": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return re.sub(
        r"[^a-z0-9]+",
        "",
        value
    )


def make_xml_id(name):
    value = clean_text(name).lower()

    replacements = {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
        "’": "_",
        "'": "_",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    )

    return value.strip("_")


def valid_channel(name):
    name = clean_text(name)

    if not name:
        return False

    bad = {
        "BENİM KANALIM",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TİVİBU CANLI TV",
        "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "TİVİBU SPOR CANLI İZLE",
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "GİRİŞ YAP",
        "ÜYE OL",
    }

    if name.upper() in bad:
        return False

    if len(name) > 100:
        return False

    return True


def valid_title(title):
    title = clean_text(title)

    if not title:
        return False

    if len(title) > 300:
        return False

    bad = {
        "CANLI TV",
        "KANALLAR",
        "PROGRAMLAR",
        "ANA SAYFA",
        "FAVORİ KANALLARIM",
        "TİVİBU NEDİR?",
        "TRT1 CANLI İZLE",
        "TRT 1 CANLI İZLE",
        "GİRİŞ YAP",
        "ÜYE OL",
    }

    if title.upper() in bad:
        return False

    return True


def xml_datetime(dt):
    return (
        dt.strftime("%Y%m%d%H%M%S")
        + " +0300"
    )


# ==============================================================
# HTTP
# ==============================================================

def fetch_page(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=45
    ) as response:

        data = response.read()

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        return data.decode(
            charset,
            errors="replace"
        )


# ==============================================================
# HTML LINK PARSER
# ==============================================================

class LinkParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.links = []

        self.in_anchor = False
        self.current_href = None
        self.current_text = []

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        if tag.lower() != "a":
            return

        attrs = dict(
            attrs
        )

        self.in_anchor = True

        self.current_href = (
            attrs.get("href")
        )

        self.current_text = []

    def handle_data(self, data):

        if self.in_anchor:

            self.current_text.append(
                data
            )

    def handle_endtag(self, tag):

        if tag.lower() != "a":
            return

        if self.in_anchor:

            text = clean_text(
                " ".join(
                    self.current_text
                )
            )

            self.links.append({
                "href": self.current_href,
                "text": text
            })

        self.in_anchor = False
        self.current_href = None
        self.current_text = []


def parse_links(source):

    parser = LinkParser()

    parser.feed(
        source
    )

    return parser.links


# ==============================================================
# KANALLARI ÇIKAR
# ==============================================================

def collect_channels(source):

    links = parse_links(
        source
    )

    channels = OrderedDict()

    for link in links:

        href = link["href"]
        text = clean_text(
            link["text"]
        )

        if not href or not text:
            continue

        if not href.startswith(
            "/kanallar/"
        ):
            continue

        # Program linkleri de /kanallar/ kullanıyor.
        # Saat oku varsa programdır.
        if "→" in text:
            continue

        if re.search(
            r"\d{1,2}:\d{2}",
            text
        ):
            continue

        if href.startswith("/"):
            href = BASE_URL + href

        href = (
            href
            .split("?", 1)[0]
            .split("#", 1)[0]
            .rstrip("/")
        )

        if not re.search(
            r"/kanallar/[^/?#]+$",
            href
        ):
            continue

        if not valid_channel(text):
            continue

        key = normalize_key(
            text
        )

        if key in channels:
            continue

        channels[key] = {
            "name": text,
            "url": href,
            "id": make_xml_id(text),
        }

    return channels


# ==============================================================
# PROGRAM LİNKLERİNİ BUL
# ==============================================================

def collect_program_links(source):

    links = parse_links(
        source
    )

    programs = []

    for link in links:

        href = link["href"]
        text = clean_text(
            link["text"]
        )

        if not href or not text:
            continue

        if not href.startswith(
            "/kanallar/"
        ):
            continue

        if "→" not in text:
            continue

        if not re.search(
            r"\d{1,2}:\d{2}\s*→\s*\d{1,2}:\d{2}",
            text
        ):
            continue

        if href.startswith("/"):
            href = BASE_URL + href

        href = (
            href
            .split("?", 1)[0]
            .split("#", 1)[0]
            .rstrip("/")
            .lower()
        )

        programs.append({
            "url": href,
            "text": text,
        })

    return programs


# ==============================================================
# PROGRAM PARSE
# ==============================================================

PROGRAM_RE = re.compile(
    r"^(?P<title>.*?)"
    r"\s+"
    r"(?P<category>"
    r"Film|"
    r"Dizi|"
    r"Yaşam|"
    r"Spor Programı|"
    r"Spor|"
    r"Haber|"
    r"Belgesel|"
    r"Çocuk|"
    r"Müzik|"
    r"Eğlence|"
    r"Aktüalite|"
    r"Diğer"
    r")"
    r"\s*-\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})"
    r"\s*→\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
    r"(?:\s+Canlı)?$",
    re.IGNORECASE
)


def parse_program(text):

    text = clean_text(
        text
    )

    match = PROGRAM_RE.match(
        text
    )

    if not match:
        return None

    title = clean_text(
        match.group("title")
    )

    if not valid_title(title):
        return None

    sh = int(
        match.group("sh")
    )

    sm = int(
        match.group("sm")
    )

    eh = int(
        match.group("eh")
    )

    em = int(
        match.group("em")
    )

    return {
        "title": title,
        "start_hour": sh,
        "start_minute": sm,
        "end_hour": eh,
        "end_minute": em,
    }


# ==============================================================
# BUGÜNÜN PROGRAMINA DÖNÜŞTÜR
# ==============================================================

def convert_program(
    channel,
    parsed
):

    start = datetime(
        TODAY.year,
        TODAY.month,
        TODAY.day,
        parsed["start_hour"],
        parsed["start_minute"],
        tzinfo=TURKEY_TZ
    )

    end = datetime(
        TODAY.year,
        TODAY.month,
        TODAY.day,
        parsed["end_hour"],
        parsed["end_minute"],
        tzinfo=TURKEY_TZ
    )

    # Örneğin:
    # 23:45 -> 01:30
    # Bitiş ertesi gündür.
    if end <= start:
        end += timedelta(
            days=1
        )

    return {
        "channel": channel["name"],
        "title": parsed["title"],
        "start": start,
        "end": end,
    }


# ==============================================================
# PROGRAMLARI KANALA EŞLEŞTİR
# ==============================================================

def build_programs(
    source,
    channels
):

    channel_url_map = {}

    for channel in channels.values():

        channel_url_map[
            channel["url"]
            .lower()
            .rstrip("/")
        ] = channel

    links = collect_program_links(
        source
    )

    print(
        f"Toplam program bağlantısı: "
        f"{len(links)}"
    )

    programs = []

    seen = set()

    for link in links:

        channel = channel_url_map.get(
            link["url"]
        )

        if channel is None:
            continue

        parsed = parse_program(
            link["text"]
        )

        if parsed is None:
            continue

        program = convert_program(
            channel,
            parsed
        )

        # ------------------------------------------------------
        # BUGÜN BAŞLAYAN PROGRAMLAR
        #
        # 05.09 00:xx'e taşan kayıtlar bugünün XML'ine
        # dahil edilmeyecek.
        #
        # Ancak 04.09 23:xx'te başlayan programın stop'u
        # 05.09 olabilir.
        # ------------------------------------------------------

        if program["start"].date() != TODAY:
            continue

        key = (
            normalize_key(
                program["channel"]
            ),
            program["start"],
            normalize_key(
                program["title"]
            )
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        programs.append(
            program
        )

    # ----------------------------------------------------------
    # KANAL + SAAT SIRASI
    # ----------------------------------------------------------

    programs.sort(
        key=lambda x: (
            normalize_key(
                x["channel"]
            ),
            x["start"]
        )
    )

    return programs


# ==============================================================
# KANAL İÇİN ÇAKIŞMA / BİTİŞ DÜZELTME
# ==============================================================

def fix_program_times(programs):

    grouped = defaultdict(list)

    for program in programs:

        grouped[
            normalize_key(
                program["channel"]
            )
        ].append(
            program
        )

    result = []

    for _, items in grouped.items():

        items.sort(
            key=lambda x: x["start"]
        )

        for index, program in enumerate(
            items
        ):

            start = program["start"]
            end = program["end"]

            if index + 1 < len(items):

                next_start = items[
                    index + 1
                ]["start"]

                # Bir sonraki program mevcut programdan önce
                # başlıyorsa veriyi bozma.
                if next_start > start:

                    if next_start < end:
                        end = next_start

            if end <= start:

                end = (
                    start
                    + timedelta(
                        minutes=30
                    )
                )

            # Bir günlük EPG'de saçma uzunlukları önle.
            if end - start > timedelta(
                hours=12
            ):

                end = (
                    start
                    + timedelta(
                        hours=3
                    )
                )

            result.append({
                "channel": program["channel"],
                "title": program["title"],
                "start": start,
                "end": end,
            })

    result.sort(
        key=lambda x: (
            x["start"],
            normalize_key(
                x["channel"]
            )
        )
    )

    return result


# ==============================================================
# XML
# ==============================================================

def write_xml(
    channels,
    programs
):

    used_channels = {
        normalize_key(
            program["channel"]
        )
        for program in programs
    }

    final_channels = []

    for key, channel in channels.items():

        if key not in used_channels:
            continue

        if channel["name"].upper() == "BENİM KANALIM":
            continue

        if (
            channel["name"].upper()
            == "TİVİBU SPOR CANLI İZLE"
        ):
            continue

        final_channels.append(
            channel
        )

    tv = Element(
        "tv",
        {
            "generator-info-name":
                "Tivibu Günlük EPG",
            "generator-info-url":
                "https://www.tivibu.com.tr/",
        }
    )

    # ----------------------------------------------------------
    # KANALLAR
    # ----------------------------------------------------------

    for channel in final_channels:

        channel_element = SubElement(
            tv,
            "channel",
            {
                "id": channel["id"]
            }
        )

        display_name = SubElement(
            channel_element,
            "display-name",
            {
                "lang": "tr"
            }
        )

        display_name.text = channel["name"]

    channel_ids = {
        normalize_key(
            channel["name"]
        ): channel["id"]
        for channel in final_channels
    }

    # ----------------------------------------------------------
    # PROGRAMLAR
    # ----------------------------------------------------------

    for program in programs:

        channel_id = channel_ids.get(
            normalize_key(
                program["channel"]
            )
        )

        if not channel_id:
            continue

        programme = SubElement(
            tv,
            "programme",
            {
                "channel": channel_id,
                "start": xml_datetime(
                    program["start"]
                ),
                "stop": xml_datetime(
                    program["end"]
                ),
            }
        )

        title = SubElement(
            programme,
            "title",
            {
                "lang": "tr"
            }
        )

        title.text = program["title"]

    tree = ElementTree(
        tv
    )

    try:

        import xml.etree.ElementTree as ET

        ET.indent(
            tree,
            space="  "
        )

    except Exception:
        pass

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    return len(final_channels)


# ==============================================================
# ANA
# ==============================================================

def main():

    print("=" * 70)
    print("TIVIBU GÜNLÜK EPG")
    print("=" * 70)

    print(
        f"Tarih: "
        f"{TODAY.strftime('%d.%m.%Y')}"
    )

    print(
        f"Saat: "
        f"{NOW.strftime('%H:%M:%S')}"
    )

    print("=" * 70)

    # ----------------------------------------------------------
    # ANA SAYFAYI TEK SEFER ÇEK
    # ----------------------------------------------------------

    print(
        "Tivibu canlı TV sayfası alınıyor..."
    )

    try:

        source = fetch_page(
            LIVE_URL
        )

    except Exception as e:

        print(
            f"Sayfa alınamadı: {e}"
        )

        return

    # ----------------------------------------------------------
    # KANALLAR
    # ----------------------------------------------------------

    channels = collect_channels(
        source
    )

    print()
    print(
        f"Bulunan gerçek kanal: "
        f"{len(channels)}"
    )

    # ----------------------------------------------------------
    # PROGRAMLAR
    # ----------------------------------------------------------

    programs = build_programs(
        source,
        channels
    )

    print()
    print(
        f"Bugünün ham programı: "
        f"{len(programs)}"
    )

    # ----------------------------------------------------------
    # PROGRAM SAATLERİ
    # ----------------------------------------------------------

    programs = fix_program_times(
        programs
    )

    # ----------------------------------------------------------
    # KANAL SAYILARI
    # ----------------------------------------------------------

    counts = defaultdict(
        int
    )

    for program in programs:

        counts[
            program["channel"]
        ] += 1

    print()
    print("=" * 70)
    print("KANAL PROGRAM KONTROLÜ")
    print("=" * 70)

    for channel_name in sorted(
        counts
    ):

        print(
            f"{channel_name}: "
            f"{counts[channel_name]}"
        )

    # ----------------------------------------------------------
    # ÖRNEKLER
    # ----------------------------------------------------------

    print()
    print("=" * 70)
    print("ÖRNEK PROGRAMLAR")
    print("=" * 70)

    for test_channel in [
        "KANAL D",
        "ATV",
        "TRT 1",
        "TRT SPOR",
        "TRT 3 SPOR",
        "CNN TÜRK",
        "TİVİBU SPOR",
        "SİNEMA TV",
        "SİNEMA 2",
    ]:

        test_key = normalize_key(
            test_channel
        )

        matches = [
            p
            for p in programs
            if normalize_key(
                p["channel"]
            ) == test_key
        ]

        print(
            f"{test_channel}: "
            f"{len(matches)} program"
        )

        for program in matches[:8]:

            print(
                "    "
                f"{program['start'].strftime('%H:%M')}"
                f" - "
                f"{program['end'].strftime('%H:%M')}"
                f" | "
                f"{program['title']}"
            )

    # ----------------------------------------------------------
    # XML
    # ----------------------------------------------------------

    xml_channels = write_xml(
        channels,
        programs
    )

    # ----------------------------------------------------------
    # SONUÇ
    # ----------------------------------------------------------

    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)

    print(
        f"Keşfedilen kanal: "
        f"{len(channels)}"
    )

    print(
        f"EPG kanal: "
        f"{xml_channels}"
    )

    print(
        f"Toplam program: "
        f"{len(programs)}"
    )

    print(
        f"Tarih: "
        f"{TODAY.strftime('%d.%m.%Y')}"
    )

    print(
        f"{OUTPUT_FILE} oluşturuldu."
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
