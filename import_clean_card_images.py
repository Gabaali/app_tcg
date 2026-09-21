"""Import real card scans, preserving card identities and all user data.

python import_clean_card_images.py prepare
python import_clean_card_images.py apply

Only Poneglyph images explicitly classified as scans are selected. SAMPLE
stock images and community proxy reconstructions are never substituted.
"""
import argparse
import hashlib
import html
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DB = ROOT / "onepiece_tcg.sqlite"
OUTPUT = ROOT / "assets" / "clean_cards"
CACHE = OUTPUT / "catalog"
PLAN = OUTPUT / "import_plan.json"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch(url, payload=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=40) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(min(float(error.headers.get("Retry-After", 2 ** attempt)), 60))


def card_key(card):
    fields = ("product_set", "card_number", "name", "rarity", "variant")
    raw = "\x1f".join(str(card.get(field, "")).strip() for field in fields)
    return hashlib.sha256(raw.encode()).hexdigest()


def product_id(url):
    match = re.search(r"/(?:product|card)/(\d+)", url or "")
    return match.group(1) if match else None


def set_links(set_code):
    cache = CACHE / f"terminal_{set_code}.json"
    if cache.exists():
        return read_json(cache)
    page = fetch(f"https://onepiece.app/set/{set_code}").decode("utf-8")
    matches = {}
    # Use only the full checklist rows, not marketing/top-card links.
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        link = re.search(r'href="(/card/[^\"]+)"', cells[1])
        if not link:
            continue
        text = lambda value: html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        key = json.dumps([text(cells[0]), text(cells[1]), text(cells[2]), text(cells[3]).replace("—", "")])
        matches.setdefault(key, []).append(product_id(link.group(1)))
    write_json(cache, matches)
    return matches


def download_scan(entry):
    path = ROOT / entry["local_path"]
    if not path.exists():
        data = fetch(entry["source_url"])
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
            raise ValueError("Scan response is not an image")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    return entry


def prepare():
    CACHE.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        local = [dict(row) for row in conn.execute("SELECT c.*, s.label AS set_label FROM cards c LEFT JOIN sets s USING(pack_id)")]
        terminal = [dict(row) for row in conn.execute("SELECT * FROM terminal_cards")]
    numbers = sorted({c["base_id"] for c in local} | {
        c["card_number"] for c in terminal if re.fullmatch(r"[A-Z]+\d*-\d+", c["card_number"])
    })
    missing = [number for number in numbers if not (CACHE / f"{number}.json").exists()]
    for start in range(0, len(missing), 50):
        batch = missing[start:start + 50]
        response = json.loads(fetch("https://api.poneglyph.one/v1/cards/batch", {"card_numbers": batch, "lang": "en"}))
        for number in batch:
            write_json(CACHE / f"{number}.json", response.get("data", {}).get(number, {}))
        print(f"Catalog: {min(start + 50, len(missing))}/{len(missing)}", flush=True)
        time.sleep(0.15)

    candidates = []
    for number in numbers:
        card = read_json(CACHE / f"{number}.json")
        for variant in card.get("variants", []):
            scan = variant.get("images", {}).get("scan", {})
            url = scan.get("display") or scan.get("full")
            if not url or urlparse(url).hostname != "cdn.poneglyph.one":
                continue
            index = variant["index"]
            path = OUTPUT / "scans" / number / f"{index}{Path(urlparse(url).path).suffix}"
            candidates.append(dict(
                base_id=number, variant_index=index, label=variant.get("label"),
                product_set=(variant.get("product") or {}).get("set_code"),
                tcgplayer_id=product_id((variant.get("market") or {}).get("tcgplayer_url")),
                source_url=url, local_path=path.relative_to(ROOT).as_posix(),
            ))
    downloaded, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(download_scan, entry): entry for entry in candidates}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                downloaded.append(future.result())
            except (OSError, ValueError) as error:
                errors.append(dict(card=futures[future], error=str(error)))
            if index % 100 == 0 or index == len(futures):
                print(f"Scans: {index}/{len(futures)}", flush=True)
    downloaded.sort(key=lambda e: (e["base_id"], e["variant_index"]))
    by_product = {}
    for entry in downloaded:
        if entry["tcgplayer_id"]:
            by_product.setdefault(entry["tcgplayer_id"], []).append(entry)
    links = {}
    for set_code in sorted({c["product_set"] for c in terminal}):
        try:
            links[set_code] = set_links(set_code)
        except (OSError, ValueError) as error:
            errors.append(dict(set_code=set_code, error=str(error)))
    terminal_updates = []
    for card in terminal:
        key = json.dumps([card["card_number"], card["name"], card["rarity"], card["variant"]])
        ids = set(links.get(card["product_set"], {}).get(key, []))
        if len(ids) != 1:
            continue
        scans = by_product.get(next(iter(ids)), [])
        if len(scans) == 1 and scans[0]["base_id"] == card["card_number"]:
            terminal_updates.append(dict(card_key=card_key(card), card=card, **scans[0]))

    # A p1/p2 suffix is not an API variant index. Only assign the unambiguous
    # standard printing to cards here; exact alternate arts use card_key above.
    card_updates = []
    standard = {e["base_id"]: e for e in downloaded if e["variant_index"] == 0 and e["label"] == "Standard"}
    for card in local:
        scan = standard.get(card["base_id"])
        label = re.sub(r"[^A-Z0-9]", "", str(card["set_label"] or "").upper())
        if scan and not card["variant_suffix"] and label == scan["product_set"]:
            card_updates.append(dict(card_uid=card["card_uid"], **scan))
    plan = dict(created_at=datetime.now(timezone.utc).isoformat(), scans=downloaded,
                card_updates=card_updates, terminal_updates=terminal_updates, errors=errors,
                cards_total=len(local), terminal_total=len(terminal))
    write_json(PLAN, plan)
    print(json.dumps({"scans": len(downloaded), "cards_updates": len(card_updates),
                      "terminal_updates": len(terminal_updates), "errors": len(errors)}), flush=True)


