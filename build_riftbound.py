import html
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests


# ============================================================
# CONFIGURATION
# ============================================================

GALLERY_URL = (
    "https://playriftbound.com/fr-fr/card-gallery/"
)

DB_PATH = Path("riftbound_tcg.sqlite")

RAW_JSON_PATH = Path(
    "riftbound_cards_raw.json"
)

TIMEOUT = 90

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140 Safari/537.36"
    )
}


# ============================================================
# OUTILS
# ============================================================

def nested(data, *keys):
    """
    Accès sécurisé à un dictionnaire imbriqué.
    """

    current = data

    for key in keys:

        if not isinstance(current, dict):
            return None

        current = current.get(key)

    return current


def remove_accents(value):
    value = str(value or "")

    normalized = unicodedata.normalize(
        "NFKD",
        value
    )

    return "".join(
        c
        for c in normalized
        if not unicodedata.combining(c)
    )


def rarity_key(value):
    """
    Conserve la rareté affichée par Riot dans rarity_raw,
    mais crée aussi une rareté canonique pratique pour
    le futur simulateur.
    """

    text = (
        remove_accents(value)
        .lower()
        .strip()
    )

    # Important : Uncommon avant Common.
    if (
        "uncommon" in text
        or "peu commune" in text
        or "peu commun" in text
    ):
        return "uncommon"

    if (
        text == "common"
        or text == "commune"
        or text == "commun"
    ):
        return "common"

    if "epic" in text or "epique" in text:
        return "epic"

    if "ultimate" in text or "ultime" in text:
        return "ultimate"

    if "showcase" in text:
        return "showcase"

    if (
        "overnumbered" in text
        or "surnumeraire" in text
    ):
        return "overnumbered"

    if "promo" in text:
        return "promo"

    if "rare" in text:
        return "rare"

    # Valeur inconnue : on la conserve sous forme slug.
    text = re.sub(
        r"[^a-z0-9]+",
        "_",
        text
    )

    return text.strip("_") or "unknown"


def rich_text_to_plain(body):
    """
    Transforme le HTML des effets de carte en texte simple.
    """

    if not body:
        return ""

    text = str(body)

    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"</p>\s*<p[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"<[^>]+>",
        "",
        text
    )

    text = html.unescape(text)

    # Quelques symboles utiles Riftbound.
    replacements = {
        ":rb_might:":
            "{might}",

        ":rb_exhaust:":
            "{exhaust}",

        ":rb_rune_rainbow:":
            "{power:any}",

        ":rb_rune_fury:":
            "{power:fury}",

        ":rb_rune_calm:":
            "{power:calm}",

        ":rb_rune_mind:":
            "{power:mind}",

        ":rb_rune_body:":
            "{power:body}",

        ":rb_rune_chaos:":
            "{power:chaos}",

        ":rb_rune_order:":
            "{power:order}",
    }

    for old, new in replacements.items():
        text = text.replace(
            old,
            new
        )

    text = re.sub(
        r":rb_energy_(\d+):",
        lambda match:
            f"{{energy:{match.group(1)}}}",
        text
    )

    return "\n".join(
        line.strip()
        for line in text.splitlines()
        if line.strip()
    )


def full_resolution_url(url):
    """
    Retire les paramètres de redimensionnement éventuels
    du CDN Riot.
    """

    if not url:
        return None

    parsed = urlsplit(
        str(url)
    )

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            ""
        )
    )


# ============================================================
# RÉCUPÉRATION DU __NEXT_DATA__
# ============================================================

def fetch_next_data():

    print(
        f"Téléchargement : {GALLERY_URL}"
    )

    response = requests.get(
        GALLERY_URL,
        headers=HEADERS,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    page = response.text

    match = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\']'
        r'[^>]*>(.*?)</script>',
        page,
        flags=re.DOTALL | re.IGNORECASE
    )

    if not match:
        raise RuntimeError(
            "__NEXT_DATA__ introuvable. "
            "La structure du site a peut-être changé."
        )

    return json.loads(
        match.group(1)
    )


# ============================================================
# TROUVER LA LISTE DE CARTES
# ============================================================

def find_cards(node, depth=0):
    """
    Recherche récursivement la liste d'objets contenant
    le champ publicCode.

    Cela évite de dépendre du numéro exact d'une blade
    dans la page Riot.
    """

    if depth > 20:
        return None

    if isinstance(node, list):

        if (
            node
            and isinstance(node[0], dict)
            and "publicCode" in node[0]
        ):
            return node

        for item in node:

            result = find_cards(
                item,
                depth + 1
            )

            if result:
                return result

    elif isinstance(node, dict):

        for value in node.values():

            result = find_cards(
                value,
                depth + 1
            )

            if result:
                return result

    return None


