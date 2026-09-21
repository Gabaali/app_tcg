import re
import sqlite3
import time
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import requests


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = "onepiece_tcg.sqlite"

BASE_URL = "https://onepiece.app/set/{}"

REQUEST_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/140 Safari/537.36"
    )
}


# ============================================================
# NORMALISATION DES SETS
# ============================================================

def normalize_set_code(label):
    """
    OP-01      -> OP01
    OP01       -> OP01
    EB-01      -> EB01
    PRB-01     -> PRB01
    OP14-EB04  -> OP14
    OP15-EB04  -> OP15

    Les Starter Deck ST ne sont pas traités comme boosters.
    """

    if not label:
        return None

    text = str(label).upper().strip()

    match = re.search(
        r"\b(OP|EB|PRB)\s*[-_]?\s*(\d{1,2})\b",
        text
    )

    if not match:
        return None

    prefix = match.group(1)
    number = int(match.group(2))

    return f"{prefix}{number:02d}"


# ============================================================
# OUTILS PANDAS
# ============================================================

def normalized(text):
    return (
        str(text)
        .replace("\xa0", " ")
        .strip()
        .lower()
    )


def flatten_columns(df):

    columns = []

    for col in df.columns:

        if isinstance(col, tuple):

            value = " ".join(
                str(x)
                for x in col
                if str(x).lower() != "nan"
            )

        else:
            value = str(col)

        columns.append(
            value.strip()
        )

    df.columns = columns

    return df


def find_column(df, wanted):

    target = normalized(wanted)

    for column in df.columns:

        if target in normalized(column):
            return column

    return None


def find_table(tables, required_columns):

    required = [
        normalized(x)
        for x in required_columns
    ]

    for original in tables:

        df = flatten_columns(
            original.copy()
        )

        columns = [
            normalized(c)
            for c in df.columns
        ]

        valid = True

        for requirement in required:

            if not any(
                requirement in column
                for column in columns
            ):
                valid = False
                break

        if valid:
            return df

    return None


# ============================================================
# PARSING DES DROP RATES
# ============================================================

def parse_expected_per_box(per_box, odds):
    """
    Exemples :

    7
        -> 7.0

    2
        -> 2.0

    <1 + "~1 in 48 boxes"
        -> 1 / 48

    "~5 per box"
        -> 5.0
    """

    per_box_text = str(
        per_box
    ).strip()

    odds_text = (
        str(odds)
        .lower()
        .replace(",", ".")
        .strip()
    )

    cleaned = (
        per_box_text
        .replace("~", "")
        .replace(",", ".")
        .strip()
    )

    # Valeur numérique directe
    try:
        return float(cleaned)

    except ValueError:
        pass


    # ~1 in 48 boxes
    match = re.search(
        r"1\s+in\s+([\d.]+)\s+box",
        odds_text
    )

    if match:

        boxes = float(
            match.group(1)
        )

        if boxes > 0:
            return 1.0 / boxes


    # ~5 per box
    match = re.search(
        r"([\d.]+)\s+per\s+box",
        odds_text
    )

    if match:

        return float(
            match.group(1)
        )


    return None


# ============================================================
# NORMALISATION DES NOMS DE CARTES
# ============================================================

