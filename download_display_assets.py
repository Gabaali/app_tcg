"""Download display assets without modifying the SQLite database.

Run with Python; Pillow is required for the card back PNG conversion.
DON!! images are matched by exact set/name from the original catalog.
"""
import hashlib
import html
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
BACK_URL = "https://raw.githubusercontent.com/33Shin/optcg-simulator/main/public/assets/imgs/back.webp"


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
        return response.read()


def card_key(card):
    fields = ("product_set", "card_number", "name", "rarity", "variant")
    return hashlib.sha256("\x1f".join(str(card[field]).strip() for field in fields).encode()).hexdigest()


def download_set(item):
    set_code, cards = item
    page_url = f"https://onepiece.app/set/{set_code}"
    page = fetch(page_url).decode("utf-8")
    links = {}
    for href, label in re.findall(r'<a\b[^>]*href="(/card/[^\"]+)"[^>]*>(.*?)</a>', page, re.S):
        name = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
        if name.startswith("DON!!"):
            links.setdefault(name, set()).add(href)
    entries, missing = {}, []
    for card in cards:
        matches = links.get(card["name"], set())
        if len(matches) != 1:
            missing.append(f"{set_code}: {card['name']} (no unique match)")
            continue
        href = next(iter(matches))
        product_id = re.match(r"/card/(\d+)", href).group(1)
        image_url = f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_400w.jpg"
        path = ASSETS / "don" / f"{product_id}.jpg"
        try:
            if not path.exists():
                try:
                    data = fetch(image_url)
                except OSError:
                    image_url = image_url.replace("_400w", "_200w")
                    data = fetch(image_url)
                if not data.startswith(b"\xff\xd8\xff"):
                    raise ValueError("Response is not a JPEG image")
                path.write_bytes(data)
            entries[card_key(card)] = dict(
                product_set=set_code, name=card["name"], variant=card["variant"],
                local_path=path.relative_to(ROOT).as_posix(),
                image_url=image_url, source_page="https://onepiece.app" + href,
            )
        except (OSError, ValueError) as error:
            missing.append(f"{set_code}: {card['name']} ({error})")
    return entries, missing


def main():
    (ASSETS / "don").mkdir(parents=True, exist_ok=True)
    back = ASSETS / "one_piece_card_back.webp"
    if not back.exists():
        data = fetch(BACK_URL)
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError("Response is not a WebP image")
        back.write_bytes(data)
    png = ASSETS / "one_piece_card_back.png"
    if not png.exists():
        from PIL import Image
        with Image.open(back) as image:
            image.save(png, "PNG")
    with sqlite3.connect((ROOT / "onepiece_tcg.sqlite").as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cards = conn.execute("SELECT * FROM terminal_cards WHERE rarity = 'DON!!'").fetchall()
    groups = {}
    for card in cards:
        groups.setdefault(card["product_set"], []).append(dict(card))
    manifest_path = ASSETS / "don_images.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    missing = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for entries, errors in pool.map(download_set, groups.items()):
            manifest.update(entries)
            missing.extend(errors)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ASSETS / "don_images_unavailable.json").write_text(
        json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"DON images: {len(manifest)}/{len(cards)}; unavailable: {len(missing)}")
    for message in missing:
        print(message.encode("ascii", "backslashreplace").decode())


if __name__ == "__main__":
    main()
