import json
import re
import sqlite3
import sys
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

if len(sys.argv) >= 2:
    DATA_DIR = Path(sys.argv[1])
else:
    DATA_DIR = Path("OPTCG-main/english")

DB_PATH = Path("onepiece_tcg.sqlite")

CARDS_DIR = DATA_DIR / "cards"
PACKS_FILE = DATA_DIR / "packs.json"


# ============================================================
# OUTILS
# ============================================================

def json_text(value):
    if value is None:
        return None

    if isinstance(value, (list, dict)):
        return json.dumps(
            value,
            ensure_ascii=False
        )

    return str(value)


def split_print_id(card_id):
    """
    Exemples :

    OP01-001
        -> OP01-001, None

    OP01-001_p1
        -> OP01-001, p1

    OP01-001_r1
        -> OP01-001, r1
    """

    match = re.match(
        r"^(.*?)(?:_((?:p|r|jp)\d+))?$",
        card_id,
        re.IGNORECASE
    )

    if not match:
        return card_id, None

    return match.group(1), match.group(2)


def variant_family(suffix):

    if not suffix:
        return "base"

    suffix = suffix.lower()

    if suffix.startswith("p"):
        return "parallel"

    if suffix.startswith("r"):
        return "reprint"

    if suffix.startswith("jp"):
        return "jp_variant"

    return "other"


# ============================================================
# VÉRIFICATIONS
# ============================================================

print()
print("Dossier de données :", DATA_DIR.resolve())
print("Dossier cards       :", CARDS_DIR.resolve())
print()

if not DATA_DIR.exists():
    raise FileNotFoundError(
        f"Dossier introuvable : {DATA_DIR}"
    )

if not CARDS_DIR.exists():
    raise FileNotFoundError(
        f"Dossier cards introuvable : {CARDS_DIR}"
    )

if not PACKS_FILE.exists():
    raise FileNotFoundError(
        f"packs.json introuvable : {PACKS_FILE}"
    )


# ============================================================
# BASE SQLITE
# ============================================================

conn = sqlite3.connect(DB_PATH)

conn.execute(
    "PRAGMA journal_mode=WAL"
)

conn.execute(
    "PRAGMA foreign_keys=ON"
)

cursor = conn.cursor()


cursor.executescript("""
CREATE TABLE IF NOT EXISTS sets (

    pack_id TEXT PRIMARY KEY,

    raw_title TEXT,
    title TEXT,
    label TEXT,

    source_language TEXT
);


CREATE TABLE IF NOT EXISTS cards (

    card_uid TEXT PRIMARY KEY,

    print_id TEXT NOT NULL,
    base_id TEXT NOT NULL,

    variant_suffix TEXT,
    variant_family TEXT,

    pack_id TEXT,

    name TEXT,

    rarity TEXT,
    category TEXT,

    colors TEXT,

    cost INTEGER,
    power INTEGER,
    counter INTEGER,
    block_number INTEGER,

    attributes TEXT,
    types TEXT,

    effect TEXT,
    trigger_text TEXT,

    image_url TEXT,

    source_file TEXT
);


CREATE INDEX IF NOT EXISTS idx_cards_print_id
ON cards(print_id);


CREATE INDEX IF NOT EXISTS idx_cards_base_id
ON cards(base_id);


CREATE INDEX IF NOT EXISTS idx_cards_pack
ON cards(pack_id);


CREATE INDEX IF NOT EXISTS idx_cards_name
ON cards(name);


CREATE INDEX IF NOT EXISTS idx_cards_rarity
ON cards(rarity);


CREATE INDEX IF NOT EXISTS idx_cards_variant
ON cards(variant_family);
""")


# ============================================================
# IMPORT DES EXTENSIONS
# ============================================================

print("Import des extensions...")


with PACKS_FILE.open(
    "r",
    encoding="utf-8"
) as f:

    packs_data = json.load(f)


# packs.json peut être :
#
# [
#    {...},
#    {...}
# ]
#
# OU
#
# {
#    "569101": {...},
#    ...
# }

if isinstance(packs_data, dict):

    packs = list(
        packs_data.values()
    )

elif isinstance(packs_data, list):

    packs = packs_data

else:

    raise TypeError(
        "Format packs.json inconnu : "
        f"{type(packs_data)}"
    )


set_count = 0


for pack in packs:

    if not isinstance(pack, dict):
        continue

    title_parts = (
        pack.get("title_parts")
        or {}
    )

    pack_id = str(
        pack.get("id")
        or ""
    )

    if not pack_id:
        continue

    cursor.execute("""
    INSERT OR REPLACE INTO sets (
        pack_id,
        raw_title,
        title,
        label,
        source_language
    )
    VALUES (?, ?, ?, ?, ?)
    """, (

        pack_id,

        pack.get("raw_title"),

        title_parts.get("title"),

        title_parts.get("label"),

        DATA_DIR.name

    ))

    set_count += 1


conn.commit()


print(
    f"{set_count} extensions importées."
)


# ============================================================
# RECHERCHE DES FICHIERS CARTES
# ============================================================

print()
print("Recherche des cartes...")


files = sorted(
    CARDS_DIR.rglob("*.json")
)