def normalize_card_name(name):

    text = str(
        name or ""
    ).strip()

    # Enlève seulement un numéro terminal final :
    #
    # Monkey.D.Luffy (118)
    # ->
    # Monkey.D.Luffy
    #
    # mais conserve :
    #
    # Gol.D.Roger (Manga)

    text = re.sub(
        r"\s+\(\d+\)\s*$",
        "",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.lower().strip()


# ============================================================
# RECHERCHE D'UNE CLASSE DE DROP
# ============================================================

def exact_class(
    available_classes,
    *names
):

    wanted = {
        normalized(name)
        for name in names
    }

    for original in available_classes:

        if normalized(original) in wanted:
            return original

    return None


def contains_class(
    available_classes,
    *terms
):

    wanted = [
        normalized(term)
        for term in terms
    ]

    for original in available_classes:

        text = normalized(
            original
        )

        if all(
            term in text
            for term in wanted
        ):
            return original

    return None


# ============================================================
# CLASSIFICATION D'UNE CARTE
# ============================================================

def classify_terminal_card(
    set_code,
    available_classes,
    name,
    rarity,
    variant
):

    name_text = str(
        name or ""
    ).strip()

    name_lower = (
        name_text
        .lower()
        .strip()
    )

    normalized_name = normalize_card_name(
        name_text
    )

    rarity_text = (
        str(rarity or "")
        .upper()
        .strip()
    )

    variant_text = (
        str(variant or "")
        .upper()
        .strip()
    )


    # ========================================================
    # DON!!
    #
    # Tous les DON sont volontairement considérés comme
    # DON classiques pour notre base.
    #
    # Même si Terminal leur donne ALT/SP/MANGA dans sa
    # colonne variant, on ne les mélange PAS avec les slots
    # Manga/SP/Alt-art des cartes classiques.
    # ========================================================

    if rarity_text in {
        "DON!!",
        "DON"
    }:
        return "DON!!"


    # ========================================================
    # PROMO / PR
    #
    # On les classe, mais on ne leur donne pas le taux
    # Manga/SP/ALT d'un booster.
    # ========================================================

    if rarity_text in {
        "PR",
        "PROMO"
    }:

        if variant_text in {
            "ALT",
            "AA",
            "PARALLEL"
        }:
            return "Promo ALT"

        if variant_text == "SP":
            return "Promo SP"

        if variant_text == "MANGA":
            return "Promo MANGA"

        return "Promo"


    # ========================================================
    # CLASSE SPÉCIALE PORTANT LE NOM EXACT DE LA CARTE
    #
    # Exemple :
    #
    # Gol.D.Roger (Manga)
    #
    # peut avoir son propre taux distinct de Manga rare.
    # ========================================================

    for drop_class in available_classes:

        if (
            normalize_card_name(
                drop_class
            )
            == normalized_name
        ):
            return drop_class


    # ========================================================
    # RED SUPER ALTERNATE ART
    # ========================================================

    if (
        "red super alternate art"
        in name_lower
        or
        "red super alt-art"
        in name_lower
    ):

        result = exact_class(
            available_classes,
            "Red super alt-art",
            "Red super alternate art"
        )

        if result:
            return result

        result = contains_class(
            available_classes,
            "red",
            "super",
            "alt"
        )

        if result:
            return result


    # ========================================================
    # SUPER ALTERNATE ART
    #
    # IMPORTANT :
    # doit être testé après RED SUPER.
    # ========================================================

    if (
        "super alternate art"
        in name_lower
        or
        "super alt-art"
        in name_lower
    ):

        result = exact_class(
            available_classes,
            "Super alt-art",
            "Super alternate art"
        )

        if result:
            return result


        # Recherche souple sans prendre
        # "Red super alt-art".

        for drop_class in available_classes:

            lower = normalized(
                drop_class
            )

            if (
                "super" in lower
                and "alt" in lower
                and "red" not in lower
            ):
                return drop_class


    # ========================================================
    # SIGNATURE
    # ========================================================

    if "signature" in name_lower:

        result = contains_class(
            available_classes,
            "signature"
        )

        if result:
            return result


    # ========================================================
    # ANNIVERSARY / GOLD / SILVER
    # ========================================================

    if (
        "anniversary" in name_lower
        or "gold anniversary" in name_lower
        or "silver anniversary" in name_lower
    ):

        result = contains_class(
            available_classes,
            "anniversary"
        )

        if result:
            return result


    # ========================================================
    # TREASURE RARE
    # ========================================================

    if (
        rarity_text == "TR"
        or variant_text == "TR"
        or "treasure rare" in name_lower
    ):

        result = exact_class(
            available_classes,
            "Treasure rare",
            "Treasure Rare",
            "TR"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "treasure"
        )

        if result:
            return result


        # On sait malgré tout ce que c'est.
        return "Treasure Rare"


    # ========================================================
    # MANGA
    # ========================================================

    if (
        variant_text == "MANGA"
        or "manga" in name_lower
        or "comic parallel" in name_lower
    ):

        result = exact_class(
            available_classes,
            "Manga rare",
            "Manga / comic parallel",
            "Manga",
            "Comic parallel"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "manga"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "comic",
            "parallel"
        )

        if result:
            return result


        return "Manga"


    # ========================================================
    # SP / WANTED / ANIME ART
    # ========================================================

    if (
        variant_text == "SP"
        or "wanted poster" in name_lower
        or "anime art" in name_lower
    ):

        result = exact_class(
            available_classes,
            "SP / wanted poster",
            "SP / anime art",
            "SP"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "wanted"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "anime",
            "art"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "sp"
        )

        if result:
            return result


        return "SP"


    # ========================================================
    # ALT ART SECRET RARE
    # ========================================================

    if (
        variant_text in {
            "ALT",
            "AA",
            "PARALLEL"
        }
        and rarity_text == "SEC"
    ):

        result = exact_class(
            available_classes,
            "Alt-art SEC",
            "SEC parallel"
        )

        if result:
            return result


        result = contains_class(
            available_classes,
            "alt",
            "sec"
        )

        if result:
            return result


        return "Alt-art SEC"


    # ========================================================
    # AUTRES ALT ARTS
    # ========================================================

    if variant_text in {
        "ALT",
        "AA",
        "PARALLEL"
    }:

        result = exact_class(
            available_classes,
            "Alt-art (SR/L/R)",
            "Alt-art parallel",
            "Alt-art"
        )

        if result:
            return result


        # Recherche une Alt-art générique mais évite :
        #
        # Alt-art SEC
        # Super alt-art
        # Red super alt-art

        for drop_class in available_classes:

            lower = normalized(
                drop_class
            )

            if (
                (
                    "alt-art" in lower
                    or "alt art" in lower
                )
                and "sec" not in lower
                and "super" not in lower
                and "red" not in lower
            ):
                return drop_class


        return "Alt-art"


    # ========================================================
    # SECRET RARE
    # ========================================================

    if rarity_text == "SEC":

        result = exact_class(
            available_classes,
            "SEC",
            "Secret Rare"
        )

        if result:
            return result

        return "SEC"


    # ========================================================
    # SUPER RARE
    # ========================================================

    if rarity_text == "SR":

        result = exact_class(
            available_classes,
            "SR",
            "Super Rare"
        )

        if result:
            return result

        return "SR"


    # ========================================================
    # LEADER
    # ========================================================

    if rarity_text in {
        "L",
        "LEADER"
    }:

        result = exact_class(
            available_classes,
            "Leader"
        )

        if result:
            return result

        return "Leader"


    # ========================================================
    # RARE
    # ========================================================

    if rarity_text in {
        "R",
        "RARE"
    }:

        result = exact_class(
            available_classes,
            "Rare"
        )

        if result:
            return result

        return "Rare"


    # ========================================================
    # COMMON
    # ========================================================

    if rarity_text in {
        "C",
        "COMMON"
    }:

        return "Common"


    # ========================================================
    # UNCOMMON
    # ========================================================

    if rarity_text in {
        "UC",
        "UNCOMMON"
    }:

        return "Uncommon"


    # ========================================================
    # CAS INCONNU
    # ========================================================

    return None


# ============================================================
# OUVERTURE SQLITE
# ============================================================

print()
print(
    f"Ouverture de {DB_PATH}"
)

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


# ============================================================
# VÉRIFICATION DES TABLES
# ============================================================

tables = cursor.execute(
    """
    SELECT name
    FROM sqlite_master
    WHERE type = 'table'
    """
).fetchall()


table_names = {
    row[0]
    for row in tables
}


if "sets" not in table_names:

    raise RuntimeError(
        "La table 'sets' n'existe pas. "
        "Lance build_cards_db.py avant ce script."
    )


# ============================================================
# AJOUT DE sets.set_code
# ============================================================

set_columns = [
    row[1]
    for row in cursor.execute(
        "PRAGMA table_info(sets)"
    ).fetchall()
]


if "set_code" not in set_columns:

    print(
        "Ajout de sets.set_code..."
    )

    cursor.execute(
        """
        ALTER TABLE sets
        ADD COLUMN set_code TEXT
        """
    )

    conn.commit()


# ============================================================
# REMPLISSAGE DES SET CODES
# ============================================================

set_rows = cursor.execute(
    """
    SELECT
        pack_id,
        label
    FROM sets
    """
).fetchall()


for pack_id, label in set_rows:

    set_code = normalize_set_code(
        label
    )

    cursor.execute(
        """
        UPDATE sets
        SET set_code = ?
        WHERE pack_id = ?
        """,
        (
            set_code,
            pack_id
        )
    )


conn.commit()


# ============================================================
# TABLES DROP RATES
# ============================================================

cursor.executescript(
    """
    CREATE TABLE IF NOT EXISTS pull_rates (

        set_code TEXT NOT NULL,
        drop_class TEXT NOT NULL,

        per_box_text TEXT,
        odds_text TEXT,

        expected_per_box REAL,

        source_url TEXT,
        fetched_at TEXT,

        PRIMARY KEY (
            set_code,
            drop_class
        )
    );


    CREATE TABLE IF NOT EXISTS terminal_cards (

        terminal_id INTEGER PRIMARY KEY AUTOINCREMENT,

        product_set TEXT NOT NULL,

        card_number TEXT,
        name TEXT,

        rarity TEXT,
        variant TEXT,

        drop_class TEXT,

        UNIQUE (
            product_set,
            card_number,
            name,
            rarity,
            variant
        )
    );


    CREATE TABLE IF NOT EXISTS card_drop_estimates (

        terminal_id INTEGER PRIMARY KEY,

        product_set TEXT,

        card_number TEXT,
        name TEXT,

        rarity TEXT,
        variant TEXT,

        drop_class TEXT,

        cards_in_class INTEGER,

        class_expected_per_box REAL,

        expected_copies_per_box REAL,

        probability_per_box REAL,

        one_in_boxes REAL,

        model TEXT,

        FOREIGN KEY (
            terminal_id
        )
        REFERENCES terminal_cards(
            terminal_id
        )
    );


    CREATE INDEX IF NOT EXISTS
        idx_terminal_cards_number
    ON terminal_cards(
        card_number
    );


    CREATE INDEX IF NOT EXISTS
        idx_terminal_cards_product
    ON terminal_cards(
        product_set
    );


    CREATE INDEX IF NOT EXISTS
        idx_terminal_cards_class
    ON terminal_cards(
        drop_class
    );


    CREATE INDEX IF NOT EXISTS
        idx_drop_product
    ON card_drop_estimates(
        product_set
    );


    CREATE INDEX IF NOT EXISTS
        idx_drop_class
    ON card_drop_estimates(
        drop_class
    );
    """
)

conn.commit()


# ============================================================
# SETS À TRAITER
# ============================================================

set_codes = [
    row[0]
    for row in cursor.execute(
        """
        SELECT DISTINCT set_code

        FROM sets

        WHERE set_code IS NOT NULL

        ORDER BY set_code
        """
    ).fetchall()
]


print()
print(
    "Sets détectés :"
)

print(
    ", ".join(
        set_codes
    )
)

print()

print(
    f"{len(set_codes)} sets à traiter."
)


# ============================================================
# SESSION HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    HEADERS
)


