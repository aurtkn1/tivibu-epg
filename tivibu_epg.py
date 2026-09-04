import re
import html
import time
import unicodedata
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")

MAX_WORKERS = 12
REQUEST_TIMEOUT = 35
RETRY_COUNT = 3

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
    "TİVİBU TANITIM SAYFASI",
    "TİVİBU CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU NEDİR?",
    "TİVİBU SPOR CANLI İZLE",
    "TRT1 CANLI İZLE",
}

BAD_CHANNEL_PARTS = {
    "FAVORİ KANALLARIM",
    "CANLI TV, KANAL VE PROGRAMLAR",
    "TİVİBU SPOR CANLI İZLE",
    "TRT1 CANLI İZLE",
}

TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})"
)


class ChannelLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.links = []

        self.in_a = False
        self.current_href = ""
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag != "a":
            return

        attrs = dict(attrs)

        self.in_a = True
        self.current_href = attrs.get("href", "")
        self.current_text = []

    def handle_data(self, data):
        if self.in_a:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a":
            return

        if not self.in_a:
            return

        text = clean_text(
            " ".join(self.current_text)
        )

        href = clean_text(
            self.current_href
        )

        if href and text:
            self.links.append(
                {
                    "href": href,
                    "text": text,
                }
            )

        self.in_a = False
        self.current_href = ""
        self.current_text = []


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "br",
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "td",
        "th",
    }

    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "svg",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.lines = []
        self.current = []

        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self.flush()

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return

        if self.skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self.flush()

    def handle_data(self, data):
        if self.skip_depth:
            return

        data = data.replace(
            "\xa0",
            " "
        )

        if data.strip():
            self.current.append(data)

    def flush(self):
        text = clean_text(
            " ".join(self.current)
        )

        if text:
            self.lines.append(text)

        self.current = []

    def get_lines(self):
        self.flush()
        return self.lines


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
    return clean_text(value).upper()


def is_bad_channel(name):
    normalized = normalize_name(name)

    if normalized in BAD_CHANNELS:
        return True

    for part in BAD_CHANNEL_PARTS:
        if part in normalized:
            return True

    return False


def slugify(value):
    value = clean_text(value)

    replacements = {
        "ç": "c",
        "Ç": "c",
        "ğ": "g",
        "Ğ": "g",
        "ı": "i",
        "İ": "i",
        "ö": "o",
        "Ö": "o",
        "ş": "s",
        "Ş": "s",
        "ü": "u",
        "Ü": "u",
        "â": "a",
        "Â": "a",
        "î": "i",
        "Î": "i",
        "û": "u",
        "Û": "u",
        "’": "",
        "'": "",
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


def unique_id(name, used_ids):
    base = slugify(name)

    result = base
    counter = 2

    while result in used_ids:
        result = f"{base}_{counter}"
        counter += 1

    used_ids.add(result)

    return result


def http_get(url):
    last_error = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Cache-Control": "no-cache",
                },
            )

            with urlopen(
                request,
                timeout=REQUEST_TIMEOUT
            ) as response:

                raw = response.read()

                encoding = (
                    response.headers.get_content_charset()
                    or "utf-8"
                )

                return raw.decode(
                    encoding,
                    errors="replace"
                )

        except Exception as exc:
            last_error = exc

            if attempt < RETRY_COUNT:
                time.sleep(
                    attempt
                )

    raise last_error


def get_main_page():
    print(
        "Tivibu canlı TV kanalları alınıyor..."
    )

    raw_html = http_get(
        LIVE_TV_URL
    )

    parser = ChannelLinkParser()

    parser.feed(
        raw_html
    )

    channels = []
    seen_urls = set()

    for link in parser.links:
        href = clean_text(
            link["href"]
        )

        name = clean_text(
            link["text"]
        )

        if not href or not name:
            continue

        if "/kanallar/" not in href:
            continue

        if is_bad_channel(name):
            continue

        full_url = urljoin(
            BASE_URL,
            href
        )

        full_url = full_url.split(
            "#"
        )[0]

        if full_url in seen_urls:
            continue

        seen_urls.add(
            full_url
        )

        channels.append(
            {
                "name": name,
                "url": full_url,
            }
        )

    return channels