# ============================================================
# NORMALISATION D'UNE CARTE
# ============================================================

def flatten_card(card):

    public_code = str(
        card.get("publicCode")
        or ""
    ).strip()

    # Exemple :
    #
    # OGN-194/298
    # OGN-007a/298
    # OGN-299*
    #
    code_head = (
        public_code
        .split("/")[0]
        .strip()
    )

    signed = "*" in public_code

    code = code_head.replace(
        "*",
        ""
    )

    # Une variante Alt Art utilise notamment :
    #
    # OGN-007a
    #
    suffix = (
        code.split("-", 1)[1]
        if "-" in code
        else ""
    )

    alt_art = bool(
        re.search(
            r"\d+[a-z]$",
            suffix,
            flags=re.IGNORECASE
        )
    )

    # OGN-007a -> OGN-007
    #
    # Utile plus tard pour grouper les différentes
    # impressions d'une même carte.
    base_code = re.sub(
        r"(\d+)[a-z]$",
        r"\1",
        code,
        flags=re.IGNORECASE
    )

    set_code = nested(
        card,
        "set",
        "value",
        "id"
    )

    set_name = nested(
        card,
        "set",
        "value",
        "label"
    )

    rarity = nested(
        card,
        "rarity",
        "value",
        "label"
    )

    card_types = (
        nested(
            card,
            "cardType",
            "type"
        )
        or []
    )

    type_labels = [
        item.get("label")
        for item in card_types
        if isinstance(item, dict)
        and item.get("label")
    ]

    domains_raw = (
        nested(
            card,
            "domain",
            "values"
        )
        or []
    )

    domains = [
        item.get("label")
        for item in domains_raw
        if isinstance(item, dict)
        and item.get("label")
    ]

    illustrators_raw = (
        nested(
            card,
            "illustrator",
            "values"
        )
        or []
    )

    illustrators = [
        item.get("label")
        for item in illustrators_raw
        if isinstance(item, dict)
        and item.get("label")
    ]

    rules_html = (
        nested(
            card,
            "text",
            "richText",
            "body"
        )
        or ""
    )

    image_url = (
        nested(
            card,
            "cardImage",
            "url"
        )
        or ""
    )

    riot_id = str(
        card.get("id")
        or ""
    )

    # ID stable.
    card_uid = (
        riot_id
        or public_code
        or code
    )

    return {
        "card_uid":
            card_uid,

        "riot_id":
            riot_id,

        "code":
            code,

        "public_code":
            public_code,

        "base_code":
            base_code,

        "set_code":
            set_code,

        "set_name":
            set_name,

        "collector_number":
            card.get(
                "collectorNumber"
            ),

        "name":
            card.get(
                "name"
            ),

        "card_type":
            " / ".join(
                type_labels
            ),

        "rarity_raw":
            rarity,

        "rarity_key":
            rarity_key(
                rarity
            ),

        "domains_json":
            json.dumps(
                domains,
                ensure_ascii=False
            ),

        "energy":
            nested(
                card,
                "energy",
                "value",
                "label"
            ),

        "might":
            nested(
                card,
                "might",
                "value",
                "label"
            ),

        "power":
            nested(
                card,
                "power",
                "value",
                "label"
            ),

        "illustrator":
            ", ".join(
                illustrators
            ),

        "orientation":
            card.get(
                "orientation"
            ),

        "is_alt_art":
            int(
                alt_art
            ),

        "is_signed":
            int(
                signed
            ),

        "is_variant":
            int(
                alt_art
                or signed
            ),

        "rules_text":
            rich_text_to_plain(
                rules_html
            ),

        "rules_html":
            rules_html,

        "image_url":
            image_url,

        "image_full_url":
            full_resolution_url(
                image_url
            ),

        "source_url":
            (
                GALLERY_URL
                + "#card-gallery--"
                + riot_id
                if riot_id
                else GALLERY_URL
            ),

        "raw_json":
            json.dumps(
                card,
                ensure_ascii=False,
                separators=(",", ":")
            ),

        "active":
            1,

        "updated_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }


# ============================================================
# DATABASE
# ============================================================

def create_database(conn):

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    conn.execute(
        "PRAGMA journal_mode = WAL"
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sets (

            set_code TEXT PRIMARY KEY,

            set_name TEXT,

            card_count INTEGER
                NOT NULL DEFAULT 0,

            base_card_count INTEGER
                NOT NULL DEFAULT 0,

            variant_card_count INTEGER
                NOT NULL DEFAULT 0,

            updated_at TEXT
        );


        CREATE TABLE IF NOT EXISTS cards (

            card_uid TEXT PRIMARY KEY,

            riot_id TEXT,

            code TEXT,
            public_code TEXT,

            base_code TEXT,

            set_code TEXT,
            set_name TEXT,

            collector_number INTEGER,

            name TEXT NOT NULL,

            card_type TEXT,

            rarity_raw TEXT,
            rarity_key TEXT,

            domains_json TEXT,

            energy TEXT,
            might TEXT,
            power TEXT,

            illustrator TEXT,

            orientation TEXT,

            is_alt_art INTEGER
                NOT NULL DEFAULT 0,

            is_signed INTEGER
                NOT NULL DEFAULT 0,

            is_variant INTEGER
                NOT NULL DEFAULT 0,

            rules_text TEXT,
            rules_html TEXT,

            image_url TEXT,
            image_full_url TEXT,

            local_image_path TEXT,

            source_url TEXT,

            raw_json TEXT,

            active INTEGER
                NOT NULL DEFAULT 1,

            updated_at TEXT,

            FOREIGN KEY(set_code)
                REFERENCES sets(set_code)
        );


        CREATE INDEX IF NOT EXISTS
            idx_cards_set
        ON cards(set_code);


        CREATE INDEX IF NOT EXISTS
            idx_cards_code
        ON cards(code);


        CREATE INDEX IF NOT EXISTS
            idx_cards_base_code
        ON cards(base_code);


        CREATE INDEX IF NOT EXISTS
            idx_cards_name
        ON cards(name);


        CREATE INDEX IF NOT EXISTS
            idx_cards_rarity
        ON cards(rarity_key);


        CREATE INDEX IF NOT EXISTS
            idx_cards_variant
        ON cards(is_variant);
        """
    )


# ============================================================
# IMPORT
# ============================================================

def import_cards(conn, cards):

    # Les anciennes entrées restent dans la base mais sont
    # désactivées si Riot les retire de la galerie.
    conn.execute(
        """
        UPDATE cards
        SET active = 0
        """
    )

    insert_sql = """
    INSERT INTO cards (

        card_uid,
        riot_id,

        code,
        public_code,
        base_code,

        set_code,
        set_name,

        collector_number,

        name,
        card_type,

        rarity_raw,
        rarity_key,

        domains_json,

        energy,
        might,
        power,

        illustrator,
        orientation,

        is_alt_art,
        is_signed,
        is_variant,

        rules_text,
        rules_html,

        image_url,
        image_full_url,

        source_url,

        raw_json,

        active,
        updated_at
    )

    VALUES (

        :card_uid,
        :riot_id,

        :code,
        :public_code,
        :base_code,

        :set_code,
        :set_name,

        :collector_number,

        :name,
        :card_type,

        :rarity_raw,
        :rarity_key,

        :domains_json,

        :energy,
        :might,
        :power,

        :illustrator,
        :orientation,

        :is_alt_art,
        :is_signed,
        :is_variant,

        :rules_text,
        :rules_html,

        :image_url,
        :image_full_url,

        :source_url,

        :raw_json,

        :active,
        :updated_at
    )

    ON CONFLICT(card_uid)

    DO UPDATE SET

        riot_id =
            excluded.riot_id,

        code =
            excluded.code,

        public_code =
            excluded.public_code,

        base_code =
            excluded.base_code,

        set_code =
            excluded.set_code,

        set_name =
            excluded.set_name,

        collector_number =
            excluded.collector_number,

        name =
            excluded.name,

        card_type =
            excluded.card_type,

        rarity_raw =
            excluded.rarity_raw,

        rarity_key =
            excluded.rarity_key,

        domains_json =
            excluded.domains_json,

        energy =
            excluded.energy,

        might =
            excluded.might,

        power =
            excluded.power,

        illustrator =
            excluded.illustrator,

        orientation =
            excluded.orientation,

        is_alt_art =
            excluded.is_alt_art,

        is_signed =
            excluded.is_signed,

        is_variant =
            excluded.is_variant,

        rules_text =
            excluded.rules_text,

        rules_html =
            excluded.rules_html,

        image_url =
            excluded.image_url,

        image_full_url =
            excluded.image_full_url,

        source_url =
            excluded.source_url,

        raw_json =
            excluded.raw_json,

        active =
            1,

        updated_at =
            excluded.updated_at
    """


    for index, card in enumerate(
        cards,
        start=1
    ):

        # Le set doit exister avant la carte.
        conn.execute(
            """
            INSERT INTO sets (
                set_code,
                set_name,
                updated_at
            )

            VALUES (?, ?, ?)

            ON CONFLICT(set_code)

            DO UPDATE SET

                set_name =
                    excluded.set_name,

                updated_at =
                    excluded.updated_at
            """,
            (
                card[
                    "set_code"
                ],

                card[
                    "set_name"
                ],

                card[
                    "updated_at"
                ]
            )
        )

        conn.execute(
            insert_sql,
            card
        )

        if index % 100 == 0:

            conn.commit()

            print(
                f"{index}/{len(cards)} "
                f"cartes importées..."
            )


    # ========================================================
    # RECALCUL DES STATS DES SETS
    # ========================================================

    conn.execute(
        """
        UPDATE sets

        SET
            card_count = (

                SELECT COUNT(*)

                FROM cards c

                WHERE
                    c.set_code =
                        sets.set_code

                    AND c.active = 1
            ),

            base_card_count = (

                SELECT COUNT(*)

                FROM cards c

                WHERE
                    c.set_code =
                        sets.set_code

                    AND c.active = 1

                    AND c.is_variant = 0
            ),

            variant_card_count = (

                SELECT COUNT(*)

                FROM cards c

                WHERE
                    c.set_code =
                        sets.set_code

                    AND c.active = 1

                    AND c.is_variant = 1
            )
        """
    )

    conn.commit()


# ============================================================
# MAIN
# ============================================================

def main():

    next_data = fetch_next_data()

    raw_cards = find_cards(
        next_data.get(
            "props",
            next_data
        )
    )

    if not raw_cards:

        raise RuntimeError(
            "Impossible de trouver la liste des cartes "
            "dans __NEXT_DATA__."
        )


    print(
        f"{len(raw_cards)} cartes trouvées."
    )


    # Snapshot brut utile pour debug.
    RAW_JSON_PATH.write_text(
        json.dumps(
            raw_cards,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


    cards = [
        flatten_card(card)
        for card in raw_cards
    ]


    # On ignore uniquement les entrées totalement invalides.
    cards = [
        card
        for card in cards

        if (
            card["card_uid"]
            and card["name"]
            and card["set_code"]
        )
    ]


    cards.sort(
        key=lambda card: (
            str(
                card["set_code"]
            ),
            int(
                card["collector_number"]
                or 0
            ),
            str(
                card["public_code"]
            )
        )
    )


    with sqlite3.connect(
        DB_PATH
    ) as conn:

        create_database(
            conn
        )

        import_cards(
            conn,
            cards
        )


        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM cards
            WHERE active = 1
            """
        ).fetchone()[0]


        sets = conn.execute(
            """
            SELECT
                set_code,
                set_name,
                card_count,
                base_card_count,
                variant_card_count

            FROM sets

            WHERE card_count > 0

            ORDER BY set_code
            """
        ).fetchall()


        rarities = conn.execute(
            """
            SELECT
                rarity_key,
                rarity_raw,
                COUNT(*)

            FROM cards

            WHERE active = 1

            GROUP BY
                rarity_key,
                rarity_raw

            ORDER BY COUNT(*) DESC
            """
        ).fetchall()


    print()
    print(
        "=" * 70
    )
    print(
        "IMPORT TERMINÉ"
    )
    print(
        "=" * 70
    )

    print(
        f"Cartes actives : {total}"
    )

    print(
        f"SQLite         : {DB_PATH.resolve()}"
    )

    print(
        f"Snapshot JSON  : {RAW_JSON_PATH.resolve()}"
    )


    print()
    print(
        "Sets :"
    )

    for row in sets:

        print(
            f"  {row[0]:8} "
            f"| {row[1]:25} "
            f"| {row[2]:4} cartes "
            f"| base {row[3]:4} "
            f"| variantes {row[4]:4}"
        )


    print()
    print(
        "Raretés :"
    )

    for rarity, raw, count in rarities:

        print(
            f"  {rarity:15} "
            f"| {str(raw):20} "
            f"| {count}"
        )


if __name__ == "__main__":
    main()