def apply():
    plan = read_json(PLAN)
    for entry in plan["card_updates"] + plan["terminal_updates"]:
        path = (ROOT / entry["local_path"]).resolve()
        if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
            raise ValueError(f"Missing or invalid scan path: {path}")
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"before_clean_images_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%f}.sqlite"
    with closing(sqlite3.connect(DB, timeout=30)) as conn:
        with closing(sqlite3.connect(backup)) as copy:
            conn.backup(copy)
        conn.execute("BEGIN IMMEDIATE")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
        if "clean_image_path" not in columns:
            conn.execute("ALTER TABLE cards ADD COLUMN clean_image_path TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS card_image_overrides (
            card_key TEXT PRIMARY KEY, local_image_path TEXT NOT NULL,
            source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
            source_product_id TEXT, updated_at TEXT NOT NULL
        )""")
        for entry in plan["card_updates"]:
            conn.execute("UPDATE cards SET clean_image_path=? WHERE card_uid=?",
                         (entry["local_path"], entry["card_uid"]))
        for entry in plan["terminal_updates"]:
            # A regenerated terminal_id does not change this identity.
            conn.execute("""INSERT INTO card_image_overrides VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_key) DO UPDATE SET local_image_path=excluded.local_image_path,
                source_url=excluded.source_url, source_kind=excluded.source_kind,
                source_product_id=excluded.source_product_id, updated_at=excluded.updated_at""",
                (entry["card_key"], entry["local_path"], entry["source_url"], "poneglyph_scan",
                 entry["tcgplayer_id"], datetime.now(timezone.utc).isoformat()))
        conn.commit()
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed")
    write_json(OUTPUT / "last_import.json", dict(backup=str(backup.relative_to(ROOT)),
               cards_updated=len(plan["card_updates"]), terminal_updated=len(plan["terminal_updates"])))
    print(f"Applied. Backup: {backup}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    args = parser.parse_args()
    prepare() if args.action == "prepare" else apply()
