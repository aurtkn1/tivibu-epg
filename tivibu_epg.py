import re
import html
import unicodedata
from datetime import datetime, timedelta
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")


BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
    "TİVİBU SPOR CANLI İZLE",
    "TRT1 CANLI İZLE",
}


TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*→\s*(\d{1,2}):(\d{2})"
)


class TivibuHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.anchors = []

        self.current_href = None
        self.current_text = []
        self.anchor_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        attrs_dict = dict(attrs)

        self.current_href = attrs_dict.get(
            "href",
            ""
        )

        self.current_text = []
        self.anchor_depth = 1

    def handle_startendtag(self, tag, attrs):
        if tag.lower() != "a":
            return

        attrs_dict = dict(attrs)

        self.anchors.append(
            {
                "href": attrs_dict.get(
                    "href",
                    ""
                ),
                "text": "",
            }
        )

    def handle_data(self, data):
        if self.anchor_depth > 0:
            self.current_text.append(data)

    def handle_entityref(self, name):
        if self.anchor_depth > 0:
            self.current_text.append(
                html.unescape(
                    f"&{name};"
                )
            )

    def handle_charref(self, name):
        if self.anchor_depth > 0:
            self.current_text.append(
                html.unescape(
                    f"&#{name};"
                )
            )

    def handle_endtag(self, tag):
        if tag.lower() != "a":
            return

        if self.anchor_depth <= 0:
            return

        text = " ".join(
            self.current_text
        )

        text = clean_text(text)

        self.anchors.append(
            {
                "href": self.current_href or "",
                "text": text,
            }
        )

        self.current_href = None
        self.current_text = []
        self.anchor_depth = 0


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(
        str(value)
    )

    value = value.replace(
        "\xa0",
        " "
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def normalize_name(value):
    return clean_text(
        value
    ).upper()


def is_bad_channel(name):
    normalized = normalize_name(
        name
    )

    if normalized in BAD_CHANNELS:
        return True

    if "FAVORİ KANALLARIM" in normalized:
        return True

    if "CANLI TV, KANAL VE PROGRAMLAR" in normalized:
        return True

    if "TİVİBU SPOR CANLI İZLE" in normalized:
        return True

    if "TRT1 CANLI İZLE" in normalized:
        return True

    return False


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c",
        "Ç": "C",
        "ğ": "g",
        "Ğ": "G",
        "ı": "i",
        "İ": "I",
        "ö": "o",
        "Ö": "O",
        "ş": "s",
        "Ş": "S",
        "ü": "u",
        "Ü": "U",
        "â": "a",
        "Â": "A",
        "î": "i",
        "Î": "I",
        "û": "u",
        "Û": "U",
    }

    for old, new in replacements.items():
        value = value.replace(
            old,
            new
        )

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        c
        for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = value.replace(
        "&",
        " ve "
    )

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value
    )

    value = re.sub(
        r"_+",
        "_",
        value
    )

    value = value.strip("_")

    if not value:
        value = "channel"

    return value


def unique_id(name, used):
    base = slugify(name)

    channel_id = base
    number = 2

    while channel_id in used:
        channel_id = (
            f"{base}_{number}"
        )
        number += 1

    used.add(channel_id)

    return channel_id


def extract_channel_key(href):
    href = clean_text(href)

    if not href:
        return ""

    parsed = urlparse(
        href
    )

    query = parse_qs(
        parsed.query
    )

    for key in (
        "i",
        "channel",
        "channelId",
        "channelid",
    ):
        values = query.get(key)

        if not values:
            continue

        value = values[0]

        match = re.search(
            r"(ch[a-zA-Z0-9_-]+)",
            value
        )

        if match:
            return match.group(1)

    match = re.search(
        r"(ch[a-zA-Z0-9_-]{6,})",
        href
    )

    if match:
        return match.group(1)

    return href


def parse_program_text(text):
    text = clean_text(text)

    if not text:
        return None

    text = re.sub(
        r"\s+Canlı\s*$",
        "",
        text,
        flags=re.IGNORECASE
    )

    match = TIME_RE.search(
        text
    )

    if not match:
        return None

    sh = int(
        match.group(1)
    )
    sm = int(
        match.group(2)
    )
    eh = int(
        match.group(3)
    )
    em = int(
        match.group(4)
    )

    if sh > 23 or eh > 23:
        return None

    if sm > 59 or em > 59:
        return None

    title = text[
        :match.start()
    ]

    title = clean_text(
        title
    )

    title = re.sub(
        r"\s*-\s*$",
        "",
        title
    )

    title = clean_text(
        title
    )

    if not title:
        return None

    return {
        "title": title,
        "start_hour": sh,
        "start_minute": sm,
        "end_hour": eh,
        "end_minute": em,
    }