successful_sets = []
failed_sets = []


# ============================================================
# IMPORT DES SETS
# ============================================================

for position, set_code in enumerate(
    set_codes,
    start=1
):

    url = BASE_URL.format(
        set_code
    )


    print()
    print(
        "=" * 70
    )

    print(
        f"[{position}/{len(set_codes)}] "
        f"{set_code}"
    )

    print(
        f"  {url}"
    )


    # ========================================================
    # TÉLÉCHARGEMENT
    # ========================================================

    try:

        response = session.get(
            url,
            timeout=30
        )


        if response.status_code == 404:

            print(
                "  Page inexistante."
            )

            failed_sets.append(
                set_code
            )

            continue


        response.raise_for_status()


        html_tables = pd.read_html(
            StringIO(
                response.text
            )
        )


    except Exception as exc:

        print(
            "  ERREUR téléchargement :",
            exc
        )

        failed_sets.append(
            set_code
        )

        continue


    # ========================================================
    # TABLEAU DES PULL RATES
    # ========================================================

    rate_table = find_table(
        html_tables,
        [
            "rarity / class",
            "per box",
            "odds"
        ]
    )


    if rate_table is None:

        print(
            "  Tableau pull rates introuvable."
        )

        failed_sets.append(
            set_code
        )

        continue


    # ========================================================
    # TABLEAU DES CARTES
    # ========================================================

    cards_table = find_table(
        html_tables,
        [
            "number",
            "name",
            "rarity",
            "variant"
        ]
    )


    if cards_table is None:

        print(
            "  Tableau cartes introuvable."
        )

        failed_sets.append(
            set_code
        )

        continue


    # ========================================================
    # EXTRACTION DES TAUX
    # ========================================================

    class_column = find_column(
        rate_table,
        "rarity / class"
    )

    per_box_column = find_column(
        rate_table,
        "per box"
    )

    odds_column = find_column(
        rate_table,
        "odds"
    )


    rate_rows = []

    available_classes = []


    for _, row in rate_table.iterrows():

        drop_class = str(
            row[class_column]
        ).strip()


        if (
            not drop_class
            or drop_class.lower() == "nan"
        ):
            continue


        per_box = str(
            row[per_box_column]
        ).strip()


        odds = str(
            row[odds_column]
        ).strip()


        expected = parse_expected_per_box(
            per_box,
            odds
        )


        rate_rows.append(
            (
                drop_class,
                per_box,
                odds,
                expected
            )
        )


        available_classes.append(
            drop_class
        )


    print(
        "  Classes :"
    )


    for (
        drop_class,
        per_box,
        odds,
        expected
    ) in rate_rows:

        print(
            f"    {drop_class}: {odds}"
        )


    # ========================================================
    # EXTRACTION DES CARTES
    # ========================================================

    number_column = find_column(
        cards_table,
        "number"
    )

    name_column = find_column(
        cards_table,
        "name"
    )

    rarity_column = find_column(
        cards_table,
        "rarity"
    )

    variant_column = find_column(
        cards_table,
        "variant"
    )


    card_rows = []


    for _, row in cards_table.iterrows():

        number = str(
            row[number_column]
        ).strip()


        name = str(
            row[name_column]
        ).strip()


        rarity = str(
            row[rarity_column]
        ).strip()


        variant = str(
            row[variant_column]
        ).strip()


        if number.lower() == "nan":
            continue


        if name.lower() == "nan":
            name = ""


        if rarity.lower() == "nan":
            rarity = ""


        if variant.lower() in {
            "nan",
            "none",
            "null",
            "-",
            "—"
        }:
            variant = ""


        drop_class = classify_terminal_card(
            set_code,
            available_classes,
            name,
            rarity,
            variant
        )


        card_rows.append(
            (
                number,
                name,
                rarity,
                variant,
                drop_class
            )
        )


    # ========================================================
    # MISE À JOUR SQLITE DU SET
    # ========================================================

    try:

        with conn:

            # -----------------------------------------------
            # Enfants avant parents à cause de FOREIGN KEY
            # -----------------------------------------------

            cursor.execute(
                """
                DELETE FROM card_drop_estimates

                WHERE terminal_id IN (

                    SELECT terminal_id

                    FROM terminal_cards

                    WHERE product_set = ?
                )
                """,
                (
                    set_code,
                )
            )


            cursor.execute(
                """
                DELETE FROM terminal_cards

                WHERE product_set = ?
                """,
                (
                    set_code,
                )
            )


            cursor.execute(
                """
                DELETE FROM pull_rates

                WHERE set_code = ?
                """,
                (
                    set_code,
                )
            )


            # -----------------------------------------------
            # Pull rates
            # -----------------------------------------------

            for (
                drop_class,
                per_box,
                odds,
                expected
            ) in rate_rows:

                cursor.execute(
                    """
                    INSERT INTO pull_rates (

                        set_code,
                        drop_class,

                        per_box_text,
                        odds_text,

                        expected_per_box,

                        source_url,
                        fetched_at
                    )

                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,

                    (
                        set_code,
                        drop_class,

                        per_box,
                        odds,

                        expected,

                        url,

                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )
                )


            # -----------------------------------------------
            # Cartes Terminal
            # -----------------------------------------------

            for (
                number,
                name,
                rarity,
                variant,
                drop_class
            ) in card_rows:

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO terminal_cards (

                        product_set,

                        card_number,
                        name,

                        rarity,
                        variant,

                        drop_class
                    )

                    VALUES (?, ?, ?, ?, ?, ?)
                    """,

                    (
                        set_code,

                        number,
                        name,

                        rarity,
                        variant,

                        drop_class
                    )
                )


    except Exception as exc:

        print(
            "  ERREUR SQLite :",
            exc
        )

        failed_sets.append(
            set_code
        )

        continue


    # ========================================================
    # STATISTIQUES DU SET
    # ========================================================

    actual_count = cursor.execute(
        """
        SELECT COUNT(*)

        FROM terminal_cards

        WHERE product_set = ?
        """,
        (
            set_code,
        )
    ).fetchone()[0]


    classified_count = cursor.execute(
        """
        SELECT COUNT(*)

        FROM terminal_cards

        WHERE
            product_set = ?
            AND drop_class IS NOT NULL
        """,
        (
            set_code,
        )
    ).fetchone()[0]


    print(
        f"  Cartes Terminal : "
        f"{actual_count}"
    )


    print(
        f"  Classées        : "
        f"{classified_count}"
    )


    print(
        f"  Non classées    : "
        f"{actual_count - classified_count}"
    )


    successful_sets.append(
        set_code
    )


    time.sleep(
        REQUEST_DELAY
    )