def extract_program_section(lines):
    start_index = None

    for i, line in enumerate(lines):
        normalized = normalize_name(
            line
        )

        if normalized == "GÜNÜN PROGRAMLARI":
            start_index = i + 1
            break

        if "GÜNÜN PROGRAMLARI" in normalized:
            start_index = i + 1
            break

    if start_index is None:
        return []

    section = []

    stop_words = (
        "YENİ VE POPÜLER",
        "ÇOK İZLENEN",
        "TÜMÜNÜ GÖR",
        "TİVİBU PAKETLER",
        "YASAL METİNLER",
        "ÖNE ÇIKANLAR",
        "KANAL D CANLI YAYINI",
        "TİVİBU İLE",
    )

    for line in lines[start_index:]:
        normalized = normalize_name(
            line
        )

        if any(
            word in normalized
            for word in stop_words
        ):
            break

        section.append(
            line
        )

    return section


def parse_schedule_from_lines(lines):
    programs = []

    i = 0

    while i < len(lines):
        line = clean_text(
            lines[i]
        )

        if not line:
            i += 1
            continue

        match = TIME_RANGE_RE.search(
            line
        )

        if match:
            title = clean_text(
                line[
                    :match.start()
                ]
            )

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

            if (
                title
                and sh <= 23
                and eh <= 23
                and sm <= 59
                and em <= 59
            ):
                programs.append(
                    {
                        "title": title,
                        "start_hour": sh,
                        "start_minute": sm,
                        "end_hour": eh,
                        "end_minute": em,
                    }
                )

            i += 1
            continue

        if i + 1 < len(lines):
            next_line = clean_text(
                lines[i + 1]
            )

            match = TIME_RANGE_RE.fullmatch(
                next_line
            )

            if match:
                title = line

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

                if (
                    title
                    and sh <= 23
                    and eh <= 23
                    and sm <= 59
                    and em <= 59
                ):
                    programs.append(
                        {
                            "title": title,
                            "start_hour": sh,
                            "start_minute": sm,
                            "end_hour": eh,
                            "end_minute": em,
                        }
                    )

                    i += 2
                    continue

        i += 1

    return programs


def fetch_channel(channel):
    try:
        raw_html = http_get(
            channel["url"]
        )

        parser = TextExtractor()

        parser.feed(
            raw_html
        )

        lines = parser.get_lines()

        schedule_lines = extract_program_section(
            lines
        )

        programs = parse_schedule_from_lines(
            schedule_lines
        )

        return {
            "channel": channel,
            "programs": programs,
            "error": None,
        }

    except Exception as exc:
        return {
            "channel": channel,
            "programs": [],
            "error": str(exc),
        }


def assign_program_dates(
    programs,
    today,
    now_local
):
    if not programs:
        return []

    first_start = (
        programs[0]["start_hour"] * 60
        + programs[0]["start_minute"]
    )

    now_minutes = (
        now_local.hour * 60
        + now_local.minute
    )

    if first_start > now_minutes:
        current_date = (
            today
            - timedelta(days=1)
        )
    else:
        current_date = today

    previous_start = None

    result = []

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


def clean_programs(
    programs,
    today
):
    result = []
    seen = set()

    for program in programs:
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

        title = clean_text(
            program["title"]
        )

        if not title:
            continue

        # Sitedeki bazı gereksiz metinleri ele.
        if title in {
            "Canlı Yayın",
            "Program Akışı",
            "Günün Programları",
        }:
            continue

        key = (
            title.lower(),
            program["start"],
            program["stop"],
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            {
                "title": title,
                "start": program["start"],
                "stop": program["stop"],
            }
        )

    result.sort(
        key=lambda x: (
            x["start"],
            x["stop"],
        )
    )

    return result