def fetch_html():
    print(
        "Tivibu canlı TV sayfası alınıyor..."
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        page = browser.new_page(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=120000,
        )

        page.wait_for_timeout(
            7000
        )

        html_text = page.content()

        browser.close()

    return html_text


def collect_data(raw_html):
    parser = TivibuHTMLParser()

    parser.feed(
        raw_html
    )

    channels = []
    channel_seen = set()

    groups = OrderedDict()

    # ---------------------------------------------------------
    # GERÇEK KANALLAR
    # ---------------------------------------------------------

    for anchor in parser.anchors:
        href = clean_text(
            anchor["href"]
        )

        text = clean_text(
            anchor["text"]
        )

        if not href or not text:
            continue

        if "/kanallar/" not in href:
            continue

        if is_bad_channel(text):
            continue

        if href.startswith("/"):
            full_href = (
                BASE_URL + href
            )
        else:
            full_href = href

        full_href = full_href.split(
            "#"
        )[0]

        if full_href in channel_seen:
            continue

        channel_seen.add(
            full_href
        )

        channels.append(
            {
                "name": text,
                "url": full_href,
            }
        )

    # ---------------------------------------------------------
    # PROGRAMLAR
    # ---------------------------------------------------------

    for anchor in parser.anchors:
        href = clean_text(
            anchor["href"]
        )

        text = clean_text(
            anchor["text"]
        )

        if not href:
            continue

        if "/rv?" not in href:
            continue

        if not TIME_RE.search(
            text
        ):
            continue

        program = parse_program_text(
            text
        )

        if not program:
            continue

        if href.startswith("/"):
            full_href = (
                BASE_URL + href
            )
        else:
            full_href = href

        group_key = extract_channel_key(
            full_href
        )

        if not group_key:
            continue

        if group_key not in groups:
            groups[group_key] = []

        groups[group_key].append(
            program
        )

    return channels, groups


def assign_dates(programs, base_date):
    if not programs:
        return []

    result = []

    current_date = base_date

    previous_start = None

    for program in programs:
        start_minutes = (
            program["start_hour"] * 60
            + program["start_minute"]
        )

        if (
            previous_start is not None
            and start_minutes < previous_start
        ):
            current_date = (
                current_date
                + timedelta(days=1)
            )

        start_dt = datetime(
            current_date.year,
            current_date.month,
            current_date.day,
            program["start_hour"],
            program["start_minute"],
            tzinfo=ISTANBUL,
        )

        end_minutes = (
            program["end_hour"] * 60
            + program["end_minute"]
        )

        stop_date = current_date

        if end_minutes <= start_minutes:
            stop_date = (
                stop_date
                + timedelta(days=1)
            )

        stop_dt = datetime(
            stop_date.year,
            stop_date.month,
            stop_date.day,
            program["end_hour"],
            program["end_minute"],
            tzinfo=ISTANBUL,
        )

        result.append(
            {
                "title": program["title"],
                "start": start_dt,
                "stop": stop_dt,
            }
        )

        previous_start = start_minutes

    return result


def remove_duplicate_programs(programs):
    result = []

    seen = set()

    for program in sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["stop"],
            x["title"],
        ),
    ):
        key = (
            program["start"],
            program["stop"],
            normalize_name(
                program["title"]
            ),
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            program
        )

    return result


def build_epg(
    channels,
    groups,
    today,
):
    used_ids = set()

    output_channels = []
    output_programs = []

    group_keys = list(
        groups.keys()
    )

    print()
    print(
        f"Toplam program bağlantı grubu: {len(group_keys)}"
    )

    # Tivibu canlı TV sayfasındaki kanal sırası,
    # program gruplarının sırasıyla eşleştirilir.
    #
    # Program grubu olmayan sahte kanallar yukarıda
    # zaten filtrelenmiştir.

    limit = min(
        len(channels),
        len(group_keys)
    )

    for index in range(limit):
        channel = channels[index]
        group_key = group_keys[index]

        channel_name = clean_text(
            channel["name"]
        )

        raw_programs = groups[
            group_key
        ]

        dated_programs = assign_dates(
            raw_programs,
            today,
        )

        todays = []

        for program in dated_programs:
            if program["start"].date() != today:
                continue

            if program["stop"] <= program["start"]:
                continue

            duration = (
                program["stop"]
                - program["start"]
            ).total_seconds()

            if duration <= 0:
                continue

            if duration > 24 * 60 * 60:
                continue

            todays.append(
                program
            )

        todays = remove_duplicate_programs(
            todays
        )

        if not todays:
            continue

        channel_id = unique_id(
            channel_name,
            used_ids
        )

        output_channels.append(
            {
                "id": channel_id,
                "name": channel_name,
            }
        )

        for program in todays:
            output_programs.append(
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "title": program["title"],
                    "start": program["start"],
                    "stop": program["stop"],
                }
            )

        print(
            f"[{index + 1}/{limit}] "
            f"{channel_name}: "
            f"{len(todays)} program"
        )

    return (
        output_channels,
        output_programs
    )