# ============================================================
# CALCUL DES TAUX PAR CARTE
# ============================================================

print()
print(
    "=" * 70
)

print(
    "CALCUL DES PROBABILITÉS PAR CARTE"
)

print(
    "=" * 70
)


for set_code in successful_sets:

    # ========================================================
    # NETTOYAGE DES ESTIMATIONS DU SET
    # ========================================================

    with conn:

        cursor.execute(
            """
            DELETE FROM card_drop_estimates

            WHERE terminal_id IN (

                SELECT terminal_id

                FROM terminal_cards

                WHERE product_set = ?
            )
            """,
            (
                set_code,
            )
        )


    # ========================================================
    # CLASSES AVEC TAUX DISPONIBLE
    # ========================================================

    classes = cursor.execute(
        """
        SELECT

            drop_class,
            expected_per_box

        FROM pull_rates

        WHERE
            set_code = ?
            AND expected_per_box IS NOT NULL
        """,
        (
            set_code,
        )
    ).fetchall()


    for (
        drop_class,
        expected_per_box
    ) in classes:


        cards = cursor.execute(
            """
            SELECT

                terminal_id,
                card_number,
                name,
                rarity,
                variant

            FROM terminal_cards

            WHERE
                product_set = ?
                AND drop_class = ?
            """,

            (
                set_code,
                drop_class
            )
        ).fetchall()


        number_of_cards = len(
            cards
        )


        if number_of_cards == 0:
            continue


        # ====================================================
        # ESPÉRANCE PAR CARTE
        #
        # Hypothèse :
        # distribution uniforme dans la classe.
        # ====================================================

        expected_card = (
            expected_per_box
            / number_of_cards
        )


        # ====================================================
        # PROBABILITÉ D'AU MOINS UNE COPIE DANS UNE BOX
        # ====================================================

        if expected_per_box <= 1:

            probability = (
                expected_card
            )


        else:

            probability = (
                1
                -
                (
                    1
                    -
                    (
                        1
                        / number_of_cards
                    )
                )
                ** expected_per_box
            )


        if probability > 0:

            one_in_boxes = (
                1
                / probability
            )

        else:

            one_in_boxes = None


        # ====================================================
        # ENREGISTREMENT
        # ====================================================

        with conn:

            for (
                terminal_id,
                number,
                name,
                rarity,
                variant
            ) in cards:


                cursor.execute(
                    """
                    INSERT OR REPLACE
                    INTO card_drop_estimates (

                        terminal_id,

                        product_set,

                        card_number,
                        name,

                        rarity,
                        variant,

                        drop_class,

                        cards_in_class,

                        class_expected_per_box,

                        expected_copies_per_box,

                        probability_per_box,

                        one_in_boxes,

                        model
                    )

                    VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,

                    (
                        terminal_id,

                        set_code,

                        number,
                        name,

                        rarity,
                        variant,

                        drop_class,

                        number_of_cards,

                        expected_per_box,

                        expected_card,

                        probability,

                        one_in_boxes,

                        (
                            "equal_distribution_"
                            "within_drop_class"
                        )
                    )
                )


# ============================================================
# STATISTIQUES GLOBALES
# ============================================================

pull_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM pull_rates
    """
).fetchone()[0]