def build_epg(
    results,
    today,
    now_local
):
    used_ids = set()

    channels = []
    programs = []

    for result in results:
        channel = result["channel"]
        raw_programs = result["programs"]

        if not raw_programs:
            continue

        dated_programs = assign_program_dates(
            raw_programs,
            today,
            now_local
        )

        todays_programs = clean_programs(
            dated_programs,
            today
        )

        if not todays_programs:
            continue

        channel_name = clean_text(
            channel["name"]
        )

        if is_bad_channel(
            channel_name
        ):
            continue

        channel_id = unique_id(
            channel_name,
            used_ids
        )

        channels.append(
            {
                "id": channel_id,
                "name": channel_name,
            }
        )

        for program in todays_programs:
            programs.append(
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "title": program["title"],
                    "start": program["start"],
                    "stop": program["stop"],
                }
            )

    return channels, programs


def xml_escape(value):
    return html.escape(
        str(value),
        quote=False
    )


def xml_datetime(value):
    # Türkiye sabit +0300
    return value.strftime(
        "%Y%m%d%H%M%S +0300"
    )


def write_xml(
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


def validate_xml(
    channels,
    programs
):
    if not channels:
        raise RuntimeError(
            "Hiç kanal bulunamadı."
        )

    if not programs:
        raise RuntimeError(
            "Hiç program bulunamadı. "
            "EPG boş bırakılmadı."
        )

    channel_ids = {
        channel["id"]
        for channel in channels
    }

    for program in programs:
        if program["channel_id"] not in channel_ids:
            raise RuntimeError(
                "Geçersiz programme/channel eşleşmesi bulundu."
            )


def main():
    started = time.time()

    now_local = datetime.now(
        ISTANBUL
    )

    today = now_local.date()

    print()
    print("=" * 70)
    print("TIVIBU GÜNLÜK EPG")
    print("=" * 70)
    print(
        f"Tarih: {today.strftime('%d.%m.%Y')}"
    )
    print(
        f"Saat:  {now_local.strftime('%H:%M:%S')}"
    )
    print("=" * 70)

    channels = get_main_page()

    print(
        f"Bulunan gerçek kanal: {len(channels)}"
    )

    if not channels:
        raise RuntimeError(
            "Tivibu canlı TV sayfasından kanal bulunamadı."
        )

    print()
    print(
        f"{len(channels)} kanal paralel taranıyor..."
    )
    print(
        f"Paralel bağlantı sayısı: {MAX_WORKERS}"
    )
    print()

    results = []

    success_count = 0
    failed_count = 0
    program_count = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                fetch_channel,
                channel
            ): channel
            for channel in channels
        }

        completed = 0

        for future in as_completed(
            futures
        ):
            completed += 1

            result = future.result()

            channel = result["channel"]
            programs = result["programs"]
            error = result["error"]

            results.append(
                result
            )

            if error:
                failed_count += 1

                print(
                    f"[{completed}/{len(channels)}] "
                    f"{channel['name']}: HATA"
                )

            else:
                if programs:
                    success_count += 1
                    program_count += len(
                        programs
                    )

                    print(
                        f"[{completed}/{len(channels)}] "
                        f"{channel['name']}: "
                        f"{len(programs)} program"
                    )

                else:
                    print(
                        f"[{completed}/{len(channels)}] "
                        f"{channel['name']}: 0 program"
                    )

    print()
    print("=" * 70)
    print("TARAMA SONUCU")
    print("=" * 70)
    print(
        f"Program bulunan kanal : {success_count}"
    )
    print(
        f"Başarısız kanal       : {failed_count}"
    )
    print(
        f"Ham program sayısı    : {program_count}"
    )
    print("=" * 70)

    xml_channels, xml_programs = build_epg(
        results,
        today,
        now_local
    )

    print()
    print(
        f"Bugünün XML programı: {len(xml_programs)}"
    )
    print(
        f"XML kanal sayısı: {len(xml_channels)}"
    )

    validate_xml(
        xml_channels,
        xml_programs
    )

    write_xml(
        xml_channels,
        xml_programs
    )

    elapsed = time.time() - started

    print()
    print("=" * 70)
    print("EPG BAŞARIYLA OLUŞTURULDU")
    print("=" * 70)
    print(
        f"Dosya   : {OUTPUT_FILE}"
    )
    print(
        f"Kanal   : {len(xml_channels)}"
    )
    print(
        f"Program : {len(xml_programs)}"
    )
    print(
        f"Süre    : {elapsed:.1f} saniye"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