print(
    f"{len(files)} fichiers JSON trouvés."
)


if not files:

    print()
    print("ERREUR : aucune carte trouvée.")
    print()
    print(
        "Vérifie le contenu de :"
    )
    print(
        CARDS_DIR.resolve()
    )

    conn.close()
    sys.exit(1)


# ============================================================
# IMPORT D'UNE CARTE
# ============================================================

def import_card(
    card,
    file_path
):

    if not isinstance(card, dict):
        return False


    print_id = str(
        card.get("id")
        or ""
    ).strip()


    if not print_id:
        return False


    base_id, suffix = split_print_id(
        print_id
    )


    # normalement fourni directement
    # dans le JSON
    pack_id = str(
        card.get("pack_id")
        or file_path.parent.name
    )


    # permet d'éviter les collisions
    # si la même carte apparaît
    # dans plusieurs produits
    card_uid = (
        f"{pack_id}:{print_id}"
    )


    image_url = (
        card.get("img_full_url")
        or card.get("image")
        or card.get("img_url")
    )


    cursor.execute("""
    INSERT OR REPLACE INTO cards (

        card_uid,

        print_id,
        base_id,

        variant_suffix,
        variant_family,

        pack_id,

        name,
        rarity,
        category,

        colors,

        cost,
        power,
        counter,
        block_number,

        attributes,
        types,

        effect,
        trigger_text,

        image_url,

        source_file
    )

    VALUES (
        ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?,
        ?, ?
    )
    """, (

        card_uid,

        print_id,
        base_id,

        suffix,
        variant_family(suffix),

        pack_id,

        card.get("name"),
        card.get("rarity"),
        card.get("category"),

        json_text(
            card.get("colors")
        ),

        card.get("cost"),
        card.get("power"),
        card.get("counter"),

        card.get("block_number")
        or card.get("block"),

        json_text(
            card.get("attributes")
        ),

        json_text(
            card.get("types")
        ),

        card.get("effect"),

        card.get("trigger"),

        image_url,

        str(
            file_path.relative_to(
                CARDS_DIR
            )
        )

    ))

    return True


# ============================================================
# IMPORT DE TOUS LES JSON
# ============================================================

total_cards = 0
errors = 0


for index, file_path in enumerate(
    files,
    start=1
):

    try:

        with file_path.open(
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)


        # ------------------------------------
        # CAS 1
        #
        # Un fichier = une carte
        #
        # {
        #   "id": "OP01-001",
        #   ...
        # }
        # ------------------------------------

        if (
            isinstance(data, dict)
            and "id" in data
        ):

            if import_card(
                data,
                file_path
            ):

                total_cards += 1


        # ------------------------------------
        # CAS 2
        #
        # Un fichier = liste de cartes
        # ------------------------------------

        elif isinstance(data, list):

            for card in data:

                if import_card(
                    card,
                    file_path
                ):

                    total_cards += 1


        # ------------------------------------
        # CAS 3
        #
        # {"cards": [...]}
        # ------------------------------------

        elif (
            isinstance(data, dict)
            and isinstance(
                data.get("cards"),
                list
            )
        ):

            for card in data["cards"]:

                if import_card(
                    card,
                    file_path
                ):

                    total_cards += 1


        else:

            print(
                "Format ignoré :",
                file_path
            )


    except Exception as exc:

        errors += 1

        print(
            f"ERREUR dans "
            f"{file_path}: {exc}"
        )


    # commit tous les 500 fichiers
    if index % 500 == 0:

        conn.commit()

        print(
            f"{index}/{len(files)} "
            f"fichiers analysés..."
        )


conn.commit()


# ============================================================
# STATISTIQUES
# ============================================================

set_count = cursor.execute("""
SELECT COUNT(*)
FROM sets
""").fetchone()[0]


card_count = cursor.execute("""
SELECT COUNT(*)
FROM cards
""").fetchone()[0]


parallel_count = cursor.execute("""
SELECT COUNT(*)
FROM cards
WHERE variant_family = 'parallel'
""").fetchone()[0]


reprint_count = cursor.execute("""
SELECT COUNT(*)
FROM cards
WHERE variant_family = 'reprint'
""").fetchone()[0]


unique_base_cards = cursor.execute("""
SELECT COUNT(
    DISTINCT base_id
)
FROM cards
""").fetchone()[0]


print()
print(
    "===================================="
)
print(
    "IMPORT TERMINÉ"
)
print(
    "===================================="
)

print(
    f"Extensions          : {set_count}"
)

print(
    f"Impressions/cartes  : {card_count}"
)

print(
    f"Cartes de base      : {unique_base_cards}"
)

print(
    f"Parallèles          : {parallel_count}"
)

print(
    f"Réimpressions       : {reprint_count}"
)

print(
    f"Erreurs             : {errors}"
)

print(
    f"Base                : "
    f"{DB_PATH.resolve()}"
)


# Quelques exemples

print()
print(
    "Quelques cartes :"
)


examples = cursor.execute("""
SELECT
    print_id,
    name,
    rarity,
    variant_family,
    pack_id

FROM cards

LIMIT 10
""").fetchall()


for row in examples:

    print(
        " | ".join(
            str(value)
            for value in row
        )
    )


conn.close()