terminal_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM terminal_cards
    """
).fetchone()[0]


estimated_count = cursor.execute(
    """
    SELECT COUNT(*)
    FROM card_drop_estimates
    """
).fetchone()[0]


unclassified_count = cursor.execute(
    """
    SELECT COUNT(*)

    FROM terminal_cards

    WHERE drop_class IS NULL
    """
).fetchone()[0]


classified_without_rate = cursor.execute(
    """
    SELECT COUNT(*)

    FROM terminal_cards t

    LEFT JOIN card_drop_estimates e
        ON e.terminal_id = t.terminal_id

    WHERE
        t.drop_class IS NOT NULL
        AND e.terminal_id IS NULL
    """
).fetchone()[0]


print()
print(
    "=" * 70
)

print(
    "IMPORT DES DROP RATES TERMINÉ"
)

print(
    "=" * 70
)


print(
    f"Sets traités          : "
    f"{len(successful_sets)}"
)

print(
    f"Sets échoués          : "
    f"{len(failed_sets)}"
)

print(
    f"Classes de drop       : "
    f"{pull_count}"
)

print(
    f"Cartes Terminal       : "
    f"{terminal_count}"
)

print(
    f"Estimations           : "
    f"{estimated_count}"
)

print(
    f"Classées sans taux    : "
    f"{classified_without_rate}"
)

print(
    f"Non classées          : "
    f"{unclassified_count}"
)


# ============================================================
# NON CLASSÉES
# ============================================================

print()
print(
    "Répartition des cartes non classées :"
)


unclassified_rows = cursor.execute(
    """
    SELECT

        rarity,
        variant,
        COUNT(*) AS nombre

    FROM terminal_cards

    WHERE drop_class IS NULL

    GROUP BY
        rarity,
        variant

    ORDER BY
        nombre DESC
    """
).fetchall()


if not unclassified_rows:

    print(
        "  Aucune carte non classée."
    )


else:

    for (
        rarity,
        variant,
        count
    ) in unclassified_rows:

        variant_display = (
            variant
            if variant
            else "-"
        )

        print(
            f"  {rarity:10} | "
            f"{variant_display:12} | "
            f"{count}"
        )


# ============================================================
# CLASSÉES MAIS SANS DROP RATE
# ============================================================

print()
print(
    "Cartes classées sans taux :"
)


without_rate_rows = cursor.execute(
    """
    SELECT

        t.rarity,
        t.variant,
        t.drop_class,

        COUNT(*) AS nombre

    FROM terminal_cards t

    LEFT JOIN card_drop_estimates e
        ON e.terminal_id = t.terminal_id

    WHERE
        t.drop_class IS NOT NULL
        AND e.terminal_id IS NULL

    GROUP BY
        t.rarity,
        t.variant,
        t.drop_class

    ORDER BY
        nombre DESC
    """
).fetchall()


for (
    rarity,
    variant,
    drop_class,
    count
) in without_rate_rows:

    variant_display = (
        variant
        if variant
        else "-"
    )

    print(
        f"  {rarity:10} | "
        f"{variant_display:10} | "
        f"{drop_class:20} | "
        f"{count}"
    )


# ============================================================
# TREASURE RARE
# ============================================================

print()
print(
    "Treasure Rare :"
)


treasure_rows = cursor.execute(
    """
    SELECT

        product_set,
        card_number,
        name,

        drop_class,

        ROUND(
            probability_per_box * 100,
            4
        ),

        ROUND(
            one_in_boxes,
            1
        )

    FROM card_drop_estimates

    WHERE rarity = 'TR'

    ORDER BY product_set
    """
).fetchall()


if treasure_rows:

    for row in treasure_rows:

        print(
            "  "
            +
            " | ".join(
                str(value)
                for value in row
            )
        )


else:

    print(
        "  Aucune Treasure Rare estimée."
    )


# ============================================================
# HITS LES PLUS RARES
# ============================================================

print()
print(
    "Exemples de hits rares :"
)


examples = cursor.execute(
    """
    SELECT

        product_set,
        card_number,
        name,

        rarity,
        variant,

        drop_class,

        ROUND(
            probability_per_box * 100,
            4
        ) AS percent,

        ROUND(
            one_in_boxes,
            1
        ) AS one_in_boxes

    FROM card_drop_estimates

    WHERE
        probability_per_box IS NOT NULL
        AND probability_per_box < 0.10

    ORDER BY
        one_in_boxes DESC

    LIMIT 30
    """
).fetchall()


for row in examples:

    print(
        "  "
        +
        " | ".join(
            str(value)
            for value in row
        )
    )


# ============================================================
# SETS ÉCHOUÉS
# ============================================================

if failed_sets:

    print()
    print(
        "Sets non mis à jour :"
    )

    print(
        "  "
        +
        ", ".join(
            failed_sets
        )
    )


# ============================================================
# FIN
# ============================================================

conn.close()


print()
print(
    f"Base mise à jour : {DB_PATH}"
)