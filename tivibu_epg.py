import re
import html
import unicodedata
from datetime import datetime, date, timedelta
from collections import OrderedDict
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright


BASE_URL = "https://www.tivibu.com.tr"
LIVE_TV_URL = f"{BASE_URL}/canli-tv"
OUTPUT_FILE = "epg.xml"

ISTANBUL = ZoneInfo("Europe/Istanbul")

BAD_CHANNELS = {
    "BENİM KANALIM",
    "FAVORİ KANALLARIM",
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

SCHEDULE_RE = re.compile(
    r"^(.*?)\s+(?:Film|Dizi|Çocuk|Spor|Haber|Aktüalite|Yaşam|Eğlence|Belgesel|Müzik|Diğer|Global|Sinema|Lifestyle|Genel)?\s*-\s*"
    r"(\d{1,2}):(\d{2})\s*→\s*(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)

SCHEDULE_RE_FALLBACK = re.compile(
    r"^(.*?)\s+-\s*(\d{1,2}):(\d{2})\s*→\s*(\d{1,2}):(\d{2})"
)

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*→\s*(\d{1,2}):(\d{2})"
)


def clean_text(value):
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_name(value):
    value = clean_text(value).upper()
    return value


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
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_")

    if not value:
        value = "channel"

    return value


def unique_channel_id(name, used_ids):
    base = slugify(name)
    channel_id = base
    number = 2

    while channel_id in used_ids:
        channel_id = f"{base}_{number}"
        number += 1

    used_ids.add(channel_id)
    return channel_id


def is_bad_channel(name):
    normalized = normalize_name(name)

    if normalized in BAD_CHANNELS:
        return True

    for part in BAD_CHANNEL_PARTS:
        if part in normalized:
            return True

    return False


def extract_channel_key(href):
    if not href:
        return None

    href = html.unescape(href).strip()

    parsed = urlparse(href)

    query = parse_qs(parsed.query)

    for key in ("i", "channel", "channelId"):
        values = query.get(key)

        if values:
            raw = values[0]

            match = re.search(r"(ch[a-zA-Z0-9_-]+)", raw)

            if match:
                return match.group(1)

    match = re.search(r"(ch[a-zA-Z0-9_-]{6,})", href)

    if match:
        return match.group(1)

    return href


def extract_program(text):
    text = clean_text(text)

    if not text:
        return None

    text = re.sub(r"\s+Canlı\s*$", "", text, flags=re.IGNORECASE)
    text = clean_text(text)

    match = SCHEDULE_RE.match(text)

    if not match:
        match = SCHEDULE_RE_FALLBACK.match(text)

    if not match:
        return None

    title = clean_text(match.group(1))
    sh = int(match.group(2))
    sm = int(match.group(3))
    eh = int(match.group(4))
    em = int(match.group(5))

    if sh > 23 or eh > 23:
        return None

    if sm > 59 or em > 59:
        return None

    if not title:
        return None

    title = re.sub(
        r"\s+(Film|Dizi|Çocuk|Spor|Haber|Aktüalite|Yaşam|Eğlence|Belgesel|Müzik|Diğer|Global|Sinema|Lifestyle|Genel)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = clean_text(title)

    if not title:
        return None

    return {
        "title": title,
        "start_hour": sh,
        "start_minute": sm,
        "end_hour": eh,
        "end_minute": em,
    }


def get_schedule_date(programs, today, now_local):
    if not programs:
        return []

    now_minutes = now_local.hour * 60 + now_local.minute

    result = []

    first_start = (
        programs[0]["start_hour"] * 60
        + programs[0]["start_minute"]
    )

    # Sayfa sabah saatlerinde açıldığında listenin ilk maddesi
    # önceki günün geç saatlerine ait olabiliyor.
    if now_local.hour < 12 and first_start > now_minutes:
        current_date = today - timedelta(days=1)
    else:
        current_date = today

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
            current_date += timedelta(days=1)

        start_dt = datetime(
            current_date.year,
            current_date.month,
            current_date.day,
            program["start_hour"],
            program["start_minute"],
            tzinfo=ISTANBUL,
        )

        stop_date = current_date

        if (
            program["end_hour"] * 60
            + program["end_minute"]
        ) <= start_minutes:
            stop_date += timedelta(days=1)

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


def xml_time(dt):
    return dt.strftime("%Y%m%d%H%M%S +0300")


def xml_escape(value):
    return html.escape(
        str(value),
        quote=False,
    )


def fetch_page():
    print("Tivibu canlı TV sayfası alınıyor...")

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
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page.goto(
            LIVE_TV_URL,
            wait_until="domcontentloaded",
            timeout=120000,
        )

        page.wait_for_timeout(5000)

        # Program listesi yüklenmiş olsun.
        try:
            page.wait_for_selector(
                'a[href*="/rv?"]',
                timeout=30000,
            )
        except Exception:
            pass

        data = page.locator("body").inner_html()

        browser.close()

    return data


def parse_html(raw_html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        raw_html,
        "html.parser",
    )

    channels = []
    channel_urls = set()

    # ---------------------------------------------------------
    # KANALLARI AL
    # ---------------------------------------------------------

    for anchor in soup.select('a[href*="/kanallar/"]'):
        name = clean_text(anchor.get_text(" ", strip=True))
        href = clean_text(anchor.get("href", ""))

        if not name or not href:
            continue

        if is_bad_channel(name):
            continue

        # Ana sayfadaki tekrarları kaldır.
        if href.startswith("/"):
            full_url = BASE_URL + href
        else:
            full_url = href

        full_url = full_url.split("#")[0]

        if full_url in channel_urls:
            continue

        channel_urls.add(full_url)

        channels.append(
            {
                "name": name,
                "url": full_url,
            }
        )

    # ---------------------------------------------------------
    # PROGRAM GRUPLARINI AL
    # ---------------------------------------------------------

    groups = OrderedDict()

    for anchor in soup.select('a[href*="/rv?"]'):
        text = clean_text(
            anchor.get_text(" ", strip=True)
        )

        if not TIME_RE.search(text):
            continue

        parsed = extract_program(text)

        if not parsed:
            continue

        href = clean_text(anchor.get("href", ""))

        if not href:
            continue

        if href.startswith("/"):
            href = BASE_URL + href

        key = extract_channel_key(href)

        if not key:
            continue

        if key not in groups:
            groups[key] = []

        groups[key].append(parsed)

    return channels, groups


def map_groups_to_channels(channels, groups):
    mapped = []

    group_keys = list(groups.keys())

    channel_index = 0

    for group_key in group_keys:
        while channel_index < len(channels):
            channel = channels[channel_index]
            channel_index += 1

            if is_bad_channel(channel["name"]):
                continue

            mapped.append(
                {
                    "channel": channel,
                    "group_key": group_key,
                    "programs": groups[group_key],
                }
            )

            break

    return mapped


def deduplicate_programs(programs):
    result = []
    seen = set()

    for program in sorted(
        programs,
        key=lambda x: x["start"],
    ):
        key = (
            program["start"],
            program["stop"],
            normalize_name(program["title"]),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(program)

    return result


def build_epg(mapped_channels, today, now_local):
    used_ids = set()
    epg_channels = []
    all_programs = []

    for item in mapped_channels:
        channel_name = clean_text(
            item["channel"]["name"]
        )

        raw_programs = item["programs"]

        scheduled = get_schedule_date(
            raw_programs,
            today,
            now_local,
        )

        todays_programs = []

        for program in scheduled:
            start_date = program["start"].date()

            if start_date != today:
                continue

            # Mantıksız programları at.
            if program["stop"] <= program["start"]:
                continue

            duration = (
                program["stop"] - program["start"]
            ).total_seconds()

            if duration <= 0:
                continue

            if duration > 24 * 60 * 60:
                continue

            todays_programs.append(program)

        todays_programs = deduplicate_programs(
            todays_programs
        )

        if not todays_programs:
            continue

        channel_id = unique_channel_id(
            channel_name,
            used_ids,
        )

        epg_channels.append(
            {
                "id": channel_id,
                "name": channel_name,
            }
        )

        for program in todays_programs:
            all_programs.append(
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "title": program["title"],
                    "start": program["start"],
                    "stop": program["stop"],
                }
            )

    return epg_channels, all_programs


def write_xml(channels, programs, today):
    lines = []

    lines.append(
        '<?xml version="1.0" encoding="UTF-8"?>'
    )

    lines.append(
        '<tv generator-info-name="Tivibu Günlük EPG" '
        'generator-info-url="https://www.tivibu.com.tr/canli-tv">'
    )

    for channel in channels:
        lines.append(
            f'  <channel id="{html.escape(channel["id"], quote=True)}">'
        )
        lines.append(
            f'    <display-name lang="tr">'
            f'{xml_escape(channel["name"])}'
            f'</display-name>'
        )
        lines.append(
            "  </channel>"
        )

    sorted_programs = sorted(
        programs,
        key=lambda x: (
            x["start"],
            x["channel_name"],
        ),
    )

    for program in sorted_programs:
        lines.append(
            f'  <programme '
            f'channel="{html.escape(program["channel_id"], quote=True)}" '
            f'start="{xml_time(program["start"])}" '
            f'stop="{xml_time(program["stop"])}">'
        )

        lines.append(
            f'    <title lang="tr">'
            f'{xml_escape(program["title"])}'
            f'</title>'
        )

        lines.append(
            "  </programme>"
        )

    lines.append("</tv>")

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(
            "\n".join(lines)
            + "\n"
        )


def print_summary(channels, programs):
    print()
    print("=" * 70)
    print("SONUÇ")
    print("=" * 70)
    print(
        f"XML kanal sayısı : {len(channels)}"
    )
    print(
        f"Toplam program   : {len(programs)}"
    )
    print(
        f"Dosya            : {OUTPUT_FILE}"
    )
    print("=" * 70)

    if programs:
        counts = {}

        for program in programs:
            name = program["channel_name"]
            counts[name] = counts.get(name, 0) + 1

        print()
        print("Kanal program sayıları:")

        for channel_name, count in counts.items():
            print(
                f"  {channel_name}: {count}"
            )

        print()
        print("İlk 20 program:")

        for program in programs[:20]:
            print(
                "  "
                f'{program["channel_name"]} | '
                f'{program["start"].strftime("%d.%m %H:%M")} - '
                f'{program["stop"].strftime("%d.%m %H:%M")} | '
                f'{program["title"]}'
            )


def main():
    now_local = datetime.now(ISTANBUL)
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

    raw_html = fetch_page()

    channels, groups = parse_html(
        raw_html
    )

    print(
        f"Bulunan gerçek kanal: {len(channels)}"
    )

    print(
        f"Bulunan program grubu: {len(groups)}"
    )

    if not groups:
        raise RuntimeError(
            "Program grubu bulunamadı. Tivibu canlı TV sayfasındaki /rv? program bağlantıları alınamadı."
        )

    mapped = map_groups_to_channels(
        channels,
        groups,
    )

    print(
        f"Eşleştirilen kanal/grup: {len(mapped)}"
    )

    epg_channels, programs = build_epg(
        mapped,
        today,
        now_local,
    )

    print(
        f"Bugünün programı: {len(programs)}"
    )

    if not programs:
        raise RuntimeError(
            "Bugünün programı bulunamadı. EPG boş bırakılmadı."
        )

    write_xml(
        epg_channels,
        programs,
        today,
    )

    print_summary(
        epg_channels,
        programs,
    )

    print()
    print("EPG başarıyla oluşturuldu.")


if __name__ == "__main__":
    main()
