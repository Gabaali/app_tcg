import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = "pokemon_tcg.sqlite"

LANGUAGE = "fr"

API_ROOT = (
    f"https://api.tcgdex.net/v2/{LANGUAGE}"
)

MAX_WORKERS = 6

TIMEOUT = 30

# Pour tester seulement quelques sets :
#
# ONLY_SETS = {
#     "sv3.5",
#     "sv4",
# }
#
# None = tous les sets

ONLY_SETS = None


# Si tu veux seulement certaines séries :
#
# Exemple :
#
# ONLY_SERIES = {
#     "sv",
# }
#
# None = toutes les séries

ONLY_SERIES = None


# ============================================================
# HTTP
# ============================================================

def create_session():

    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=[
            "GET",
        ],
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS * 2,
        pool_maxsize=MAX_WORKERS * 2,
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update({
        "User-Agent": (
            "PokemonTCG-local-database/1.0"
        )
    })

    return session


session = create_session()


def api_get(path):

    url = (
        f"{API_ROOT}/{path.lstrip('/')}"
    )

    response = session.get(
        url,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# OUTILS
# ============================================================

def json_text(value):

    if value is None:
        return None

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def bool_to_int(value):

    if value is None:
        return 0

    return 1 if value else 0


def image_url(
    base,
    quality="high",
    extension="webp",
):

    if not base:
        return None

    base = base.rstrip("/")

    return (
        f"{base}/"
        f"{quality}."
        f"{extension}"
    )


def asset_url(
    base,
    extension="webp",
):

    if not base:
        return None

    base = base.rstrip("/")

    return (
        f"{base}."
        f"{extension}"
    )


# ============================================================
# SQLITE
# ============================================================

conn = sqlite3.connect(
    DB_PATH
)

conn.execute(
    "PRAGMA foreign_keys = ON"
)

conn.execute(
    "PRAGMA journal_mode = WAL"
)

cursor = conn.cursor()


cursor.executescript(
    """
    CREATE TABLE IF NOT EXISTS series (

        series_id TEXT PRIMARY KEY,

        name TEXT NOT NULL,

        logo_base_url TEXT,
        logo_url TEXT,

        language TEXT NOT NULL,

        updated_at TEXT
    );


    CREATE TABLE IF NOT EXISTS sets (

        set_id TEXT PRIMARY KEY,

        name TEXT NOT NULL,

        series_id TEXT,

        release_date TEXT,

        card_count_total INTEGER,
        card_count_official INTEGER,
        card_count_normal INTEGER,
        card_count_reverse INTEGER,
        card_count_holo INTEGER,
        card_count_first_edition INTEGER,

        logo_base_url TEXT,
        logo_url TEXT,

        symbol_base_url TEXT,
        symbol_url TEXT,

        tcg_online_code TEXT,

        legal_standard INTEGER,
        legal_expanded INTEGER,

        boosters_json TEXT,

        language TEXT NOT NULL,

        updated_at TEXT,

        raw_json TEXT,

        FOREIGN KEY(series_id)
            REFERENCES series(series_id)
    );


    CREATE TABLE IF NOT EXISTS cards (

        card_id TEXT PRIMARY KEY,

        local_id TEXT NOT NULL,

        set_id TEXT NOT NULL,

        name TEXT NOT NULL,

        category TEXT,
        rarity TEXT,

        illustrator TEXT,

        hp INTEGER,

        stage TEXT,
        suffix TEXT,

        regulation_mark TEXT,

        evolve_from TEXT,
        description TEXT,

        retreat INTEGER,

        image_base_url TEXT,
        image_high_url TEXT,
        image_low_url TEXT,

        local_image_path TEXT,

        types_json TEXT,
        dex_ids_json TEXT,

        abilities_json TEXT,
        attacks_json TEXT,

        weaknesses_json TEXT,
        resistances_json TEXT,

        trainer_type TEXT,
        energy_type TEXT,

        legal_standard INTEGER,
        legal_expanded INTEGER,

        boosters_json TEXT,

        tcgdex_updated TEXT,

        language TEXT NOT NULL,

        raw_json TEXT,

        FOREIGN KEY(set_id)
            REFERENCES sets(set_id)
    );


    CREATE TABLE IF NOT EXISTS card_variants (

        card_id TEXT NOT NULL,

        variant TEXT NOT NULL,

        available INTEGER NOT NULL,

        PRIMARY KEY (
            card_id,
            variant
        ),

        FOREIGN KEY(card_id)
            REFERENCES cards(card_id)
            ON DELETE CASCADE
    );


    CREATE INDEX IF NOT EXISTS
        idx_cards_set
    ON cards(set_id);


    CREATE INDEX IF NOT EXISTS
        idx_cards_name
    ON cards(name);


    CREATE INDEX IF NOT EXISTS
        idx_cards_rarity
    ON cards(rarity);


    CREATE INDEX IF NOT EXISTS
        idx_cards_local_id
    ON cards(local_id);
    """
)

conn.commit()


# ============================================================
# IMPORT DES SÉRIES
# ============================================================

print()
print("=" * 70)
print("IMPORT DES SÉRIES")
print("=" * 70)


series_list = api_get(
    "series"
)


for serie in series_list:

    series_id = serie.get(
        "id"
    )

    if (
        ONLY_SERIES is not None
        and series_id not in ONLY_SERIES
    ):
        continue


    cursor.execute(
        """
        INSERT INTO series (

            series_id,
            name,

            logo_base_url,
            logo_url,

            language,

            updated_at
        )

        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(series_id)
        DO UPDATE SET

            name =
                excluded.name,

            logo_base_url =
                excluded.logo_base_url,

            logo_url =
                excluded.logo_url,

            language =
                excluded.language,

            updated_at =
                excluded.updated_at
        """,
        (
            series_id,

            serie.get(
                "name"
            ),

            serie.get(
                "logo"
            ),

            asset_url(
                serie.get(
                    "logo"
                )
            ),

            LANGUAGE,

            datetime.now(
                timezone.utc
            ).isoformat(),
        )
    )


conn.commit()


series_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM series
    """
).fetchone()[0]


print(
    f"{series_count} séries enregistrées."
)


# ============================================================
# IMPORT DES SETS
# ============================================================

print()
print("=" * 70)
print("IMPORT DES SETS")
print("=" * 70)


sets_brief = api_get(
    "sets"
)


sets_to_import = []


for brief in sets_brief:

    set_id = brief.get(
        "id"
    )


    if (
        ONLY_SETS is not None
        and set_id not in ONLY_SETS
    ):
        continue


    try:

        data = api_get(
            f"sets/{set_id}"
        )

    except Exception as exc:

        print(
            f"ERREUR set {set_id}: "
            f"{exc}"
        )

        continue


    serie = (
        data.get(
            "serie"
        )
        or {}
    )


    series_id = serie.get(
        "id"
    )


    if (
        ONLY_SERIES is not None
        and series_id not in ONLY_SERIES
    ):
        continue


    counts = (
        data.get(
            "cardCount"
        )
        or {}
    )


    legal = (
        data.get(
            "legal"
        )
        or {}
    )


    cursor.execute(
        """
        INSERT INTO sets (

            set_id,

            name,

            series_id,

            release_date,

            card_count_total,
            card_count_official,
            card_count_normal,
            card_count_reverse,
            card_count_holo,
            card_count_first_edition,

            logo_base_url,
            logo_url,

            symbol_base_url,
            symbol_url,

            tcg_online_code,

            legal_standard,
            legal_expanded,

            boosters_json,

            language,

            updated_at,

            raw_json
        )

        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?
        )

        ON CONFLICT(set_id)
        DO UPDATE SET

            name =
                excluded.name,

            series_id =
                excluded.series_id,

            release_date =
                excluded.release_date,

            card_count_total =
                excluded.card_count_total,

            card_count_official =
                excluded.card_count_official,

            card_count_normal =
                excluded.card_count_normal,

            card_count_reverse =
                excluded.card_count_reverse,

            card_count_holo =
                excluded.card_count_holo,

            card_count_first_edition =
                excluded.card_count_first_edition,

            logo_base_url =
                excluded.logo_base_url,

            logo_url =
                excluded.logo_url,

            symbol_base_url =
                excluded.symbol_base_url,

            symbol_url =
                excluded.symbol_url,

            tcg_online_code =
                excluded.tcg_online_code,

            legal_standard =
                excluded.legal_standard,

            legal_expanded =
                excluded.legal_expanded,

            boosters_json =
                excluded.boosters_json,

            language =
                excluded.language,

            updated_at =
                excluded.updated_at,

            raw_json =
                excluded.raw_json
        """,

        (
            set_id,

            data.get(
                "name"
            ),

            series_id,

            data.get(
                "releaseDate"
            ),

            counts.get(
                "total"
            ),

            counts.get(
                "official"
            ),

            counts.get(
                "normal"
            ),

            counts.get(
                "reverse"
            ),

            counts.get(
                "holo"
            ),

            counts.get(
                "firstEd"
            ),

            data.get(
                "logo"
            ),

            asset_url(
                data.get(
                    "logo"
                )
            ),

            data.get(
                "symbol"
            ),

            asset_url(
                data.get(
                    "symbol"
                )
            ),

            data.get(
                "tcgOnline"
            ),

            bool_to_int(
                legal.get(
                    "standard"
                )
            ),

            bool_to_int(
                legal.get(
                    "expanded"
                )
            ),

            json_text(
                data.get(
                    "boosters"
                )
            ),

            LANGUAGE,

            datetime.now(
                timezone.utc
            ).isoformat(),

            json_text(
                data
            ),
        )
    )


    cards = (
        data.get(
            "cards"
        )
        or []
    )


    for card in cards:

        sets_to_import.append(
            (
                set_id,
                card.get(
                    "id"
                )
            )
        )


    conn.commit()


    print(
        f"{set_id:12} "
        f"{data.get('name')} "
        f"({len(cards)} cartes)"
    )


print()
print(
    f"{len(sets_to_import)} cartes "
    f"à récupérer."
)


# ============================================================
# TÉLÉCHARGEMENT DES CARTES
# ============================================================

def fetch_card(
    item
):

    set_id, card_id = item


    try:

        data = api_get(
            f"cards/{card_id}"
        )

        return (
            set_id,
            card_id,
            data,
            None
        )


    except Exception as exc:

        return (
            set_id,
            card_id,
            None,
            str(exc)
        )


# ============================================================
# ENREGISTREMENT D'UNE CARTE
# ============================================================

def save_card(
    set_id,
    card
):

    legal = (
        card.get(
            "legal"
        )
        or {}
    )


    image_base = card.get(
        "image"
    )


    cursor.execute(
        """
        INSERT INTO cards (

            card_id,

            local_id,

            set_id,

            name,

            category,
            rarity,

            illustrator,

            hp,

            stage,
            suffix,

            regulation_mark,

            evolve_from,
            description,

            retreat,

            image_base_url,
            image_high_url,
            image_low_url,

            types_json,
            dex_ids_json,

            abilities_json,
            attacks_json,

            weaknesses_json,
            resistances_json,

            trainer_type,
            energy_type,

            legal_standard,
            legal_expanded,

            boosters_json,

            tcgdex_updated,

            language,

            raw_json
        )

        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?
        )

        ON CONFLICT(card_id)
        DO UPDATE SET

            local_id =
                excluded.local_id,

            set_id =
                excluded.set_id,

            name =
                excluded.name,

            category =
                excluded.category,

            rarity =
                excluded.rarity,

            illustrator =
                excluded.illustrator,

            hp =
                excluded.hp,

            stage =
                excluded.stage,

            suffix =
                excluded.suffix,

            regulation_mark =
                excluded.regulation_mark,

            evolve_from =
                excluded.evolve_from,

            description =
                excluded.description,

            retreat =
                excluded.retreat,

            image_base_url =
                excluded.image_base_url,

            image_high_url =
                excluded.image_high_url,

            image_low_url =
                excluded.image_low_url,

            types_json =
                excluded.types_json,

            dex_ids_json =
                excluded.dex_ids_json,

            abilities_json =
                excluded.abilities_json,

            attacks_json =
                excluded.attacks_json,

            weaknesses_json =
                excluded.weaknesses_json,

            resistances_json =
                excluded.resistances_json,

            trainer_type =
                excluded.trainer_type,

            energy_type =
                excluded.energy_type,

            legal_standard =
                excluded.legal_standard,

            legal_expanded =
                excluded.legal_expanded,

            boosters_json =
                excluded.boosters_json,

            tcgdex_updated =
                excluded.tcgdex_updated,

            language =
                excluded.language,

            raw_json =
                excluded.raw_json
        """,

        (
            card.get(
                "id"
            ),

            str(
                card.get(
                    "localId",
                    ""
                )
            ),

            set_id,

            card.get(
                "name"
            ),

            card.get(
                "category"
            ),

            card.get(
                "rarity"
            ),

            card.get(
                "illustrator"
            ),

            card.get(
                "hp"
            ),

            card.get(
                "stage"
            ),

            card.get(
                "suffix"
            ),

            card.get(
                "regulationMark"
            ),

            card.get(
                "evolveFrom"
            ),

            card.get(
                "description"
            ),

            card.get(
                "retreat"
            ),

            image_base,

            image_url(
                image_base,
                "high",
                "webp"
            ),

            image_url(
                image_base,
                "low",
                "webp"
            ),

            json_text(
                card.get(
                    "types"
                )
            ),

            json_text(
                card.get(
                    "dexId"
                )
            ),

            json_text(
                card.get(
                    "abilities"
                )
            ),

            json_text(
                card.get(
                    "attacks"
                )
            ),

            json_text(
                card.get(
                    "weaknesses"
                )
            ),

            json_text(
                card.get(
                    "resistances"
                )
            ),

            card.get(
                "trainerType"
            ),

            card.get(
                "energyType"
            ),

            bool_to_int(
                legal.get(
                    "standard"
                )
            ),

            bool_to_int(
                legal.get(
                    "expanded"
                )
            ),

            json_text(
                card.get(
                    "boosters"
                )
            ),

            card.get(
                "updated"
            ),

            LANGUAGE,

            json_text(
                card
            ),
        )
    )


    # ========================================================
    # VARIANTES
    # ========================================================

    variants = (
        card.get(
            "variants"
        )
        or {}
    )


    cursor.execute(
        """
        DELETE FROM card_variants
        WHERE card_id = ?
        """,
        (
            card.get(
                "id"
            ),
        )
    )


    for variant, available in (
        variants.items()
    ):

        cursor.execute(
            """
            INSERT INTO card_variants (

                card_id,
                variant,
                available
            )

            VALUES (?, ?, ?)
            """,

            (
                card.get(
                    "id"
                ),

                variant,

                bool_to_int(
                    available
                ),
            )
        )


# ============================================================
# IMPORT MULTITHREAD
# ============================================================

print()
print("=" * 70)
print("IMPORT DES CARTES")
print("=" * 70)


success = 0
errors = 0


with ThreadPoolExecutor(
    max_workers=MAX_WORKERS
) as executor:

    futures = {

        executor.submit(
            fetch_card,
            item
        ):
            item

        for item in sets_to_import
    }


    for index, future in enumerate(
        as_completed(
            futures
        ),
        start=1
    ):

        (
            set_id,
            card_id,
            card,
            error
        ) = future.result()


        if error:

            errors += 1

            print(
                f"ERREUR {card_id}: "
                f"{error}"
            )

        else:

            try:

                save_card(
                    set_id,
                    card
                )

                success += 1

            except Exception as exc:

                errors += 1

                print(
                    f"ERREUR SQLite "
                    f"{card_id}: {exc}"
                )


        if index % 100 == 0:

            conn.commit()

            print(
                f"{index}/"
                f"{len(sets_to_import)} "
                f"| OK: {success} "
                f"| erreurs: {errors}"
            )


conn.commit()


# ============================================================
# RÉSUMÉ
# ============================================================

series_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM series
    """
).fetchone()[0]


set_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM sets
    """
).fetchone()[0]


card_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM cards
    """
).fetchone()[0]


variant_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM card_variants
    WHERE available = 1
    """
).fetchone()[0]


print()
print("=" * 70)
print("IMPORT TERMINÉ")
print("=" * 70)

print(
    f"Séries          : "
    f"{series_count}"
)

print(
    f"Sets            : "
    f"{set_count}"
)

print(
    f"Cartes          : "
    f"{card_count}"
)

print(
    f"Variantes       : "
    f"{variant_count}"
)

print(
    f"Erreurs         : "
    f"{errors}"
)

print(
    f"Base            : "
    f"{DB_PATH}"
)


conn.close()