def xml_escape(value):
    return html.escape(
        str(value),
        quote=False
    )


def xml_datetime(value):
    return value.strftime(
        "%Y%m%d%H%M%S +0300"
    )


def write_epg(
    channels,
    programs
):
    lines = []

    lines.append(
        '<?xml version="1.0" encoding="UTF-8"?>'
    )

    lines.append(
        '<tv generator-info-name="Tivibu Günlük EPG" '
        'generator-info-url="https://www.tivibu.com.tr/canli-tv">'
    )

    for channel in channels:
        channel_id = html.escape(
            channel["id"],
            quote=True
        )

        name = xml_escape(
            channel["name"]
        )

        lines.append(
            f'  <channel id="{channel_id}">'
        )

        lines.append(
            f'    <display-name lang="tr">{name}</display-name>'
        )

        lines.append(
            "  </channel>"
        )

    programs = sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_name"],
            x["stop"],
        )
    )

    for program in programs:
        channel_id = html.escape(
            program["channel_id"],
            quote=True
        )

        start = xml_datetime(
            program["start"]
        )

        stop = xml_datetime(
            program["stop"]
        )

        title = xml_escape(
            program["title"]
        )

        lines.append(
            f'  <programme '
            f'channel="{channel_id}" '
            f'start="{start}" '
            f'stop="{stop}">'
        )

        lines.append(
            f'    <title lang="tr">{title}</title>'
        )

        lines.append(
            "  </programme>"
        )

    lines.append(
        "</tv>"
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(
            "\n".join(lines)
            + "\n"
        )


def validate_epg(
    channels,
    programs
):
    if not channels:
        raise RuntimeError(
            "Hiç kanal oluşturulmadı."
        )

    if not programs:
        raise RuntimeError(
            "Hiç program oluşturulmadı. "
            "EPG boş dosya olarak kaydedilmeyecek."
        )

    channel_ids = {
        channel["id"]
        for channel in channels
    }

    bad_references = []

    for program in programs:
        if program["channel_id"] not in channel_ids:
            bad_references.append(
                program
            )

    if bad_references:
        raise RuntimeError(
            "Geçersiz programme/channel eşleşmesi bulundu."
        )


def main():
    now = datetime.now(
        ISTANBUL
    )

    today = now.date()

    print()
    print("=" * 70)
    print("TIVIBU GÜNLÜK EPG")
    print("=" * 70)
    print(
        f"Tarih: {today.strftime('%d.%m.%Y')}"
    )
    print(
        f"Saat:  {now.strftime('%H:%M:%S')}"
    )
    print("=" * 70)

    raw_html = fetch_html()

    channels, groups = collect_data(
        raw_html
    )

    print(
        f"Bulunan gerçek kanal: {len(channels)}"
    )

    total_programs = sum(
        len(value)
        for value in groups.values()
    )

    print(
        f"Toplam program bağlantısı: {total_programs}"
    )

    if not channels:
        raise RuntimeError(
            "Tivibu sayfasından kanal bulunamadı."
        )

    if not groups:
        raise RuntimeError(
            "Tivibu sayfasından /rv? program bağlantıları bulunamadı."
        )

    xml_channels, xml_programs = build_epg(
        channels,
        groups,
        today
    )

    print()
    print(
        f"Bugünün programı: {len(xml_programs)}"
    )

    print(
        f"XML kanal sayısı: {len(xml_channels)}"
    )

    validate_epg(
        xml_channels,
        xml_programs
    )

    write_epg(
        xml_channels,
        xml_programs
    )

    print()
    print("=" * 70)
    print("EPG BAŞARIYLA OLUŞTURULDU")
    print("=" * 70)
    print(
        f"Dosya: {OUTPUT_FILE}"
    )
    print(
        f"Kanal: {len(xml_channels)}"
    )
    print(
        f"Program: {len(xml_programs)}"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
