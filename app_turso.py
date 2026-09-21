import base64
import hashlib
import html
import hmac
import json
import math
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections.abc import Mapping

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    import turso_serverless
except ImportError:
    turso_serverless = None


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

# Base locale de secours pour le développement. En production, les données
# joueurs sont stockées dans Turso si les secrets TURSO_* sont configurés.
APP_DB_PATH = ROOT / "onepiece_tcg.sqlite"

# "auto"  : Turso si les deux secrets sont présents, sinon SQLite local.
# "turso" : exige Turso (recommandé lorsque l'app est publiée).
# "local" : force l'ancien stockage SQLite local.
APP_DATABASE_MODE = "auto"
TRIVIA_DB_PATH = ROOT / "trivia_questions.sqlite"
LOL_TRIVIA_DB_PATH = ROOT / "lol_trivia_questions.sqlite"

TRIVIA_DATABASES = {
    "general": TRIVIA_DB_PATH,
    "lol": LOL_TRIVIA_DB_PATH,
}

TRIVIA_LABELS = {
    "general": "Culture générale",
    "lol": "League of Legends",
}

GAME_DB_PATHS = {
    "onepiece": ROOT / "onepiece_tcg.sqlite",
    "pokemon": ROOT / "pokemon_tcg.sqlite",
    "riftbound": ROOT / "riftbound_tcg.sqlite",
}

GAME_LABELS = {
    "onepiece": "One Piece",
    "pokemon": "Pokémon",
    "riftbound": "Riftbound",
}

ASSET_DIR = ROOT / "assets"

# One Piece
OP_PACKS_PER_BOX = 24
OP_COMMON_SLOTS = 6
OP_MIDDLE_SLOTS = 3
OP_CARDS_PER_PACK = 12

# Pokémon moderne (Scarlet & Violet / Mega Evolution)
# 10 cartes de jeu + 1 énergie de base. La carte code n'est pas simulée.
POKEMON_GAME_CARDS_PER_PACK = 10
POKEMON_DISPLAYED_CARDS_PER_PACK = 11
POKEMON_STANDARD_BOX_PACKS = 36

# Riftbound
RIFTBOUND_CARDS_PER_PACK = 14
RIFTBOUND_PACKS_PER_BOX = 24

# Économie du jeu
# La monnaie est entièrement interne au jeu : aucune conversion en euros.
STARTING_BALANCE_COINS = 5000
ALLOW_TEST_TOPUPS = True       # Passe à False pour masquer les outils DEV

# Mini-jeu : générateur passif de pièces
GENERATOR_INTERVAL_SECONDS = 5
GENERATOR_BASE_RATE = 1
GENERATOR_UPGRADED_RATE = 5
GENERATOR_UPGRADE_COST = 1000
GENERATOR_MAX_LEVEL = 1

# Mini-jeu : machine à sous (monnaie interne uniquement)
# 7 symboles équiprobables. Avec la table de gains ci-dessous, le retour
# théorique au joueur est d'environ 83,38 % (avantage maison ~16,62 %).
SLOT_SYMBOLS = ("7", "BAR", "◆", "★", "●", "♣", "♠")
SLOT_MIN_BET = 1
SLOT_PAIR_MULTIPLIER = 2
SLOT_TRIPLE_MULTIPLIERS = {
    "7": 10,
    "BAR": 6,
    "◆": 5,
    "★": 4,
    "●": 3,
    "♣": 3,
    "♠": 3,
}

# Mini-jeu : culture générale
TRIVIA_QUESTION_COUNT = 10
TRIVIA_COINS_PER_CORRECT = 25

# Prix internes par défaut des boosters.
# Les prix déjà enregistrés dans SQLite restent prioritaires.
DEFAULT_BOOSTER_PRICES = {
    "onepiece": {"coins": 599},
    "pokemon": {"coins": 599},
    "riftbound": {"coins": 533},
}

# Overrides facultatifs par extension.
BOOSTER_PRICE_OVERRIDES = {
    # Exemple : ("onepiece", "OP01"): 900,
    # Exemple : ("pokemon", "sv3.5"): 750,
    # Exemple : ("riftbound", "OGN"): 650,
}

# OGS / Proving Grounds est un starter, pas un booster. Les sets futurs ne
# sont proposés qu'à partir de leur date de sortie globale.
RIFTBOUND_RELEASE_DATES = {
    "OGN": "2025-10-31",
    "SFD": "2026-02-13",
    "UNL": "2026-05-08",
    "VEN": "2026-07-31",
    "RAD": "2026-10-23",
}

st.set_page_config(
    page_title="TCG Booster Simulator",
    layout="wide",
)


# ============================================================
# OUTILS GENERAUX
# ============================================================


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def strip_accents(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def clean_text(value):
    return strip_accents(value).lower().strip()


def as_bool(value):
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def resolve_local_or_url(value, db_path):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None

    value = str(value).strip()
    if not value:
        return None

    if value.startswith(("http://", "https://", "data:image/")):
        return value

    path = Path(value)
    if not path.is_absolute():
        path = Path(db_path).resolve().parent / path

    if path.is_file():
        return str(path)

    return None


def connect_db(path):
    """Connexion SQLite locale pour les bases statiques de cartes/quiz."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


class DbRow(Mapping):
    """Petit équivalent de sqlite3.Row pour le driver Turso distant.

    L'application historique utilise à la fois row[0], row["name"] et
    dict(row). Cette classe conserve ces trois comportements sans obliger à
    réécrire toutes les requêtes existantes.
    """

    def __init__(self, columns, values):
        self._columns = tuple(str(col) for col in columns)
        self._values = tuple(values)
        self._index = {name: i for i, name in enumerate(self._columns)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[str(key)]]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return repr(dict(self))


class TursoCursorAdapter:
    """Adapte le curseur DB-API Turso au comportement attendu par l'app."""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return getattr(self._cursor, "description", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    def _column_names(self):
        description = self.description or ()
        names = []
        for col in description:
            if isinstance(col, (tuple, list)):
                names.append(col[0])
            else:
                names.append(getattr(col, "name", str(col)))
        return names

    def _wrap_row(self, row):
        if row is None or isinstance(row, Mapping):
            return row
        columns = self._column_names()
        if not columns:
            return row
        return DbRow(columns, row)

    def execute(self, sql, params=()):
        self._cursor.execute(sql, params or ())
        return self

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(sql, seq_of_params)
        return self

    def fetchone(self):
        return self._wrap_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap_row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [self._wrap_row(row) for row in rows]

    def close(self):
        close = getattr(self._cursor, "close", None)
        if close:
            return close()

    def __iter__(self):
        for row in self._cursor:
            yield self._wrap_row(row)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class TursoConnectionAdapter:
    """Connexion Turso compatible avec le code SQLite historique de l'app."""

    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return TursoCursorAdapter(self._connection.cursor())

    def execute(self, sql, params=()):
        cursor = self.cursor()
        return cursor.execute(sql, params)

    def executemany(self, sql, seq_of_params):
        cursor = self.cursor()
        return cursor.executemany(sql, seq_of_params)

    def executescript(self, script):
        """Exécute un script SQL multi-instructions sur le driver distant.

        sqlite3.complete_statement évite de couper naïvement une instruction
        lorsqu'un point-virgule apparaît dans une chaîne SQL.
        """
        buffer = ""
        last_cursor = None
        for line in str(script).splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                buffer = ""
                if not statement:
                    continue
                if statement.endswith(";"):
                    statement = statement[:-1].rstrip()
                if statement:
                    last_cursor = self.execute(statement)
        if buffer.strip():
            last_cursor = self.execute(buffer.strip())
        return last_cursor

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        rollback = getattr(self._connection, "rollback", None)
        if rollback:
            return rollback()

    def close(self):
        return self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._connection, name)



def dataframe_from_cursor(cursor):
    """Convertit explicitement un curseur SQLite/Turso en DataFrame.

    Important : DbRow implémente Mapping pour rester compatible avec
    row["colonne"] et dict(row). Pandas peut sinon interpréter l'itération
    d'un DbRow comme la liste des NOMS de colonnes, et remplir le DataFrame
    avec "quantity", "card_key", etc. au lieu des vraies valeurs.
    """
    description = getattr(cursor, "description", None) or ()
    columns = []
    for col in description:
        if isinstance(col, (tuple, list)):
            columns.append(str(col[0]))
        else:
            columns.append(str(getattr(col, "name", col)))

    rows = cursor.fetchall()

    if not columns:
        return pd.DataFrame()

    data = []
    for row in rows:
        if isinstance(row, DbRow):
            data.append([row[i] for i in range(len(columns))])
        elif isinstance(row, Mapping):
            data.append([row.get(col) for col in columns])
        else:
            data.append(list(row))

    return pd.DataFrame(data, columns=columns)


def read_app_dataframe(query, params=()):
    """Lecture DataFrame depuis la base mutable de l'application.

    On n'utilise volontairement pas pd.read_sql_query avec l'adaptateur
    Turso : Pandas ne connaît pas ce wrapper DB-API et peut interpréter
    DbRow de manière incorrecte.
    """
    with connect_app() as conn:
        cursor = conn.execute(query, params or ())
        return dataframe_from_cursor(cursor)


def _read_secret(name):
    """Lit d'abord st.secrets, puis les variables d'environnement."""
    try:
        value = st.secrets.get(name, "")
        if value:
            return str(value).strip()
    except (FileNotFoundError, KeyError):
        pass
    except Exception:
        # Permet aussi d'importer le module hors de `streamlit run`.
        pass
    return str(os.environ.get(name, "") or "").strip()


def turso_credentials():
    return _read_secret("TURSO_DATABASE_URL"), _read_secret("TURSO_AUTH_TOKEN")


def app_uses_turso():
    if APP_DATABASE_MODE == "local":
        return False
    url, token = turso_credentials()
    if APP_DATABASE_MODE == "turso":
        return True
    return bool(url and token)


def connect_app():
    """Connexion aux données MUTABLES de l'application.

    En production : Turso.
    En développement sans secrets : SQLite local, sauf si
    APP_DATABASE_MODE == "turso".
    """
    if APP_DATABASE_MODE == "local":
        return connect_db(APP_DB_PATH)

    url, token = turso_credentials()
    has_url = bool(url)
    has_token = bool(token)

    if has_url != has_token:
        raise RuntimeError(
            "Configuration Turso incomplète : TURSO_DATABASE_URL et "
            "TURSO_AUTH_TOKEN doivent être fournis ensemble."
        )

    if has_url and has_token:
        if turso_serverless is None:
            raise RuntimeError(
                "Le paquet turso_serverless n'est pas installé. "
                "Ajoute turso_serverless dans requirements.txt."
            )
        raw = turso_serverless.connect(url, auth_token=token)
        conn = TursoConnectionAdapter(raw)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            # Certains moteurs distants gèrent les FK côté serveur sans
            # accepter ce PRAGMA par connexion.
            pass
        return conn

    if APP_DATABASE_MODE == "turso":
        raise RuntimeError(
            "Turso est requis mais les secrets sont absents. Crée "
            ".streamlit/secrets.toml en local ou configure les Secrets "
            "dans Streamlit Community Cloud."
        )

    return connect_db(APP_DB_PATH)

def connect_trivia(quiz_key="general"):
    path = TRIVIA_DATABASES.get(str(quiz_key), TRIVIA_DB_PATH)
    return connect_db(path)


def connect_game(game):
    return connect_db(GAME_DB_PATHS[game])


def table_exists(conn, table_name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def table_columns(conn, table_name):
    if not table_exists(conn, table_name):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_column(conn, table_name, column_name, declaration):
    if column_name not in table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
        )


def stable_hash(*parts):
    raw = "\x1f".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================================================
# QUESTIONS DE CULTURE GÉNÉRALE
# ============================================================





# ============================================================
# BASE CULTURE GÉNÉRALE SÉPARÉE
# ============================================================


def _sqlite_relation_exists(conn, relation_name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (str(relation_name),),
    ).fetchone() is not None


def validate_trivia_database(quiz_key="general"):
    """Vérifie la base externe correspondant au quiz demandé."""
    quiz_key = str(quiz_key or "general")
    path = TRIVIA_DATABASES.get(quiz_key)
    if path is None:
        return False, "Catégorie de quiz inconnue."
    if not path.is_file():
        return False, f"Base de questions introuvable : {path.name}"

    try:
        with connect_trivia(quiz_key) as conn:
            if quiz_key == "lol":
                relation = "lol_questions"
                if not _sqlite_relation_exists(conn, relation):
                    return False, "La table lol_questions est absente."
                count = conn.execute(
                    "SELECT COUNT(*) FROM lol_questions WHERE active = 1"
                ).fetchone()[0]
            else:
                relation = "trivia_questions"
                if not _sqlite_relation_exists(conn, relation):
                    return False, "La table trivia_questions est absente."
                count = conn.execute(
                    "SELECT COUNT(*) FROM trivia_questions WHERE active = 1"
                ).fetchone()[0]

            if int(count or 0) < TRIVIA_QUESTION_COUNT:
                return False, f"La base ne contient que {int(count or 0)} question(s) active(s)."
    except sqlite3.DatabaseError as exc:
        return False, f"Base de questions invalide : {exc}"
    return True, ""


# ============================================================
# TABLES APPLICATION + MIGRATION DE L'ANCIENNE APP
# ============================================================


@st.cache_resource(show_spinner=False)
def init_app_tables():
    with connect_app() as conn:
        # WAL améliore la coexistence des lectures fréquentes et des petites
        # écritures quand deux navigateurs jouent sur la même base locale.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except Exception:
            pass
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_collection (
                user_id INTEGER NOT NULL,
                card_key TEXT NOT NULL,
                game TEXT NOT NULL DEFAULT 'onepiece',
                product_set TEXT NOT NULL,
                card_number TEXT NOT NULL,
                name TEXT NOT NULL,
                rarity TEXT,
                variant TEXT,
                drop_class TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                first_obtained_at TEXT NOT NULL,
                last_obtained_at TEXT NOT NULL,
                PRIMARY KEY (user_id, card_key),
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pack_openings (
                opening_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL DEFAULT 'onepiece',
                set_code TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS opening_cards (
                opening_id INTEGER NOT NULL,
                card_key TEXT NOT NULL,
                FOREIGN KEY(opening_id)
                    REFERENCES pack_openings(opening_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                balance_coins INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS wallet_transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_coins INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                game TEXT,
                set_code TEXT,
                opening_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(opening_id)
                    REFERENCES pack_openings(opening_id)
                    ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS coin_generators (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 0,
                coins_per_tick INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL DEFAULT 5,
                last_tick_at TEXT NOT NULL,
                total_generated INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS slot_spins (
                spin_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stake_coins INTEGER NOT NULL,
                reel1 TEXT NOT NULL,
                reel2 TEXT NOT NULL,
                reel3 TEXT NOT NULL,
                multiplier INTEGER NOT NULL DEFAULT 0,
                payout_coins INTEGER NOT NULL DEFAULT 0,
                net_coins INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trivia_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_ids_json TEXT NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                reward_coins INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trivia_answers (
                answer_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                selected_index INTEGER NOT NULL CHECK(selected_index BETWEEN 0 AND 3),
                is_correct INTEGER NOT NULL DEFAULT 0,
                answered_at TEXT NOT NULL,
                UNIQUE(run_id, question_id),
                FOREIGN KEY(run_id) REFERENCES trivia_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS booster_prices (
                game TEXT NOT NULL,
                set_code TEXT NOT NULL,
                price_coins INTEGER NOT NULL,
                source_label TEXT,
                source_url TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (game, set_code)
            );

            CREATE TABLE IF NOT EXISTS decks (
                deck_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                game TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS deck_cards (
                deck_id INTEGER NOT NULL,
                card_key TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                product_set TEXT NOT NULL,
                card_number TEXT,
                name TEXT NOT NULL,
                rarity TEXT,
                variant TEXT,
                PRIMARY KEY (deck_id, card_key),
                FOREIGN KEY(deck_id)
                    REFERENCES decks(deck_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS friend_requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(sender_id, receiver_id),
                CHECK(sender_id <> receiver_id),
                FOREIGN KEY(sender_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(receiver_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS friendships (
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user1_id, user2_id),
                CHECK(user1_id < user2_id),
                FOREIGN KEY(user1_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(user2_id)
                    REFERENCES app_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS game_invites (
                invite_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                sender_deck_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                match_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(sender_id <> receiver_id),
                FOREIGN KEY(sender_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(receiver_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(sender_deck_id) REFERENCES decks(deck_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS multiplayer_matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                game TEXT NOT NULL,
                player1_id INTEGER NOT NULL,
                player2_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(player1_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(player2_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS match_players (
                match_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                deck_id INTEGER NOT NULL,
                draw_pile_json TEXT NOT NULL,
                hand_json TEXT NOT NULL,
                discard_json TEXT NOT NULL,
                PRIMARY KEY (match_id, user_id),
                FOREIGN KEY(match_id) REFERENCES multiplayer_matches(match_id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(deck_id) REFERENCES decks(deck_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS match_board_cards (
                match_id INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                card_json TEXT NOT NULL,
                x REAL NOT NULL DEFAULT 0.5,
                y REAL NOT NULL DEFAULT 0.5,
                z_index INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (match_id, instance_id),
                FOREIGN KEY(match_id) REFERENCES multiplayer_matches(match_id) ON DELETE CASCADE,
                FOREIGN KEY(owner_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );
            """
        )

        # Migration non destructive depuis l'app One Piece précédente.
        ensure_column(
            conn,
            "user_collection",
            "game",
            "TEXT NOT NULL DEFAULT 'onepiece'",
        )
        ensure_column(
            conn,
            "pack_openings",
            "game",
            "TEXT NOT NULL DEFAULT 'onepiece'",
        )
        ensure_column(conn, "pack_openings", "price_coins", "INTEGER")
        ensure_column(conn, "pack_openings", "balance_after", "INTEGER")
        # Les parties de quiz indiquent désormais explicitement la base utilisée.
        # Les anciennes parties deviennent automatiquement des parties de culture générale.
        ensure_column(
            conn,
            "trivia_runs",
            "quiz_key",
            "TEXT NOT NULL DEFAULT 'general'",
        )
        # Pour les questions à réponse libre (League of Legends), on conserve
        # la réponse saisie. selected_index reste utilisé pour les QCM.
        ensure_column(conn, "trivia_answers", "answer_text", "TEXT")

        conn.execute(
            "UPDATE user_collection SET game='onepiece' WHERE game IS NULL OR TRIM(game)=''"
        )
        conn.execute(
            "UPDATE pack_openings SET game='onepiece' WHERE game IS NULL OR TRIM(game)=''"
        )

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_collection_user
                ON user_collection(user_id);

            CREATE INDEX IF NOT EXISTS idx_collection_game_set
                ON user_collection(user_id, game, product_set);

            CREATE INDEX IF NOT EXISTS idx_openings_user_game
                ON pack_openings(user_id, game, set_code);

            CREATE INDEX IF NOT EXISTS idx_wallet_transactions_user
                ON wallet_transactions(user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_coin_generators_level
                ON coin_generators(level);

            CREATE INDEX IF NOT EXISTS idx_slot_spins_user
                ON slot_spins(user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_trivia_runs_user
                ON trivia_runs(user_id, status, created_at);

            CREATE INDEX IF NOT EXISTS idx_trivia_answers_run
                ON trivia_answers(run_id, question_id);

            CREATE INDEX IF NOT EXISTS idx_decks_user_game
                ON decks(user_id, game);

            CREATE INDEX IF NOT EXISTS idx_deck_cards_deck
                ON deck_cards(deck_id);

            CREATE INDEX IF NOT EXISTS idx_friend_requests_receiver
                ON friend_requests(receiver_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_friend_requests_sender
                ON friend_requests(sender_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_friendships_user1
                ON friendships(user1_id);

            CREATE INDEX IF NOT EXISTS idx_friendships_user2
                ON friendships(user2_id);


            CREATE INDEX IF NOT EXISTS idx_game_invites_receiver
                ON game_invites(receiver_id, status, created_at);

            CREATE INDEX IF NOT EXISTS idx_game_invites_sender
                ON game_invites(sender_id, status, created_at);

            CREATE INDEX IF NOT EXISTS idx_multiplayer_matches_player1
                ON multiplayer_matches(player1_id, status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_multiplayer_matches_player2
                ON multiplayer_matches(player2_id, status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_match_board_cards_match
                ON match_board_cards(match_id, owner_id, z_index);
            """
        )


        # Donne le solde de départ une seule fois aux comptes existants qui
        # n'ont pas encore de portefeuille.
        users = conn.execute("SELECT user_id FROM app_users").fetchall()
        for row in users:
            user_id = int(row[0])
            wallet = conn.execute(
                "SELECT 1 FROM user_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if wallet is None:
                now = now_iso()
                conn.execute(
                    """
                    INSERT INTO user_wallets (user_id, balance_coins, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, STARTING_BALANCE_COINS, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO wallet_transactions (
                        user_id, amount_coins, transaction_type, note, created_at
                    ) VALUES (?, ?, 'starter', ?, ?)
                    """,
                    (
                        user_id,
                        STARTING_BALANCE_COINS,
                        "Solde de départ",
                        now,
                    ),
                )

        # Initialise le générateur passif pour les comptes existants.
        generator_now = now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO coin_generators (
                user_id, level, coins_per_tick, interval_seconds,
                last_tick_at, total_generated, updated_at
            )
            SELECT
                user_id, 0, ?, ?, ?, 0, ?
            FROM app_users
            """,
            (
                GENERATOR_BASE_RATE,
                GENERATOR_INTERVAL_SECONDS,
                generator_now,
                generator_now,
            ),
        )


# ============================================================
# UTILISATEURS
# ============================================================


def hash_password(password, salt_hex=None):
    if salt_hex is None:
        salt = secrets.token_bytes(16)
    else:
        salt = bytes.fromhex(salt_hex)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200_000,
    )
    return salt.hex(), digest.hex()


def create_user(username, password):
    username = username.strip()
    if len(username) < 2:
        return False, "Nom trop court."
    if len(password) < 3:
        return False, "Mot de passe trop court."

    salt, password_hash = hash_password(password)
    try:
        with connect_app() as conn:
            now = now_iso()
            cursor = conn.execute(
                """
                INSERT INTO app_users (
                    username, password_salt, password_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (username, salt, password_hash, now),
            )
            user_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO user_wallets (
                    user_id, balance_coins, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (user_id, STARTING_BALANCE_COINS, now, now),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, amount_coins, transaction_type, note, created_at
                ) VALUES (?, ?, 'starter', ?, ?)
                """,
                (user_id, STARTING_BALANCE_COINS, "Solde de départ", now),
            )
            conn.execute(
                """
                INSERT INTO coin_generators (
                    user_id, level, coins_per_tick, interval_seconds,
                    last_tick_at, total_generated, updated_at
                ) VALUES (?, 0, ?, ?, ?, 0, ?)
                """,
                (
                    user_id,
                    GENERATOR_BASE_RATE,
                    GENERATOR_INTERVAL_SECONDS,
                    now,
                    now,
                ),
            )
        return True, f"Compte créé avec {format_coins(STARTING_BALANCE_COINS)}."
    except sqlite3.IntegrityError:
        return False, "Ce nom existe déjà."
    except Exception as exc:
        # Le driver Turso n'utilise pas nécessairement la classe
        # sqlite3.IntegrityError, mais renvoie bien l'erreur de contrainte.
        message = str(exc).lower()
        if "unique" in message or "constraint" in message or "integrity" in message:
            return False, "Ce nom existe déjà."
        raise


def authenticate_user(username, password):
    with connect_app() as conn:
        user = conn.execute(
            "SELECT * FROM app_users WHERE username = ?",
            (username.strip(),),
        ).fetchone()

    if user is None:
        return None

    _, digest = hash_password(password, user["password_salt"])
    if not hmac.compare_digest(digest, user["password_hash"]):
        return None

    return {"user_id": user["user_id"], "username": user["username"]}


# ============================================================
# AMIS
# ============================================================


def _friend_pair(user_a, user_b):
    """Retourne toujours la paire d'IDs dans le même ordre."""
    user_a = int(user_a)
    user_b = int(user_b)
    return (min(user_a, user_b), max(user_a, user_b))


def find_user_by_username(username):
    """Recherche exacte d'un compte, sans tenir compte de la casse."""
    username = str(username or "").strip()
    if not username:
        return None
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT user_id, username
            FROM app_users
            WHERE username = ? COLLATE NOCASE
            """,
            (username,),
        ).fetchone()
    return dict(row) if row else None


def are_friends(user_a, user_b):
    user1_id, user2_id = _friend_pair(user_a, user_b)
    if user1_id == user2_id:
        return False
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM friendships
            WHERE user1_id = ? AND user2_id = ?
            """,
            (user1_id, user2_id),
        ).fetchone()
    return row is not None


def send_friend_request(sender_id, username):
    """Envoie une demande d'ami à partir du pseudo exact."""
    target = find_user_by_username(username)
    if target is None:
        return False, "Aucun compte ne porte ce pseudo."

    sender_id = int(sender_id)
    receiver_id = int(target["user_id"])
    if sender_id == receiver_id:
        return False, "Tu ne peux pas t'ajouter toi-même."

    user1_id, user2_id = _friend_pair(sender_id, receiver_id)
    with connect_app() as conn:
        if conn.execute(
            """
            SELECT 1 FROM friendships
            WHERE user1_id = ? AND user2_id = ?
            """,
            (user1_id, user2_id),
        ).fetchone():
            return False, f"{target['username']} est déjà dans tes amis."

        same = conn.execute(
            """
            SELECT 1 FROM friend_requests
            WHERE sender_id = ? AND receiver_id = ?
            """,
            (sender_id, receiver_id),
        ).fetchone()
        if same:
            return False, "Une demande est déjà en attente."

        reverse = conn.execute(
            """
            SELECT request_id FROM friend_requests
            WHERE sender_id = ? AND receiver_id = ?
            """,
            (receiver_id, sender_id),
        ).fetchone()
        if reverse:
            return False, (
                f"{target['username']} t'a déjà envoyé une demande. "
                "Tu peux l'accepter dans les demandes reçues."
            )

        conn.execute(
            """
            INSERT INTO friend_requests (sender_id, receiver_id, created_at)
            VALUES (?, ?, ?)
            """,
            (sender_id, receiver_id, now_iso()),
        )

    return True, f"Demande envoyée à {target['username']}."


def list_incoming_friend_requests(user_id):
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                fr.request_id,
                fr.sender_id,
                u.username,
                fr.created_at
            FROM friend_requests fr
            JOIN app_users u ON u.user_id = fr.sender_id
            WHERE fr.receiver_id = ?
            ORDER BY fr.created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_outgoing_friend_requests(user_id):
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                fr.request_id,
                fr.receiver_id,
                u.username,
                fr.created_at
            FROM friend_requests fr
            JOIN app_users u ON u.user_id = fr.receiver_id
            WHERE fr.sender_id = ?
            ORDER BY fr.created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def accept_friend_request(user_id, request_id):
    """Accepte uniquement une demande réellement adressée à user_id."""
    user_id = int(user_id)
    request_id = int(request_id)
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT sender_id, receiver_id
            FROM friend_requests
            WHERE request_id = ? AND receiver_id = ?
            """,
            (request_id, user_id),
        ).fetchone()
        if row is None:
            return False, "Cette demande n'existe plus."

        sender_id = int(row["sender_id"])
        user1_id, user2_id = _friend_pair(sender_id, user_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO friendships (user1_id, user2_id, created_at)
            VALUES (?, ?, ?)
            """,
            (user1_id, user2_id, now_iso()),
        )
        # Une fois amis, toute demande croisée entre les deux comptes disparaît.
        conn.execute(
            """
            DELETE FROM friend_requests
            WHERE (sender_id = ? AND receiver_id = ?)
               OR (sender_id = ? AND receiver_id = ?)
            """,
            (sender_id, user_id, user_id, sender_id),
        )
    return True, "Demande acceptée."


def decline_friend_request(user_id, request_id):
    with connect_app() as conn:
        cur = conn.execute(
            """
            DELETE FROM friend_requests
            WHERE request_id = ? AND receiver_id = ?
            """,
            (int(request_id), int(user_id)),
        )
    return cur.rowcount > 0


def cancel_friend_request(user_id, request_id):
    with connect_app() as conn:
        cur = conn.execute(
            """
            DELETE FROM friend_requests
            WHERE request_id = ? AND sender_id = ?
            """,
            (int(request_id), int(user_id)),
        )
    return cur.rowcount > 0


def list_friends(user_id):
    user_id = int(user_id)
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                u.user_id,
                u.username,
                f.created_at
            FROM friendships f
            JOIN app_users u
              ON u.user_id = CASE
                    WHEN f.user1_id = ? THEN f.user2_id
                    ELSE f.user1_id
                 END
            WHERE f.user1_id = ? OR f.user2_id = ?
            ORDER BY u.username COLLATE NOCASE
            """,
            (user_id, user_id, user_id),
        ).fetchall()
    return [dict(row) for row in rows]


def remove_friend(user_id, friend_id):
    user1_id, user2_id = _friend_pair(user_id, friend_id)
    if user1_id == user2_id:
        return False
    with connect_app() as conn:
        cur = conn.execute(
            """
            DELETE FROM friendships
            WHERE user1_id = ? AND user2_id = ?
            """,
            (user1_id, user2_id),
        )
    return cur.rowcount > 0


# ============================================================
# MONNAIE / PORTEFEUILLE / PRIX DES BOOSTERS
# ============================================================


def format_coins(coins, show_eur=False):
    """Affiche uniquement la monnaie interne du jeu.

    ``show_eur`` est conservé dans la signature pour compatibilité avec les
    anciens appels, mais n'a volontairement plus aucun effet.
    """
    coins = int(coins or 0)
    return f"{coins:,}".replace(",", " ") + " 🪙"


def get_wallet_balance(user_id):
    with connect_app() as conn:
        row = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            now = now_iso()
            conn.execute(
                """
                INSERT INTO user_wallets (user_id, balance_coins, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, STARTING_BALANCE_COINS, now, now),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, amount_coins, transaction_type, note, created_at
                ) VALUES (?, ?, 'starter', ?, ?)
                """,
                (user_id, STARTING_BALANCE_COINS, "Solde de départ", now),
            )
            return STARTING_BALANCE_COINS
        return int(row["balance_coins"])


def add_wallet_coins(user_id, amount_coins, transaction_type="test_topup", note=None):
    amount_coins = int(amount_coins)
    if amount_coins <= 0:
        return get_wallet_balance(user_id)

    now = now_iso()
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            current = 0
            conn.execute(
                """
                INSERT INTO user_wallets (user_id, balance_coins, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                """,
                (user_id, now, now),
            )
        else:
            current = int(row["balance_coins"])

        new_balance = current + amount_coins
        conn.execute(
            "UPDATE user_wallets SET balance_coins = ?, updated_at = ? WHERE user_id = ?",
            (new_balance, now, user_id),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, amount_coins, transaction_type, note, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, amount_coins, transaction_type, note or "Crédit", now),
        )
    return new_balance



def slot_multiplier(reels):
    """Retourne le multiplicateur d'un tirage à trois rouleaux."""
    a, b, c = reels

    if a == b == c:
        return SLOT_TRIPLE_MULTIPLIERS.get(a, 3)

    if a == b or a == c or b == c:
        return SLOT_PAIR_MULTIPLIER

    return 0


def play_slot_machine(user_id, stake_coins):
    """Joue un tour et met à jour le portefeuille dans une transaction SQLite."""
    try:
        stake_coins = int(stake_coins)
    except (TypeError, ValueError):
        return False, "Mise invalide.", None

    if stake_coins < SLOT_MIN_BET:
        return False, f"Mise minimale : {format_coins(SLOT_MIN_BET)}.", None
    sync_coin_generator(user_id)

    reels = tuple(secrets.choice(SLOT_SYMBOLS) for _ in range(3))
    multiplier = slot_multiplier(reels)
    payout = stake_coins * multiplier
    net = payout - stake_coins
    now = now_iso()

    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        wallet = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        balance = int(wallet["balance_coins"]) if wallet else 0

        if balance < stake_coins:
            return False, "Solde insuffisant.", None

        new_balance = balance + net
        conn.execute(
            "UPDATE user_wallets SET balance_coins = ?, updated_at = ? WHERE user_id = ?",
            (new_balance, now, user_id),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, amount_coins, transaction_type, note, created_at
            ) VALUES (?, ?, 'slot_spin', ?, ?)
            """,
            (
                user_id,
                net,
                f"Machine à sous · mise {stake_coins} · x{multiplier}",
                now,
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO slot_spins (
                user_id, stake_coins, reel1, reel2, reel3,
                multiplier, payout_coins, net_coins, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                stake_coins,
                reels[0],
                reels[1],
                reels[2],
                multiplier,
                payout,
                net,
                now,
            ),
        )
        spin_id = int(cursor.lastrowid)

    return True, "", {
        "spin_id": spin_id,
        "reels": reels,
        "stake": stake_coins,
        "multiplier": multiplier,
        "payout": payout,
        "net": net,
        "balance": new_balance,
    }


def _parse_utc(value):
    """Parse une date ISO SQLite et garantit un datetime UTC."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_generator_locked(conn, user_id, now):
    """Crée le générateur et le portefeuille si nécessaire.

    La fonction suppose que ``conn`` est déjà dans la transaction courante.
    """
    wallet = conn.execute(
        "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if wallet is None:
        now_text = now.isoformat()
        conn.execute(
            """
            INSERT INTO user_wallets (user_id, balance_coins, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, STARTING_BALANCE_COINS, now_text, now_text),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, amount_coins, transaction_type, note, created_at
            ) VALUES (?, ?, 'starter', ?, ?)
            """,
            (user_id, STARTING_BALANCE_COINS, "Solde de départ", now_text),
        )

    row = conn.execute(
        "SELECT * FROM coin_generators WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        now_text = now.isoformat()
        conn.execute(
            """
            INSERT INTO coin_generators (
                user_id, level, coins_per_tick, interval_seconds,
                last_tick_at, total_generated, updated_at
            ) VALUES (?, 0, ?, ?, ?, 0, ?)
            """,
            (
                user_id,
                GENERATOR_BASE_RATE,
                GENERATOR_INTERVAL_SECONDS,
                now_text,
                now_text,
            ),
        )
        row = conn.execute(
            "SELECT * FROM coin_generators WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row


def sync_coin_generator(user_id):
    """Crédite tous les ticks passifs écoulés depuis la dernière synchronisation.

    Le calcul est fait côté serveur à partir de timestamps SQLite : fermer la page
    n'arrête donc pas le générateur. Les secondes restantes d'un intervalle sont
    conservées afin d'éviter toute dérive du timer.
    """
    now = datetime.now(timezone.utc)

    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_generator_locked(conn, user_id, now)

        rate = max(0, int(row["coins_per_tick"]))
        interval = max(1, int(row["interval_seconds"]))
        last_tick = _parse_utc(row["last_tick_at"])
        elapsed = max(0.0, (now - last_tick).total_seconds())
        completed_ticks = int(elapsed // interval)
        earned = completed_ticks * rate

        if completed_ticks > 0:
            new_last_tick = last_tick + timedelta(
                seconds=completed_ticks * interval
            )
            conn.execute(
                """
                UPDATE user_wallets
                SET balance_coins = balance_coins + ?, updated_at = ?
                WHERE user_id = ?
                """,
                (earned, now.isoformat(), user_id),
            )
            conn.execute(
                """
                UPDATE coin_generators
                SET last_tick_at = ?,
                    total_generated = total_generated + ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    new_last_tick.isoformat(),
                    earned,
                    now.isoformat(),
                    user_id,
                ),
            )
            last_tick = new_last_tick
            elapsed = max(0.0, (now - last_tick).total_seconds())

        refreshed = conn.execute(
            "SELECT * FROM coin_generators WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        wallet = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        remainder = elapsed % interval
        seconds_to_next = interval - remainder
        if seconds_to_next <= 0.001:
            seconds_to_next = float(interval)

        return {
            "level": int(refreshed["level"]),
            "coins_per_tick": int(refreshed["coins_per_tick"]),
            "interval_seconds": int(refreshed["interval_seconds"]),
            "total_generated": int(refreshed["total_generated"]),
            "balance": int(wallet["balance_coins"]),
            "seconds_to_next": float(seconds_to_next),
            "earned_now": int(earned),
        }


def buy_generator_upgrade(user_id):
    """Achète l'unique amélioration actuelle : 1 -> 5 pièces / 5 s."""
    # Synchronise d'abord les gains déjà acquis au taux précédent.
    sync_coin_generator(user_id)
    now = datetime.now(timezone.utc)

    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_generator_locked(conn, user_id, now)
        level = int(row["level"])

        if level >= GENERATOR_MAX_LEVEL:
            wallet = conn.execute(
                "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return (
                False,
                "Amélioration déjà achetée.",
                {
                    "level": level,
                    "coins_per_tick": int(row["coins_per_tick"]),
                    "interval_seconds": int(row["interval_seconds"]),
                    "total_generated": int(row["total_generated"]),
                    "balance": int(wallet["balance_coins"]),
                    "seconds_to_next": float(row["interval_seconds"]),
                    "earned_now": 0,
                },
            )

        wallet = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        balance = int(wallet["balance_coins"])

        if balance < GENERATOR_UPGRADE_COST:
            missing = GENERATOR_UPGRADE_COST - balance
            return (
                False,
                f"Il te manque {format_coins(missing)}.",
                {
                    "level": level,
                    "coins_per_tick": int(row["coins_per_tick"]),
                    "interval_seconds": int(row["interval_seconds"]),
                    "total_generated": int(row["total_generated"]),
                    "balance": balance,
                    "seconds_to_next": float(row["interval_seconds"]),
                    "earned_now": 0,
                },
            )

        new_balance = balance - GENERATOR_UPGRADE_COST
        now_text = now.isoformat()
        conn.execute(
            """
            UPDATE user_wallets
            SET balance_coins = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (new_balance, now_text, user_id),
        )
        conn.execute(
            """
            UPDATE coin_generators
            SET level = 1,
                coins_per_tick = ?,
                interval_seconds = ?,
                last_tick_at = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                GENERATOR_UPGRADED_RATE,
                GENERATOR_INTERVAL_SECONDS,
                now_text,
                now_text,
                user_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, amount_coins, transaction_type, note, created_at
            ) VALUES (?, ?, 'generator_upgrade', ?, ?)
            """,
            (
                user_id,
                -GENERATOR_UPGRADE_COST,
                f"Amélioration générateur : {GENERATOR_UPGRADED_RATE} pièces / {GENERATOR_INTERVAL_SECONDS} s",
                now_text,
            ),
        )

    return True, "Générateur amélioré !", sync_coin_generator(user_id)


def default_booster_price(game, set_code):
    base = DEFAULT_BOOSTER_PRICES[game]
    coins = int(BOOSTER_PRICE_OVERRIDES.get((game, str(set_code)), base["coins"]))
    return {
        "price_coins": coins,
        "source_label": None,
        "source_url": None,
    }


def get_booster_price(game, set_code):
    set_code = str(set_code)
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT price_coins, source_label, source_url, updated_at
            FROM booster_prices
            WHERE game = ? AND set_code = ?
            """,
            (game, set_code),
        ).fetchone()
        if row is not None:
            return dict(row)

        default = default_booster_price(game, set_code)
        now = now_iso()
        conn.execute(
            """
            INSERT INTO booster_prices (
                game, set_code, price_coins, source_label, source_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                game,
                set_code,
                default["price_coins"],
                default["source_label"],
                default["source_url"],
                now,
            ),
        )
        return {**default, "updated_at": now}


def set_booster_price(game, set_code, price_coins, source_label=None):
    price_coins = max(1, int(price_coins))
    with connect_app() as conn:
        conn.execute(
            """
            INSERT INTO booster_prices (
                game, set_code, price_coins, source_label, source_url, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            ON CONFLICT(game, set_code) DO UPDATE SET
                price_coins = excluded.price_coins,
                source_label = excluded.source_label,
                source_url = NULL,
                updated_at = excluded.updated_at
            """,
            (game, str(set_code), price_coins, None, now_iso()),
        )


# ============================================================
# IDENTIFIANTS STABLES DE COLLECTION
# ============================================================


def legacy_onepiece_card_key(card):
    """Conserve exactement la clé de l'ancienne app One Piece."""
    return stable_hash(
        card.get("product_set", ""),
        card.get("card_number", ""),
        card.get("name", ""),
        card.get("rarity", ""),
        card.get("variant", ""),
    )


def make_card_key(game, card):
    if game == "onepiece":
        return legacy_onepiece_card_key(card)

    # Pokémon : le finish reverse/holo n'entre volontairement pas dans la clé.
    # Le Cartedex compte la carte TCGdex, pas chaque finition comme une carte
    # distincte. Riftbound possède déjà une ligne distincte pour les traitements
    # spéciaux dans la base importée.
    source_id = card.get("source_id") or card.get("card_id") or card.get("card_uid")
    return stable_hash(game, source_id)


# ============================================================
# ONE PIECE - CHARGEMENT DES DONNEES
# ============================================================


@st.cache_data(ttl=60)
def load_op_set_catalog():
    with connect_game("onepiece") as conn:
        sets = pd.read_sql_query(
            """
            SELECT DISTINCT product_set AS set_code
            FROM terminal_cards
            WHERE product_set IS NOT NULL
            ORDER BY product_set
            """,
            conn,
        )

        if table_exists(conn, "sets"):
            cols = table_columns(conn, "sets")
            title_expr = "set_code"
            if "title" in cols and "raw_title" in cols:
                title_expr = "COALESCE(title, raw_title, set_code)"
            elif "title" in cols:
                title_expr = "COALESCE(title, set_code)"

            names = pd.read_sql_query(
                f"""
                SELECT set_code, MAX({title_expr}) AS set_name
                FROM sets
                WHERE set_code IS NOT NULL
                GROUP BY set_code
                """,
                conn,
            )
        else:
            names = pd.DataFrame(columns=["set_code", "set_name"])

    result = sets.merge(names, on="set_code", how="left")
    result["set_name"] = result["set_name"].fillna(result["set_code"])
    return result


@st.cache_data(ttl=60)
def load_op_raw_set_data(set_code):
    with connect_game("onepiece") as conn:
        cards = pd.read_sql_query(
            """
            SELECT
                t.terminal_id,
                t.product_set,
                t.card_number,
                t.name,
                t.rarity,
                COALESCE(t.variant, '') AS variant,
                t.drop_class,
                e.probability_per_box,
                e.one_in_boxes
            FROM terminal_cards t
            LEFT JOIN card_drop_estimates e
                ON e.terminal_id = t.terminal_id
            WHERE t.product_set = ?
            """,
            conn,
            params=(set_code,),
        )

        rates = pd.read_sql_query(
            """
            SELECT drop_class, expected_per_box, odds_text
            FROM pull_rates
            WHERE set_code = ?
              AND expected_per_box IS NOT NULL
              AND expected_per_box > 0
            """,
            conn,
            params=(set_code,),
        )

    return cards, rates


def op_variant_number(value):
    match = re.search(r"(\d+)", str(value or ""))
    return int(match.group(1)) if match else 9999


def op_is_special_card(row):
    variant = str(row.get("variant", "") or "").upper()
    rarity = str(row.get("rarity", "") or "").upper()
    drop_class = str(row.get("drop_class", "") or "").lower()

    if variant in {"ALT", "AA", "MANGA", "SP", "TR", "PARALLEL"}:
        return True
    if rarity == "TR":
        return True
    return any(
        word in drop_class
        for word in (
            "manga",
            "alt",
            "treasure",
            "signature",
            "super",
            "wanted",
            "anniversary",
        )
    )


@st.cache_data(ttl=60)
def build_op_image_map(set_code):
    db_path = GAME_DB_PATHS["onepiece"]
    cards, _ = load_op_raw_set_data(set_code)
    if cards.empty:
        return {}

    mapping = {}

    # DON!! téléchargés séparément dans l'app actuelle.
    manifest_path = ASSET_DIR / "don_images.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for _, card in cards.iterrows():
                if str(card.get("rarity") or "") != "DON!!":
                    continue
                entry = manifest.get(legacy_onepiece_card_key(card))
                if entry:
                    local_path = ROOT / entry.get("local_path", "")
                    mapping[int(card["terminal_id"])] = (
                        str(local_path)
                        if local_path.is_file()
                        else entry.get("image_url")
                    )
        except (OSError, ValueError, TypeError):
            pass

    numbers = sorted(set(cards["card_number"].dropna().astype(str)))
    if not numbers:
        return mapping

    with connect_game("onepiece") as conn:
        if not table_exists(conn, "cards"):
            return mapping

        cols = table_columns(conn, "cards")
        image_cols = [
            name
            for name in ("clean_image_path", "local_image_path", "image_url")
            if name in cols
        ]
        if not image_cols:
            return mapping

        # Overrides exacts de l'app actuelle.
        if table_exists(conn, "card_image_overrides"):
            keys = {
                legacy_onepiece_card_key(card): int(card["terminal_id"])
                for _, card in cards.iterrows()
            }
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = conn.execute(
                    f"""
                    SELECT card_key, local_image_path
                    FROM card_image_overrides
                    WHERE card_key IN ({placeholders})
                    """,
                    list(keys),
                ).fetchall()
                for card_key, local_image_path in rows:
                    source = resolve_local_or_url(local_image_path, db_path)
                    if source:
                        mapping[keys[card_key]] = source

        placeholders = ",".join("?" for _ in numbers)
        selected = [
            col
            for col in (
                "card_uid",
                "print_id",
                "base_id",
                "variant_suffix",
                "variant_family",
            )
            if col in cols
        ] + image_cols

        query = f"""
            SELECT {', '.join(selected)}
            FROM cards
            WHERE base_id IN ({placeholders})
        """
        local = pd.read_sql_query(query, conn, params=numbers)

    if local.empty:
        return mapping

    def image_from_row(row):
        for col in image_cols:
            source = resolve_local_or_url(row.get(col), db_path)
            if source:
                return source
        return None

    for card_number, terminal_group in cards.groupby("card_number"):
        candidates = local[local["base_id"] == card_number].copy()
        if candidates.empty:
            continue

        if "variant_suffix" not in candidates:
            candidates["variant_suffix"] = None
        if "variant_family" not in candidates:
            candidates["variant_family"] = None
        if "print_id" not in candidates:
            candidates["print_id"] = candidates.index.astype(str)

        candidates["_variant_n"] = candidates["variant_suffix"].map(op_variant_number)
        candidates = candidates.sort_values(["_variant_n", "print_id"])

        base_candidates = candidates[
            (candidates["variant_family"].fillna("").str.lower() == "base")
            | candidates["variant_suffix"].isna()
            | (candidates["variant_suffix"].fillna("") == "")
        ]
        parallel_candidates = candidates.drop(base_candidates.index, errors="ignore")

        terminal_base = []
        terminal_special = []
        for _, row in terminal_group.sort_values("terminal_id").iterrows():
            (terminal_special if op_is_special_card(row) else terminal_base).append(row)

        base_source = image_from_row(
            base_candidates.iloc[0] if not base_candidates.empty else candidates.iloc[0]
        )
        for row in terminal_base:
            tid = int(row["terminal_id"])
            if tid not in mapping:
                mapping[tid] = base_source

        if terminal_special:
            source_rows = (
                [row for _, row in parallel_candidates.iterrows()]
                if not parallel_candidates.empty
                else [row for _, row in candidates.iterrows()]
            )
            for index, row in enumerate(terminal_special):
                tid = int(row["terminal_id"])
                if tid in mapping:
                    continue
                candidate = source_rows[min(index, len(source_rows) - 1)]
                mapping[tid] = image_from_row(candidate)

    return mapping


@st.cache_data(ttl=60)
def load_op_cards(set_code):
    cards, rates = load_op_raw_set_data(set_code)
    image_map = build_op_image_map(set_code)

    if cards.empty:
        return cards, rates

    cards = cards.copy()
    cards["game"] = "onepiece"
    cards["source_id"] = cards["terminal_id"].astype(str)
    cards["set_name"] = set_code
    cards["image"] = cards["terminal_id"].map(image_map)
    cards["collectible"] = True
    cards["card_key"] = cards.apply(
        lambda row: make_card_key("onepiece", row), axis=1
    )
    return cards, rates


# ============================================================
# ONE PIECE - SIMULATION (LOGIQUE CONSERVEE)
# ============================================================


def draw_op_from_pool(pool, used_ids):
    if pool is None or pool.empty:
        return None

    available = pool[~pool["terminal_id"].isin(used_ids)]
    if available.empty:
        available = pool

    weights = available["probability_per_box"].fillna(0).astype(float)
    if weights.sum() > 0:
        index = random.choices(
            available.index.tolist(), weights=weights.tolist(), k=1
        )[0]
    else:
        index = random.choice(available.index.tolist())

    card = available.loc[index].to_dict()
    used_ids.add(int(card["terminal_id"]))
    return card


def simulate_op_pack(cards, rates):
    used_ids = set()
    pack = []

    pools = {
        name: group.reset_index(drop=True)
        for name, group in cards.dropna(subset=["drop_class"]).groupby("drop_class")
    }

    common_pool = pools.get("Common", pd.DataFrame())
    uncommon_pool = pools.get("Uncommon", pd.DataFrame())
    leader_pool = pools.get("Leader", pd.DataFrame())
    don_pool = pools.get("DON!!", pd.DataFrame())

    for _ in range(OP_COMMON_SLOTS):
        card = draw_op_from_pool(common_pool, used_ids)
        if card:
            card["_slot"] = "Common"
            pack.append(card)

    leader_rate = 0.0
    if not rates.empty:
        leader_rows = rates[
            rates["drop_class"].fillna("").str.lower() == "leader"
        ]
        if not leader_rows.empty:
            leader_rate = float(leader_rows["expected_per_box"].sum())

    has_leader = (
        not leader_pool.empty
        and random.random() < min(1.0, leader_rate / OP_PACKS_PER_BOX)
    )

    middle_cards = []
    if has_leader:
        card = draw_op_from_pool(leader_pool, used_ids)
        if card:
            card["_slot"] = "Leader"
            middle_cards.append(card)

    for _ in range(OP_MIDDLE_SLOTS - len(middle_cards)):
        card = draw_op_from_pool(uncommon_pool, used_ids)
        if card is None:
            card = draw_op_from_pool(common_pool, used_ids)
        if card:
            card["_slot"] = "Uncommon"
            middle_cards.append(card)
    pack.extend(middle_cards)

    card = draw_op_from_pool(don_pool, used_ids)
    if card is None:
        card = draw_op_from_pool(common_pool, used_ids)
    if card:
        card["_slot"] = "DON!!"
        pack.append(card)

    excluded = {
        "Common",
        "Uncommon",
        "Leader",
        "DON!!",
        "Promo",
        "Promo ALT",
        "Promo SP",
        "Promo MANGA",
    }

    high_rates = []
    for _, rate in rates.iterrows():
        drop_class = str(rate["drop_class"])
        expected = float(rate["expected_per_box"])
        if drop_class in excluded:
            continue
        pool = pools.get(drop_class)
        if pool is None or pool.empty:
            continue
        high_rates.append([drop_class, expected])

    total_expected = sum(row[1] for row in high_rates)
    rare_index = next(
        (i for i, row in enumerate(high_rates) if row[0].lower() == "rare"),
        None,
    )

    if total_expected < OP_PACKS_PER_BOX:
        missing = OP_PACKS_PER_BOX - total_expected
        if rare_index is not None:
            high_rates[rare_index][1] += missing
        elif "Rare" in pools and not pools["Rare"].empty:
            high_rates.append(["Rare", missing])
        total_expected = OP_PACKS_PER_BOX

    maximum_high = OP_PACKS_PER_BOX * 2
    if total_expected > maximum_high:
        scale = maximum_high / total_expected
        for row in high_rates:
            row[1] *= scale
        total_expected = maximum_high

    second_hit_probability = max(
        0.0,
        min(1.0, (total_expected - OP_PACKS_PER_BOX) / OP_PACKS_PER_BOX),
    )

    classes = [row[0] for row in high_rates]
    weights = [row[1] for row in high_rates]

    def draw_high():
        if not classes:
            return draw_op_from_pool(pools.get("Rare", pd.DataFrame()), used_ids)
        chosen_class = random.choices(classes, weights=weights, k=1)[0]
        card = draw_op_from_pool(
            pools.get(chosen_class, pd.DataFrame()), used_ids
        )
        if card:
            card["_slot"] = chosen_class
        return card

    card = draw_high()
    if card:
        pack.append(card)

    if random.random() < second_hit_probability:
        card = draw_high()
        if card:
            pack.append(card)
    else:
        if random.random() < 0.65:
            filler_pool, filler_name = common_pool, "Common"
        else:
            filler_pool, filler_name = uncommon_pool, "Uncommon"
        card = draw_op_from_pool(filler_pool, used_ids)
        if card is None:
            card = draw_op_from_pool(common_pool, used_ids)
        if card:
            card["_slot"] = filler_name
            pack.append(card)

    while len(pack) < OP_CARDS_PER_PACK:
        card = draw_op_from_pool(common_pool, used_ids)
        if card is None:
            break
        card["_slot"] = "Common"
        pack.append(card)

    random.shuffle(pack)
    return pack


# ============================================================
# POKEMON - PROFILS DE PULL RATES
# ============================================================

# Les taux ci-dessous sont des probabilités PAR BOOSTER observées par la
# communauté (principalement les gros échantillons TCGplayer). Pokémon ne
# publie pas d'odds de rareté. Les classes qui ne sont pas disponibles dans
# un set sont automatiquement ignorées et retombent vers la rareté normale.
#
# rare_slot : remplace la Rare du 3e slot foil
# reverse1  : remplace le premier slot reverse/foil
# reverse2  : remplace le second slot reverse/foil

POKEMON_PROFILES = {
    "sv01": {
        "rare_slot": {"double_rare": 0.1376, "ultra_rare": 0.0657},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0767,
            "special_illustration_rare": 0.0315,
            "hyper_rare": 0.0185,
        },
        "source": "TCGplayer — Scarlet & Violet, 8 000+ boosters",
    },
    "sv02": {
        "rare_slot": {"double_rare": 0.1372, "ultra_rare": 0.0664},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0770,
            "special_illustration_rare": 0.0317,
            "hyper_rare": 0.0176,
        },
        "source": "TCGplayer — Paldea Evolved, 8 000+ boosters",
    },
    "sv03": {
        "rare_slot": {"double_rare": 0.1361, "ultra_rare": 0.0663},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0760,
            "special_illustration_rare": 0.0313,
            "hyper_rare": 0.0192,
        },
        "source": "TCGplayer — Obsidian Flames",
    },
    "sv03.5": {
        "rare_slot": {"double_rare": 0.1250, "ultra_rare": 0.0625},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0833,
            "special_illustration_rare": 0.03125,
            "hyper_rare": 0.0196,
        },
        "source": "Estimations communautaires — Pokémon 151",
    },
    "sv04": {
        "rare_slot": {"double_rare": 0.1557, "ultra_rare": 0.0664},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0770,
            "special_illustration_rare": 0.0211,
            "hyper_rare": 0.0122,
        },
        "source": "TCGplayer — Paradox Rift",
    },
    "sv04.5": {
        "rare_slot": {"double_rare": 0.1589, "ultra_rare": 0.0661},
        "reverse1": {
            "shiny_rare": 0.2544,
            "shiny_ultra_rare": 0.0772,
        },
        "reverse2": {
            "illustration_rare": 0.0722,
            "special_illustration_rare": 0.0172,
            "hyper_rare": 0.0161,
        },
        "source": "TCGplayer — Paldean Fates",
    },
    "sv05": {
        "rare_slot": {"double_rare": 0.1683, "ultra_rare": 0.0667},
        "reverse1": {"ace_spec": 0.0500},
        "reverse2": {
            "illustration_rare": 0.0772,
            "special_illustration_rare": 0.0117,
            "hyper_rare": 0.0072,
        },
        "source": "TCGplayer — Temporal Forces",
    },
    "sv06": {
        "rare_slot": {"double_rare": 0.1693, "ultra_rare": 0.0661},
        "reverse1": {"ace_spec": 0.0506},
        "reverse2": {
            "illustration_rare": 0.0773,
            "special_illustration_rare": 0.0117,
            "hyper_rare": 0.0068,
        },
        "source": "TCGplayer — Twilight Masquerade",
    },
    "sv06.5": {
        "rare_slot": {"double_rare": 0.1670, "ultra_rare": 0.0670},
        "reverse1": {"ace_spec": 0.0500},
        "reverse2": {
            "illustration_rare": 0.0770,
            "special_illustration_rare": 0.0150,
            "hyper_rare": 0.0070,
        },
        "source": "Estimations communautaires — Shrouded Fable",
    },
    "sv07": {
        "rare_slot": {"double_rare": 0.1690, "ultra_rare": 0.0675},
        "reverse1": {"ace_spec": 0.0494},
        "reverse2": {
            "illustration_rare": 0.0779,
            "special_illustration_rare": 0.0111,
            "hyper_rare": 0.0073,
        },
        "source": "TCGplayer — Stellar Crown",
    },
    "sv08": {
        "rare_slot": {"double_rare": 0.1694, "ultra_rare": 0.0674},
        "reverse1": {"ace_spec": 0.0503},
        "reverse2": {
            "illustration_rare": 0.0767,
            "special_illustration_rare": 0.0115,
            "hyper_rare": 0.0053,
        },
        "source": "TCGplayer — Surging Sparks",
    },
    "sv08.5": {
        "rare_slot": {"double_rare": 0.1694, "ultra_rare": 0.0746},
        "reverse1": {
            "ace_spec": 0.0468,
            "master_ball_foil": 0.0492,
            "poke_ball_foil": 0.3310,
        },
        "reverse2": {
            "special_illustration_rare": 0.0222,
            "hyper_rare": 0.0056,
        },
        "source": "TCGplayer — Prismatic Evolutions, 1 200+ boosters",
    },
    "sv09": {
        "rare_slot": {"double_rare": 0.2029, "ultra_rare": 0.0654},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0850,
            "special_illustration_rare": 0.0116,
            "hyper_rare": 0.0073,
        },
        "source": "TCGplayer — Journey Together, 8 000+ boosters",
    },
    "sv10": {
        "rare_slot": {"double_rare": 0.1983, "ultra_rare": 0.0639},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.0829,
            "special_illustration_rare": 0.0106,
            "hyper_rare": 0.0067,
        },
        "source": "TCGplayer — Destined Rivals",
    },
    "sv10.5b": {
        "rare_slot": {"double_rare": 0.20, "ultra_rare": 0.0583},
        "reverse1": {"master_ball_foil": 0.0514},
        "reverse2": {"illustration_rare": 0.1639},
        "source": "TCGplayer — Black Bolt; classes non mesurées: profil de secours",
    },
    "sv10.5w": {
        "rare_slot": {"double_rare": 0.20, "ultra_rare": 0.0583},
        "reverse1": {"master_ball_foil": 0.0514},
        "reverse2": {"illustration_rare": 0.1639},
        "source": "TCGplayer — White Flare; classes non mesurées: profil de secours",
    },
    "me01": {
        "rare_slot": {"double_rare": 0.2091, "ultra_rare": 0.0823},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1089,
            "special_illustration_rare": 0.0099,
            "mega_hyper_rare": 0.0008,
        },
        "source": "TCGplayer — Mega Evolution",
    },
    "me02": {
        "rare_slot": {"double_rare": 0.2077, "ultra_rare": 0.0806},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1097,
            "special_illustration_rare": 0.0125,
            "mega_hyper_rare": 0.0008,
        },
        "source": "TCGplayer — Phantasmal Flames, 5 000+ boosters",
    },
    "me02.5": {
        "rare_slot": {
            "double_rare": 0.2037,
            "ultra_rare": 0.0481,
            "mega_attack_rare": 1 / 29,
        },
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1125,
            "special_illustration_rare": 0.0144,
            "mega_hyper_rare": 0.0019,
        },
        "source": "TCGplayer/PokéBeach — Ascended Heroes, 2 000+ boosters",
    },
    "me03": {
        "rare_slot": {"double_rare": 0.2097, "ultra_rare": 0.0854},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1120,
            "special_illustration_rare": 0.0123,
            "mega_hyper_rare": 0.0006,
        },
        "source": "TCGplayer — Perfect Order",
    },
    "me04": {
        "rare_slot": {"double_rare": 0.2030, "ultra_rare": 0.0829},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1066,
            "special_illustration_rare": 0.0121,
            "mega_hyper_rare": 0.0010,
        },
        "source": "TCGplayer — Chaos Rising, 8 500+ boosters",
    },
    "me05": {
        "rare_slot": {"double_rare": 0.2102, "ultra_rare": 0.0830},
        "reverse1": {},
        "reverse2": {
            "illustration_rare": 0.1101,
            "special_illustration_rare": 0.0125,
            "mega_hyper_rare": 0.0009,
        },
        "source": "TCGplayer — Pitch Black, 4 000+ boosters",
    },
}

POKEMON_GENERIC_SV = {
    "rare_slot": {"double_rare": 0.17, "ultra_rare": 0.065},
    "reverse1": {},
    "reverse2": {
        "illustration_rare": 0.078,
        "special_illustration_rare": 0.012,
        "hyper_rare": 0.007,
    },
    "source": "Profil Scarlet & Violet générique — estimation communautaire",
    "fallback": True,
}

POKEMON_GENERIC_ME = {
    "rare_slot": {"double_rare": 0.205, "ultra_rare": 0.082},
    "reverse1": {},
    "reverse2": {
        "illustration_rare": 0.110,
        "special_illustration_rare": 0.012,
        "mega_hyper_rare": 0.0009,
    },
    "source": "Profil Mega Evolution générique — estimation communautaire",
    "fallback": True,
}


def pokemon_profile(set_id):
    if set_id in POKEMON_PROFILES:
        profile = dict(POKEMON_PROFILES[set_id])
        profile["fallback"] = False
        return profile
    if str(set_id).lower().startswith("me"):
        return dict(POKEMON_GENERIC_ME)
    return dict(POKEMON_GENERIC_SV)


def pokemon_rarity_key(value):
    text = clean_text(value).replace("-", " ")
    text = re.sub(r"\s+", " ", text)

    checks = [
        (("mega hyper rare", "mega hyper"), "mega_hyper_rare"),
        (("special illustration rare", "illustration speciale rare", "rare illustration speciale"), "special_illustration_rare"),
        (("shiny ultra rare", "ultra rare shiny", "ultra rare chromatique"), "shiny_ultra_rare"),
        (("shiny rare", "rare shiny", "rare chromatique"), "shiny_rare"),
        (("black white rare", "black & white rare"), "black_white_rare"),
        (("mega attack rare",), "mega_attack_rare"),
        (("illustration rare", "rare illustration"), "illustration_rare"),
        (("double rare", "doublement rare"), "double_rare"),
        (("ultra rare",), "ultra_rare"),
        (("hyper rare",), "hyper_rare"),
        (("ace spec", "as tactique", "high tech"), "ace_spec"),
        (("uncommon", "peu commune", "peu commun"), "uncommon"),
        (("common", "commune", "commun"), "common"),
        (("rare", "rare"), "rare"),
        (("promo",), "promo"),
    ]

    for needles, key in checks:
        if any(needle in text for needle in needles):
            return key
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "unknown"


@st.cache_data(ttl=60)
def load_pokemon_set_catalog(simulatable_only=True):
    with connect_game("pokemon") as conn:
        query = """
            SELECT
                set_id AS set_code,
                name AS set_name,
                series_id,
                release_date,
                card_count_total
            FROM sets
            ORDER BY release_date, set_id
        """
        df = pd.read_sql_query(query, conn)

    if simulatable_only:
        ids = df["set_code"].fillna("").astype(str).str.lower()
        df = df[
            (ids.str.startswith("sv") | ids.str.startswith("me"))
            & ~ids.str.startswith("svp")
            & ~ids.str.startswith("mep")
        ].copy()

        # Ne propose pas un set annoncé mais pas encore sorti.
        today = datetime.now(timezone.utc).date().isoformat()
        release = df["release_date"].fillna("").astype(str)
        df = df[(release == "") | (release <= today)].copy()
    return df


@st.cache_data(ttl=60)
def load_pokemon_cards(set_code):
    db_path = GAME_DB_PATHS["pokemon"]
    with connect_game("pokemon") as conn:
        variants_available = table_exists(conn, "card_variants")
        if variants_available:
            query = """
                SELECT
                    c.card_id,
                    c.local_id,
                    c.set_id,
                    c.name,
                    c.rarity,
                    c.category,
                    c.local_image_path,
                    c.image_high_url,
                    GROUP_CONCAT(
                        CASE WHEN cv.available = 1 THEN cv.variant END,
                        ','
                    ) AS variants
                FROM cards c
                LEFT JOIN card_variants cv ON cv.card_id = c.card_id
                WHERE c.set_id = ?
                GROUP BY c.card_id
                ORDER BY CAST(c.local_id AS INTEGER), c.local_id
            """
        else:
            query = """
                SELECT
                    c.card_id,
                    c.local_id,
                    c.set_id,
                    c.name,
                    c.rarity,
                    c.category,
                    c.local_image_path,
                    c.image_high_url,
                    '' AS variants
                FROM cards c
                WHERE c.set_id = ?
                ORDER BY CAST(c.local_id AS INTEGER), c.local_id
            """

        cards = pd.read_sql_query(query, conn, params=(set_code,))

        set_row = conn.execute(
            "SELECT name FROM sets WHERE set_id = ?", (set_code,)
        ).fetchone()
        set_name = set_row[0] if set_row else set_code

    if cards.empty:
        return cards

    cards = cards.copy()
    cards["game"] = "pokemon"
    cards["source_id"] = cards["card_id"].astype(str)
    cards["product_set"] = cards["set_id"]
    cards["set_name"] = set_name
    cards["card_number"] = cards["local_id"].astype(str)
    cards["variant"] = ""
    cards["drop_class"] = cards["rarity"]
    cards["collectible"] = True
    cards["_rarity_key"] = cards["rarity"].map(pokemon_rarity_key)
    cards["image"] = cards.apply(
        lambda row: (
            resolve_local_or_url(row.get("local_image_path"), db_path)
            or resolve_local_or_url(row.get("image_high_url"), db_path)
        ),
        axis=1,
    )
    cards["card_key"] = cards.apply(
        lambda row: make_card_key("pokemon", row), axis=1
    )
    return cards


def draw_dataframe_row(pool, used=None, allow_repeat=False):
    if pool is None or pool.empty:
        return None
    available = pool
    if used is not None and not allow_repeat and "source_id" in pool.columns:
        filtered = pool[~pool["source_id"].astype(str).isin(used)]
        if not filtered.empty:
            available = filtered
    row = available.sample(n=1).iloc[0].to_dict()
    if used is not None and not allow_repeat:
        used.add(str(row.get("source_id")))
    return row


def pokemon_pool(cards, rarity_key):
    return cards[cards["_rarity_key"] == rarity_key]


def pokemon_reverse_pool(cards):
    base = cards[cards["_rarity_key"].isin(["common", "uncommon", "rare"])]
    if "variants" not in base.columns:
        return base
    reverse = base[
        base["variants"].fillna("").str.lower().str.contains("reverse")
    ]
    return reverse if not reverse.empty else base


def draw_probability_slot(cards, probability_map, fallback_pool, used=None):
    roll = random.random()
    cumulative = 0.0
    for rarity_key, probability in probability_map.items():
        cumulative += max(0.0, float(probability))
        if roll < cumulative:
            pool = pokemon_pool(cards, rarity_key)
            if not pool.empty:
                card = draw_dataframe_row(pool, used=used)
                if card:
                    card["_slot"] = rarity_key.replace("_", " ").title()
                    return card
            break

    return draw_dataframe_row(fallback_pool, used=used, allow_repeat=True)


def draw_pokemon_reverse_slot(cards, probability_map):
    roll = random.random()
    cumulative = 0.0

    for rarity_key, probability in probability_map.items():
        cumulative += max(0.0, float(probability))
        if roll >= cumulative:
            continue

        if rarity_key in {"poke_ball_foil", "master_ball_foil"}:
            card = draw_dataframe_row(pokemon_reverse_pool(cards), allow_repeat=True)
            if card:
                card["_finish"] = (
                    "Poké Ball Foil"
                    if rarity_key == "poke_ball_foil"
                    else "Master Ball Foil"
                )
                card["_slot"] = card["_finish"]
                card["variant"] = card["_finish"]
            return card

        pool = pokemon_pool(cards, rarity_key)
        if not pool.empty:
            card = draw_dataframe_row(pool, allow_repeat=True)
            if card:
                card["_slot"] = rarity_key.replace("_", " ").title()
            return card
        break

    card = draw_dataframe_row(pokemon_reverse_pool(cards), allow_repeat=True)
    if card:
        card["_finish"] = "Reverse Holo"
        card["_slot"] = "Reverse Holo"
        card["variant"] = "Reverse Holo"
    return card


def simulate_pokemon_pack(cards, set_code):
    profile = pokemon_profile(set_code)
    used = set()
    pack = []

    common = pokemon_pool(cards, "common")
    uncommon = pokemon_pool(cards, "uncommon")
    rare = pokemon_pool(cards, "rare")

    for _ in range(4):
        card = draw_dataframe_row(common, used=used)
        if card:
            card["_slot"] = "Common"
            pack.append(card)

    for _ in range(3):
        card = draw_dataframe_row(uncommon, used=used)
        if card:
            card["_slot"] = "Uncommon"
            pack.append(card)

    card = draw_pokemon_reverse_slot(cards, profile.get("reverse1", {}))
    if card:
        pack.append(card)

    card = draw_pokemon_reverse_slot(cards, profile.get("reverse2", {}))
    if card:
        pack.append(card)

    card = draw_probability_slot(
        cards,
        profile.get("rare_slot", {}),
        rare,
        used=used,
    )
    if card:
        card["_slot"] = card.get("_slot") or "Rare ou mieux"
        card["_finish"] = card.get("_finish") or "Holo"
        pack.append(card)

    # Énergie physique incluse dans les boosters modernes, mais non issue du
    # set TCGdex sélectionné : affichée, jamais ajoutée au Cartedex.
    energy = {
        "game": "pokemon",
        "source_id": f"virtual-energy-{secrets.token_hex(4)}",
        "product_set": set_code,
        "set_name": str(cards["set_name"].iloc[0]) if not cards.empty else set_code,
        "card_number": "—",
        "name": "Énergie de base",
        "rarity": "Énergie",
        "variant": "",
        "drop_class": "Energy",
        "image": None,
        "collectible": False,
        "card_key": "",
        "_slot": "Énergie",
    }
    pack.append(energy)

    return pack, profile


# ============================================================
# RIFTBOUND - CHARGEMENT ET PROFILS
# ============================================================

RIFTBOUND_PROFILES = {
    "OGN": {
        "epic_pack": 0.25,
        "alt_pack": 2 / 24,
        "overnumber_pack": 1 / 72,
        "signature_pack": 1 / 720,
        "ultimate_pack": 0.0,
        "source": "Riot (Origins) : Epic ~1/4, Alt ~2/box, Overnumber ~1/3 boxes",
        "fallback": False,
    },
    "SFD": {
        "epic_pack": 0.25,
        "alt_pack": 2.5 / 24,
        "overnumber_pack": 1 / 60,
        "signature_pack": 1 / 720,
        "ultimate_pack": 0.0,
        "source": "Spiritforged : structure Riot + ouvertures communautaires (72 packs)",
        "fallback": False,
    },
    "UNL": {
        "epic_pack": 0.27,
        "alt_pack": 3 / 24,
        "overnumber_pack": 1 / 72,
        "signature_pack": 1 / 720,
        # Riot : Ultimate < 0.1% ; 0.09% reste sous cette borne.
        "ultimate_pack": 0.0009,
        "source": "Unleashed : structure Riot + ouvertures communautaires; Ultimate <0,1% officiel",
        "fallback": False,
    },
    "VEN": {
        "epic_pack": 0.27,
        "alt_pack": 0.10,
        "overnumber_pack": 1 / 72,
        "signature_pack": 1 / 720,
        "ultimate_pack": 0.0,
        "source": "Vendetta : structure Riot + estimation à partir d'ouvertures communautaires",
        "fallback": True,
    },
    "RAD": {
        "epic_pack": 0.25,
        "alt_pack": 0.10,
        "overnumber_pack": 1 / 72,
        "signature_pack": 1 / 720,
        "ultimate_pack": 0.0009,
        "source": "Radiance : profil provisoire; structure officielle connue, odds premium à recalibrer après sortie",
        "fallback": True,
    },
}

RIFTBOUND_GENERIC_PROFILE = {
    "epic_pack": 0.25,
    "alt_pack": 0.10,
    "overnumber_pack": 1 / 72,
    "signature_pack": 1 / 720,
    "ultimate_pack": 0.0,
    "source": "Profil Riftbound générique — estimation communautaire",
    "fallback": True,
}

# Riot ne publie pas la répartition interne du slot foil C/U/R/E.
# On respecte "la plupart du temps Common/Uncommon" et on garde ce réglage
# explicitement modifiable ici.
RIFTBOUND_FOIL_WEIGHTS = {
    "common": 0.55,
    "uncommon": 0.35,
    "rare": 0.08,
    "epic": 0.02,
}


def riftbound_profile(set_code):
    return dict(RIFTBOUND_PROFILES.get(set_code, RIFTBOUND_GENERIC_PROFILE))


def riftbound_rarity_key(value):
    text = clean_text(value)
    if "uncommon" in text or "peu commune" in text or "peu commun" in text:
        return "uncommon"
    if text in {"common", "commune", "commun"}:
        return "common"
    if "ultimate" in text or "ultime" in text:
        return "ultimate"
    if "epic" in text or "epique" in text:
        return "epic"
    if "showcase" in text:
        return "showcase"
    if "overnumber" in text or "surnumeraire" in text:
        return "overnumbered"
    if "rare" in text:
        return "rare"
    if "promo" in text:
        return "promo"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "unknown"


def riftbound_treatment(row):
    card_type = clean_text(row.get("card_type"))
    if any(needle in card_type for needle in ("token", "rune", "jeton")):
        return "base"

    if as_bool(row.get("is_signed")):
        return "signature"

    rarity_key = riftbound_rarity_key(row.get("rarity_key") or row.get("rarity_raw"))
    if rarity_key == "ultimate":
        return "ultimate"

    code = str(row.get("public_code") or row.get("code") or "").upper()
    if str(row.get("set_code") or "").upper() == "VEN" and "SP" in code:
        return "special_alt"

    if as_bool(row.get("is_alt_art")):
        return "alt"

    # Certaines entrées de la galerie gardent leur rareté fonctionnelle
    # (Rare/Epic) au lieu d'un libellé Showcase. On utilise donc aussi la
    # numérotation : si le numéro dépasse le dénominateur officiel du set,
    # il s'agit d'un Overnumber.
    try:
        collector_number = int(row.get("collector_number"))
        numbered_size = int(row.get("_numbered_size"))
        if numbered_size > 0 and collector_number > numbered_size:
            return "overnumber"
    except (TypeError, ValueError):
        pass

    if rarity_key in {"showcase", "overnumbered"}:
        return "overnumber"

    return "base"


@st.cache_data(ttl=60)
def load_riftbound_set_catalog(simulatable_only=True):
    with connect_game("riftbound") as conn:
        df = pd.read_sql_query(
            """
            SELECT set_code, set_name, card_count
            FROM sets
            WHERE card_count > 0
            ORDER BY set_code
            """,
            conn,
        )

    if simulatable_only:
        today = datetime.now(timezone.utc).date().isoformat()
        released_codes = {
            code
            for code, release_date in RIFTBOUND_RELEASE_DATES.items()
            if release_date <= today
        }
        df = df[df["set_code"].isin(released_codes)].copy()
    return df


@st.cache_data(ttl=60)
def load_riftbound_cards(set_code):
    db_path = GAME_DB_PATHS["riftbound"]
    with connect_game("riftbound") as conn:
        cards = pd.read_sql_query(
            """
            SELECT
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
                is_alt_art,
                is_signed,
                is_variant,
                local_image_path,
                image_full_url,
                image_url
            FROM cards
            WHERE set_code = ? AND active = 1
            ORDER BY collector_number, public_code
            """,
            conn,
            params=(set_code,),
        )

    if cards.empty:
        return cards

    cards = cards.copy()
    cards["game"] = "riftbound"
    cards["source_id"] = cards["card_uid"].astype(str)
    cards["product_set"] = cards["set_code"]
    cards["card_number"] = cards["public_code"].fillna(cards["code"])
    cards["rarity"] = cards["rarity_raw"].fillna(cards["rarity_key"])

    # Le dénominateur est présent dans les codes normaux, par exemple
    # OGN-194/298. Il permet de reconnaître OGN-299+ comme Overnumber même
    # si la galerie lui conserve une rareté fonctionnelle Rare/Epic.
    denominators = []
    for value in cards["public_code"].dropna().astype(str):
        match = re.search(r"/(\d+)", value)
        if match:
            denominators.append(int(match.group(1)))
    numbered_size = max(denominators) if denominators else 0
    cards["_numbered_size"] = numbered_size

    cards["variant"] = cards.apply(riftbound_treatment, axis=1)
    cards["drop_class"] = cards["variant"].where(
        cards["variant"] != "base", cards["rarity"]
    )
    cards["collectible"] = True
    cards["_rarity_key"] = cards.apply(
        lambda row: riftbound_rarity_key(row.get("rarity_key") or row.get("rarity_raw")),
        axis=1,
    )
    cards["_treatment"] = cards.apply(riftbound_treatment, axis=1)
    cards["_is_token_rune"] = cards["card_type"].fillna("").map(
        lambda value: any(
            needle in clean_text(value)
            for needle in ("token", "rune", "jeton")
        )
    )
    cards["image"] = cards.apply(
        lambda row: (
            resolve_local_or_url(row.get("local_image_path"), db_path)
            or resolve_local_or_url(row.get("image_full_url"), db_path)
            or resolve_local_or_url(row.get("image_url"), db_path)
        ),
        axis=1,
    )
    cards["card_key"] = cards.apply(
        lambda row: make_card_key("riftbound", row), axis=1
    )
    return cards


def rift_base_pool(cards, rarity_key):
    return cards[
        (cards["_rarity_key"] == rarity_key)
        & (cards["_treatment"] == "base")
        & ~cards["_is_token_rune"]
    ]


def rift_premium_pool(cards, treatment):
    if treatment == "alt":
        return cards[cards["_treatment"].isin(["alt", "special_alt"])]
    return cards[cards["_treatment"] == treatment]


def rift_epic_slot_probability(epic_pack_probability):
    # 2 slots indépendants : 1 - (1-p_slot)^2 = p_pack
    p_pack = max(0.0, min(0.999999, float(epic_pack_probability)))
    return 1.0 - math.sqrt(1.0 - p_pack)


def simulate_riftbound_pack(cards, set_code):
    profile = riftbound_profile(set_code)
    used = set()
    pack = []

    common = rift_base_pool(cards, "common")
    uncommon = rift_base_pool(cards, "uncommon")
    rare = rift_base_pool(cards, "rare")
    epic = rift_base_pool(cards, "epic")

    for _ in range(7):
        card = draw_dataframe_row(common, used=used)
        if card:
            card["_slot"] = "Common"
            pack.append(card)

    for _ in range(3):
        card = draw_dataframe_row(uncommon, used=used)
        if card:
            card["_slot"] = "Uncommon"
            pack.append(card)

    # Détermine au maximum un traitement premium dans les deux slots R+.
    premium = None
    premium_order = [
        ("ultimate", profile.get("ultimate_pack", 0.0)),
        ("signature", profile.get("signature_pack", 0.0)),
        ("overnumber", profile.get("overnumber_pack", 0.0)),
        ("alt", profile.get("alt_pack", 0.0)),
    ]
    roll = random.random()
    cumulative = 0.0
    for treatment, probability in premium_order:
        pool = rift_premium_pool(cards, treatment)
        if pool.empty:
            continue
        cumulative += max(0.0, float(probability))
        if roll < cumulative:
            premium = draw_dataframe_row(pool, used=used)
            if premium:
                premium["_slot"] = treatment.replace("_", " ").title()
            break

    rare_plus = []
    if premium:
        rare_plus.append(premium)

    epic_slot_prob = rift_epic_slot_probability(profile.get("epic_pack", 0.25))
    while len(rare_plus) < 2:
        if not epic.empty and random.random() < epic_slot_prob:
            card = draw_dataframe_row(epic, used=used)
            slot_name = "Epic"
        else:
            card = draw_dataframe_row(rare, used=used)
            if card is None:
                card = draw_dataframe_row(epic, used=used)
            slot_name = "Rare"

        if card is None:
            break
        card["_slot"] = slot_name
        rare_plus.append(card)

    pack.extend(rare_plus)

    # Slot foil C/U/R/E. Les poids sont une hypothèse explicite et réglable.
    foil_classes = list(RIFTBOUND_FOIL_WEIGHTS)
    foil_weights = list(RIFTBOUND_FOIL_WEIGHTS.values())
    foil_rarity = random.choices(foil_classes, weights=foil_weights, k=1)[0]
    foil_pool = rift_base_pool(cards, foil_rarity)
    if foil_pool.empty:
        foil_pool = pd.concat([common, uncommon], ignore_index=True)
    foil = draw_dataframe_row(foil_pool, allow_repeat=True)
    if foil:
        foil["_finish"] = "Foil"
        foil["variant"] = (
            f"{foil.get('variant')} · Foil"
            if foil.get("variant") and foil.get("variant") != "base"
            else "Foil"
        )
        foil["_slot"] = "Foil"
        pack.append(foil)

    token_pool = cards[cards["_is_token_rune"]]
    token = draw_dataframe_row(token_pool, allow_repeat=True)
    if token:
        token["_slot"] = "Token / Rune"
        pack.append(token)
    else:
        pack.append(
            {
                "game": "riftbound",
                "source_id": f"virtual-token-{secrets.token_hex(4)}",
                "product_set": set_code,
                "set_name": str(cards["set_name"].iloc[0]) if not cards.empty else set_code,
                "card_number": "—",
                "name": "Token / Rune",
                "rarity": "Token",
                "variant": "",
                "drop_class": "Token / Rune",
                "image": None,
                "collectible": False,
                "card_key": "",
                "_slot": "Token / Rune",
            }
        )

    # Sécurité : la structure Riftbound doit faire 14 cartes jouables/collectibles.
    while len(pack) < RIFTBOUND_CARDS_PER_PACK:
        card = draw_dataframe_row(common, allow_repeat=True)
        if card is None:
            break
        card["_slot"] = "Common"
        pack.append(card)

    return pack[:RIFTBOUND_CARDS_PER_PACK], profile


# ============================================================
# CATALOGUES / DISPATCH MULTI-JEUX
# ============================================================


def game_database_ready(game):
    path = GAME_DB_PATHS[game]
    if not path.is_file():
        return False, f"Base introuvable : {path.name}"

    try:
        with connect_game(game) as conn:
            if game == "onepiece":
                required = {"terminal_cards", "pull_rates"}
            elif game == "pokemon":
                required = {"sets", "cards"}
            else:
                required = {"sets", "cards"}
            missing = [table for table in required if not table_exists(conn, table)]
        if missing:
            return False, "Tables manquantes : " + ", ".join(missing)
    except sqlite3.Error as exc:
        return False, str(exc)

    return True, ""


def load_booster_catalog(game):
    if game == "onepiece":
        df = load_op_set_catalog()
        return df[df["set_code"].astype(str).str.match(r"^OP\d+$")].copy()
    if game == "pokemon":
        return load_pokemon_set_catalog(simulatable_only=True)
    return load_riftbound_set_catalog(simulatable_only=True)


def load_game_cards(game, set_code):
    if game == "onepiece":
        return load_op_cards(set_code)[0]
    if game == "pokemon":
        return load_pokemon_cards(set_code)
    return load_riftbound_cards(set_code)


def simulate_pack(game, set_code, cards):
    if game == "onepiece":
        _, rates = load_op_raw_set_data(set_code)
        return simulate_op_pack(cards, rates), {
            "source": "Taux communautaires stockés dans pull_rates",
            "fallback": False,
        }
    if game == "pokemon":
        return simulate_pokemon_pack(cards, set_code)
    return simulate_riftbound_pack(cards, set_code)


def pack_card_count(game):
    return {
        "onepiece": OP_CARDS_PER_PACK,
        "pokemon": POKEMON_DISPLAYED_CARDS_PER_PACK,
        "riftbound": RIFTBOUND_CARDS_PER_PACK,
    }[game]


def pack_note(game):
    if game == "onepiece":
        return "12 cartes. Les taux proviennent de ta base communautaire One Piece."
    if game == "pokemon":
        return (
            "10 cartes de jeu + 1 Énergie de base affichée. "
            "La carte code n'est pas simulée. Structure officielle moderne; "
            "odds de raretés = estimations communautaires."
        )
    return (
        "14 cartes : 7 Common, 3 Uncommon, 2 Rare+, 1 Foil, 1 Token/Rune. "
        "Structure Riot; odds premium partiellement communautaires."
    )


# ============================================================
# COLLECTION PARTAGEE
# ============================================================


def _save_opening_with_conn(conn, user_id, game, set_code, pack, price_coins=None, balance_after=None):
    now = now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO pack_openings (
            user_id, game, set_code, opened_at, price_coins, balance_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, game, set_code, now, price_coins, balance_after),
    )
    opening_id = cursor.lastrowid

    for card in pack:
        if not card.get("collectible", True):
            continue

        card_key = card.get("card_key") or make_card_key(game, card)
        variant = str(card.get("variant") or card.get("_finish") or "")

        cursor.execute(
            """
            INSERT INTO user_collection (
                user_id,
                card_key,
                game,
                product_set,
                card_number,
                name,
                rarity,
                variant,
                drop_class,
                quantity,
                first_obtained_at,
                last_obtained_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, card_key)
            DO UPDATE SET
                quantity = quantity + 1,
                last_obtained_at = excluded.last_obtained_at
            """,
            (
                user_id,
                card_key,
                game,
                str(card.get("product_set") or set_code),
                str(card.get("card_number") or ""),
                str(card.get("name") or "Carte"),
                str(card.get("rarity") or ""),
                variant,
                str(card.get("drop_class") or card.get("_slot") or ""),
                now,
                now,
            ),
        )

        cursor.execute(
            "INSERT INTO opening_cards (opening_id, card_key) VALUES (?, ?)",
            (opening_id, card_key),
        )

    return opening_id


def save_opening(user_id, game, set_code, pack):
    """Compatibilité : enregistre une ouverture sans transaction monétaire."""
    with connect_app() as conn:
        return _save_opening_with_conn(conn, user_id, game, set_code, pack)


def purchase_pack_and_save(user_id, game, set_code, pack, price_coins):
    """Débite le portefeuille et enregistre le booster dans UNE transaction.

    Retourne (success, message, opening_id, new_balance).
    Le contrôle du solde est refait côté SQLite pour éviter un double clic ou
    deux onglets qui pourraient faire passer le portefeuille en négatif.
    """
    price_coins = int(price_coins)
    now = now_iso()

    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")

        wallet = conn.execute(
            "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if wallet is None:
            return False, "Portefeuille introuvable.", None, 0

        balance = int(wallet["balance_coins"])
        if balance < price_coins:
            return (
                False,
                f"Solde insuffisant : {format_coins(balance)} disponibles, "
                f"{format_coins(price_coins)} nécessaires.",
                None,
                balance,
            )

        new_balance = balance - price_coins
        conn.execute(
            "UPDATE user_wallets SET balance_coins = ?, updated_at = ? WHERE user_id = ?",
            (new_balance, now, user_id),
        )

        opening_id = _save_opening_with_conn(
            conn,
            user_id,
            game,
            set_code,
            pack,
            price_coins=price_coins,
            balance_after=new_balance,
        )

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                user_id, amount_coins, transaction_type, game, set_code,
                opening_id, note, created_at
            ) VALUES (?, ?, 'booster_purchase', ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                -price_coins,
                game,
                str(set_code),
                opening_id,
                f"Ouverture booster {GAME_LABELS.get(game, game)} {set_code}",
                now,
            ),
        )

    return True, "Booster acheté.", opening_id, new_balance



def load_user_collection(user_id, game=None, set_code=None):
    query = "SELECT * FROM user_collection WHERE user_id = ?"
    params = [user_id]
    if game:
        query += " AND game = ?"
        params.append(game)
    if set_code:
        query += " AND product_set = ?"
        params.append(set_code)

    return read_app_dataframe(query, params=params)


def available_overview(game):
    if game == "onepiece":
        with connect_game(game) as conn:
            return pd.read_sql_query(
                """
                SELECT product_set, COUNT(*) AS total_cards
                FROM terminal_cards
                WHERE drop_class IS NOT NULL
                  AND drop_class NOT LIKE 'Promo%'
                GROUP BY product_set
                """,
                conn,
            )

    if game == "pokemon":
        with connect_game(game) as conn:
            return pd.read_sql_query(
                """
                SELECT set_id AS product_set, COUNT(*) AS total_cards
                FROM cards
                GROUP BY set_id
                """,
                conn,
            )

    with connect_game(game) as conn:
        return pd.read_sql_query(
            """
            SELECT set_code AS product_set, COUNT(*) AS total_cards
            FROM cards
            WHERE active = 1
              AND LOWER(COALESCE(rarity_key, '')) != 'promo'
            GROUP BY set_code
            """,
            conn,
        )


def collection_overview(user_id, game):
    available = available_overview(game)
    collection = load_user_collection(user_id, game=game)

    if collection.empty:
        owned = pd.DataFrame(
            columns=["product_set", "unique_owned", "copies_owned"]
        )
    else:
        owned = (
            collection.groupby("product_set")
            .agg(
                unique_owned=("card_key", "count"),
                copies_owned=("quantity", "sum"),
            )
            .reset_index()
        )

    result = available.merge(owned, on="product_set", how="left")
    result["unique_owned"] = result["unique_owned"].fillna(0).astype(int)
    result["copies_owned"] = result["copies_owned"].fillna(0).astype(int)
    result["completion"] = (
        result["unique_owned"] / result["total_cards"].replace(0, pd.NA) * 100
    ).fillna(0.0)
    return result.sort_values("product_set")


def load_cartedex_cards(game, set_code):
    cards = load_game_cards(game, set_code).copy()
    if game == "onepiece" and not cards.empty:
        cards = cards[
            ~cards["drop_class"].fillna("").str.startswith("Promo")
        ].copy()
    elif game == "riftbound" and not cards.empty:
        cards = cards[cards["_rarity_key"] != "promo"].copy()
    return cards



# ============================================================
# DECKS / MODE JEU LOCAL
# ============================================================


def list_user_decks(user_id, game=None):
    query = """
        SELECT
            d.deck_id,
            d.user_id,
            d.name,
            d.game,
            d.created_at,
            d.updated_at,
            COALESCE(SUM(dc.quantity), 0) AS card_count,
            COUNT(dc.card_key) AS unique_cards
        FROM decks d
        LEFT JOIN deck_cards dc ON dc.deck_id = d.deck_id
        WHERE d.user_id = ?
    """
    params = [user_id]
    if game:
        query += " AND d.game = ?"
        params.append(game)
    query += " GROUP BY d.deck_id ORDER BY d.updated_at DESC, d.deck_id DESC"

    return read_app_dataframe(query, params=params)


def get_user_deck(user_id, deck_id):
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT
                d.deck_id, d.user_id, d.name, d.game, d.created_at, d.updated_at,
                COALESCE((
                    SELECT SUM(dc.quantity)
                    FROM deck_cards dc
                    WHERE dc.deck_id = d.deck_id
                ), 0) AS card_count
            FROM decks d
            WHERE d.deck_id = ? AND d.user_id = ?
            """,
            (int(deck_id), int(user_id)),
        ).fetchone()
    return dict(row) if row else None


def create_deck(user_id, name, game):
    name = str(name or "").strip()
    if not name:
        return False, "Donne un nom au deck.", None
    if game not in GAME_LABELS:
        return False, "Jeu invalide.", None

    now = now_iso()
    with connect_app() as conn:
        cursor = conn.execute(
            """
            INSERT INTO decks (user_id, name, game, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(user_id), name, game, now, now),
        )
        deck_id = int(cursor.lastrowid)
    return True, "Deck créé.", deck_id


def rename_deck(user_id, deck_id, new_name):
    new_name = str(new_name or "").strip()
    if not new_name:
        return False, "Nom vide."
    with connect_app() as conn:
        cursor = conn.execute(
            """
            UPDATE decks
            SET name = ?, updated_at = ?
            WHERE deck_id = ? AND user_id = ?
            """,
            (new_name, now_iso(), int(deck_id), int(user_id)),
        )
    return (cursor.rowcount > 0, "Deck renommé." if cursor.rowcount > 0 else "Deck introuvable.")


def delete_deck(user_id, deck_id):
    with connect_app() as conn:
        cursor = conn.execute(
            "DELETE FROM decks WHERE deck_id = ? AND user_id = ?",
            (int(deck_id), int(user_id)),
        )
    return cursor.rowcount > 0


def load_deck_cards(deck_id):
    return read_app_dataframe(
        """
        SELECT
            card_key,
            quantity,
            product_set,
            card_number,
            name,
            rarity,
            variant
        FROM deck_cards
        WHERE deck_id = ?
        ORDER BY product_set, card_number, name
        """,
        params=(int(deck_id),),
    )


def build_owned_card_catalog(user_id, game):
    """Retourne les cartes possédées enrichies avec leur image/source de jeu."""
    collection = load_user_collection(user_id, game=game)
    if collection.empty:
        return pd.DataFrame()

    collection = collection.copy()
    collection["quantity"] = collection["quantity"].fillna(0).astype(int)
    collection = collection[collection["quantity"] > 0]
    if collection.empty:
        return pd.DataFrame()

    loaded = []
    for set_code in sorted(collection["product_set"].dropna().astype(str).unique()):
        try:
            set_cards = load_game_cards(game, set_code).copy()
        except Exception:
            continue
        if set_cards.empty or "card_key" not in set_cards.columns:
            continue
        set_cards = set_cards.drop_duplicates("card_key", keep="first")
        loaded.append(set_cards)

    if loaded:
        full = pd.concat(loaded, ignore_index=True, sort=False)
        full = full.drop_duplicates("card_key", keep="first")
        keep = collection[["card_key", "quantity"]].rename(
            columns={"quantity": "owned_quantity"}
        )
        result = full.merge(keep, on="card_key", how="inner")
    else:
        result = pd.DataFrame()

    # Fallback pour une carte de collection qui ne peut plus être résolue
    # dans une base externe après une mise à jour.
    resolved = set(result["card_key"].astype(str)) if not result.empty else set()
    missing_rows = collection[~collection["card_key"].astype(str).isin(resolved)]
    fallback = []
    for _, row in missing_rows.iterrows():
        fallback.append(
            {
                "game": game,
                "source_id": row["card_key"],
                "card_key": row["card_key"],
                "product_set": row["product_set"],
                "card_number": row["card_number"],
                "name": row["name"],
                "rarity": row["rarity"],
                "variant": row["variant"],
                "drop_class": row["drop_class"],
                "image": None,
                "owned_quantity": int(row["quantity"]),
                "collectible": True,
            }
        )
    if fallback:
        result = pd.concat([result, pd.DataFrame(fallback)], ignore_index=True, sort=False)

    if result.empty:
        return result

    for col, default in (
        ("game", game),
        ("product_set", ""),
        ("card_number", ""),
        ("name", ""),
        ("rarity", ""),
        ("variant", ""),
        ("drop_class", ""),
        ("image", None),
    ):
        if col not in result.columns:
            result[col] = default

    result["game"] = game
    return result.sort_values(
        ["product_set", "card_number", "name"],
        na_position="last",
    ).reset_index(drop=True)


def set_deck_card_quantity(user_id, deck_id, card_key, quantity):
    deck = get_user_deck(user_id, deck_id)
    if not deck:
        return False, "Deck introuvable."

    quantity = int(quantity)
    with connect_app() as conn:
        owned = conn.execute(
            """
            SELECT card_key, game, product_set, card_number, name,
                   rarity, variant, quantity
            FROM user_collection
            WHERE user_id = ? AND card_key = ? AND game = ?
            """,
            (int(user_id), str(card_key), deck["game"]),
        ).fetchone()

        if owned is None:
            return False, "Cette carte n'est pas dans ta collection."

        owned_qty = int(owned["quantity"] or 0)
        if quantity < 0:
            quantity = 0
        if quantity > owned_qty:
            return False, f"Tu ne possèdes que {owned_qty} exemplaire(s) de cette carte."

        if quantity == 0:
            conn.execute(
                "DELETE FROM deck_cards WHERE deck_id = ? AND card_key = ?",
                (int(deck_id), str(card_key)),
            )
        else:
            conn.execute(
                """
                INSERT INTO deck_cards (
                    deck_id, card_key, quantity, product_set,
                    card_number, name, rarity, variant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(deck_id, card_key) DO UPDATE SET
                    quantity = excluded.quantity,
                    product_set = excluded.product_set,
                    card_number = excluded.card_number,
                    name = excluded.name,
                    rarity = excluded.rarity,
                    variant = excluded.variant
                """,
                (
                    int(deck_id),
                    str(card_key),
                    quantity,
                    str(owned["product_set"] or ""),
                    str(owned["card_number"] or ""),
                    str(owned["name"] or ""),
                    str(owned["rarity"] or ""),
                    str(owned["variant"] or ""),
                ),
            )
        conn.execute(
            "UPDATE decks SET updated_at = ? WHERE deck_id = ?",
            (now_iso(), int(deck_id)),
        )

    return True, "Deck mis à jour."


def load_deck_runtime_cards(user_id, deck_id):
    """Reconstruit les données complètes des cartes d'un deck pour le plateau."""
    deck = get_user_deck(user_id, deck_id)
    if not deck:
        return None, []

    rows = load_deck_cards(deck_id)
    if rows.empty:
        return deck, []

    by_key = {}
    for set_code in sorted(rows["product_set"].dropna().astype(str).unique()):
        try:
            cards = load_game_cards(deck["game"], set_code)
        except Exception:
            continue
        if cards.empty:
            continue
        for _, card in cards.drop_duplicates("card_key", keep="first").iterrows():
            by_key[str(card["card_key"])] = card.to_dict()

    result = []
    for _, row in rows.iterrows():
        key = str(row["card_key"])
        card = dict(by_key.get(key) or {})
        if not card:
            card = {
                "game": deck["game"],
                "source_id": key,
                "card_key": key,
                "product_set": row["product_set"],
                "card_number": row["card_number"],
                "name": row["name"],
                "rarity": row["rarity"],
                "variant": row["variant"],
                "drop_class": "",
                "image": None,
                "collectible": True,
            }
        card["game"] = deck["game"]
        card["card_key"] = key
        result.append({"card": card, "quantity": int(row["quantity"])})

    return deck, result


def start_local_play_session(user_id, deck_id):
    deck, deck_rows = load_deck_runtime_cards(user_id, deck_id)
    if not deck:
        return False, "Deck introuvable."
    if not deck_rows:
        return False, "Ce deck est vide."

    draw_pile = []
    for entry in deck_rows:
        for _ in range(int(entry["quantity"])):
            draw_pile.append(
                {
                    "instance_id": secrets.token_hex(8),
                    "card": dict(entry["card"]),
                }
            )
    random.shuffle(draw_pile)

    st.session_state["local_play"] = {
        "session_id": secrets.token_hex(8),
        "deck_id": int(deck_id),
        "deck_name": deck["name"],
        "game": deck["game"],
        "draw_pile": draw_pile,
        "hand": [],
        "board": [],
        "discard": [],
    }
    return True, "Partie prête."


def play_draw_cards(count=1):
    state = st.session_state.get("local_play")
    if not state:
        return 0
    drawn = 0
    for _ in range(max(0, int(count))):
        if not state["draw_pile"]:
            break
        state["hand"].append(state["draw_pile"].pop())
        drawn += 1
    return drawn


def _move_instance(source_name, target_name, instance_id):
    state = st.session_state.get("local_play")
    if not state:
        return False
    source = state[source_name]
    for index, instance in enumerate(source):
        if instance["instance_id"] == instance_id:
            state[target_name].append(source.pop(index))
            return True
    return False


def play_hand_to_board(instance_id):
    return _move_instance("hand", "board", instance_id)


def play_hand_to_discard(instance_id):
    return _move_instance("hand", "discard", instance_id)


def play_board_to_discard(instance_id):
    return _move_instance("board", "discard", instance_id)


def play_board_to_hand(instance_id):
    return _move_instance("board", "hand", instance_id)



# ============================================================
# MULTIJOUEUR LOCAL / PARTIES ENTRE AMIS
# ============================================================


def multiplayer_card_snapshot(card):
    """Copie minimale et sérialisable d'une carte pour une partie réseau."""
    image = card.get("image")
    if image is not None:
        try:
            if pd.isna(image):
                image = None
        except Exception:
            pass
    return {
        "game": str(card.get("game") or ""),
        "card_key": str(card.get("card_key") or card.get("source_id") or ""),
        "name": str(card.get("name") or "Carte"),
        "image": str(image) if image else "",
    }


def build_multiplayer_deck_state(user_id, deck_id):
    """Construit une pioche persistable à partir d'un deck du joueur."""
    deck, deck_rows = load_deck_runtime_cards(user_id, deck_id)
    if not deck:
        return None, None, "Deck introuvable."
    if not deck_rows:
        return None, None, "Ce deck est vide."

    draw_pile = []
    for entry in deck_rows:
        snapshot = multiplayer_card_snapshot(entry["card"])
        for _ in range(int(entry["quantity"])):
            draw_pile.append(
                {
                    "instance_id": secrets.token_hex(10),
                    "card": dict(snapshot),
                }
            )
    random.shuffle(draw_pile)
    return deck, draw_pile, None


def user_has_active_match(user_id):
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT match_id
            FROM multiplayer_matches
            WHERE status = 'active'
              AND (player1_id = ? OR player2_id = ?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (int(user_id), int(user_id)),
        ).fetchone()
    return int(row["match_id"]) if row else None


def send_game_invite(sender_id, receiver_id, sender_deck_id):
    sender_id = int(sender_id)
    receiver_id = int(receiver_id)
    sender_deck_id = int(sender_deck_id)

    if sender_id == receiver_id:
        return False, "Tu ne peux pas t'inviter toi-même."
    if not are_friends(sender_id, receiver_id):
        return False, "Ce joueur n'est pas dans tes amis."

    deck = get_user_deck(sender_id, sender_deck_id)
    if not deck:
        return False, "Deck introuvable."
    if int(deck.get("card_count") or 0) <= 0:
        return False, "Ce deck est vide."
    if user_has_active_match(sender_id):
        return False, "Tu as déjà une partie active."

    with connect_app() as conn:
        existing = conn.execute(
            """
            SELECT invite_id
            FROM game_invites
            WHERE sender_id = ? AND receiver_id = ? AND status = 'pending'
            ORDER BY invite_id DESC
            LIMIT 1
            """,
            (sender_id, receiver_id),
        ).fetchone()
        if existing:
            return False, "Une invitation est déjà en attente pour cet ami."

        now = now_iso()
        conn.execute(
            """
            INSERT INTO game_invites (
                sender_id, receiver_id, sender_deck_id, game,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (sender_id, receiver_id, sender_deck_id, str(deck["game"]), now, now),
        )
    return True, "Invitation envoyée."


def list_incoming_game_invites(user_id):
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                gi.invite_id,
                gi.sender_id,
                gi.sender_deck_id,
                gi.game,
                gi.created_at,
                u.username AS sender_username,
                d.name AS sender_deck_name
            FROM game_invites gi
            JOIN app_users u ON u.user_id = gi.sender_id
            JOIN decks d ON d.deck_id = gi.sender_deck_id
            WHERE gi.receiver_id = ? AND gi.status = 'pending'
            ORDER BY gi.created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_outgoing_game_invites(user_id):
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                gi.invite_id,
                gi.receiver_id,
                gi.game,
                gi.created_at,
                u.username AS receiver_username,
                d.name AS sender_deck_name
            FROM game_invites gi
            JOIN app_users u ON u.user_id = gi.receiver_id
            JOIN decks d ON d.deck_id = gi.sender_deck_id
            WHERE gi.sender_id = ? AND gi.status = 'pending'
            ORDER BY gi.created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_game_invite(user_id, invite_id):
    with connect_app() as conn:
        cur = conn.execute(
            """
            UPDATE game_invites
            SET status = 'cancelled', updated_at = ?
            WHERE invite_id = ? AND sender_id = ? AND status = 'pending'
            """,
            (now_iso(), int(invite_id), int(user_id)),
        )
    return cur.rowcount > 0


def decline_game_invite(user_id, invite_id):
    with connect_app() as conn:
        cur = conn.execute(
            """
            UPDATE game_invites
            SET status = 'declined', updated_at = ?
            WHERE invite_id = ? AND receiver_id = ? AND status = 'pending'
            """,
            (now_iso(), int(invite_id), int(user_id)),
        )
    return cur.rowcount > 0


def accept_game_invite(user_id, invite_id, receiver_deck_id):
    user_id = int(user_id)
    invite_id = int(invite_id)
    receiver_deck_id = int(receiver_deck_id)

    with connect_app() as conn:
        invite = conn.execute(
            """
            SELECT *
            FROM game_invites
            WHERE invite_id = ? AND receiver_id = ? AND status = 'pending'
            """,
            (invite_id, user_id),
        ).fetchone()
    if not invite:
        return False, "Cette invitation n'est plus disponible.", None

    sender_id = int(invite["sender_id"])
    sender_deck_id = int(invite["sender_deck_id"])
    game = str(invite["game"])

    receiver_deck = get_user_deck(user_id, receiver_deck_id)
    if not receiver_deck:
        return False, "Deck introuvable.", None
    if str(receiver_deck["game"]) != game:
        return False, "Choisis un deck du même jeu que ton ami.", None
    if int(receiver_deck.get("card_count") or 0) <= 0:
        return False, "Ce deck est vide.", None

    if user_has_active_match(user_id) or user_has_active_match(sender_id):
        return False, "L'un des deux joueurs a déjà une partie active.", None

    sender_deck, sender_pile, error = build_multiplayer_deck_state(sender_id, sender_deck_id)
    if error:
        return False, f"Deck de ton ami : {error}", None
    _, receiver_pile, error = build_multiplayer_deck_state(user_id, receiver_deck_id)
    if error:
        return False, error, None

    now = now_iso()
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """
            SELECT status
            FROM game_invites
            WHERE invite_id = ? AND receiver_id = ?
            """,
            (invite_id, user_id),
        ).fetchone()
        if not current or current["status"] != "pending":
            conn.rollback()
            return False, "Cette invitation vient d'être modifiée.", None

        busy = conn.execute(
            """
            SELECT 1
            FROM multiplayer_matches
            WHERE status = 'active'
              AND (
                    player1_id IN (?, ?)
                 OR player2_id IN (?, ?)
              )
            LIMIT 1
            """,
            (sender_id, user_id, sender_id, user_id),
        ).fetchone()
        if busy:
            conn.rollback()
            return False, "L'un des deux joueurs a déjà une partie active.", None

        cur = conn.execute(
            """
            INSERT INTO multiplayer_matches (
                game, player1_id, player2_id, status, version, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', 1, ?, ?)
            """,
            (game, sender_id, user_id, now, now),
        )
        match_id = int(cur.lastrowid)

        conn.execute(
            """
            INSERT INTO match_players (
                match_id, user_id, deck_id,
                draw_pile_json, hand_json, discard_json
            ) VALUES (?, ?, ?, ?, '[]', '[]')
            """,
            (match_id, sender_id, sender_deck_id, json.dumps(sender_pile, ensure_ascii=False)),
        )
        conn.execute(
            """
            INSERT INTO match_players (
                match_id, user_id, deck_id,
                draw_pile_json, hand_json, discard_json
            ) VALUES (?, ?, ?, ?, '[]', '[]')
            """,
            (match_id, user_id, receiver_deck_id, json.dumps(receiver_pile, ensure_ascii=False)),
        )
        conn.execute(
            """
            UPDATE game_invites
            SET status = 'accepted', match_id = ?, updated_at = ?
            WHERE invite_id = ?
            """,
            (match_id, now, invite_id),
        )
        conn.commit()

    return True, "Partie créée.", match_id


def list_active_matches(user_id):
    with connect_app() as conn:
        rows = conn.execute(
            """
            SELECT
                m.match_id,
                m.game,
                m.updated_at,
                CASE WHEN m.player1_id = ? THEN m.player2_id ELSE m.player1_id END AS opponent_id,
                CASE WHEN m.player1_id = ? THEN u2.username ELSE u1.username END AS opponent_username,
                mp.deck_id,
                d.name AS deck_name
            FROM multiplayer_matches m
            JOIN app_users u1 ON u1.user_id = m.player1_id
            JOIN app_users u2 ON u2.user_id = m.player2_id
            JOIN match_players mp ON mp.match_id = m.match_id AND mp.user_id = ?
            JOIN decks d ON d.deck_id = mp.deck_id
            WHERE m.status = 'active'
              AND (m.player1_id = ? OR m.player2_id = ?)
            ORDER BY m.updated_at DESC
            """,
            (int(user_id), int(user_id), int(user_id), int(user_id), int(user_id)),
        ).fetchall()
    return [dict(row) for row in rows]


def close_multiplayer_match(user_id, match_id):
    user_id = int(user_id)
    match_id = int(match_id)
    with connect_app() as conn:
        cur = conn.execute(
            """
            UPDATE multiplayer_matches
            SET status = 'ended', version = version + 1, updated_at = ?
            WHERE match_id = ? AND status = 'active'
              AND (player1_id = ? OR player2_id = ?)
            """,
            (now_iso(), match_id, user_id, user_id),
        )
    return cur.rowcount > 0


def _loads_list(value):
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def load_multiplayer_match_state(user_id, match_id):
    user_id = int(user_id)
    match_id = int(match_id)
    with connect_app() as conn:
        match = conn.execute(
            """
            SELECT *
            FROM multiplayer_matches
            WHERE match_id = ? AND status = 'active'
              AND (player1_id = ? OR player2_id = ?)
            """,
            (match_id, user_id, user_id),
        ).fetchone()
        if not match:
            return None

        opponent_id = int(match["player2_id"] if int(match["player1_id"]) == user_id else match["player1_id"])
        me = conn.execute(
            """
            SELECT mp.*, u.username, d.name AS deck_name
            FROM match_players mp
            JOIN app_users u ON u.user_id = mp.user_id
            JOIN decks d ON d.deck_id = mp.deck_id
            WHERE mp.match_id = ? AND mp.user_id = ?
            """,
            (match_id, user_id),
        ).fetchone()
        opponent = conn.execute(
            """
            SELECT mp.*, u.username, d.name AS deck_name
            FROM match_players mp
            JOIN app_users u ON u.user_id = mp.user_id
            JOIN decks d ON d.deck_id = mp.deck_id
            WHERE mp.match_id = ? AND mp.user_id = ?
            """,
            (match_id, opponent_id),
        ).fetchone()
        board_rows = conn.execute(
            """
            SELECT instance_id, owner_id, card_json, x, y, z_index
            FROM match_board_cards
            WHERE match_id = ?
            ORDER BY z_index, created_at
            """,
            (match_id,),
        ).fetchall()

    if not me or not opponent:
        return None

    my_draw = _loads_list(me["draw_pile_json"])
    my_hand = _loads_list(me["hand_json"])
    my_discard = _loads_list(me["discard_json"])
    opponent_draw = _loads_list(opponent["draw_pile_json"])
    opponent_hand = _loads_list(opponent["hand_json"])
    opponent_discard = _loads_list(opponent["discard_json"])

    my_board = []
    opponent_board = []
    for row in board_rows:
        try:
            card = json.loads(row["card_json"])
        except Exception:
            card = {"name": "Carte", "image": ""}
        item = {
            "instance_id": str(row["instance_id"]),
            "card": card,
            "x": float(row["x"]),
            "y": float(row["y"]),
            "z": int(row["z_index"]),
        }
        if int(row["owner_id"]) == user_id:
            my_board.append(item)
        else:
            opponent_board.append(item)

    return {
        "match_id": match_id,
        "session_id": f"match-{match_id}-user-{user_id}",
        "game": str(match["game"]),
        "version": int(match["version"]),
        "me": {
            "user_id": user_id,
            "username": str(me["username"]),
            "deck_name": str(me["deck_name"]),
            "draw_pile": my_draw,
            "hand": my_hand,
            "discard": my_discard,
        },
        "opponent": {
            "user_id": opponent_id,
            "username": str(opponent["username"]),
            "deck_name": str(opponent["deck_name"]),
            "draw_count": len(opponent_draw),
            "hand_count": len(opponent_hand),
            "discard_count": len(opponent_discard),
        },
        "my_board": my_board,
        "opponent_board": opponent_board,
    }


def _touch_multiplayer_match(conn, match_id):
    conn.execute(
        """
        UPDATE multiplayer_matches
        SET version = version + 1, updated_at = ?
        WHERE match_id = ?
        """,
        (now_iso(), int(match_id)),
    )


def multiplayer_draw_card(user_id, match_id):
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        player = conn.execute(
            """
            SELECT mp.*
            FROM match_players mp
            JOIN multiplayer_matches m ON m.match_id = mp.match_id
            WHERE mp.match_id = ? AND mp.user_id = ? AND m.status = 'active'
            """,
            (int(match_id), int(user_id)),
        ).fetchone()
        if not player:
            conn.rollback()
            return False
        draw_pile = _loads_list(player["draw_pile_json"])
        hand = _loads_list(player["hand_json"])
        if not draw_pile:
            conn.rollback()
            return False
        hand.append(draw_pile.pop())
        conn.execute(
            """
            UPDATE match_players
            SET draw_pile_json = ?, hand_json = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (
                json.dumps(draw_pile, ensure_ascii=False),
                json.dumps(hand, ensure_ascii=False),
                int(match_id), int(user_id),
            ),
        )
        _touch_multiplayer_match(conn, match_id)
        conn.commit()
    return True


def _pop_player_instance(items, instance_id):
    for index, item in enumerate(items):
        if str(item.get("instance_id")) == str(instance_id):
            return items.pop(index)
    return None


def multiplayer_hand_to_board(user_id, match_id, instance_id, x=0.5, y=0.48):
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        player = conn.execute(
            """
            SELECT mp.*
            FROM match_players mp
            JOIN multiplayer_matches m ON m.match_id = mp.match_id
            WHERE mp.match_id = ? AND mp.user_id = ? AND m.status = 'active'
            """,
            (int(match_id), int(user_id)),
        ).fetchone()
        if not player:
            conn.rollback()
            return False
        hand = _loads_list(player["hand_json"])
        item = _pop_player_instance(hand, instance_id)
        if not item:
            conn.rollback()
            return False
        z = conn.execute(
            "SELECT COALESCE(MAX(z_index), 0) + 1 AS next_z FROM match_board_cards WHERE match_id = ?",
            (int(match_id),),
        ).fetchone()["next_z"]
        now = now_iso()
        conn.execute(
            "UPDATE match_players SET hand_json = ? WHERE match_id = ? AND user_id = ?",
            (json.dumps(hand, ensure_ascii=False), int(match_id), int(user_id)),
        )
        conn.execute(
            """
            INSERT INTO match_board_cards (
                match_id, instance_id, owner_id, card_json,
                x, y, z_index, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(match_id), str(instance_id), int(user_id),
                json.dumps(item.get("card") or {}, ensure_ascii=False),
                max(0.0, min(1.0, float(x))),
                max(0.0, min(1.0, float(y))),
                int(z), now, now,
            ),
        )
        _touch_multiplayer_match(conn, match_id)
        conn.commit()
    return True


def multiplayer_hand_to_discard(user_id, match_id, instance_id):
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        player = conn.execute(
            """
            SELECT mp.*
            FROM match_players mp
            JOIN multiplayer_matches m ON m.match_id = mp.match_id
            WHERE mp.match_id = ? AND mp.user_id = ? AND m.status = 'active'
            """,
            (int(match_id), int(user_id)),
        ).fetchone()
        if not player:
            conn.rollback()
            return False
        hand = _loads_list(player["hand_json"])
        discard = _loads_list(player["discard_json"])
        item = _pop_player_instance(hand, instance_id)
        if not item:
            conn.rollback()
            return False
        discard.append(item)
        conn.execute(
            """
            UPDATE match_players SET hand_json = ?, discard_json = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (
                json.dumps(hand, ensure_ascii=False),
                json.dumps(discard, ensure_ascii=False),
                int(match_id), int(user_id),
            ),
        )
        _touch_multiplayer_match(conn, match_id)
        conn.commit()
    return True


def multiplayer_move_board_card(user_id, match_id, instance_id, x, y, z_index):
    x = max(0.0, min(1.0, float(x)))
    y = max(0.0, min(1.0, float(y)))
    z_index = max(1, min(1_000_000, int(z_index)))
    with connect_app() as conn:
        cur = conn.execute(
            """
            UPDATE match_board_cards
            SET x = ?, y = ?, z_index = ?, updated_at = ?
            WHERE match_id = ? AND instance_id = ? AND owner_id = ?
              AND EXISTS (
                  SELECT 1 FROM multiplayer_matches m
                  WHERE m.match_id = match_board_cards.match_id AND m.status = 'active'
              )
            """,
            (x, y, z_index, now_iso(), int(match_id), str(instance_id), int(user_id)),
        )
        if cur.rowcount:
            _touch_multiplayer_match(conn, match_id)
    return cur.rowcount > 0


def multiplayer_board_to_zone(user_id, match_id, instance_id, target):
    if target not in {"hand", "discard"}:
        return False
    field = "hand_json" if target == "hand" else "discard_json"
    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        board = conn.execute(
            """
            SELECT card_json
            FROM match_board_cards
            WHERE match_id = ? AND instance_id = ? AND owner_id = ?
            """,
            (int(match_id), str(instance_id), int(user_id)),
        ).fetchone()
        player = conn.execute(
            f"""
            SELECT {field}
            FROM match_players
            WHERE match_id = ? AND user_id = ?
            """,
            (int(match_id), int(user_id)),
        ).fetchone()
        if not board or not player:
            conn.rollback()
            return False
        items = _loads_list(player[field])
        try:
            card = json.loads(board["card_json"])
        except Exception:
            card = {"name": "Carte", "image": ""}
        items.append({"instance_id": str(instance_id), "card": card})
        conn.execute(
            f"UPDATE match_players SET {field} = ? WHERE match_id = ? AND user_id = ?",
            (json.dumps(items, ensure_ascii=False), int(match_id), int(user_id)),
        )
        conn.execute(
            "DELETE FROM match_board_cards WHERE match_id = ? AND instance_id = ? AND owner_id = ?",
            (int(match_id), str(instance_id), int(user_id)),
        )
        _touch_multiplayer_match(conn, match_id)
        conn.commit()
    return True

# ============================================================
# COMPOSANT DE PLATEAU PLEIN ÉCRAN
# ============================================================

PLAY_SURFACE_COMPONENT_DIR = Path(__file__).resolve().parent / "_play_surface_component"

PLAY_SURFACE_COMPONENT_HTML = r'''<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
:root {
  color-scheme: dark;
  --card-w: 168px;
}
* { box-sizing: border-box; }
html, body { margin:0; width:100%; height:100%; overflow:hidden; background:#000; }
body {
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color:#fff;
  user-select:none;
}
button { font:inherit; }
#game {
  position:fixed;
  inset:0;
  overflow:hidden;
  background:
    radial-gradient(circle at 50% 42%, rgba(38,52,63,.30) 0%, rgba(13,17,20,.16) 34%, rgba(0,0,0,0) 68%),
    #000;
}
#game::before {
  content:"";
  position:absolute;
  inset:0;
  pointer-events:none;
  background:
    linear-gradient(180deg, rgba(255,255,255,.018), transparent 14%, transparent 82%, rgba(255,255,255,.018)),
    radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,.62) 100%);
}
.topbar {
  position:absolute;
  right:28px;
  bottom:28px;
  z-index:60000;
  display:flex;
  align-items:center;
  justify-content:flex-end;
  pointer-events:none;
}
.draw-button {
  pointer-events:auto;
  min-width:150px;
  height:50px;
  padding:0 24px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.22);
  color:#fff;
  background:rgba(18,20,24,.82);
  box-shadow:0 12px 38px rgba(0,0,0,.46), inset 0 1px 0 rgba(255,255,255,.06);
  cursor:pointer;
  transition:transform .15s ease, border-color .15s ease, background .15s ease;
  backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
}
.draw-button:hover:not(:disabled) {
  transform:translateY(-2px) scale(1.025);
  border-color:rgba(255,255,255,.48);
  background:rgba(31,34,40,.92);
}
.draw-button:active:not(:disabled) { transform:scale(.985); }
.draw-button:disabled { opacity:.35; cursor:default; }
.draw-count {
  display:block;
  margin-top:2px;
  font-size:11px;
  color:rgba(255,255,255,.55);
}
.exit-button {
  position:absolute;
  left:20px;
  top:20px;
  z-index:10001;
  width:42px;
  height:42px;
  border-radius:50%;
  border:1px solid rgba(255,255,255,.16);
  background:rgba(10,10,12,.62);
  color:rgba(255,255,255,.8);
  cursor:pointer;
  font-size:21px;
  backdrop-filter:blur(10px);
}
.exit-button:hover { background:rgba(255,255,255,.10); color:#fff; }
.discard-indicator {
  position:absolute;
  right:22px;
  top:22px;
  z-index:10001;
  font-size:12px;
  color:rgba(255,255,255,.50);
  letter-spacing:.04em;
}
#board {
  position:absolute;
  left:0;
  right:0;
  top:76px;
  bottom:245px;
  overflow:visible;
}
.board-card {
  position:absolute;
  width:136px;
  aspect-ratio:5/7;
  border-radius:10px;
  overflow:visible;
  cursor:grab;
  touch-action:none;
  transform:translateZ(0);
  filter:drop-shadow(0 12px 18px rgba(0,0,0,.58));
}
.board-card:hover {
  z-index:15000 !important;
}
.board-card.dragging { cursor:grabbing; }
.board-card .card-shell {
  width:100%; height:100%; border-radius:10px; overflow:hidden;
  transform-origin:50% 50%;
  box-shadow:0 0 0 1px rgba(255,255,255,.12);
  transition:transform .18s cubic-bezier(.2,.8,.2,1), box-shadow .18s ease, filter .18s ease;
}
.board-card:hover .card-shell {
  transform:scale(1.70);
  box-shadow:0 0 0 1px rgba(255,255,255,.42), 0 24px 48px rgba(0,0,0,.70);
  filter:drop-shadow(0 8px 14px rgba(0,0,0,.35));
}
.board-card img, .hand-card img {
  width:100%; height:100%; display:block; object-fit:contain; pointer-events:none;
}
.missing {
  width:100%; height:100%; display:flex; align-items:center; justify-content:center;
  padding:10px; text-align:center; background:#222; color:#aaa; border-radius:10px;
}
.board-actions {
  position:absolute;
  left:50%;
  top:50%;
  transform:translate(-50%,-50%);
  display:none;
  gap:7px;
  z-index:20;
}
.board-card.selected .board-actions { display:flex; }
.board-action {
  border:1px solid rgba(255,255,255,.24);
  background:rgba(0,0,0,.82);
  color:white;
  border-radius:999px;
  min-width:42px;
  height:36px;
  padding:0 11px;
  cursor:pointer;
  box-shadow:0 5px 18px rgba(0,0,0,.45);
}
#hand-zone {
  position:absolute;
  left:50%;
  bottom:36px;
  width:min(1240px, calc(100vw - 260px));
  height:320px;
  transform:translateX(-50%);
  z-index:20000;
  pointer-events:none;
  overflow:visible;
}
.hand-card {
  --x:0px;
  --y:0px;
  --r:0deg;
  position:absolute;
  left:50%;
  bottom:0;
  width:var(--card-w);
  aspect-ratio:5/7;
  border-radius:11px;
  transform-origin:50% 100%;
  transform:translateX(calc(-50% + var(--x))) translateY(var(--y)) rotate(var(--r)) translateZ(0);
  transition:transform .18s cubic-bezier(.2,.8,.2,1), filter .18s ease;
  filter:drop-shadow(0 10px 18px rgba(0,0,0,.72));
  pointer-events:auto;
  cursor:pointer;
}
.hand-card .card-shell {
  position:absolute; inset:0; border-radius:11px; overflow:hidden;
  box-shadow:0 0 0 1px rgba(255,255,255,.12);
  background:#17191d;
}
.hand-card:hover {
  transform:translateX(calc(-50% + var(--x))) translateY(-82px) rotate(0deg) scale(1.43) translateZ(0);
  z-index:50000 !important;
  filter:drop-shadow(0 24px 36px rgba(0,0,0,.82));
}
.hand-card.selected {
  transform:translateX(calc(-50% + var(--x))) translateY(-112px) rotate(0deg) scale(1.22) translateZ(0);
  z-index:49000 !important;
  filter:drop-shadow(0 24px 40px rgba(0,0,0,.86));
}
.hand-card.selected:hover {
  transform:translateX(calc(-50% + var(--x))) translateY(-124px) rotate(0deg) scale(1.34) translateZ(0);
}
.hand-actions {
  position:absolute;
  left:50%;
  bottom:calc(100% + 12px);
  transform:translateX(-50%);
  display:none;
  align-items:center;
  gap:8px;
  white-space:nowrap;
  z-index:60000;
}
.hand-card.selected .hand-actions { display:flex; }
.hand-action {
  min-width:88px;
  height:39px;
  padding:0 16px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.26);
  background:rgba(12,13,16,.92);
  color:#fff;
  font-weight:700;
  cursor:pointer;
  box-shadow:0 8px 28px rgba(0,0,0,.60);
  backdrop-filter:blur(10px);
}
.hand-action.primary {
  background:#fff;
  color:#050505;
  border-color:#fff;
}
.hand-action:hover { transform:translateY(-1px); }
.empty-hand {
  position:absolute;
  left:50%;
  bottom:44px;
  transform:translateX(-50%);
  color:rgba(255,255,255,.26);
  font-size:13px;
  pointer-events:none;
}
@media (max-width:900px) {
  :root { --card-w:138px; }
  .topbar { right:16px; bottom:18px; }
  .draw-button { min-width:126px; height:46px; padding:0 17px; }
  #board { bottom:220px; }
  #hand-zone { bottom:28px; width:calc(100vw - 170px); height:270px; }
  .hand-card:hover { transform:translateX(calc(-50% + var(--x))) translateY(-64px) rotate(0deg) scale(1.34); }
  .board-card:hover .card-shell { transform:scale(1.48); }
}
</style>
</head>
<body>
<div id="game">
  <button id="exit" class="exit-button" title="Quitter la table">×</button>
  <div class="topbar">
    <button id="draw" class="draw-button">Piocher<span id="drawCount" class="draw-count"></span></button>
  </div>
  <div id="discardIndicator" class="discard-indicator"></div>
  <div id="board"></div>
  <div id="hand-zone"></div>
</div>
<script>
(() => {
  let cfg = null;
  let selectedHand = null;
  let selectedBoard = null;
  let zCounter = 100;

  function send(type, data={}) {
    window.parent.postMessage({isStreamlitMessage:true, type, ...data}, "*");
  }
  function ready() {
    send("streamlit:componentReady", {apiVersion:1});
    send("streamlit:setFrameHeight", {height: Math.max(720, window.innerHeight)});
  }
  function setValue(value) {
    send("streamlit:setComponentValue", {value});
  }
  function action(name, instanceId=null) {
    setValue({
      event_id: Date.now().toString(36) + Math.random().toString(36).slice(2),
      action: name,
      instance_id: instanceId,
      session_id: cfg ? cfg.session_id : null,
    });
  }
  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  }
  function art(card) {
    if (card.image) return `<img draggable="false" src="${esc(card.image)}" alt="${esc(card.name || 'Carte')}">`;
    return `<div class="missing">${esc(card.name || 'Carte')}</div>`;
  }
  function boardStorageKey() {
    return `tcg-board-v2:${cfg.session_id}`;
  }
  function loadPositions() {
    try { return JSON.parse(localStorage.getItem(boardStorageKey()) || '{}'); }
    catch (_) { return {}; }
  }
  function savePositions(positions) {
    try { localStorage.setItem(boardStorageKey(), JSON.stringify(positions)); } catch (_) {}
  }
  function clamp(v,a,b) { return Math.max(a, Math.min(b,v)); }

  function renderBoard() {
    const board = document.getElementById('board');
    board.innerHTML = '';
    const positions = loadPositions();
    const cards = cfg.board || [];

    cards.forEach((item, index) => {
      const el = document.createElement('div');
      el.className = 'board-card';
      el.dataset.id = item.instance_id;
      const old = positions[item.instance_id];
      const fallbackX = 60 + (index % 7) * 112;
      const fallbackY = 40 + Math.floor(index / 7) * 130;
      el.style.left = `${old?.x ?? fallbackX}px`;
      el.style.top = `${old?.y ?? fallbackY}px`;
      el.style.zIndex = old?.z ?? (index + 1);
      zCounter = Math.max(zCounter, Number(old?.z || index + 1));
      el.innerHTML = `
        <div class="card-shell">${art(item)}</div>
        <div class="board-actions">
          <button class="board-action" data-act="hand" title="Reprendre en main">↩</button>
          <button class="board-action" data-act="discard" title="Défausser">🗑</button>
        </div>`;
      if (selectedBoard === item.instance_id) el.classList.add('selected');
      board.appendChild(el);

      let drag = null;
      let moved = false;
      el.addEventListener('pointerdown', ev => {
        if (ev.target.closest('button')) return;
        ev.preventDefault();
        const r = el.getBoundingClientRect();
        const br = board.getBoundingClientRect();
        drag = {dx: ev.clientX-r.left, dy: ev.clientY-r.top, br, sx:ev.clientX, sy:ev.clientY};
        moved = false;
        zCounter += 1;
        el.style.zIndex = zCounter;
        el.classList.add('dragging');
        el.setPointerCapture(ev.pointerId);
      });
      el.addEventListener('pointermove', ev => {
        if (!drag) return;
        if (Math.abs(ev.clientX-drag.sx) + Math.abs(ev.clientY-drag.sy) > 5) moved = true;
        const x = clamp(ev.clientX-drag.br.left-drag.dx, 0, board.clientWidth-el.offsetWidth);
        const y = clamp(ev.clientY-drag.br.top-drag.dy, 0, board.clientHeight-el.offsetHeight);
        el.style.left = `${x}px`;
        el.style.top = `${y}px`;
      });
      function finish() {
        if (!drag) return;
        const wasMoved = moved;
        drag = null;
        el.classList.remove('dragging');
        positions[item.instance_id] = {
          x: parseFloat(el.style.left)||0,
          y: parseFloat(el.style.top)||0,
          z: parseInt(el.style.zIndex||'1',10),
        };
        savePositions(positions);
        if (!wasMoved) {
          selectedBoard = selectedBoard === item.instance_id ? null : item.instance_id;
          selectedHand = null;
          renderBoard();
          renderHand();
        }
      }
      el.addEventListener('pointerup', finish);
      el.addEventListener('pointercancel', finish);

      el.querySelector('[data-act="hand"]').addEventListener('click', ev => {
        ev.stopPropagation(); action('board_to_hand', item.instance_id);
      });
      el.querySelector('[data-act="discard"]').addEventListener('click', ev => {
        ev.stopPropagation(); action('board_to_discard', item.instance_id);
      });
    });
  }

  function layoutHand() {
    const cards = [...document.querySelectorAll('.hand-card')];
    const n = cards.length;
    if (!n) return;
    const center = (n - 1) / 2;
    const available = Math.min(window.innerWidth * .70, 1060);
    const step = n > 1 ? Math.min(112, available / Math.max(1, n - 1)) : 0;
    const maxAngle = Math.min(17, 3.2 * center);
    cards.forEach((el, i) => {
      const rel = i - center;
      const x = rel * step;
      const normalized = center ? rel / center : 0;
      const rot = normalized * maxAngle;
      const y = Math.abs(normalized) * 18;
      el.style.setProperty('--x', `${x}px`);
      el.style.setProperty('--y', `${y}px`);
      el.style.setProperty('--r', `${rot}deg`);
      el.style.zIndex = 1000 + i;
    });
  }

  function renderHand() {
    const hand = document.getElementById('hand-zone');
    hand.innerHTML = '';
    const cards = cfg.hand || [];
    if (!cards.length) {
      hand.innerHTML = '<div class="empty-hand">Main vide</div>';
      return;
    }
    cards.forEach((item, index) => {
      const el = document.createElement('div');
      el.className = 'hand-card';
      el.dataset.id = item.instance_id;
      if (selectedHand === item.instance_id) el.classList.add('selected');
      el.innerHTML = `
        <div class="card-shell">${art(item)}</div>
        <div class="hand-actions">
          <button class="hand-action primary" data-act="board">Poser</button>
          <button class="hand-action" data-act="discard">Défausser</button>
        </div>`;
      hand.appendChild(el);
      el.addEventListener('click', ev => {
        if (ev.target.closest('button')) return;
        selectedHand = selectedHand === item.instance_id ? null : item.instance_id;
        selectedBoard = null;
        renderHand();
        renderBoard();
      });
      el.querySelector('[data-act="board"]').addEventListener('click', ev => {
        ev.stopPropagation(); action('hand_to_board', item.instance_id);
      });
      el.querySelector('[data-act="discard"]').addEventListener('click', ev => {
        ev.stopPropagation(); action('hand_to_discard', item.instance_id);
      });
    });
    requestAnimationFrame(layoutHand);
  }

  function render(nextCfg) {
    cfg = nextCfg || {};
    selectedHand = null;
    selectedBoard = null;
    const draw = document.getElementById('draw');
    draw.disabled = !cfg.can_draw;
    document.getElementById('drawCount').textContent = `${cfg.draw_count || 0} carte(s) dans la pioche`;
    document.getElementById('discardIndicator').textContent = `Défausse ${cfg.discard_count || 0}`;
    renderBoard();
    renderHand();
    send("streamlit:setFrameHeight", {height: Math.max(720, window.innerHeight)});
  }

  document.getElementById('draw').addEventListener('click', () => action('draw'));
  document.getElementById('exit').addEventListener('click', () => action('exit'));
  document.getElementById('game').addEventListener('click', ev => {
    if (ev.target.id !== 'game' && ev.target.id !== 'board') return;
    if (selectedHand || selectedBoard) {
      selectedHand = null;
      selectedBoard = null;
      renderHand();
      renderBoard();
    }
  });
  window.addEventListener('resize', () => {
    layoutHand();
    send("streamlit:setFrameHeight", {height: Math.max(720, window.innerHeight)});
  }, {passive:true});
  window.addEventListener('message', ev => {
    if (ev.data && ev.data.type === 'streamlit:render') {
      render(ev.data.args?.payload || {});
    }
  });
  ready();
})();
</script>
</body>
</html>
'''


def get_play_surface_component():
    """Crée le composant JS local utilisé par le plateau de jeu."""
    PLAY_SURFACE_COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = PLAY_SURFACE_COMPONENT_DIR / "index.html"
    try:
        current = index_path.read_text(encoding="utf-8") if index_path.is_file() else None
        if current != PLAY_SURFACE_COMPONENT_HTML:
            index_path.write_text(PLAY_SURFACE_COMPONENT_HTML, encoding="utf-8")
    except OSError:
        pass
    return components.declare_component(
        "play_surface",
        path=str(PLAY_SURFACE_COMPONENT_DIR),
    )


PLAY_SURFACE_COMPONENT = get_play_surface_component()


# ============================================================
# COMPOSANT DE TABLE MULTIJOUEUR PARTAGÉE
# ============================================================

MULTIPLAYER_SURFACE_COMPONENT_DIR = Path(__file__).resolve().parent / "_multiplayer_surface_component"

MULTIPLAYER_SURFACE_COMPONENT_HTML = r'''<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
:root { color-scheme:dark; --board-card-w:126px; --hand-card-w:170px; }
* { box-sizing:border-box; }
html,body { margin:0; width:100%; height:100%; overflow:hidden; background:#050607; }
body { font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#fff; user-select:none; }
button { font:inherit; }
#game {
  position:fixed; inset:0; overflow:hidden;
  background:
    radial-gradient(circle at 50% 50%, rgba(60,78,92,.20), rgba(10,12,14,.12) 34%, transparent 67%),
    linear-gradient(180deg,#0a0d10 0%,#050607 48%,#07090b 52%,#020303 100%);
}
#game::after {
  content:""; position:absolute; inset:0; pointer-events:none; z-index:3;
  background:radial-gradient(ellipse at center, transparent 45%, rgba(0,0,0,.55) 100%);
}
.half-board { position:absolute; left:0; right:0; height:50%; overflow:visible; z-index:10; }
#opponent-board { top:0; }
#my-board { bottom:0; }
.center-line {
  position:absolute; left:3%; right:3%; top:50%; height:1px; z-index:20; pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.20) 12%,rgba(255,255,255,.34) 50%,rgba(255,255,255,.20) 88%,transparent);
  box-shadow:0 0 22px rgba(160,220,255,.10);
}
.center-line::after {
  content:"ZONE ADVERSE   •   TA ZONE";
  position:absolute; left:50%; top:0; transform:translate(-50%,-50%);
  padding:3px 12px; border-radius:999px; background:#050607;
  color:rgba(255,255,255,.26); font-size:10px; letter-spacing:.14em; white-space:nowrap;
}
.player-label {
  position:absolute; z-index:50000; left:22px; display:flex; gap:10px; align-items:center;
  color:rgba(255,255,255,.74); pointer-events:none;
}
#opponentLabel { top:18px; }
#myLabel { bottom:22px; }
.player-name { font-weight:800; font-size:14px; color:#fff; }
.player-meta { font-size:11px; color:rgba(255,255,255,.48); }
.exit-button {
  position:absolute; right:22px; top:18px; z-index:60000; width:42px; height:42px; border-radius:50%;
  border:1px solid rgba(255,255,255,.16); background:rgba(10,10,12,.68); color:#ddd; cursor:pointer; font-size:21px;
}
.exit-button:hover { background:rgba(255,255,255,.10); color:#fff; }
.draw-wrap { position:absolute; right:26px; bottom:27px; z-index:60000; }
.draw-button {
  min-width:154px; height:52px; padding:0 23px; border-radius:999px; border:1px solid rgba(255,255,255,.24);
  background:rgba(15,18,21,.90); color:white; cursor:pointer; box-shadow:0 14px 42px rgba(0,0,0,.60);
  backdrop-filter:blur(12px); transition:transform .16s ease,border-color .16s ease;
}
.draw-button:hover:not(:disabled) { transform:translateY(-2px) scale(1.025); border-color:rgba(255,255,255,.55); }
.draw-button:disabled { opacity:.35; cursor:default; }
.draw-count { display:block; margin-top:2px; font-size:11px; color:rgba(255,255,255,.52); }
.board-card {
  position:absolute; width:var(--board-card-w); aspect-ratio:5/7; border-radius:9px; overflow:visible;
  filter:drop-shadow(0 10px 17px rgba(0,0,0,.62)); transform:translate(-50%,-50%); z-index:100;
}
.board-card.mine { cursor:grab; touch-action:none; }
.board-card.mine.dragging { cursor:grabbing; z-index:50000!important; }
.board-card:hover { z-index:45000!important; }
.board-card .card-shell {
  width:100%; height:100%; border-radius:9px; overflow:hidden; background:#181b1e;
  box-shadow:0 0 0 1px rgba(255,255,255,.11);
  transition:transform .18s cubic-bezier(.2,.8,.2,1),box-shadow .18s ease,filter .18s ease;
  transform-origin:50% 50%;
}
.board-card.mine:hover .card-shell { transform:scale(1.65); box-shadow:0 0 0 1px rgba(255,255,255,.36),0 25px 50px rgba(0,0,0,.72); }
.board-card.opponent .card-shell { transform:rotate(180deg); }
.board-card.opponent:hover .card-shell { transform:rotate(180deg) scale(1.65); box-shadow:0 0 0 1px rgba(255,255,255,.36),0 25px 50px rgba(0,0,0,.72); }
.board-card.mine.dragging .card-shell { transform:scale(1.04)!important; }
.board-card img,.hand-card img { width:100%; height:100%; display:block; object-fit:contain; pointer-events:none; }
.missing { width:100%; height:100%; display:flex; align-items:center; justify-content:center; padding:8px; text-align:center; background:#222; color:#aaa; border-radius:9px; }
.board-actions { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); display:none; gap:7px; z-index:60000; }
.board-card.mine.selected .board-actions { display:flex; }
.board-action {
  min-width:42px; height:36px; padding:0 11px; border-radius:999px; border:1px solid rgba(255,255,255,.25);
  background:rgba(0,0,0,.88); color:white; cursor:pointer; box-shadow:0 6px 20px rgba(0,0,0,.55);
}
#hand-zone {
  position:absolute; left:50%; bottom:48px; width:min(1180px,calc(100vw - 330px)); height:300px;
  transform:translateX(-50%); z-index:30000; pointer-events:none; overflow:visible;
}
.hand-card {
  --x:0px; --y:0px; --r:0deg; position:absolute; left:50%; bottom:0; width:var(--hand-card-w); aspect-ratio:5/7;
  border-radius:11px; transform-origin:50% 100%; pointer-events:auto; cursor:pointer;
  transform:translateX(calc(-50% + var(--x))) translateY(var(--y)) rotate(var(--r));
  transition:transform .18s cubic-bezier(.2,.8,.2,1),filter .18s ease; filter:drop-shadow(0 11px 19px rgba(0,0,0,.76));
}
.hand-card .card-shell { position:absolute; inset:0; border-radius:11px; overflow:hidden; background:#17191d; box-shadow:0 0 0 1px rgba(255,255,255,.12); }
.hand-card:hover { transform:translateX(calc(-50% + var(--x))) translateY(-88px) rotate(0deg) scale(1.42); z-index:52000!important; }
.hand-card.selected { transform:translateX(calc(-50% + var(--x))) translateY(-116px) rotate(0deg) scale(1.24); z-index:51000!important; }
.hand-card.selected:hover { transform:translateX(calc(-50% + var(--x))) translateY(-126px) rotate(0deg) scale(1.36); }
.hand-actions { position:absolute; left:50%; bottom:calc(100% + 12px); transform:translateX(-50%); display:none; gap:8px; white-space:nowrap; z-index:60000; }
.hand-card.selected .hand-actions { display:flex; }
.hand-action { min-width:88px; height:39px; padding:0 15px; border-radius:999px; border:1px solid rgba(255,255,255,.26); background:rgba(12,13,16,.94); color:#fff; font-weight:750; cursor:pointer; box-shadow:0 8px 28px rgba(0,0,0,.60); }
.hand-action.primary { background:#fff; color:#050505; border-color:#fff; }
.opponent-hand {
  position:absolute; top:17px; left:50%; transform:translateX(-50%); height:84px; z-index:25000; pointer-events:none;
}
.opponent-back {
  position:absolute; left:50%; top:0; width:52px; aspect-ratio:5/7; border-radius:5px;
  background:linear-gradient(145deg,#252a31,#090b0d); border:1px solid rgba(255,255,255,.14); box-shadow:0 5px 11px rgba(0,0,0,.48);
  transform-origin:50% 0%;
}
.empty-hand { position:absolute; left:50%; bottom:42px; transform:translateX(-50%); color:rgba(255,255,255,.23); font-size:12px; }
@media (max-width:900px) {
  :root { --board-card-w:105px; --hand-card-w:142px; }
  #hand-zone { width:calc(100vw - 220px); bottom:34px; height:250px; }
  .draw-wrap { right:14px; bottom:18px; }
  .draw-button { min-width:124px; height:46px; padding:0 15px; }
  .board-card.mine:hover .card-shell { transform:scale(1.48); }
  .board-card.opponent:hover .card-shell { transform:rotate(180deg) scale(1.48); }
}
</style>
</head>
<body>
<div id="game">
  <div id="opponent-board" class="half-board"></div>
  <div id="my-board" class="half-board"></div>
  <div class="center-line"></div>
  <div id="opponentLabel" class="player-label"></div>
  <div id="myLabel" class="player-label"></div>
  <div id="opponentHand" class="opponent-hand"></div>
  <div id="hand-zone"></div>
  <div class="draw-wrap"><button id="draw" class="draw-button">Piocher<span id="drawCount" class="draw-count"></span></button></div>
  <button id="exit" class="exit-button" title="Quitter la table">×</button>
</div>
<script>
(() => {
  let cfg=null, selectedHand=null, selectedBoard=null, zCounter=100, dragging=false, pendingCfg=null;
  const send=(type,data={})=>window.parent.postMessage({isStreamlitMessage:true,type,...data},"*");
  const ready=()=>{ send("streamlit:componentReady",{apiVersion:1}); send("streamlit:setFrameHeight",{height:Math.max(720,window.innerHeight)}); };
  const setValue=value=>send("streamlit:setComponentValue",{value});
  function action(name, instanceId=null, extra={}) {
    setValue({event_id:Date.now().toString(36)+Math.random().toString(36).slice(2), action:name, instance_id:instanceId, session_id:cfg?.session_id, match_id:cfg?.match_id, ...extra});
  }
  const esc=v=>String(v??"").replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  function art(card) { return card.image ? `<img draggable="false" src="${esc(card.image)}" alt="${esc(card.name||'Carte')}">` : `<div class="missing">${esc(card.name||'Carte')}</div>`; }
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

  function makeBoardCard(item, mine) {
    const el=document.createElement('div');
    el.className=`board-card ${mine?'mine':'opponent'}`;
    el.dataset.id=item.instance_id;
    const x=clamp(Number(item.x??.5),.03,.97);
    const y=clamp(Number(item.y??.5),.06,.94);
    el.style.left=`${mine ? x*100 : (1-x)*100}%`;
    el.style.top=`${mine ? y*100 : (1-y)*100}%`;
    el.style.zIndex=Number(item.z||1);
    zCounter=Math.max(zCounter,Number(item.z||1));
    el.innerHTML=`<div class="card-shell">${art(item)}</div>${mine?`<div class="board-actions"><button class="board-action" data-act="hand" title="Reprendre en main">↩</button><button class="board-action" data-act="discard" title="Défausser">🗑</button></div>`:''}`;
    if (mine && selectedBoard===item.instance_id) el.classList.add('selected');
    if (!mine) return el;

    let drag=null,moved=false;
    el.addEventListener('pointerdown',ev=>{
      if(ev.target.closest('button')) return;
      ev.preventDefault(); moved=false; dragging=true; zCounter+=1; el.style.zIndex=zCounter; el.classList.add('dragging');
      const br=document.getElementById('my-board').getBoundingClientRect();
      drag={br,sx:ev.clientX,sy:ev.clientY}; el.setPointerCapture(ev.pointerId);
    });
    el.addEventListener('pointermove',ev=>{
      if(!drag) return;
      if(Math.abs(ev.clientX-drag.sx)+Math.abs(ev.clientY-drag.sy)>5) moved=true;
      const x=clamp((ev.clientX-drag.br.left)/drag.br.width,.04,.96);
      const y=clamp((ev.clientY-drag.br.top)/drag.br.height,.08,.92);
      el.style.left=`${x*100}%`; el.style.top=`${y*100}%`;
    });
    function finish(){
      if(!drag) return; const wasMoved=moved; drag=null; dragging=false; el.classList.remove('dragging');
      if(wasMoved){
        const x=clamp(parseFloat(el.style.left)/100,0,1), y=clamp(parseFloat(el.style.top)/100,0,1);
        action('move_board',item.instance_id,{x,y,z:parseInt(el.style.zIndex||'1',10)});
      } else {
        selectedBoard=selectedBoard===item.instance_id?null:item.instance_id; selectedHand=null; renderBoards(); renderHand();
      }
      if(pendingCfg){ const next=pendingCfg; pendingCfg=null; render(next); }
    }
    el.addEventListener('pointerup',finish); el.addEventListener('pointercancel',finish);
    el.querySelector('[data-act="hand"]').addEventListener('click',ev=>{ev.stopPropagation();action('board_to_hand',item.instance_id);});
    el.querySelector('[data-act="discard"]').addEventListener('click',ev=>{ev.stopPropagation();action('board_to_discard',item.instance_id);});
    return el;
  }

  function renderBoards(){
    const mine=document.getElementById('my-board'), opp=document.getElementById('opponent-board'); mine.innerHTML=''; opp.innerHTML='';
    (cfg.my_board||[]).forEach(item=>mine.appendChild(makeBoardCard(item,true)));
    (cfg.opponent_board||[]).forEach(item=>opp.appendChild(makeBoardCard(item,false)));
  }

  function layoutHand(){
    const cards=[...document.querySelectorAll('.hand-card')], n=cards.length; if(!n)return;
    const center=(n-1)/2, available=Math.min(window.innerWidth*.67,1030), step=n>1?Math.min(114,available/Math.max(1,n-1)):0, maxAngle=Math.min(17,3.1*center);
    cards.forEach((el,i)=>{const rel=i-center,norm=center?rel/center:0; el.style.setProperty('--x',`${rel*step}px`); el.style.setProperty('--y',`${Math.abs(norm)*17}px`); el.style.setProperty('--r',`${norm*maxAngle}deg`); el.style.zIndex=1000+i;});
  }

  function renderHand(){
    const hand=document.getElementById('hand-zone'); hand.innerHTML=''; const cards=cfg.hand||[];
    if(selectedHand && !cards.some(x=>x.instance_id===selectedHand)) selectedHand=null;
    if(!cards.length){hand.innerHTML='<div class="empty-hand">Main vide</div>';return;}
    cards.forEach(item=>{
      const el=document.createElement('div'); el.className='hand-card'; el.dataset.id=item.instance_id; if(selectedHand===item.instance_id)el.classList.add('selected');
      el.innerHTML=`<div class="card-shell">${art(item)}</div><div class="hand-actions"><button class="hand-action primary" data-act="board">Poser</button><button class="hand-action" data-act="discard">Défausser</button></div>`;
      hand.appendChild(el);
      el.addEventListener('click',ev=>{if(ev.target.closest('button'))return;selectedHand=selectedHand===item.instance_id?null:item.instance_id;selectedBoard=null;renderHand();renderBoards();});
      el.querySelector('[data-act="board"]').addEventListener('click',ev=>{ev.stopPropagation();action('hand_to_board',item.instance_id);});
      el.querySelector('[data-act="discard"]').addEventListener('click',ev=>{ev.stopPropagation();action('hand_to_discard',item.instance_id);});
    }); requestAnimationFrame(layoutHand);
  }

  function renderOpponentHand(){
    const host=document.getElementById('opponentHand'); host.innerHTML=''; const n=Math.min(14,Number(cfg.opponent_hand_count||0));
    for(let i=0;i<n;i++){const el=document.createElement('div');el.className='opponent-back';const rel=i-(n-1)/2;el.style.transform=`translateX(calc(-50% + ${rel*18}px)) rotate(${rel*2.2}deg)`;el.style.zIndex=100+i;host.appendChild(el);}
  }

  function render(nextCfg){
    if(dragging){pendingCfg=nextCfg;return;} cfg=nextCfg||{};
    const handIds=new Set((cfg.hand||[]).map(x=>x.instance_id)); if(selectedHand&&!handIds.has(selectedHand))selectedHand=null;
    const boardIds=new Set((cfg.my_board||[]).map(x=>x.instance_id)); if(selectedBoard&&!boardIds.has(selectedBoard))selectedBoard=null;
    document.getElementById('draw').disabled=!cfg.can_draw;
    document.getElementById('drawCount').textContent=`${cfg.draw_count||0} carte(s)`;
    document.getElementById('myLabel').innerHTML=`<div><div class="player-name">${esc(cfg.me_name||'Toi')}</div><div class="player-meta">${esc(cfg.me_deck||'')} · Défausse ${cfg.discard_count||0}</div></div>`;
    document.getElementById('opponentLabel').innerHTML=`<div><div class="player-name">${esc(cfg.opponent_name||'Adversaire')}</div><div class="player-meta">${cfg.opponent_draw_count||0} pioche · ${cfg.opponent_hand_count||0} main · ${cfg.opponent_discard_count||0} défausse</div></div>`;
    renderBoards();renderHand();renderOpponentHand();send("streamlit:setFrameHeight",{height:Math.max(720,window.innerHeight)});
  }

  document.getElementById('draw').addEventListener('click',()=>action('draw'));
  document.getElementById('exit').addEventListener('click',()=>action('exit'));
  document.getElementById('game').addEventListener('click',ev=>{
    if(ev.target.id!=='game'&&!ev.target.classList.contains('half-board'))return;
    if(selectedHand||selectedBoard){selectedHand=null;selectedBoard=null;renderHand();renderBoards();}
  });
  window.addEventListener('resize',()=>{layoutHand();send("streamlit:setFrameHeight",{height:Math.max(720,window.innerHeight)});},{passive:true});
  window.addEventListener('message',ev=>{if(ev.data&&ev.data.type==='streamlit:render')render(ev.data.args?.payload||{});});
  ready();
})();
</script>
</body>
</html>
'''


def get_multiplayer_surface_component():
    MULTIPLAYER_SURFACE_COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = MULTIPLAYER_SURFACE_COMPONENT_DIR / "index.html"
    try:
        current = index_path.read_text(encoding="utf-8") if index_path.is_file() else None
        if current != MULTIPLAYER_SURFACE_COMPONENT_HTML:
            index_path.write_text(MULTIPLAYER_SURFACE_COMPONENT_HTML, encoding="utf-8")
    except OSError:
        pass
    return components.declare_component(
        "multiplayer_surface",
        path=str(MULTIPLAYER_SURFACE_COMPONENT_DIR),
    )


MULTIPLAYER_SURFACE_COMPONENT = get_multiplayer_surface_component()


def play_component_card(instance):
    """Version minimale d'une carte envoyée au composant plein écran."""
    card = instance["card"]
    return {
        "instance_id": str(instance["instance_id"]),
        "name": str(card.get("name") or "Carte"),
        "image": reveal_image_source(card.get("image")),
    }


def play_surface_payload(state):
    return {
        "session_id": str(state["session_id"]),
        "deck_name": str(state.get("deck_name") or "Deck"),
        "game": str(state.get("game") or ""),
        "draw_count": len(state["draw_pile"]),
        "discard_count": len(state["discard"]),
        "can_draw": bool(state["draw_pile"]),
        "hand": [play_component_card(item) for item in state["hand"]],
        "board": [play_component_card(item) for item in state["board"]],
    }


def render_play_surface(user_id, state):
    """Affiche uniquement la table plein écran pendant une partie locale."""
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"],
        header[data-testid="stHeader"],
        #MainMenu,
        footer,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] {
            display:none !important;
        }
        html, body, [data-testid="stAppViewContainer"], .stApp {
            overflow:hidden !important;
            background:#000 !important;
        }
        .block-container {
            max-width:none !important;
            padding:0 !important;
            margin:0 !important;
        }
        .st-key-play_surface_host {
            position:fixed !important;
            inset:0 !important;
            z-index:2147483000 !important;
            width:100vw !important;
            height:100vh !important;
            margin:0 !important;
            padding:0 !important;
            background:#000 !important;
        }
        .st-key-play_surface_host > div,
        .st-key-play_surface_host [data-testid="stVerticalBlock"] {
            width:100% !important;
            height:100% !important;
            margin:0 !important;
            padding:0 !important;
        }
        .st-key-play_surface_host iframe {
            position:absolute !important;
            inset:0 !important;
            width:100vw !important;
            height:100vh !important;
            min-height:100vh !important;
            border:0 !important;
            background:#000 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="play_surface_host"):
        event = PLAY_SURFACE_COMPONENT(
            payload=play_surface_payload(state),
            key=f"play_surface_{state['session_id']}",
            default=None,
        )

    if not isinstance(event, dict):
        return

    event_id = str(event.get("event_id") or "")
    if not event_id:
        return
    if st.session_state.get("last_play_surface_event") == event_id:
        return
    st.session_state["last_play_surface_event"] = event_id

    if str(event.get("session_id") or "") != str(state["session_id"]):
        return

    action_name = str(event.get("action") or "")
    instance_id = str(event.get("instance_id") or "")

    if action_name == "draw":
        play_draw_cards(1)
    elif action_name == "hand_to_board" and instance_id:
        play_hand_to_board(instance_id)
    elif action_name == "hand_to_discard" and instance_id:
        play_hand_to_discard(instance_id)
    elif action_name == "board_to_hand" and instance_id:
        play_board_to_hand(instance_id)
    elif action_name == "board_to_discard" and instance_id:
        play_board_to_discard(instance_id)
    elif action_name == "exit":
        st.session_state.pop("local_play", None)
        st.session_state.pop("last_play_surface_event", None)
    else:
        return

    st.rerun()


def multiplayer_component_card(instance):
    card = instance.get("card") or {}
    return {
        "instance_id": str(instance.get("instance_id") or ""),
        "name": str(card.get("name") or "Carte"),
        "image": reveal_image_source(card.get("image")),
    }


def multiplayer_surface_payload(state):
    my_board = []
    for item in state.get("my_board", []):
        payload = multiplayer_component_card(item)
        payload.update({
            "x": float(item.get("x", 0.5)),
            "y": float(item.get("y", 0.5)),
            "z": int(item.get("z", 1)),
        })
        my_board.append(payload)

    opponent_board = []
    for item in state.get("opponent_board", []):
        payload = multiplayer_component_card(item)
        payload.update({
            "x": float(item.get("x", 0.5)),
            "y": float(item.get("y", 0.5)),
            "z": int(item.get("z", 1)),
        })
        opponent_board.append(payload)

    me = state["me"]
    opponent = state["opponent"]
    return {
        "session_id": str(state["session_id"]),
        "match_id": int(state["match_id"]),
        "version": int(state.get("version", 0)),
        "game": str(state.get("game") or ""),
        "me_name": str(me.get("username") or "Toi"),
        "me_deck": str(me.get("deck_name") or ""),
        "draw_count": len(me.get("draw_pile", [])),
        "discard_count": len(me.get("discard", [])),
        "can_draw": bool(me.get("draw_pile")),
        "hand": [multiplayer_component_card(item) for item in me.get("hand", [])],
        "my_board": my_board,
        "opponent_name": str(opponent.get("username") or "Adversaire"),
        "opponent_draw_count": int(opponent.get("draw_count", 0)),
        "opponent_hand_count": int(opponent.get("hand_count", 0)),
        "opponent_discard_count": int(opponent.get("discard_count", 0)),
        "opponent_board": opponent_board,
    }


def render_multiplayer_surface(user_id, state):
    """Table partagée : le joueur courant est toujours affiché en bas."""
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"],
        header[data-testid="stHeader"],
        #MainMenu, footer,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] { display:none !important; }
        html, body, [data-testid="stAppViewContainer"], .stApp {
            overflow:hidden !important; background:#050607 !important;
        }
        .block-container { max-width:none !important; padding:0 !important; margin:0 !important; }
        .st-key-multiplayer_surface_host {
            position:fixed !important; inset:0 !important; z-index:2147483000 !important;
            width:100vw !important; height:100vh !important; margin:0 !important; padding:0 !important;
            background:#050607 !important;
        }
        .st-key-multiplayer_surface_host > div,
        .st-key-multiplayer_surface_host [data-testid="stVerticalBlock"] {
            width:100% !important; height:100% !important; margin:0 !important; padding:0 !important;
        }
        .st-key-multiplayer_surface_host iframe {
            position:absolute !important; inset:0 !important; width:100vw !important; height:100vh !important;
            min-height:100vh !important; border:0 !important; background:#050607 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="multiplayer_surface_host"):
        event = MULTIPLAYER_SURFACE_COMPONENT(
            payload=multiplayer_surface_payload(state),
            key=f"multiplayer_surface_{state['match_id']}_{user_id}",
            default=None,
        )

    if not isinstance(event, dict):
        return

    event_id = str(event.get("event_id") or "")
    if not event_id:
        return
    last_key = f"last_multiplayer_surface_event_{state['match_id']}"
    if st.session_state.get(last_key) == event_id:
        return
    st.session_state[last_key] = event_id

    if str(event.get("session_id") or "") != str(state["session_id"]):
        return
    if int(event.get("match_id") or 0) != int(state["match_id"]):
        return

    action_name = str(event.get("action") or "")
    instance_id = str(event.get("instance_id") or "")
    match_id = int(state["match_id"])

    if action_name == "draw":
        multiplayer_draw_card(user_id, match_id)
    elif action_name == "hand_to_board" and instance_id:
        multiplayer_hand_to_board(user_id, match_id, instance_id)
    elif action_name == "hand_to_discard" and instance_id:
        multiplayer_hand_to_discard(user_id, match_id, instance_id)
    elif action_name == "board_to_hand" and instance_id:
        multiplayer_board_to_zone(user_id, match_id, instance_id, "hand")
    elif action_name == "board_to_discard" and instance_id:
        multiplayer_board_to_zone(user_id, match_id, instance_id, "discard")
    elif action_name == "move_board" and instance_id:
        try:
            x = float(event.get("x", 0.5))
            y = float(event.get("y", 0.5))
            z = int(event.get("z", 1))
        except (TypeError, ValueError):
            return
        multiplayer_move_board_card(user_id, match_id, instance_id, x, y, z)
    elif action_name == "exit":
        st.session_state.pop("multiplayer_match_id", None)
        st.session_state.pop(last_key, None)
        st.rerun()
        return
    else:
        return

    st.rerun()


def _multiplayer_table_fragment(user_id, match_id):
    state = load_multiplayer_match_state(user_id, match_id)
    if state is None:
        st.session_state.pop("multiplayer_match_id", None)
        st.warning("Cette partie n'est plus active.")
        return
    render_multiplayer_surface(user_id, state)


if hasattr(st, "fragment"):
    multiplayer_table_fragment = st.fragment(run_every="1s")(_multiplayer_table_fragment)
else:
    multiplayer_table_fragment = _multiplayer_table_fragment


def render_free_board(user_id, state):
    """Plateau libre : les cartes sont déplaçables au pixel près dans le navigateur.

    Les positions sont sauvegardées dans localStorage. Elles survivent donc aux
    reruns Streamlit provoqués par une pioche ou une défausse.
    """
    cards_html = []
    for instance in state["board"]:
        card = instance["card"]
        image_src = reveal_image_source(card.get("image"))
        name = html.escape(str(card.get("name") or "Carte"))
        instance_id = html.escape(str(instance["instance_id"]), quote=True)
        if image_src:
            artwork = (
                f'<img draggable="false" src="{html.escape(image_src, quote=True)}" '
                f'alt="{name}">'
            )
        else:
            artwork = f'<div class="missing">{name}</div>'
        cards_html.append(
            f'<div class="board-card" data-id="{instance_id}">{artwork}</div>'
        )

    empty_text = "" if cards_html else '<div class="empty">Pose une carte depuis ta main</div>'
    storage_key = f"tcg-board:{user_id}:{state['session_id']}"

    components.html(
        f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
html, body {{ margin:0; background:#0b0f12; overflow:hidden; font-family:Inter,system-ui,sans-serif; }}
#board {{
    position:relative;
    width:100%;
    height:640px;
    overflow:hidden;
    border:1px solid #ffffff22;
    border-radius:18px;
    background:
      radial-gradient(circle at 50% 40%, #17372b 0%, #10281f 38%, #09140f 78%),
      #0d1b15;
    box-shadow: inset 0 0 90px #0008;
}}
#board::before {{
    content:"";
    position:absolute;
    inset:0;
    pointer-events:none;
    opacity:.16;
    background-image:
      linear-gradient(#ffffff22 1px, transparent 1px),
      linear-gradient(90deg, #ffffff22 1px, transparent 1px);
    background-size:42px 42px;
}}
.board-card {{
    position:absolute;
    width:145px;
    aspect-ratio:5/7;
    left:20px;
    top:20px;
    border-radius:10px;
    overflow:hidden;
    cursor:grab;
    user-select:none;
    touch-action:none;
    transform:translateZ(0);
    box-shadow:0 10px 25px #0009, 0 0 0 1px #ffffff28;
    transition:box-shadow .12s ease, scale .12s ease;
}}
.board-card:active {{
    cursor:grabbing;
    scale:1.035;
    box-shadow:0 18px 38px #000c, 0 0 0 2px #ffffff55;
}}
.board-card img {{ width:100%; height:100%; display:block; object-fit:contain; pointer-events:none; }}
.missing {{
    width:100%; height:100%; display:flex; align-items:center; justify-content:center;
    padding:12px; text-align:center; color:#ddd; background:#252a31;
}}
.empty {{
    position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
    color:#ffffff55; font-size:20px; pointer-events:none;
}}
.board-help {{
    position:absolute; right:14px; bottom:10px; color:#ffffff55; font-size:12px;
    pointer-events:none;
}}
</style>
</head>
<body>
<div id="board">
  {''.join(cards_html)}
  {empty_text}
  <div class="board-help">Maintiens et déplace les cartes librement</div>
</div>
<script>
(() => {{
    const board = document.getElementById('board');
    const storageKey = {json.dumps(storage_key)};
    const cards = [...document.querySelectorAll('.board-card')];
    let saved = {{}};
    try {{ saved = JSON.parse(localStorage.getItem(storageKey) || '{{}}'); }} catch (_) {{}}
    let zCounter = 10;

    function clamp(v, a, b) {{ return Math.max(a, Math.min(b, v)); }}
    function savePosition(el) {{
        saved[el.dataset.id] = {{
            x: parseFloat(el.style.left) || 0,
            y: parseFloat(el.style.top) || 0,
            z: parseInt(el.style.zIndex || '1', 10),
        }};
        try {{ localStorage.setItem(storageKey, JSON.stringify(saved)); }} catch (_) {{}}
    }}

    cards.forEach((el, index) => {{
        const old = saved[el.dataset.id];
        if (old) {{
            el.style.left = old.x + 'px';
            el.style.top = old.y + 'px';
            el.style.zIndex = old.z || (index + 1);
            zCounter = Math.max(zCounter, old.z || 0);
        }} else {{
            const col = index % 6;
            const row = Math.floor(index / 6);
            el.style.left = (24 + col * 105) + 'px';
            el.style.top = (24 + row * 120) + 'px';
            el.style.zIndex = index + 1;
            savePosition(el);
        }}

        let drag = null;
        el.addEventListener('pointerdown', (event) => {{
            event.preventDefault();
            el.setPointerCapture(event.pointerId);
            const rect = el.getBoundingClientRect();
            const boardRect = board.getBoundingClientRect();
            drag = {{
                dx: event.clientX - rect.left,
                dy: event.clientY - rect.top,
                boardRect,
            }};
            zCounter += 1;
            el.style.zIndex = zCounter;
        }});

        el.addEventListener('pointermove', (event) => {{
            if (!drag) return;
            const maxX = board.clientWidth - el.offsetWidth;
            const maxY = board.clientHeight - el.offsetHeight;
            const x = clamp(event.clientX - drag.boardRect.left - drag.dx, 0, maxX);
            const y = clamp(event.clientY - drag.boardRect.top - drag.dy, 0, maxY);
            el.style.left = x + 'px';
            el.style.top = y + 'px';
        }});

        function endDrag() {{
            if (!drag) return;
            drag = null;
            savePosition(el);
        }}
        el.addEventListener('pointerup', endDrag);
        el.addEventListener('pointercancel', endDrag);
    }});
}})();
</script>
</body>
</html>""",
        height=660,
        scrolling=False,
    )


def show_play_card(instance, *, location, session_id):
    card = instance["card"]
    iid = instance["instance_id"]
    with st.container(border=True):
        image = card.get("image")
        if image is not None and not (isinstance(image, float) and math.isnan(image)) and str(image).strip():
            st.image(image, use_container_width=True)
        else:
            st.markdown(
                '<div style="width:100%;aspect-ratio:5/7;background:#333;border-radius:9px;'
                'display:flex;align-items:center;justify-content:center;color:#bbb;">Image indisponible</div>',
                unsafe_allow_html=True,
            )
        st.markdown(f"**{card.get('name') or 'Carte'}**")
        details = card_details(card)
        if details:
            st.caption(details)

        if location == "hand":
            c1, c2 = st.columns(2)
            if c1.button(
                "Poser",
                key=f"play_board_{session_id}_{iid}",
                use_container_width=True,
            ):
                play_hand_to_board(iid)
                st.rerun()
            if c2.button(
                "Défausser",
                key=f"play_discard_{session_id}_{iid}",
                use_container_width=True,
            ):
                play_hand_to_discard(iid)
                st.rerun()


def deck_page(user_id):
    st.header("Decks")

    # ========================================================
    # CRÉATION / LISTE DES DECKS
    # ========================================================

    create_col, list_col = st.columns([1, 2])

    with create_col:
        st.subheader("Nouveau deck")

        new_game = st.selectbox(
            "Jeu",
            list(GAME_LABELS),
            format_func=lambda value: GAME_LABELS[value],
            key="new_deck_game",
        )

        new_name = st.text_input(
            "Nom du deck",
            key="new_deck_name",
        )

        if st.button(
            "Créer le deck",
            type="primary",
            use_container_width=True,
        ):
            success, message, deck_id = create_deck(
                user_id,
                new_name,
                new_game,
            )

            if success:
                st.session_state["selected_deck_id"] = deck_id
                st.success(message)
                st.rerun()

            st.error(message)

    decks = list_user_decks(user_id)

    with list_col:
        st.subheader("Mes decks")

        if decks.empty:
            st.info("Tu n'as encore aucun deck.")
        else:
            shown = decks.copy()
            shown["Jeu"] = shown["game"].map(GAME_LABELS)
            shown["Cartes"] = shown["card_count"].astype(int)
            shown["Uniques"] = shown["unique_cards"].astype(int)
            shown = shown.rename(columns={"name": "Nom"})

            st.dataframe(
                shown[["Nom", "Jeu", "Cartes", "Uniques"]],
                hide_index=True,
                use_container_width=True,
            )

    if decks.empty:
        return

    # ========================================================
    # SÉLECTION DU DECK
    # ========================================================

    options = decks["deck_id"].astype(int).tolist()
    current = st.session_state.get("selected_deck_id")

    if current not in options:
        current = options[0]
        st.session_state["selected_deck_id"] = current

    deck_id = st.selectbox(
        "Modifier un deck",
        options,
        index=options.index(current),
        format_func=lambda value: (
            f"{decks.loc[decks['deck_id'] == value, 'name'].iloc[0]} — "
            f"{GAME_LABELS[decks.loc[decks['deck_id'] == value, 'game'].iloc[0]]}"
        ),
        key="selected_deck_id",
    )

    deck = get_user_deck(user_id, deck_id)

    if not deck:
        return

    st.divider()

    title_col, delete_col = st.columns([4, 1])

    with title_col:
        renamed = st.text_input(
            "Nom",
            value=deck["name"],
            key=f"rename_deck_{deck_id}",
        )

        if (
            renamed.strip() != deck["name"]
            and st.button(
                "Renommer",
                key=f"rename_button_{deck_id}",
            )
        ):
            success, message = rename_deck(
                user_id,
                deck_id,
                renamed,
            )
            (st.success if success else st.error)(message)
            st.rerun()

    with delete_col:
        st.write("")
        st.write("")

        if st.button(
            "Supprimer",
            key=f"delete_deck_{deck_id}",
            type="secondary",
        ):
            delete_deck(user_id, deck_id)

            local_play = st.session_state.get("local_play") or {}

            if local_play.get("deck_id") == deck_id:
                st.session_state.pop("local_play", None)

            st.session_state.pop("selected_deck_id", None)
            st.rerun()

    # ========================================================
    # DONNÉES DU DECK / COLLECTION
    # ========================================================

    owned = build_owned_card_catalog(
        user_id,
        deck["game"],
    )

    deck_cards = load_deck_cards(deck_id)

    if owned.empty:
        st.warning("Tu ne possèdes encore aucune carte de ce jeu.")
        return

    owned = owned.copy()
    owned["card_key"] = owned["card_key"].astype(str)

    if not deck_cards.empty:
        deck_cards = deck_cards.copy()
        deck_cards["card_key"] = deck_cards["card_key"].astype(str)

    deck_qty = (
        dict(
            zip(
                deck_cards["card_key"],
                deck_cards["quantity"].astype(int),
            )
        )
        if not deck_cards.empty
        else {}
    )

    owned_qty = dict(
        zip(
            owned["card_key"],
            owned["owned_quantity"].astype(int),
        )
    )

    total_cards = sum(deck_qty.values())
    unique_cards = len(deck_qty)

    st.markdown(
        f"### {GAME_LABELS[deck['game']]} · {deck['name']}"
    )

    metric1, metric2 = st.columns(2)

    with metric1:
        st.metric("Cartes dans le deck", total_cards)

    with metric2:
        st.metric("Cartes uniques", unique_cards)

    # ========================================================
    # PETITE FONCTION D'AFFICHAGE D'IMAGE
    # ========================================================

    def show_builder_image(source):
        has_image = (
            source is not None
            and not (
                isinstance(source, float)
                and math.isnan(source)
            )
            and str(source).strip()
        )

        if has_image:
            try:
                st.image(
                    source,
                    use_container_width=True,
                )
                return
            except Exception:
                pass

        st.markdown(
            """
            <div style="
                width:100%;
                aspect-ratio:5/7;
                border-radius:10px;
                background:#25262d;
                border:1px solid #444;
                display:flex;
                align-items:center;
                justify-content:center;
                color:#999;
                font-size:30px;
            ">🃏</div>
            """,
            unsafe_allow_html=True,
        )

    # ========================================================
    # CARTES ACTUELLEMENT DANS LE DECK
    # ========================================================

    st.subheader("Deck")

    if deck_cards.empty:
        st.info("Deck vide. Ajoute des cartes depuis la galerie juste en dessous.")
    else:
        # On récupère les images et les quantités possédées depuis le catalogue.
        enriched = deck_cards.merge(
            owned[
                [
                    "card_key",
                    "image",
                    "owned_quantity",
                    "drop_class",
                ]
            ],
            on="card_key",
            how="left",
            suffixes=("", "_owned"),
        )

        # Cartes volontairement plus grandes dans la zone du deck.
        DECK_COLUMNS = 4

        for start_index in range(
            0,
            len(enriched),
            DECK_COLUMNS,
        ):
            columns = st.columns(DECK_COLUMNS)
            rows = enriched.iloc[
                start_index:
                start_index + DECK_COLUMNS
            ]

            for column, (_, card) in zip(
                columns,
                rows.iterrows(),
            ):
                card_key = str(card["card_key"])
                current_qty = int(card["quantity"])
                max_qty = int(
                    card.get("owned_quantity", current_qty)
                    if not pd.isna(card.get("owned_quantity", current_qty))
                    else current_qty
                )

                with column:
                    with st.container(border=True):
                        # Dans le deck, on privilégie l'artwork : aucune
                        # information textuelle supplémentaire n'est affichée.
                        show_builder_image(card.get("image"))

                        st.markdown(
                            f"<div style='text-align:center; font-size:1.25rem; "
                            f"font-weight:800; margin:.15rem 0 .35rem 0;'>"
                            f"×{current_qty}</div>",
                            unsafe_allow_html=True,
                        )

                        minus_col, plus_col = st.columns(2, gap="small")

                        with minus_col:
                            if st.button(
                                "−",
                                key=(
                                    f"deck_remove_one_"
                                    f"{deck_id}_{card_key}"
                                ),
                                use_container_width=True,
                                help="Retirer une copie du deck",
                            ):
                                success, message = set_deck_card_quantity(
                                    user_id,
                                    deck_id,
                                    card_key,
                                    max(0, current_qty - 1),
                                )

                                if not success:
                                    st.error(message)
                                else:
                                    st.rerun()

                        with plus_col:
                            if st.button(
                                "+",
                                key=(
                                    f"deck_add_one_top_"
                                    f"{deck_id}_{card_key}"
                                ),
                                use_container_width=True,
                                disabled=current_qty >= max_qty,
                                help=(
                                    "Ajouter une copie supplémentaire"
                                    if current_qty < max_qty
                                    else "Toutes tes copies sont déjà dans le deck"
                                ),
                            ):
                                success, message = set_deck_card_quantity(
                                    user_id,
                                    deck_id,
                                    card_key,
                                    current_qty + 1,
                                )

                                if not success:
                                    st.error(message)
                                else:
                                    st.rerun()

    st.divider()

    # ========================================================
    # GALERIE DE CARTES DISPONIBLES
    # ========================================================

    st.subheader("Ajouter des cartes")

    filter1, filter2 = st.columns(2)

    with filter1:
        set_values = ["Toutes"] + sorted(
            owned["product_set"]
            .fillna("")
            .astype(str)
            .unique()
            .tolist()
        )

        set_filter = st.selectbox(
            "Extension",
            set_values,
            key=f"deck_set_filter_{deck_id}",
        )

    with filter2:
        search = st.text_input(
            "Rechercher une carte",
            key=f"deck_search_{deck_id}",
        ).strip().lower()

    filtered = owned.copy()

    if set_filter != "Toutes":
        filtered = filtered[
            filtered["product_set"].astype(str)
            == set_filter
        ]

    if search:
        filtered = filtered[
            filtered["name"]
            .fillna("")
            .astype(str)
            .str.lower()
            .str.contains(
                search,
                regex=False,
            )
            |
            filtered["card_number"]
            .fillna("")
            .astype(str)
            .str.lower()
            .str.contains(
                search,
                regex=False,
            )
        ]

    if filtered.empty:
        st.info("Aucune carte ne correspond à ce filtre.")
        return

    # Pagination volontairement large : on garde une galerie visuelle sans
    # créer plusieurs centaines de widgets sur une seule page Streamlit.
    PAGE_SIZE = 36
    page_count = max(
        1,
        math.ceil(len(filtered) / PAGE_SIZE),
    )

    if page_count > 1:
        page = st.number_input(
            "Page",
            min_value=1,
            max_value=page_count,
            value=1,
            step=1,
            key=f"deck_gallery_page_{deck_id}_{set_filter}",
        )
    else:
        page = 1

    page_start = (int(page) - 1) * PAGE_SIZE
    visible = filtered.iloc[
        page_start:
        page_start + PAGE_SIZE
    ]

    GALLERY_COLUMNS = 6

    for start_index in range(
        0,
        len(visible),
        GALLERY_COLUMNS,
    ):
        columns = st.columns(GALLERY_COLUMNS)
        rows = visible.iloc[
            start_index:
            start_index + GALLERY_COLUMNS
        ]

        for column, (_, card) in zip(
            columns,
            rows.iterrows(),
        ):
            card_key = str(card["card_key"])
            current_qty = int(deck_qty.get(card_key, 0))
            max_qty = int(card.get("owned_quantity", 0))
            can_add = current_qty < max_qty

            with column:
                with st.container(border=True):
                    show_builder_image(card.get("image"))

                    st.markdown(
                        f"**{card.get('name', '')}**"
                    )

                    details = (
                        f"{card.get('product_set', '')} "
                        f"{card.get('card_number', '')}"
                    ).strip()

                    if card.get("rarity"):
                        details += f" · {card.get('rarity')}"

                    st.caption(details)

                    st.caption(
                        f"Deck ×{current_qty} / Possédé ×{max_qty}"
                    )

                    if st.button(
                        "+ Ajouter",
                        key=(
                            f"deck_gallery_add_"
                            f"{deck_id}_{card_key}"
                        ),
                        use_container_width=True,
                        type="primary" if can_add else "secondary",
                        disabled=not can_add,
                    ):
                        success, message = set_deck_card_quantity(
                            user_id,
                            deck_id,
                            card_key,
                            current_qty + 1,
                        )

                        if not success:
                            st.error(message)
                        else:
                            st.rerun()

def _multiplayer_lobby_panel(user_id):
    """Lobby rafraîchi automatiquement pour les invitations entre amis."""
    decks = list_user_decks(user_id)
    friends = list_friends(user_id)
    active_matches = list_active_matches(user_id)
    incoming = list_incoming_game_invites(user_id)
    outgoing = list_outgoing_game_invites(user_id)

    if active_matches:
        st.subheader("Partie en cours")
        for match in active_matches:
            with st.container(border=True):
                info_col, join_col, close_col = st.columns([5, 1.35, 1.15])
                with info_col:
                    st.markdown(f"**{match['opponent_username']}**")
                    st.caption(
                        f"{GAME_LABELS.get(match['game'], match['game'])} · "
                        f"{match['deck_name']}"
                    )
                with join_col:
                    if st.button(
                        "Rejoindre",
                        key=f"join_match_{match['match_id']}",
                        type="primary",
                        use_container_width=True,
                    ):
                        st.session_state["multiplayer_match_id"] = int(match["match_id"])
                        st.session_state.pop("local_play", None)
                        st.rerun()
                with close_col:
                    if st.button(
                        "Terminer",
                        key=f"close_match_{match['match_id']}",
                        use_container_width=True,
                    ):
                        close_multiplayer_match(user_id, match["match_id"])
                        if st.session_state.get("multiplayer_match_id") == int(match["match_id"]):
                            st.session_state.pop("multiplayer_match_id", None)
                        st.rerun()
        st.divider()

    st.subheader("Inviter un ami")
    if decks.empty:
        st.warning("Crée d'abord un deck dans l'onglet Decks.")
    elif not friends:
        st.info("Ajoute d'abord un ami dans l'onglet Amis.")
    else:
        usable_decks = decks[decks["card_count"].fillna(0).astype(int) > 0].copy()
        if usable_decks.empty:
            st.warning("Tes decks sont vides. Ajoute des cartes avant de lancer une partie.")
        else:
            deck_options = usable_decks["deck_id"].astype(int).tolist()
            selected_deck_id = st.selectbox(
                "Ton deck",
                deck_options,
                format_func=lambda value: (
                    f"{usable_decks.loc[usable_decks['deck_id'] == value, 'name'].iloc[0]} — "
                    f"{GAME_LABELS.get(usable_decks.loc[usable_decks['deck_id'] == value, 'game'].iloc[0], usable_decks.loc[usable_decks['deck_id'] == value, 'game'].iloc[0])} — "
                    f"{int(usable_decks.loc[usable_decks['deck_id'] == value, 'card_count'].iloc[0])} cartes"
                ),
                key="multiplayer_invite_deck",
            )
            for friend in friends:
                with st.container(border=True):
                    c1, c2 = st.columns([5, 1.5])
                    with c1:
                        st.markdown(f"**{friend['username']}**")
                    with c2:
                        if st.button(
                            "Inviter",
                            key=f"invite_friend_{friend['user_id']}",
                            use_container_width=True,
                        ):
                            ok, message = send_game_invite(
                                user_id,
                                friend["user_id"],
                                selected_deck_id,
                            )
                            if ok:
                                st.toast(message)
                            else:
                                st.warning(message)
                            st.rerun()

    if incoming:
        st.divider()
        st.subheader(f"Invitations reçues · {len(incoming)}")
        for invite in incoming:
            compatible = decks[
                (decks["game"] == invite["game"])
                & (decks["card_count"].fillna(0).astype(int) > 0)
            ].copy() if not decks.empty else pd.DataFrame()

            with st.container(border=True):
                st.markdown(f"**{invite['sender_username']} t'invite**")
                st.caption(
                    f"{GAME_LABELS.get(invite['game'], invite['game'])} · "
                    f"Deck adverse : {invite['sender_deck_name']}"
                )
                if compatible.empty:
                    st.warning("Tu n'as aucun deck non vide pour ce jeu.")
                    if st.button(
                        "Refuser",
                        key=f"decline_game_invite_{invite['invite_id']}",
                    ):
                        decline_game_invite(user_id, invite["invite_id"])
                        st.rerun()
                else:
                    options = compatible["deck_id"].astype(int).tolist()
                    my_deck_id = st.selectbox(
                        "Ton deck pour cette partie",
                        options,
                        format_func=lambda value: (
                            f"{compatible.loc[compatible['deck_id'] == value, 'name'].iloc[0]} — "
                            f"{int(compatible.loc[compatible['deck_id'] == value, 'card_count'].iloc[0])} cartes"
                        ),
                        key=f"accept_deck_{invite['invite_id']}",
                    )
                    accept_col, decline_col = st.columns(2)
                    with accept_col:
                        if st.button(
                            "✓ Accepter",
                            key=f"accept_game_invite_{invite['invite_id']}",
                            type="primary",
                            use_container_width=True,
                        ):
                            ok, message, match_id = accept_game_invite(
                                user_id,
                                invite["invite_id"],
                                my_deck_id,
                            )
                            if ok:
                                st.session_state["multiplayer_match_id"] = int(match_id)
                                st.session_state.pop("local_play", None)
                                st.rerun()
                            st.warning(message)
                    with decline_col:
                        if st.button(
                            "✕ Refuser",
                            key=f"decline_game_invite_{invite['invite_id']}",
                            use_container_width=True,
                        ):
                            decline_game_invite(user_id, invite["invite_id"])
                            st.rerun()

    if outgoing:
        st.divider()
        with st.expander(f"Invitations envoyées · {len(outgoing)}", expanded=True):
            for invite in outgoing:
                c1, c2 = st.columns([5, 1.4])
                with c1:
                    st.write(f"{invite['receiver_username']}")
                    st.caption(
                        f"{GAME_LABELS.get(invite['game'], invite['game'])} · "
                        f"{invite['sender_deck_name']} · En attente"
                    )
                with c2:
                    if st.button(
                        "Annuler",
                        key=f"cancel_game_invite_{invite['invite_id']}",
                        use_container_width=True,
                    ):
                        cancel_game_invite(user_id, invite["invite_id"])
                        st.rerun()


if hasattr(st, "fragment"):
    multiplayer_lobby_panel = st.fragment(run_every="1s")(_multiplayer_lobby_panel)
else:
    multiplayer_lobby_panel = _multiplayer_lobby_panel


def play_page(user_id):
    # Une table multijoueur active prend tout l'écran. Les deux navigateurs
    # lisent et modifient le même état SQLite ; chaque client affiche son
    # propre demi-plateau en bas et celui de l'adversaire en haut.
    match_id = st.session_state.get("multiplayer_match_id")
    if match_id:
        if load_multiplayer_match_state(user_id, match_id) is not None:
            multiplayer_table_fragment(user_id, int(match_id))
            return
        st.session_state.pop("multiplayer_match_id", None)

    # Le mode local reste disponible comme bac à sable de test.
    local_state = st.session_state.get("local_play")
    if local_state:
        render_play_surface(user_id, local_state)
        return

    st.header("Jouer")
    multiplayer_lobby_panel(user_id)

    st.divider()
    with st.expander("Table solo de test"):
        decks = list_user_decks(user_id)
        if decks.empty:
            st.info("Crée d'abord un deck dans l'onglet Decks.")
            return

        deck_options = decks["deck_id"].astype(int).tolist()
        selected_deck_id = st.selectbox(
            "Deck solo",
            deck_options,
            format_func=lambda value: (
                f"{decks.loc[decks['deck_id'] == value, 'name'].iloc[0]} — "
                f"{GAME_LABELS[decks.loc[decks['deck_id'] == value, 'game'].iloc[0]]} — "
                f"{int(decks.loc[decks['deck_id'] == value, 'card_count'].iloc[0])} cartes"
            ),
            key="play_deck_selector",
        )
        selected_size = int(
            decks.loc[decks["deck_id"] == selected_deck_id, "card_count"].iloc[0]
        )
        if st.button(
            "Entrer sur la table solo",
            type="secondary",
            use_container_width=True,
            disabled=selected_size <= 0,
        ):
            success, message = start_local_play_session(user_id, selected_deck_id)
            if success:
                st.session_state.pop("last_play_surface_event", None)
                st.rerun()
            st.error(message)


# ============================================================
# AFFICHAGE DES CARTES
# ============================================================


def card_details(card):
    pieces = []
    for key in ("card_number", "rarity", "variant", "drop_class"):
        value = str(card.get(key) or "").strip()
        if value and value not in pieces and value.lower() != "base":
            pieces.append(value)
    finish = str(card.get("_finish") or "").strip()
    if finish and finish not in pieces:
        pieces.append(finish)
    return " · ".join(pieces)


def show_card(card, quantity=None, cartedex_mode=False):
    if cartedex_mode and quantity is not None and quantity <= 0:
        with st.container(border=True):
            st.markdown(
                '<div style="width:100%;aspect-ratio:5/7;'
                'background:#b8b8b8;border-radius:10px;'
                'border:1px solid #8a8a8a;margin-bottom:10px;"></div>',
                unsafe_allow_html=True,
            )
            st.markdown(f"**{card['name']}**")
        return

    with st.container(border=True):
        image = card.get("image")
        if image:
            try:
                st.image(image, use_container_width=True)
            except Exception:
                st.markdown("### Carte")
        else:
            st.markdown(
                '<div style="width:100%;aspect-ratio:5/7;'
                'background:#444;border-radius:10px;display:flex;'
                'align-items:center;justify-content:center;color:#ddd;'
                'margin-bottom:10px;">Image indisponible</div>',
                unsafe_allow_html=True,
            )

        st.markdown(f"**{card['name']}**")
        details = card_details(card)
        if details:
            st.caption(details)
        if quantity is not None and quantity > 0:
            st.success(f"Possédé ×{quantity}")


def rarity_rank(card):
    game = card.get("game")
    text = clean_text(
        " ".join(
            str(card.get(field) or "")
            for field in ("rarity", "variant", "drop_class", "_slot")
        )
    )

    if game == "onepiece":
        if "manga" in text or "comic" in text:
            return 100
        if "signature" in text or "super alt" in text:
            return 95
        if "treasure" in text:
            return 90
        if "sp" in text:
            return 85
        if "alt" in text or "parallel" in text:
            return 80
        if "sec" in text:
            return 70
        if "sr" in text:
            return 60
        if "rare" in text or re.search(r"\br\b", text):
            return 50
        if "don" in text:
            return 30
        if "uncommon" in text or "uc" in text:
            return 20
        return 10

    if game == "pokemon":
        order = [
            ("mega hyper", 100),
            ("special illustration", 95),
            ("black white", 94),
            ("hyper rare", 90),
            ("mega attack", 88),
            ("ultra rare", 85),
            ("illustration rare", 80),
            ("shiny ultra", 78),
            ("shiny rare", 76),
            ("double rare", 70),
            ("master ball", 65),
            ("ace spec", 62),
            ("rare", 55),
            ("reverse", 40),
            ("uncommon", 20),
            ("common", 10),
            ("energie", 0),
        ]
        for needle, rank in order:
            if needle in text:
                return rank
        return 30

    order = [
        ("ultimate", 100),
        ("signature", 95),
        ("overnumber", 90),
        ("special alt", 87),
        ("alt", 85),
        ("showcase", 84),
        ("epic", 70),
        ("rare", 55),
        ("foil", 40),
        ("uncommon", 20),
        ("common", 10),
        ("token", 0),
        ("rune", 0),
    ]
    for needle, rank in order:
        if needle in text:
            return rank
    return 30


def booster_reveal_order(card):
    return rarity_rank(card)


def reveal_image_source(source):
    if not source:
        return ""
    source = str(source)
    if source.startswith(("https://", "http://", "data:image/")):
        return source
    try:
        path = Path(source)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return ""


def reveal_effect_profile(card):
    """Retourne un thème visuel selon le jeu et la rareté révélée."""
    game = str(card.get("game") or "")
    text = clean_text(
        " ".join(
            str(card.get(field) or "")
            for field in ("rarity", "variant", "drop_class", "_slot", "_treatment")
        )
    )
    rank = rarity_rank(card)

    # Common / Uncommon : aucune mise en scène supplémentaire.
    if rank <= 20:
        return {
            "name": "none",
            "label": "",
            "primary": "#8d99ae",
            "secondary": "#adb5bd",
            "accent": "#ffffff",
            "particles": 0,
            "flash": False,
            "duration": 0.75,
        }

    if game == "onepiece":
        if "manga" in text or "comic" in text:
            return {"name": "manga", "label": "MANGA", "primary": "#ff2d2d", "secondary": "#111111", "accent": "#ffd166", "particles": 22, "flash": True, "duration": 1.35}
        if "signature" in text or "super alt" in text:
            return {"name": "legendary", "label": "SIGNATURE", "primary": "#ffd166", "secondary": "#ff4d8d", "accent": "#ffffff", "particles": 24, "flash": True, "duration": 1.30}
        if "treasure" in text:
            return {"name": "emerald", "label": "TREASURE", "primary": "#1dd3b0", "secondary": "#ffd166", "accent": "#ffffff", "particles": 18, "flash": True, "duration": 1.20}
        if re.search(r"(^|\s)sp($|\s)", text):
            return {"name": "prism", "label": "SPECIAL", "primary": "#ff4fd8", "secondary": "#8a5cff", "accent": "#7cf7ff", "particles": 18, "flash": True, "duration": 1.15}
        if "alt" in text or "parallel" in text:
            return {"name": "prism", "label": "ALT ART", "primary": "#6ee7ff", "secondary": "#d96cff", "accent": "#ffd166", "particles": 16, "flash": False, "duration": 1.10}
        if "sec" in text:
            return {"name": "gold", "label": "SECRET", "primary": "#ffd166", "secondary": "#ff9f1c", "accent": "#fff3b0", "particles": 14, "flash": False, "duration": 1.05}
        if "sr" in text:
            return {"name": "energy", "label": "SUPER RARE", "primary": "#41a7ff", "secondary": "#6c63ff", "accent": "#d9f3ff", "particles": 11, "flash": False, "duration": 0.95}
        if "leader" in text or re.search(r"\bl\b", text):
            return {"name": "energy", "label": "LEADER", "primary": "#ef476f", "secondary": "#7b2cbf", "accent": "#ffffff", "particles": 9, "flash": False, "duration": 0.90}
        if "rare" in text or re.search(r"\br\b", text):
            return {"name": "silver", "label": "RARE", "primary": "#dce6f2", "secondary": "#7aa7c7", "accent": "#ffffff", "particles": 7, "flash": False, "duration": 0.85}
        # DON!! ou autre traitement intermédiaire.
        return {"name": "soft", "label": "", "primary": "#ef476f", "secondary": "#30343f", "accent": "#ffffff", "particles": 5, "flash": False, "duration": 0.80}

    if game == "pokemon":
        if "mega hyper" in text:
            return {"name": "legendary", "label": "MEGA HYPER RARE", "primary": "#fff4b8", "secondary": "#ff5e5b", "accent": "#ffffff", "particles": 26, "flash": True, "duration": 1.35}
        if "special illustration" in text or "black white" in text:
            return {"name": "prism", "label": "SPECIAL ILLUSTRATION", "primary": "#ff70a6", "secondary": "#70d6ff", "accent": "#e9ff70", "particles": 22, "flash": True, "duration": 1.25}
        if "hyper rare" in text:
            return {"name": "gold", "label": "HYPER RARE", "primary": "#ffe066", "secondary": "#ff9f1c", "accent": "#fff7cc", "particles": 20, "flash": True, "duration": 1.20}
        if "mega attack" in text:
            return {"name": "manga", "label": "MEGA ATTACK", "primary": "#ff3b30", "secondary": "#ff9f1c", "accent": "#ffffff", "particles": 18, "flash": True, "duration": 1.15}
        if "ultra rare" in text:
            return {"name": "gold", "label": "ULTRA RARE", "primary": "#ffd166", "secondary": "#f77f00", "accent": "#ffffff", "particles": 16, "flash": False, "duration": 1.10}
        if "illustration rare" in text:
            return {"name": "prism", "label": "ILLUSTRATION RARE", "primary": "#7bdff2", "secondary": "#b2f7ef", "accent": "#f7d6e0", "particles": 14, "flash": False, "duration": 1.05}
        if "shiny ultra" in text or "shiny rare" in text or "chromatique" in text:
            return {"name": "emerald", "label": "SHINY", "primary": "#72efdd", "secondary": "#80ff72", "accent": "#ffffff", "particles": 14, "flash": False, "duration": 1.05}
        if "double rare" in text:
            return {"name": "energy", "label": "DOUBLE RARE", "primary": "#55c2ff", "secondary": "#7b61ff", "accent": "#ffffff", "particles": 11, "flash": False, "duration": 0.95}
        if "master ball" in text:
            return {"name": "prism", "label": "MASTER BALL", "primary": "#b5179e", "secondary": "#4361ee", "accent": "#ffffff", "particles": 13, "flash": False, "duration": 1.00}
        if "ace spec" in text or "as tactique" in text or "high tech" in text:
            return {"name": "neon", "label": "ACE SPEC", "primary": "#ff2fb3", "secondary": "#7b2cff", "accent": "#ffffff", "particles": 12, "flash": False, "duration": 1.00}
        if "rare" in text:
            return {"name": "silver", "label": "RARE", "primary": "#dce6f2", "secondary": "#5aa9e6", "accent": "#ffffff", "particles": 7, "flash": False, "duration": 0.85}
        if "reverse" in text:
            return {"name": "soft", "label": "REVERSE", "primary": "#9bf6ff", "secondary": "#a0c4ff", "accent": "#ffffff", "particles": 5, "flash": False, "duration": 0.80}
        return {"name": "soft", "label": "", "primary": "#5aa9e6", "secondary": "#4361ee", "accent": "#ffffff", "particles": 5, "flash": False, "duration": 0.80}

    # Riftbound
    if "ultimate" in text:
        return {"name": "legendary", "label": "ULTIMATE", "primary": "#fff2a8", "secondary": "#9d4edd", "accent": "#66ffff", "particles": 28, "flash": True, "duration": 1.40}
    if "signature" in text:
        return {"name": "manga", "label": "SIGNATURE", "primary": "#ffbe0b", "secondary": "#d00000", "accent": "#ffffff", "particles": 24, "flash": True, "duration": 1.30}
    if "overnumber" in text:
        return {"name": "gold", "label": "OVERNUMBERED", "primary": "#ffd166", "secondary": "#ff7b00", "accent": "#fff3b0", "particles": 21, "flash": True, "duration": 1.22}
    if "special alt" in text:
        return {"name": "prism", "label": "SPECIAL ALT", "primary": "#ff4fd8", "secondary": "#00e5ff", "accent": "#ffd166", "particles": 20, "flash": True, "duration": 1.20}
    if "alt" in text or "showcase" in text:
        return {"name": "prism", "label": "ALT ART", "primary": "#2de2e6", "secondary": "#ff49db", "accent": "#f9f871", "particles": 17, "flash": False, "duration": 1.12}
    if "epic" in text:
        return {"name": "neon", "label": "EPIC", "primary": "#c77dff", "secondary": "#7b2cbf", "accent": "#e0aaff", "particles": 14, "flash": False, "duration": 1.02}
    if "rare" in text:
        return {"name": "energy", "label": "RARE", "primary": "#00b4d8", "secondary": "#4361ee", "accent": "#caf0f8", "particles": 8, "flash": False, "duration": 0.88}
    if "foil" in text:
        return {"name": "silver", "label": "FOIL", "primary": "#dff7ff", "secondary": "#8ecae6", "accent": "#ffffff", "particles": 7, "flash": False, "duration": 0.85}
    return {"name": "soft", "label": "", "primary": "#00b4d8", "secondary": "#4361ee", "accent": "#ffffff", "particles": 5, "flash": False, "duration": 0.80}


def show_card_reveal(card, reveal_id):
    """Révélation animée pilotée par JavaScript/requestAnimationFrame.

    Le flip et le zoom utilisent uniquement ``transform`` (GPU friendly).
    Les paillettes et le rectangle d'impact sont dessinés dans un canvas
    au-dessus de la carte. Le halo circulaire utilise un second canvas derrière
    la carte, ce qui crée naturellement l'effet d'éclipse sans masque rectangulaire.
    """
    effect = reveal_effect_profile(card)
    rank = rarity_rank(card)
    animate = rank > 20

    # ========================================================
    # INTENSITÉ VISUELLE PILOTÉE PAR LA RARETÉ
    # ========================================================
    # rank va globalement de 0/10 (cartes ordinaires) à 100
    # (Manga / Ultimate / très gros chase). À partir de 20,
    # les effets montent progressivement : davantage de
    # paillettes, paillettes plus lumineuses et halo plus fort.
    rarity_level = max(0.0, min(1.0, (rank - 20) / 80.0))

    if animate:
        particle_count = int(
            round(5 + 55 * (rarity_level ** 1.35))
        )
        particle_brightness = 0.75 + 1.15 * (rarity_level ** 1.15)
        particle_size_scale = 0.85 + 0.45 * rarity_level
        halo_strength = 0.30 + 0.95 * (rarity_level ** 1.18)
        halo_radius = 0.78 + 0.32 * rarity_level
    else:
        particle_count = 0
        particle_brightness = 0.0
        particle_size_scale = 1.0
        halo_strength = 0.0
        halo_radius = 1.0

    # On garde les couleurs définies par le profil du jeu, mais
    # le nombre / la luminosité sont désormais calculés depuis rank.
    effect = dict(effect)
    effect["particles"] = particle_count
    effect["particleBrightness"] = round(particle_brightness, 3)
    effect["particleSizeScale"] = round(particle_size_scale, 3)
    effect["haloStrength"] = round(halo_strength, 3)
    effect["haloRadius"] = round(halo_radius, 3)

    # Plus la carte est rare, plus la révélation prend son temps.
    base_duration = float(effect["duration"])
    if animate:
        rarity_duration = 0.86 + max(0, rank - 30) * 0.0138
        flip_duration = max(base_duration, rarity_duration)
    else:
        flip_duration = 0.54

    image_src = reveal_image_source(card.get("image"))
    game = card.get("game", "")

    back_src = ""
    if game == "onepiece":
        back_path = ASSET_DIR / "one_piece_card_back.png"
        if back_path.is_file():
            back_src = reveal_image_source(back_path)

    if back_src:
        back_artwork = (
            f'<img src="{html.escape(back_src, quote=True)}" alt="Dos de carte">'
        )
    else:
        label = html.escape(GAME_LABELS.get(game, "TCG"))
        back_artwork = f'<div class="generic-back">{label}</div>'

    name = html.escape(str(card.get("name") or "Carte"))
    artwork = (
        f'<img src="{html.escape(image_src, quote=True)}" alt="{name}" '
        'onerror="this.hidden=true;this.nextElementSibling.hidden=false;">'
        '<span hidden>Image indisponible</span>'
        if image_src
        else '<span>Image indisponible</span>'
    )

    badge_html = (
        f'<div id="rarityBadge" class="rarity-badge">{html.escape(effect["label"])}</div>'
        if effect.get("label")
        else '<div id="rarityBadge" class="rarity-badge"></div>'
    )

    js_config = json.dumps(
        {
            "animate": animate,
            "durationMs": round(flip_duration * 1000),
            "rank": int(rank),
            "effect": effect,
            "revealId": str(reveal_id),
        },
        ensure_ascii=False,
    )

    components.html(
        f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
html, body {{
    width:100%;
    height:100%;
    margin:0;
    overflow:visible;
    background:transparent;
}}
body {{
    font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#f4f4f4;
}}

.stage {{
    position:relative;
    width:100%;
    height:620px;
    display:flex;
    align-items:center;
    justify-content:center;
    overflow:visible;
    isolation:isolate;
}}

.scene {{
    position:relative;
    width:min(310px, 72vw);
    aspect-ratio:5/7;
    perspective:1500px;
    z-index:3;
    cursor:grab;
    touch-action:none;
    user-select:none;
    -webkit-user-select:none;
}}

.scene.inspecting {{
    cursor:grabbing;
}}

.tilt-frame {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    transform-style:preserve-3d;
    transform:rotateX(0deg) rotateY(0deg) scale(1) translateZ(0);
    transform-origin:50% 50%;
    will-change:transform;
}}

.card {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    transform-style:preserve-3d;
    transform:rotateY(0deg) scale(.985) translateZ(0);
    transform-origin:50% 50%;
    will-change:transform;
    backface-visibility:visible;
}}

.face {{
    position:absolute;
    inset:0;
    display:flex;
    align-items:center;
    justify-content:center;
    border-radius:12px;
    overflow:hidden;
    backface-visibility:hidden;
    -webkit-backface-visibility:hidden;
    transform:translateZ(0);
}}

.back {{
    background:#1e2430;
    border:1px solid #555;
    box-shadow:inset 0 0 0 10px #293140,0 10px 22px #0005;
}}

.front {{
    transform:rotateY(180deg) translateZ(0);
    background:#252525;
    box-shadow:0 0 0 1px #ffffff20;
}}

.face img {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    object-fit:contain;
    z-index:1;
    user-select:none;
    -webkit-user-select:none;
    -webkit-user-drag:none;
}}

.generic-back {{
    padding:20px;
    text-align:center;
    font-size:28px;
    font-weight:800;
}}

/* Couche holographique interne. Elle ne déborde jamais de la carte. */
.holo {{
    position:absolute;
    inset:0;
    z-index:3;
    pointer-events:none;
    opacity:0;
    border-radius:12px;
    overflow:hidden;
    background:
        linear-gradient(
            118deg,
            transparent 8%,
            {effect['primary']}55 34%,
            {effect['secondary']}66 52%,
            {effect['accent']}55 68%,
            transparent 92%
        );
    background-size:220% 220%;
    mix-blend-mode:screen;
    will-change:opacity,background-position;
}}

/* Halo circulaire réellement derrière la carte. */
#haloCanvas {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    z-index:1;
    pointer-events:none;
}}

/* Effets au-dessus de la carte : impact + paillettes. */
#fxCanvas {{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    z-index:8;
    pointer-events:none;
}}

#flash {{
    position:absolute;
    inset:0;
    z-index:7;
    pointer-events:none;
    background:#fff;
    opacity:0;
    will-change:opacity;
}}

.rarity-badge {{
    position:absolute;
    z-index:10;
    left:50%;
    top:8px;
    padding:5px 12px;
    border-radius:999px;
    transform:translate(-50%,-10px) scale(.90);
    opacity:0;
    background:#0d0d0ddd;
    border:1px solid #ffffff88;
    color:#fff;
    font-size:11px;
    font-weight:900;
    letter-spacing:.12em;
    white-space:nowrap;
    text-transform:uppercase;
    will-change:opacity,transform;
}}

@media (max-width:800px) {{
    .stage {{ height:560px; }}
    .scene {{ width:min(280px,70vw); }}
}}
</style>
</head>
<body data-reveal="{html.escape(str(reveal_id), quote=True)}">
<div id="stage" class="stage">
    <canvas id="haloCanvas"></canvas>
    <canvas id="fxCanvas"></canvas>
    <div id="flash"></div>
    <div id="scene" class="scene">
        {badge_html}
        <div id="tiltFrame" class="tilt-frame">
            <div id="card" class="card">
                <div class="face back">{back_artwork}</div>
                <div class="face front">
                    {artwork}
                    <div id="holo" class="holo"></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
(() => {{
    const cfg = {js_config};
    const stage = document.getElementById('stage');
    const scene = document.getElementById('scene');
    const tiltFrame = document.getElementById('tiltFrame');
    const card = document.getElementById('card');
    const haloCanvas = document.getElementById('haloCanvas');
    const haloCtx = haloCanvas.getContext('2d', {{ alpha:true }});
    const canvas = document.getElementById('fxCanvas');
    const ctx = canvas.getContext('2d', {{ alpha:true }});
    const flash = document.getElementById('flash');
    const holo = document.getElementById('holo');
    const badge = document.getElementById('rarityBadge');

    const colors = [
        cfg.effect.primary,
        cfg.effect.secondary,
        cfg.effect.accent,
    ];

    const duration = Math.max(1, cfg.durationMs);
    const startDelay = 110;
    const impactProgress = .735;
    const impactDuration = Math.max(620, Math.min(920, duration * .62));
    const sparkleStartProgress = .14;
    const persistentStartProgress = .60;
    const particleCount = cfg.animate ? Number(cfg.effect.particles || 0) : 0;
    const particleBrightness = cfg.animate ? Number(cfg.effect.particleBrightness || 1) : 0;
    const particleSizeScale = cfg.animate ? Number(cfg.effect.particleSizeScale || 1) : 1;
    const haloStrength = cfg.animate ? Number(cfg.effect.haloStrength || 0) : 0;
    const haloRadius = cfg.animate ? Number(cfg.effect.haloRadius || 1) : 1;

    let stageRect = null;
    let cardRect = null;
    let dpr = 1;

    // PRNG déterministe : une même carte ne change pas de disposition
    // à chaque frame, ce qui évite tout scintillement parasite.
    let seed = 2166136261;
    for (const ch of String(cfg.revealId)) {{
        seed ^= ch.charCodeAt(0);
        seed = Math.imul(seed, 16777619);
    }}
    function rnd() {{
        seed += 0x6D2B79F5;
        let t = seed;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }}

    function clamp(v, a=0, b=1) {{ return Math.min(b, Math.max(a, v)); }}
    function lerp(a,b,t) {{ return a + (b-a)*t; }}
    function smoothstep(t) {{
        t = clamp(t);
        return t*t*(3-2*t);
    }}
    function easeInOutCubic(t) {{
        t = clamp(t);
        return t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3)/2;
    }}
    function easeOutCubic(t) {{
        t = clamp(t);
        return 1 - Math.pow(1-t, 3);
    }}
    function easeOutExpo(t) {{
        t = clamp(t);
        return t >= 1 ? 1 : 1 - Math.pow(2, -10*t);
    }}

    function hexToRgb(hex) {{
        let h = String(hex || '#ffffff').replace('#','').trim();
        if (h.length === 3) h = h.split('').map(x => x+x).join('');
        const n = parseInt(h, 16);
        if (!Number.isFinite(n)) return [255,255,255];
        return [(n>>16)&255, (n>>8)&255, n&255];
    }}
    function rgba(hex, alpha) {{
        const [r,g,b] = hexToRgb(hex);
        return `rgba(${{r}},${{g}},${{b}},${{alpha}})`;
    }}

    function resizeCanvas() {{
        stageRect = stage.getBoundingClientRect();
        cardRect = scene.getBoundingClientRect();
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        haloCanvas.width = Math.max(1, Math.round(stageRect.width * dpr));
        haloCanvas.height = Math.max(1, Math.round(stageRect.height * dpr));
        haloCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

        canvas.width = Math.max(1, Math.round(stageRect.width * dpr));
        canvas.height = Math.max(1, Math.round(stageRect.height * dpr));
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }}

    window.addEventListener('resize', resizeCanvas, {{ passive:true }});
    resizeCanvas();

    function localCardBox() {{
        if (!stageRect || !cardRect) resizeCanvas();
        const x = cardRect.left - stageRect.left;
        const y = cardRect.top - stageRect.top;
        return {{ x, y, w:cardRect.width, h:cardRect.height }};
    }}

    function starPath(x, y, r, rotation=0) {{
        const spikes = 4;
        const inner = r * .28;
        ctx.beginPath();
        for (let i=0; i<spikes*2; i++) {{
            const rr = i % 2 === 0 ? r : inner;
            const a = rotation + i * Math.PI / spikes - Math.PI/2;
            const px = x + Math.cos(a) * rr;
            const py = y + Math.sin(a) * rr;
            if (i === 0) ctx.moveTo(px,py); else ctx.lineTo(px,py);
        }}
        ctx.closePath();
    }}

    const flying = Array.from({{ length:particleCount }}, (_, i) => ({{
        side: rnd() < .5 ? -1 : 1,
        u: rnd(),
        v: rnd(),
        size: (1.8 + rnd()*3.4) * particleSizeScale,
        delay: sparkleStartProgress + rnd()*.42,
        life: .26 + rnd()*.28,
        driftX: (rnd()-.5)*70,
        driftY: -18 - rnd()*58,
        color: colors[i % colors.length],
        spin: rnd()*Math.PI*2,
    }}));

    const inside = Array.from({{
        length: Math.max(0, Math.min(55, Math.round(particleCount * .75)))
    }}, (_, i) => ({{
        u: .05 + rnd()*.90,
        v: .05 + rnd()*.90,
        size: (1.25 + rnd()*2.8) * particleSizeScale,
        phase: rnd()*Math.PI*2,
        speed: .0014 + rnd()*.0025,
        color: colors[i % colors.length],
    }}));

    function drawEclipseHalo(box, p, elapsed) {{
        haloCtx.clearRect(0, 0, stageRect.width, stageRect.height);
        if (!cfg.animate || haloStrength <= 0 || p < .18) return;

        // Halo 100 % circulaire, dessiné sur un canvas placé DERRIÈRE la
        // carte. Aucun masque rectangulaire n'est nécessaire : la carte
        // cache naturellement le centre, comme lors d'une éclipse.
        const reveal = smoothstep((p - .18) / .30);
        const pulse = .96 + .06 * Math.sin(elapsed * .0025);
        const intensity = clamp(haloStrength * reveal * pulse, 0, 1.55);

        const cx = box.x + box.w / 2;
        const cy = box.y + box.h / 2;
        const outerR = Math.max(box.w, box.h) * haloRadius;

        haloCtx.save();
        haloCtx.globalCompositeOperation = 'lighter';

        // Dégradé circulaire avec chute rapide de luminosité.
        // La zone la plus intense se trouve près de la silhouette de la carte,
        // puis tombe rapidement vers zéro afin de ne pas éclairer tout l'écran.
        const corona = haloCtx.createRadialGradient(
            cx, cy, 0,
            cx, cy, outerR
        );
        corona.addColorStop(0.00, rgba(cfg.effect.accent, .92 * intensity));
        corona.addColorStop(0.30, rgba(cfg.effect.accent, .72 * intensity));
        corona.addColorStop(0.46, rgba(cfg.effect.primary, .48 * intensity));
        corona.addColorStop(0.58, rgba(cfg.effect.primary, .20 * intensity));
        corona.addColorStop(0.68, rgba(cfg.effect.secondary, .065 * intensity));
        corona.addColorStop(0.75, rgba(cfg.effect.secondary, .012 * intensity));
        corona.addColorStop(0.80, rgba(cfg.effect.secondary, 0));
        corona.addColorStop(1.00, rgba(cfg.effect.secondary, 0));

        haloCtx.fillStyle = corona;
        haloCtx.fillRect(0, 0, stageRect.width, stageRect.height);

        haloCtx.restore();
    }}

    function drawImpact(box, elapsed, impactTime) {{
        if (!cfg.animate) return;
        const t = clamp((elapsed - impactTime) / impactDuration);
        if (t <= 0 || t >= 1) return;

        const growth = easeOutExpo(t);
        const fade = Math.pow(1-t, .82);
        const scale = lerp(0, 1.42, growth);
        const w = box.w * scale;
        const h = box.h * scale;
        const x = box.x + box.w/2 - w/2;
        const y = box.y + box.h/2 - h/2;
        const radius = Math.max(5, 18 * Math.min(1, scale));

        ctx.save();
        ctx.globalAlpha = fade;
        ctx.lineWidth = 2.3;
        ctx.strokeStyle = cfg.effect.primary;
        ctx.shadowColor = cfg.effect.secondary;
        ctx.shadowBlur = 22 + 26*(1-t);
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, radius);
        ctx.stroke();

        ctx.globalAlpha = fade * .42;
        ctx.lineWidth = 7;
        ctx.strokeStyle = cfg.effect.secondary;
        ctx.shadowBlur = 34;
        ctx.stroke();
        ctx.restore();
    }}

    function drawFlying(box, p, elapsed) {{
        if (!cfg.animate || particleCount === 0) return;

        for (const s of flying) {{
            const lt = (p - s.delay) / s.life;
            if (lt <= 0 || lt >= 1) continue;
            const e = easeOutCubic(lt);
            const edgeX = box.x + box.w * s.u;
            const edgeY = box.y + box.h * s.v;
            const x = edgeX + s.driftX * e;
            const y = edgeY + s.driftY * e;
            const alpha = Math.sin(Math.PI * lt) * .92;
            const r = s.size * (0.45 + Math.sin(Math.PI*lt)*.75);

            ctx.save();
            const brightAlpha = Math.min(1, alpha * (.70 + particleBrightness * .22));
            ctx.globalAlpha = brightAlpha;
            ctx.fillStyle = s.color;
            ctx.shadowColor = s.color;
            ctx.shadowBlur = 5 + 9 * particleBrightness;
            starPath(x, y, r, s.spin + elapsed*.0035);
            ctx.fill();

            // Petit coeur blanc : presque absent sur une Rare, très net
            // sur les chase cards. Il donne de la luminosité sans grossir
            // artificiellement toutes les particules.
            const core = clamp((particleBrightness - .80) / 1.10);
            if (core > 0) {{
                ctx.globalAlpha = brightAlpha * core * .78;
                ctx.fillStyle = '#ffffff';
                ctx.shadowColor = '#ffffff';
                ctx.shadowBlur = 4 + 7 * core;
                starPath(x, y, r * (.24 + .12 * core), s.spin + elapsed*.0035);
                ctx.fill();
            }}
            ctx.restore();
        }}
    }}

    function drawPersistent(box, p, elapsed) {{
        if (!cfg.animate || p < persistentStartProgress) return;

        // Liseré coloré strictement à la taille de la carte.
        const edgePulse = .14 + .10 * (1 + Math.sin(elapsed*.0032)) / 2;
        ctx.save();
        ctx.globalAlpha = edgePulse;
        ctx.strokeStyle = cfg.effect.primary;
        ctx.lineWidth = 2;
        ctx.shadowColor = cfg.effect.secondary;
        ctx.shadowBlur = 13;
        ctx.beginPath();
        ctx.roundRect(box.x, box.y, box.w, box.h, 12);
        ctx.stroke();
        ctx.restore();

        ctx.save();
        ctx.beginPath();
        ctx.rect(box.x+2, box.y+2, box.w-4, box.h-4);
        ctx.clip();

        for (const s of inside) {{
            const alpha = Math.max(0, Math.sin(elapsed*s.speed + s.phase));
            if (alpha < .14) continue;
            const x = box.x + s.u*box.w;
            const y = box.y + s.v*box.h;
            const r = s.size * (.6 + alpha*.55);
            ctx.save();
            const brightAlpha = Math.min(1, alpha * (.62 + particleBrightness * .22));
            ctx.globalAlpha = brightAlpha;
            ctx.fillStyle = s.color;
            ctx.shadowColor = s.color;
            ctx.shadowBlur = 4 + 8 * particleBrightness;
            starPath(x,y,r,elapsed*.0018+s.phase);
            ctx.fill();

            const core = clamp((particleBrightness - .85) / 1.05);
            if (core > 0) {{
                ctx.globalAlpha = brightAlpha * core * .70;
                ctx.fillStyle = '#ffffff';
                ctx.shadowColor = '#ffffff';
                ctx.shadowBlur = 3 + 6 * core;
                starPath(x,y,r*.28,elapsed*.0018+s.phase);
                ctx.fill();
            }}
            ctx.restore();
        }}
        ctx.restore();
    }}

    function updateBadge(elapsed, impactTime) {{
        if (!badge || !badge.textContent.trim() || !cfg.animate) return;
        const t = elapsed - impactTime;
        let opacity = 0;
        let y = -10;
        let scale = .90;
        if (t >= 0 && t < 260) {{
            const k = easeOutCubic(t/260);
            opacity = k;
            y = lerp(-10,0,k);
            scale = lerp(.90,1,k);
        }} else if (t >= 260 && t < 980) {{
            opacity = 1;
            y = 0;
            scale = 1;
        }} else if (t >= 980 && t < 1260) {{
            const k = smoothstep((t-980)/280);
            opacity = 1-k;
            y = -4*k;
            scale = 1-.05*k;
        }}
        badge.style.opacity = String(opacity);
        badge.style.transform = `translate(-50%,${{y}}px) scale(${{scale}})`;
        badge.style.boxShadow = `0 0 16px ${{cfg.effect.primary}}`;
    }}

    function updateFlash(elapsed, impactTime) {{
        if (!cfg.effect.flash || !cfg.animate) {{
            flash.style.opacity = '0';
            return;
        }}
        const t = (elapsed - impactTime)/420;
        let a = 0;
        if (t > 0 && t < 1) {{
            a = t < .18 ? .58*(t/.18) : .58*(1-(t-.18)/.82);
        }}
        flash.style.opacity = String(Math.max(0,a));
    }}

    // ========================================================
    // MODE D'OBSERVATION 3D
    // ========================================================
    // Pas besoin d'un vrai mesh 3D : la carte est un plan HTML placé dans
    // une scène en perspective. Pendant un clic maintenu, le plan s'incline
    // en suivant la souris / le doigt. Le mouvement est interpolé dans la
    // même boucle requestAnimationFrame pour rester fluide.
    let revealComplete = false;
    let inspecting = false;
    let targetTiltX = 0;
    let targetTiltY = 0;
    let currentTiltX = 0;
    let currentTiltY = 0;
    let targetInspectScale = 1;
    let currentInspectScale = 1;
    let inspectNx = 0;
    let inspectNy = 0;

    function updateInspectionTarget(event) {{
        const rect = scene.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const nx = clamp((event.clientX - rect.left) / rect.width, 0, 1) * 2 - 1;
        const ny = clamp((event.clientY - rect.top) / rect.height, 0, 1) * 2 - 1;
        inspectNx = nx;
        inspectNy = ny;

        // Inclinaison volontairement légère pour garder une lecture naturelle.
        targetTiltY = nx * 11.5;
        targetTiltX = -ny * 9.0;
        targetInspectScale = 1.025;
    }}

    function stopInspection() {{
        inspecting = false;
        targetTiltX = 0;
        targetTiltY = 0;
        targetInspectScale = 1;
        inspectNx = 0;
        inspectNy = 0;
        scene.classList.remove('inspecting');
    }}

    scene.addEventListener('pointerdown', (event) => {{
        if (!revealComplete) return;
        inspecting = true;
        scene.classList.add('inspecting');
        try {{ scene.setPointerCapture(event.pointerId); }} catch (_) {{}}
        updateInspectionTarget(event);
        event.preventDefault();
    }});

    scene.addEventListener('pointermove', (event) => {{
        if (!inspecting || !revealComplete) return;
        updateInspectionTarget(event);
        event.preventDefault();
    }});

    scene.addEventListener('pointerup', stopInspection);
    scene.addEventListener('pointercancel', stopInspection);
    scene.addEventListener('lostpointercapture', stopInspection);
    scene.addEventListener('contextmenu', (event) => event.preventDefault());
    scene.addEventListener('dragstart', (event) => event.preventDefault());

    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) cfg.animate = false;

    let start = null;
    function frame(ts) {{
        if (start === null) start = ts;
        const elapsed = ts - start;
        const motionElapsed = Math.max(0, elapsed - startDelay);
        const p = cfg.animate ? clamp(motionElapsed / duration) : 1;

        // Rotation 3D très régulière, contrôlée par une seule horloge JS.
        const rotP = easeInOutCubic(p);
        const rotation = 180 * rotP;

        // Petit zoom / dézoom, sans mouvement vertical.
        // La bosse est centrée sur le moment de l'impact.
        const sigma = .19;
        const bump = Math.exp(-Math.pow((p-impactProgress)/sigma,2));
        const startScale = lerp(.985, 1, smoothstep(p/.28));
        const scale = startScale + (cfg.animate ? .034*bump*(1-p*.16) : 0);

        revealComplete = p >= 1;
        card.style.transform = `rotateY(${{rotation.toFixed(3)}}deg) scale(${{scale.toFixed(4)}}) translateZ(0)`;

        // Une fois la révélation terminée, l'enveloppe extérieure s'incline
        // en douceur vers la souris pendant le clic maintenu.
        const tiltEase = inspecting ? .16 : .105;
        currentTiltX += (targetTiltX - currentTiltX) * tiltEase;
        currentTiltY += (targetTiltY - currentTiltY) * tiltEase;
        currentInspectScale += (targetInspectScale - currentInspectScale) * .12;
        tiltFrame.style.transform =
            `rotateX(${{currentTiltX.toFixed(3)}}deg) ` +
            `rotateY(${{currentTiltY.toFixed(3)}}deg) ` +
            `scale(${{currentInspectScale.toFixed(4)}}) translateZ(0)`;

        // Holo interne. En mode observation, son reflet suit aussi légèrement
        // la souris, ce qui renforce la sensation de matière sans WebGL.
        if (cfg.animate && p > .48) {{
            const reveal = smoothstep((p-.48)/.32);
            const pulse = .10 + .08*(1+Math.sin(elapsed*.0027))/2;
            const inspectBoost = inspecting ? .09 : 0;
            holo.style.opacity = String(Math.min(.34, reveal * pulse + inspectBoost));
            if (inspecting) {{
                const hx = 50 + inspectNx * 38;
                const hy = 50 + inspectNy * 32;
                holo.style.backgroundPosition = `${{hx.toFixed(1)}}% ${{hy.toFixed(1)}}%`;
            }} else {{
                holo.style.backgroundPosition = `${{(elapsed*.018)%220}}% 50%`;
            }}
        }} else {{
            holo.style.opacity = '0';
        }}

        ctx.clearRect(0,0,stageRect.width,stageRect.height);
        const box = localCardBox();
        const impactTime = startDelay + duration*impactProgress;

        drawEclipseHalo(box, p, elapsed);
        drawFlying(box, p, elapsed);
        drawImpact(box, elapsed, impactTime);
        drawPersistent(box, p, elapsed);
        updateBadge(elapsed, impactTime);
        updateFlash(elapsed, impactTime);

        // On continue après le flip afin de garder les paillettes internes
        // vivantes, mais la carte elle-même ne bouge plus.
        requestAnimationFrame(frame);
    }}

    requestAnimationFrame(frame);
}})();
</script>
</body>
</html>""",
        height=630,
        scrolling=False,
    )


# ============================================================
# LOGIN
# ============================================================


def login_page():
    st.title("TCG Booster Simulator")

    login_tab, register_tab = st.tabs(["Connexion", "Créer un compte"])

    with login_tab:
        with st.form("login_form"):
            username = st.text_input("Nom")
            password = st.text_input("Mot de passe", type="password")
            submit = st.form_submit_button("Connexion")

        if submit:
            user = authenticate_user(username, password)
            if user:
                st.session_state["user_id"] = user["user_id"]
                st.session_state["username"] = user["username"]
                st.rerun()
            else:
                st.error("Identifiants incorrects.")

    with register_tab:
        with st.form("register_form"):
            username = st.text_input("Nouveau nom", key="new_username")
            password = st.text_input(
                "Nouveau mot de passe", type="password", key="new_password"
            )
            submit = st.form_submit_button("Créer le compte")

        if submit:
            success, message = create_user(username, password)
            (st.success if success else st.error)(message)


# ============================================================
# PAGE BOOSTER
# ============================================================


def booster_page(user_id):
    st.header("Ouvrir un booster")

    available_games = []
    unavailable = {}
    for game in GAME_LABELS:
        ok, error = game_database_ready(game)
        if ok:
            available_games.append(game)
        else:
            unavailable[game] = error

    if not available_games:
        st.error("Aucune base de jeu exploitable n'a été trouvée.")
        return

    game = st.selectbox(
        "Jeu",
        available_games,
        format_func=lambda value: GAME_LABELS[value],
        key="booster_game",
    )

    if unavailable:
        with st.expander("Bases non disponibles"):
            for key, error in unavailable.items():
                st.caption(f"{GAME_LABELS[key]} : {error}")

    catalog = load_booster_catalog(game)
    if catalog.empty:
        st.warning("Aucun booster supporté trouvé dans cette base.")
        return

    labels = {
        row.set_code: f"{row.set_code} — {row.set_name}"
        for row in catalog.itertuples()
    }

    set_code = st.selectbox(
        "Booster / extension",
        options=list(labels),
        format_func=lambda value: labels[value],
        key=f"booster_set_{game}",
    )

    cards = load_game_cards(game, set_code)
    if cards.empty:
        st.error("Aucune carte trouvée pour cette extension.")
        return

    collection = load_user_collection(user_id, game=game, set_code=set_code)
    copies = (
        int(pd.to_numeric(collection["quantity"], errors="coerce").fillna(0).sum())
        if not collection.empty and "quantity" in collection.columns
        else 0
    )

    price_info = get_booster_price(game, set_code)
    price_coins = int(price_info["price_coins"])
    balance = get_wallet_balance(user_id)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Cartes / booster", pack_card_count(game))
    with col2:
        if game == "pokemon":
            st.metric("Boosters / display standard", POKEMON_STANDARD_BOX_PACKS)
        elif game == "onepiece":
            st.metric("Boosters / box", OP_PACKS_PER_BOX)
        else:
            st.metric("Boosters / box", RIFTBOUND_PACKS_PER_BOX)
    with col3:
        st.metric("Prix", format_coins(price_coins))
    with col4:
        st.metric("Solde", format_coins(balance))

    session_pack_key = f"{game}:{set_code}"
    last_key = st.session_state.get("last_pack_key")
    current_pack = st.session_state.get("last_pack", [])
    reveal_index = st.session_state.get("pack_reveal_index", len(current_pack))
    opening_in_progress = (
        last_key == session_pack_key and current_pack and reveal_index < len(current_pack)
    )

    can_afford = balance >= price_coins
    button_label = f"✨ Ouvrir le booster — {format_coins(price_coins, show_eur=False)}"

    if not can_afford:
        missing = price_coins - balance
        st.warning(
            f"Il te manque {format_coins(missing)} pour ouvrir ce booster."
        )

    if st.button(
        button_label,
        type="primary",
        use_container_width=True,
        disabled=opening_in_progress or not can_afford,
    ):
        pack, profile = simulate_pack(game, set_code, cards)
        success, message, opening_id, new_balance = purchase_pack_and_save(
            user_id, game, set_code, pack, price_coins
        )

        if not success:
            st.error(message)
            st.rerun()

        # Révélation du bulk vers les hits.
        pack = sorted(pack, key=booster_reveal_order)
        st.session_state["last_pack"] = pack
        st.session_state["last_pack_key"] = session_pack_key
        st.session_state["last_pack_profile"] = profile
        st.session_state["last_pack_price"] = price_coins
        st.session_state["pack_reveal_index"] = 0
        st.session_state["pack_reveal_id"] = secrets.token_hex(8)
        st.rerun()

    if st.session_state.get("last_pack_key") != session_pack_key:
        return

    pack = st.session_state.get("last_pack", [])
    if not pack:
        return

    profile = st.session_state.get("last_pack_profile", {})

    reveal_index = st.session_state.get("pack_reveal_index", len(pack))
    if reveal_index < len(pack):
        # Mode révélation plein écran : pendant l'ouverture, toute l'interface
        # Streamlit est recouverte par un écran noir. Seuls restent visibles :
        # le compteur en haut, les infos à gauche, la carte au centre et la
        # zone de navigation à droite.
        st.markdown(
            """
            <style>
            /* Masque les chrome Streamlit uniquement pendant une révélation. */
            header[data-testid="stHeader"],
            section[data-testid="stSidebar"],
            div[data-testid="stToolbar"],
            div[data-testid="stDecoration"],
            #MainMenu,
            footer {
                display: none !important;
            }

            /* Le container devient un véritable calque plein écran. */
            .st-key-reveal_fullscreen {
                position: fixed !important;
                inset: 0 !important;
                z-index: 999999 !important;
                width: 100vw !important;
                height: 100vh !important;
                max-width: none !important;
                margin: 0 !important;
                padding: 22px 34px !important;
                background: #000 !important;
                overflow: hidden !important;
            }
            .st-key-reveal_fullscreen > div,
            .st-key-reveal_fullscreen [data-testid="stVerticalBlock"] {
                max-width: none !important;
            }
            .st-key-reveal_fullscreen > div[data-testid="stVerticalBlock"],
            .st-key-reveal_fullscreen > div > div[data-testid="stVerticalBlock"] {
                height: 100% !important;
                min-height: calc(100vh - 44px) !important;
                display: flex !important;
                flex-direction: column !important;
            }

            .fullscreen-counter {
                width: 100%;
                text-align: center;
                font-size: 0.86rem;
                line-height: 1;
                color: rgba(255,255,255,.58);
                font-weight: 650;
                letter-spacing: .055em;
                padding: 2px 0 4px;
                user-select: none;
            }

            .fullscreen-card-meta {
                width: 100%;
                text-align: left;
                padding: 0 1.2rem 0 0;
            }
            .fullscreen-card-name {
                color: rgba(255,255,255,.96);
                font-size: clamp(1.08rem, 1.55vw, 1.55rem);
                line-height: 1.15;
                font-weight: 800;
                margin-bottom: .65rem;
            }
            .fullscreen-card-details {
                color: rgba(255,255,255,.58);
                font-size: clamp(.78rem, .95vw, .94rem);
                line-height: 1.65;
                word-break: break-word;
            }

            /* La flèche est la seule indication de la zone cliquable. */
            .st-key-reveal_next_zone button {
                min-height: min(72vh, 650px) !important;
                width: 100% !important;
                padding: 0 !important;
                border: 0 !important;
                border-radius: 0 !important;
                background: transparent !important;
                color: rgba(255,255,255,.62) !important;
                font-size: clamp(2.1rem, 3vw, 3.4rem) !important;
                font-weight: 250 !important;
                box-shadow: none !important;
                transition: color .18s ease, transform .18s ease,
                            background .18s ease !important;
            }
            .st-key-reveal_next_zone button:hover {
                color: white !important;
                background: linear-gradient(90deg, transparent, rgba(255,255,255,.035)) !important;
                transform: translateX(5px);
            }
            .st-key-reveal_next_zone button:focus,
            .st-key-reveal_next_zone button:active {
                border: 0 !important;
                outline: 0 !important;
                box-shadow: none !important;
            }

            /* Centre verticalement la ligne principale dans le viewport. */
            .st-key-reveal_fullscreen [data-testid="stHorizontalBlock"] {
                align-items: center !important;
                flex: 1 1 auto !important;
                min-height: calc(100vh - 92px) !important;
            }

            /* Évite un scroll de page pendant l'ouverture. */
            html, body, [data-testid="stAppViewContainer"], .stApp {
                overflow: hidden !important;
                background: #000 !important;
            }

            @media (max-width: 850px) {
                .st-key-reveal_fullscreen {
                    padding: 14px 14px !important;
                }
                .fullscreen-card-name {
                    font-size: .98rem;
                }
                .fullscreen-card-details {
                    font-size: .72rem;
                }
                .st-key-reveal_next_zone button {
                    min-height: 58vh !important;
                }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        current_card = pack[reveal_index]
        current_name = html.escape(str(current_card.get("name") or "Carte"))
        current_details = html.escape(card_details(current_card))

        with st.container(key="reveal_fullscreen"):
            st.markdown(
                f'<div class="fullscreen-counter">Carte {reveal_index + 1} / {len(pack)}</div>',
                unsafe_allow_html=True,
            )

            info_col, card_col, next_col = st.columns(
                [1.18, 2.70, 0.82],
                vertical_alignment="center",
                gap="large",
            )

            with info_col:
                details_html = (
                    f'<div class="fullscreen-card-details">{current_details}</div>'
                    if current_details
                    else ""
                )
                st.markdown(
                    f"""
                    <div class="fullscreen-card-meta">
                        <div class="fullscreen-card-name">{current_name}</div>
                        {details_html}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with card_col:
                show_card_reveal(
                    current_card,
                    f"{st.session_state.get('pack_reveal_id', '')}-{reveal_index}",
                )

            with next_col:
                help_text = (
                    "Carte suivante"
                    if reveal_index + 1 < len(pack)
                    else "Voir le récapitulatif"
                )
                if st.button(
                    "→",
                    key="reveal_next_zone",
                    help=help_text,
                    use_container_width=True,
                ):
                    st.session_state["pack_reveal_index"] = reveal_index + 1
                    st.rerun()
        return

    st.subheader("Contenu du booster")
    for start in range(0, len(pack), 4):
        columns = st.columns(4)
        for column, card in zip(columns, pack[start : start + 4]):
            with column:
                show_card(card)

    saved_count = sum(1 for card in pack if card.get("collectible", True))
    st.success(f"{saved_count} carte(s) ont été ajoutées à ton Cartedex.")


# ============================================================
# PAGE CARTEDEX MULTI-JEUX
# ============================================================


def cartedex_game_page(user_id, game):
    ready, error = game_database_ready(game)
    if not ready:
        st.warning(error)
        return

    overview = collection_overview(user_id, game)
    if overview.empty:
        st.info("Aucune extension disponible.")
        return

    total_unique = int(overview["unique_owned"].sum())
    total_available = int(overview["total_cards"].sum())
    total_copies = int(overview["copies_owned"].sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Uniques", f"{total_unique}/{total_available}")
    m2.metric("Copies", total_copies)
    m3.metric(
        "Complétion globale",
        f"{(100 * total_unique / total_available) if total_available else 0:.1f}%",
    )

    display = overview.copy()
    display["Progression"] = display["completion"].map(lambda x: f"{x:.1f}%")
    display = display.rename(
        columns={
            "product_set": "Extension",
            "unique_owned": "Uniques",
            "total_cards": "Total",
            "copies_owned": "Copies",
        }
    )
    st.dataframe(
        display[["Extension", "Uniques", "Total", "Copies", "Progression"]],
        hide_index=True,
        use_container_width=True,
    )

    set_code = st.selectbox(
        "Voir une extension",
        overview["product_set"].tolist(),
        key=f"cartedex_set_{game}",
    )

    cards = load_cartedex_cards(game, set_code)
    if cards.empty:
        st.info("Aucune carte dans cette extension.")
        return

    collection = load_user_collection(user_id, game=game, set_code=set_code)
    quantities = (
        dict(zip(collection["card_key"], collection["quantity"]))
        if not collection.empty
        else {}
    )
    cards = cards.copy()
    cards["quantity"] = cards["card_key"].map(quantities).fillna(0).astype(int)

    total_cards = len(cards)
    owned_unique = int((cards["quantity"] > 0).sum())
    total_copies_set = int(cards["quantity"].sum())
    progress = owned_unique / total_cards if total_cards else 0

    c1, c2, c3 = st.columns(3)
    c1.metric("Cartes uniques", f"{owned_unique}/{total_cards}")
    c2.metric("Copies possédées", total_copies_set)
    c3.metric("Complétion", f"{progress * 100:.1f}%")
    st.progress(progress)

    f1, f2 = st.columns(2)
    with f1:
        only_owned = st.checkbox(
            "Afficher uniquement mes cartes", key=f"only_owned_{game}_{set_code}"
        )
    with f2:
        search = st.text_input(
            "Rechercher", key=f"search_{game}_{set_code}"
        ).strip().lower()

    filtered = cards.copy()
    if only_owned:
        filtered = filtered[filtered["quantity"] > 0]
    if search:
        filtered = filtered[
            filtered["name"].fillna("").str.lower().str.contains(search, regex=False)
            | filtered["card_number"].fillna("").astype(str).str.lower().str.contains(search, regex=False)
        ]

    page_size = 24
    page_count = max(1, math.ceil(len(filtered) / page_size))
    page = st.number_input(
        "Page",
        min_value=1,
        max_value=page_count,
        value=1,
        step=1,
        key=f"page_{game}_{set_code}",
    )
    start = (int(page) - 1) * page_size
    visible = filtered.iloc[start : start + page_size]

    for start_index in range(0, len(visible), 4):
        columns = st.columns(4)
        rows = visible.iloc[start_index : start_index + 4]
        for column, (_, card) in zip(columns, rows.iterrows()):
            with column:
                show_card(
                    card,
                    quantity=int(card["quantity"]),
                    cartedex_mode=True,
                )


def cartedex_page(user_id):
    st.header("Cartedex")

    game = st.radio(
        "Jeu",
        list(GAME_LABELS),
        format_func=lambda value: GAME_LABELS[value],
        horizontal=True,
        key="cartedex_game",
    )
    st.subheader(GAME_LABELS[game])
    cartedex_game_page(user_id, game)



# ============================================================
# QUIZ : CULTURE GÉNÉRALE + LEAGUE OF LEGENDS
# ============================================================


def trivia_question_count(quiz_key="general"):
    ok, _ = validate_trivia_database(quiz_key)
    if not ok:
        return 0
    with connect_trivia(quiz_key) as conn:
        if quiz_key == "lol":
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM lol_questions WHERE active = 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trivia_questions WHERE active = 1"
            ).fetchone()
    return int(row["n"] or 0)


def get_trivia_question(question_id, quiz_key="general"):
    """Charge une question depuis l'une des bases externes.

    Le lecteur accepte l'ancien schéma QCM ainsi que le nouveau schéma riche
    (QCM/réponse libre, image, explication, source). Cela permet de remplacer
    les bases SQLite sans devoir migrer l'application à chaque évolution.
    """
    quiz_key = str(quiz_key or "general")
    table = "lol_questions" if quiz_key == "lol" else "trivia_questions"

    with connect_trivia(quiz_key) as conn:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE question_id = ? AND active = 1",
            (int(question_id),),
        ).fetchone()

    if row is None:
        return None

    data = dict(row)
    qtype = str(data.get("question_type") or "mcq").strip().lower()
    if qtype not in {"mcq", "free"}:
        qtype = "mcq"

    options = None
    correct_index = data.get("correct_index")
    if qtype == "mcq":
        options = [
            data.get("option_a"),
            data.get("option_b"),
            data.get("option_c"),
            data.get("option_d"),
        ]
        options = [str(value or "").strip() for value in options]
        if len(options) != 4 or not all(options):
            return None
        try:
            correct_index = int(correct_index)
        except (TypeError, ValueError):
            return None
        if correct_index not in range(4):
            return None

    correct_answer = str(data.get("correct_answer") or "").strip()
    if qtype == "mcq" and not correct_answer:
        correct_answer = options[correct_index]

    raw_accepted = data.get("accepted_answers_json")
    try:
        accepted = json.loads(raw_accepted or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        accepted = []
    if not isinstance(accepted, list):
        accepted = []
    accepted = [str(value).strip() for value in accepted if str(value).strip()]
    if correct_answer and correct_answer not in accepted:
        accepted.append(correct_answer)

    image_url = (
        data.get("image_url")
        or data.get("image")
        or data.get("media_url")
        or ""
    )
    explanation = (
        data.get("explanation")
        or data.get("anecdote")
        or data.get("correction")
        or ""
    )

    return {
        "question_id": int(data["question_id"]),
        "category": data.get("category") or TRIVIA_LABELS.get(quiz_key, "Quiz"),
        "subcategory": data.get("subcategory") or "",
        "difficulty": data.get("difficulty") or "",
        "question_type": qtype,
        "question": data.get("question") or "",
        "options": options,
        "correct_index": correct_index,
        "correct_answer": correct_answer,
        "accepted_answers": accepted,
        "image_url": str(image_url or "").strip(),
        "image_alt": str(data.get("image_alt") or "").strip(),
        "explanation": str(explanation or "").strip(),
        "source_name": str(data.get("source_name") or "").strip(),
        "source_url": str(data.get("source_url") or "").strip(),
        "image_source_url": str(data.get("image_source_url") or "").strip(),
        "data_version": str(data.get("data_version") or "").strip(),
    }


def _normalize_trivia_answer(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Accepte les variantes typographiques usuelles : apostrophes, tirets,
    # espaces et accents ne doivent pas faire échouer une bonne réponse.
    return "".join(ch for ch in text if ch.isalnum())


def _free_answer_is_correct(question, answer_text):
    candidate = _normalize_trivia_answer(answer_text)
    if not candidate:
        return False
    accepted = list(question.get("accepted_answers") or [])
    if question.get("correct_answer"):
        accepted.append(question["correct_answer"])
    return any(candidate == _normalize_trivia_answer(value) for value in accepted)


def get_trivia_run(run_id, user_id):
    if not run_id:
        return None
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT run_id, user_id, question_ids_json, current_index,
                   correct_count, reward_coins, status, created_at, completed_at,
                   COALESCE(quiz_key, 'general') AS quiz_key
            FROM trivia_runs
            WHERE run_id = ? AND user_id = ?
            """,
            (int(run_id), int(user_id)),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["question_ids"] = [int(x) for x in json.loads(data.pop("question_ids_json"))]
    data["quiz_key"] = str(data.get("quiz_key") or "general")
    return data


def get_active_trivia_run(user_id, quiz_key="general"):
    with connect_app() as conn:
        row = conn.execute(
            """
            SELECT run_id FROM trivia_runs
            WHERE user_id = ? AND status = 'active'
              AND COALESCE(quiz_key, 'general') = ?
            ORDER BY run_id DESC LIMIT 1
            """,
            (int(user_id), str(quiz_key)),
        ).fetchone()
    return get_trivia_run(row["run_id"], user_id) if row else None


def start_trivia_run(user_id, quiz_key="general"):
    quiz_key = str(quiz_key or "general")
    ok, _ = validate_trivia_database(quiz_key)
    if not ok:
        return None

    active = get_active_trivia_run(user_id, quiz_key)
    if active:
        # Si la base a été remplacée entre deux lancements, une ancienne partie
        # peut référencer des IDs qui n'existent plus. On la ferme proprement.
        idx = int(active.get("current_index") or 0)
        ids = active.get("question_ids") or []
        if idx < len(ids) and get_trivia_question(ids[idx], quiz_key) is not None:
            return active
        abandon_trivia_run(active["run_id"], user_id)

    with connect_trivia(quiz_key) as trivia_conn:
        table = "lol_questions" if quiz_key == "lol" else "trivia_questions"
        rows = trivia_conn.execute(
            f"SELECT question_id FROM {table} WHERE active = 1 ORDER BY RANDOM() LIMIT ?",
            (TRIVIA_QUESTION_COUNT,),
        ).fetchall()
    question_ids = [int(row["question_id"]) for row in rows]
    if len(question_ids) < TRIVIA_QUESTION_COUNT:
        return None

    with connect_app() as conn:
        cursor = conn.execute(
            """
            INSERT INTO trivia_runs (
                user_id, question_ids_json, current_index, correct_count,
                reward_coins, status, created_at, quiz_key
            ) VALUES (?, ?, 0, 0, 0, 'active', ?, ?)
            """,
            (int(user_id), json.dumps(question_ids), now_iso(), quiz_key),
        )
        run_id = int(cursor.lastrowid)
    return get_trivia_run(run_id, user_id)


def abandon_trivia_run(run_id, user_id):
    with connect_app() as conn:
        conn.execute(
            """
            UPDATE trivia_runs SET status = 'abandoned', completed_at = ?
            WHERE run_id = ? AND user_id = ? AND status = 'active'
            """,
            (now_iso(), int(run_id), int(user_id)),
        )


def submit_trivia_answer(run_id, user_id, question_id, selected_index=None, answer_text=None):
    now = now_iso()

    with connect_app() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            """
            SELECT question_ids_json, current_index, correct_count, status,
                   COALESCE(quiz_key, 'general') AS quiz_key
            FROM trivia_runs WHERE run_id = ? AND user_id = ?
            """,
            (int(run_id), int(user_id)),
        ).fetchone()
        if run is None or run["status"] != "active":
            return False, "Partie inactive.", None

        quiz_key = str(run["quiz_key"] or "general")
        question = get_trivia_question(question_id, quiz_key)
        if question is None:
            return False, "Question introuvable.", None

        question_ids = [int(x) for x in json.loads(run["question_ids_json"])]
        current_index = int(run["current_index"])
        if current_index >= len(question_ids) or question_ids[current_index] != int(question_id):
            return False, "Question invalide.", None

        if conn.execute(
            "SELECT 1 FROM trivia_answers WHERE run_id = ? AND question_id = ?",
            (int(run_id), int(question_id)),
        ).fetchone():
            return False, "Question déjà répondue.", None

        qtype = question.get("question_type", "mcq")
        stored_index = 0
        stored_text = None
        if qtype == "free":
            stored_text = str(answer_text or "").strip()
            if not stored_text:
                return False, "Entre une réponse.", None
            is_correct = _free_answer_is_correct(question, stored_text)
        else:
            if selected_index is None:
                return False, "Choisis une réponse.", None
            selected_index = int(selected_index)
            if selected_index not in range(4):
                return False, "Réponse invalide.", None
            stored_index = selected_index
            is_correct = selected_index == int(question["correct_index"])

        conn.execute(
            """
            INSERT INTO trivia_answers (
                run_id, question_id, selected_index, is_correct, answered_at, answer_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(run_id), int(question_id), stored_index, int(is_correct), now, stored_text),
        )

        new_correct = int(run["correct_count"]) + int(is_correct)
        new_index = current_index + 1
        completed = new_index >= len(question_ids)
        reward = 0

        if completed:
            reward = new_correct * TRIVIA_COINS_PER_CORRECT
            conn.execute(
                """
                UPDATE trivia_runs
                SET current_index = ?, correct_count = ?, reward_coins = ?,
                    status = 'completed', completed_at = ?
                WHERE run_id = ?
                """,
                (new_index, new_correct, reward, now, int(run_id)),
            )
            wallet = conn.execute(
                "SELECT balance_coins FROM user_wallets WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            balance = int(wallet["balance_coins"]) if wallet else STARTING_BALANCE_COINS
            if wallet is None:
                conn.execute(
                    "INSERT INTO user_wallets (user_id, balance_coins, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (int(user_id), balance, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO wallet_transactions (user_id, amount_coins, transaction_type, note, created_at)
                    VALUES (?, ?, 'starter', 'Solde de départ', ?)
                    """,
                    (int(user_id), balance, now),
                )
            conn.execute(
                "UPDATE user_wallets SET balance_coins = ?, updated_at = ? WHERE user_id = ?",
                (balance + reward, now, int(user_id)),
            )
            label = TRIVIA_LABELS.get(quiz_key, "Quiz")
            conn.execute(
                """
                INSERT INTO wallet_transactions (user_id, amount_coins, transaction_type, note, created_at)
                VALUES (?, ?, 'trivia_reward', ?, ?)
                """,
                (int(user_id), reward, f"{label} · {new_correct}/{len(question_ids)}", now),
            )
        else:
            conn.execute(
                "UPDATE trivia_runs SET current_index = ?, correct_count = ? WHERE run_id = ?",
                (new_index, new_correct, int(run_id)),
            )

        result = {
            "is_correct": bool(is_correct),
            "correct_answer": question.get("correct_answer") or "",
            "explanation": question.get("explanation") or "",
            "source_name": question.get("source_name") or "",
            "source_url": question.get("source_url") or "",
            "completed": completed,
            "correct_count": new_correct,
            "total": len(question_ids),
            "reward": reward,
            "quiz_key": quiz_key,
        }
    return True, "", result


# ============================================================
# JEUX / GENERATEUR PASSIF
# ============================================================


def _generator_live_panel(user_id):
    status = sync_coin_generator(user_id)

    rate = status["coins_per_tick"]
    interval = status["interval_seconds"]
    remaining = max(0.0, min(float(interval), status["seconds_to_next"]))
    progress = max(0.0, min(1.0, 1.0 - remaining / interval))

    c1, c2, c3 = st.columns(3)
    c1.metric("Solde", format_coins(status["balance"]))
    c2.metric("Production", f"{rate} 🪙 / {interval} s")
    c3.metric("Total généré", format_coins(status["total_generated"]))

    st.progress(progress)
    st.caption(f"Prochain gain dans {remaining:.1f} s")

    if status["level"] < GENERATOR_MAX_LEVEL:
        st.subheader("Amélioration")
        affordable = status["balance"] >= GENERATOR_UPGRADE_COST
        if st.button(
            f"{GENERATOR_UPGRADED_RATE} / {GENERATOR_INTERVAL_SECONDS} s — {format_coins(GENERATOR_UPGRADE_COST)}",
            type="primary",
            use_container_width=True,
            disabled=not affordable,
            key="generator_upgrade_button",
        ):
            success, message, _ = buy_generator_upgrade(user_id)
            if success:
                st.success(message)
            else:
                st.error(message)
            st.rerun()
    else:
        pass


# Les fragments Streamlit permettent au compteur de se rafraîchir sans
# relancer toute l'application. Sur une vieille version de Streamlit, la page
# reste fonctionnelle mais se met à jour au prochain rerun/interacton.
if hasattr(st, "fragment"):
    generator_live_panel = st.fragment(run_every="1s")(_generator_live_panel)
else:
    generator_live_panel = _generator_live_panel


def friends_page(user_id):
    st.header("Amis")

    with st.form("friend_add_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        with c1:
            friend_username = st.text_input(
                "Pseudo",
                placeholder="Pseudo exact de ton ami",
                label_visibility="collapsed",
            )
        with c2:
            submitted = st.form_submit_button(
                "Ajouter",
                use_container_width=True,
                type="primary",
            )

    if submitted:
        ok, message = send_friend_request(user_id, friend_username)
        if ok:
            st.success(message)
        else:
            st.warning(message)

    incoming = list_incoming_friend_requests(user_id)
    if incoming:
        st.subheader(f"Demandes reçues · {len(incoming)}")
        for request in incoming:
            with st.container(border=True):
                name_col, accept_col, decline_col = st.columns([5, 1.25, 1.25])
                with name_col:
                    st.markdown(f"### {request['username']}")
                with accept_col:
                    if st.button(
                        "✓ Accepter",
                        key=f"friend_accept_{request['request_id']}",
                        use_container_width=True,
                        type="primary",
                    ):
                        ok, message = accept_friend_request(
                            user_id,
                            request["request_id"],
                        )
                        if ok:
                            st.toast(message)
                        else:
                            st.warning(message)
                        st.rerun()
                with decline_col:
                    if st.button(
                        "✕ Refuser",
                        key=f"friend_decline_{request['request_id']}",
                        use_container_width=True,
                    ):
                        decline_friend_request(user_id, request["request_id"])
                        st.rerun()

    friends = list_friends(user_id)
    st.subheader(f"Mes amis · {len(friends)}")
    if not friends:
        st.info("Tu n'as pas encore d'ami ajouté.")
    else:
        for friend in friends:
            with st.container(border=True):
                name_col, action_col = st.columns([6, 1.4])
                with name_col:
                    st.markdown(f"### {friend['username']}")
                with action_col:
                    if st.button(
                        "Retirer",
                        key=f"friend_remove_{friend['user_id']}",
                        use_container_width=True,
                    ):
                        remove_friend(user_id, friend["user_id"])
                        st.rerun()

    outgoing = list_outgoing_friend_requests(user_id)
    if outgoing:
        with st.expander(f"Demandes envoyées · {len(outgoing)}"):
            for request in outgoing:
                row1, row2 = st.columns([5, 1.4])
                with row1:
                    st.write(f"{request['username']}")
                    st.caption("En attente")
                with row2:
                    if st.button(
                        "Annuler",
                        key=f"friend_cancel_{request['request_id']}",
                        use_container_width=True,
                    ):
                        cancel_friend_request(user_id, request["request_id"])
                        st.rerun()


def slot_machine_panel(user_id):
    balance = get_wallet_balance(user_id)
    result = st.session_state.get("slot_result")

    st.markdown(
        """
        <style>
        .slot-machine {
            max-width: 760px;
            margin: .4rem auto 1.1rem auto;
        }
        .slot-reels {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin: .8rem 0 1.1rem 0;
        }
        .slot-reel {
            position: relative;
            height: 150px;
            border: 1px solid rgba(128,128,128,.34);
            border-radius: 16px;
            background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.025));
            overflow: hidden;
            box-shadow: inset 0 0 30px rgba(0,0,0,.22);
        }
        .slot-reel::before,
        .slot-reel::after {
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            height: 34px;
            z-index: 3;
            pointer-events: none;
        }
        .slot-reel::before {
            top: 0;
            background: linear-gradient(180deg, rgba(0,0,0,.42), transparent);
        }
        .slot-reel::after {
            bottom: 0;
            background: linear-gradient(0deg, rgba(0,0,0,.42), transparent);
        }
        .slot-strip {
            position: absolute;
            inset: 0 0 auto 0;
            width: 100%;
            transform: translateY(var(--slot-end, 0px));
        }
        .slot-strip.spinning {
            will-change: transform, filter;
        }
        .slot-cell {
            height: 150px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: Inter, system-ui, sans-serif;
            font-size: clamp(2.7rem, 7vw, 5.2rem);
            line-height: 1;
            font-weight: 900;
            letter-spacing: -.06em;
        }
        /* Les keyframes du tirage sont générées avec un nom unique pour
           forcer le redémarrage de l'animation à chaque pari. */
        .slot-result {
            text-align: center;
            font-size: 1.15rem;
            font-weight: 700;
            min-height: 1.8rem;
        }
        .slot-paytable {
            text-align: center;
            opacity: .72;
            font-size: .82rem;
            margin-top: .75rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    animation_css = ""
    result_style = ""

    if result:
        reel_blocks = []
        spin_id = int(result["spin_id"])
        spin_animation = f"slotSpin_{spin_id}"
        result_animation = f"slotResult_{spin_id}"
        longest_duration = 0.0

        # Plus de cellules donnent l'impression d'un vrai rouleau et le nom
        # d'animation unique garantit un nouveau spin à chaque clic sur Jouer.
        for reel_index, final_symbol in enumerate(result["reels"]):
            rng = random.Random(spin_id * 1009 + reel_index * 9176)
            sequence = [rng.choice(SLOT_SYMBOLS) for _ in range(14)] + [final_symbol]
            cells = "".join(
                f'<div class="slot-cell">{html.escape(str(symbol))}</div>'
                for symbol in sequence
            )
            end_y = -150 * (len(sequence) - 1)
            duration = 0.92 + reel_index * 0.22
            longest_duration = max(longest_duration, duration)
            reel_blocks.append(
                '<div class="slot-reel">'
                f'<div class="slot-strip spinning" '
                f'style="--slot-end:{end_y}px;animation:{spin_animation} {duration:.2f}s cubic-bezier(.10,.72,.16,1) both">'
                f'{cells}</div></div>'
            )
        reel_html = "".join(reel_blocks)
        animation_css = f"""
        <style>
        @keyframes {spin_animation} {{
            0%   {{ transform: translateY(0); filter: blur(0); }}
            10%  {{ filter: blur(7px); }}
            62%  {{ filter: blur(4px); }}
            86%  {{ filter: blur(1.5px); }}
            100% {{ transform: translateY(var(--slot-end)); filter: blur(0); }}
        }}
        @keyframes {result_animation} {{
            0%, 88% {{ opacity:0; transform:translateY(5px); }}
            100%    {{ opacity:1; transform:translateY(0); }}
        }}
        </style>
        """
        result_style = (
            f"animation:{result_animation} {longest_duration + 0.18:.2f}s ease-out both"
        )
    else:
        reel_html = "".join(
            '<div class="slot-reel"><div class="slot-strip">'
            '<div class="slot-cell">—</div></div></div>'
            for _ in range(3)
        )

    if result:
        if result["multiplier"] > 0:
            result_text = f"x{result['multiplier']} · {format_coins(result['payout'])}"
        else:
            result_text = f"−{format_coins(result['stake'])}"
    else:
        result_text = ""

    st.markdown(
        f"""
        {animation_css}
        <div class="slot-machine">
            <div class="slot-reels">{reel_html}</div>
            <div class="slot-result" style="{result_style}">{html.escape(result_text)}</div>
            <div class="slot-paytable">
                7 7 7 ×10 &nbsp;·&nbsp; BAR ×6 &nbsp;·&nbsp; ◆ ×5 &nbsp;·&nbsp; ★ ×4
                &nbsp;·&nbsp; 3 identiques ×3 &nbsp;·&nbsp; 2 identiques ×2
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([2, 1])
    with c1:
        max_bet = max(SLOT_MIN_BET, balance)
        default_bet = min(100, max_bet)
        stake = st.number_input(
            "Mise",
            min_value=SLOT_MIN_BET,
            max_value=max_bet,
            value=default_bet,
            step=10,
            disabled=balance < SLOT_MIN_BET,
            key="slot_stake",
        )
    with c2:
        st.metric("Solde", format_coins(balance))

    if st.button(
        "Jouer",
        type="primary",
        use_container_width=True,
        disabled=balance < SLOT_MIN_BET,
        key="slot_spin_button",
    ):
        ok, message, spin = play_slot_machine(user_id, stake)
        if not ok:
            st.error(message)
        else:
            st.session_state["slot_result"] = spin
            st.rerun()


def trivia_panel(user_id, quiz_key="general"):
    quiz_key = str(quiz_key or "general")
    ok, error = validate_trivia_database(quiz_key)
    if not ok:
        st.error(error)
        st.caption(f"Place {TRIVIA_DATABASES[quiz_key].name} dans le même dossier que app.py.")
        return

    get_wallet_balance(user_id)
    run_state_key = f"trivia_run_id_{quiz_key}"
    feedback_key = f"trivia_feedback_{quiz_key}"

    run_id = st.session_state.get(run_state_key)
    run = get_trivia_run(run_id, user_id) if run_id else None
    if run is not None and run.get("quiz_key") != quiz_key:
        run = None
        st.session_state.pop(run_state_key, None)

    if run is None or run.get("status") == "abandoned":
        run = get_active_trivia_run(user_id, quiz_key)
        if run:
            st.session_state[run_state_key] = run["run_id"]

    feedback = st.session_state.get(feedback_key)
    if feedback:
        if feedback["is_correct"]:
            st.success("Bonne réponse")
        else:
            st.error(f"Réponse correcte : {feedback['correct_answer']}")

        if feedback.get("explanation"):
            st.write(feedback["explanation"])
        if feedback.get("source_url"):
            source_label = feedback.get("source_name") or "Source"
            st.caption(f"{source_label} · {feedback['source_url']}")

        if feedback["completed"]:
            c1, c2 = st.columns(2)
            c1.metric("Score", f"{feedback['correct_count']} / {feedback['total']}")
            c2.metric("Gain", format_coins(feedback["reward"]))
            if st.button("Rejouer", type="primary", use_container_width=True, key=f"trivia_replay_{quiz_key}"):
                st.session_state.pop(feedback_key, None)
                st.session_state.pop(run_state_key, None)
                new_run = start_trivia_run(user_id, quiz_key)
                if new_run:
                    st.session_state[run_state_key] = new_run["run_id"]
                st.rerun()
        else:
            if st.button("Question suivante", type="primary", use_container_width=True, key=f"trivia_next_{quiz_key}"):
                st.session_state.pop(feedback_key, None)
                st.rerun()
        return

    if run and run.get("status") == "completed":
        c1, c2 = st.columns(2)
        c1.metric("Score", f"{run['correct_count']} / {len(run['question_ids'])}")
        c2.metric("Gain", format_coins(run["reward_coins"]))
        if st.button("Rejouer", type="primary", use_container_width=True, key=f"trivia_replay_completed_{quiz_key}"):
            st.session_state.pop(run_state_key, None)
            new_run = start_trivia_run(user_id, quiz_key)
            if new_run:
                st.session_state[run_state_key] = new_run["run_id"]
            st.rerun()
        return

    if not run:
        c1, c2 = st.columns(2)
        c1.metric("Questions", TRIVIA_QUESTION_COUNT)
        c2.metric("Gain / réponse", format_coins(TRIVIA_COINS_PER_CORRECT))
        if st.button("Commencer", type="primary", use_container_width=True, key=f"trivia_start_{quiz_key}"):
            new_run = start_trivia_run(user_id, quiz_key)
            if new_run is None:
                st.error("Pas assez de questions disponibles.")
            else:
                st.session_state[run_state_key] = new_run["run_id"]
                st.rerun()
        return

    question_ids = run["question_ids"]
    index = int(run["current_index"])
    if index >= len(question_ids):
        st.rerun()
        return

    question = get_trivia_question(question_ids[index], quiz_key)
    if question is None:
        # Cas fréquent après remplacement d'un fichier SQLite : les IDs d'une
        # ancienne partie ne correspondent plus à la nouvelle base.
        abandon_trivia_run(run["run_id"], user_id)
        st.session_state.pop(run_state_key, None)
        st.session_state.pop(feedback_key, None)
        st.rerun()
        return

    c1, c2 = st.columns([4, 1])
    with c1:
        st.progress((index + 1) / len(question_ids), text=f"Question {index + 1} / {len(question_ids)}")
    with c2:
        st.metric("Score", run["correct_count"])

    labels = [str(question.get("category") or "")]
    if question.get("subcategory"):
        labels.append(str(question["subcategory"]))
    if question.get("difficulty"):
        labels.append(str(question["difficulty"]).capitalize())
    st.caption(" · ".join(x for x in labels if x))

    if question.get("image_url"):
        image_cols = st.columns([1, 2, 1])
        with image_cols[1]:
            st.image(
                question["image_url"],
                caption=question.get("image_alt") or None,
                use_container_width=True,
            )

    st.subheader(question["question"])

    selected_index = None
    answer_text = None
    if question.get("question_type") == "free":
        answer_text = st.text_input(
            "Réponse",
            label_visibility="collapsed",
            placeholder="Ta réponse",
            key=f"trivia_free_{quiz_key}_{run['run_id']}_{question['question_id']}",
        )
        can_validate = bool(str(answer_text or "").strip())
    else:
        options = question["options"]
        selected = st.radio(
            "Réponse",
            options,
            index=None,
            label_visibility="collapsed",
            key=f"trivia_answer_{quiz_key}_{run['run_id']}_{question['question_id']}",
        )
        can_validate = selected is not None
        if selected is not None:
            selected_index = options.index(selected)

    c1, c2 = st.columns([4, 1])
    validate = c1.button(
        "Valider",
        type="primary",
        use_container_width=True,
        disabled=not can_validate,
        key=f"trivia_validate_{quiz_key}_{run['run_id']}_{question['question_id']}",
    )
    abandon = c2.button(
        "Quitter",
        use_container_width=True,
        key=f"trivia_abandon_{quiz_key}_{run['run_id']}",
    )

    if abandon:
        abandon_trivia_run(run["run_id"], user_id)
        st.session_state.pop(run_state_key, None)
        st.session_state.pop(feedback_key, None)
        st.rerun()

    if validate:
        ok, message, result = submit_trivia_answer(
            run["run_id"],
            user_id,
            question["question_id"],
            selected_index=selected_index,
            answer_text=answer_text,
        )
        if not ok:
            st.error(message)
        else:
            st.session_state[feedback_key] = result
            st.rerun()


def games_page(user_id):
    st.header("Jeux")

    game = st.radio(
        "Jeu",
        ["Générateur", "Machine à sous", "Culture générale", "League of Legends"],
        horizontal=True,
        label_visibility="collapsed",
        key="games_navigation",
    )

    if game == "Générateur":
        generator_live_panel(user_id)
    elif game == "Machine à sous":
        slot_machine_panel(user_id)
    elif game == "Culture générale":
        trivia_panel(user_id, "general")
    else:
        trivia_panel(user_id, "lol")


# ============================================================
# PORTEFEUILLE
# ============================================================


def wallet_page(user_id):
    st.header("Portefeuille")

    balance = get_wallet_balance(user_id)
    st.metric("Solde actuel", format_coins(balance))

    if ALLOW_TEST_TOPUPS:
        st.divider()
        st.subheader("Recharge DEV")

        c1, c2, c3 = st.columns(3)
        if c1.button("+ 1 000 🪙", key="topup_1000", use_container_width=True, type="secondary"):
            add_wallet_coins(user_id, 1000, note="Recharge DEV")
            st.rerun()
        if c2.button("+ 5 000 🪙", key="topup_5000", use_container_width=True, type="secondary"):
            add_wallet_coins(user_id, 5000, note="Recharge DEV")
            st.rerun()
        if c3.button("+ 10 000 🪙", key="topup_10000", use_container_width=True, type="secondary"):
            add_wallet_coins(user_id, 10000, note="Recharge DEV")
            st.rerun()

        custom_col, custom_button_col = st.columns([2, 1])
        with custom_col:
            custom_amount = st.number_input(
                "Montant personnalisé",
                min_value=1,
                max_value=1_000_000,
                value=2_500,
                step=100,
                key="dev_custom_topup",
            )
        with custom_button_col:
            st.write("")
            st.write("")
            if st.button(
                "Ajouter",
                key="topup_custom",
                use_container_width=True,
            ):
                add_wallet_coins(user_id, int(custom_amount), note="Recharge DEV personnalisée")
                st.rerun()

        st.divider()

    tx = read_app_dataframe(
        """
        SELECT
            amount_coins,
            transaction_type,
            game,
            set_code,
            note,
            created_at
        FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY transaction_id DESC
        LIMIT 100
        """,
        params=(user_id,),
    )

    if tx.empty:
        st.info("Aucun mouvement sur le portefeuille.")
        return

    tx = tx.copy()
    tx["Montant"] = tx["amount_coins"].map(
        lambda value: ("+" if int(value) > 0 else "") + format_coins(int(value))
    )
    tx["Jeu"] = tx["game"].map(GAME_LABELS).fillna("")
    tx = tx.rename(
        columns={
            "set_code": "Extension",
            "note": "Détail",
            "created_at": "Date",
        }
    )
    st.dataframe(
        tx[["Montant", "Jeu", "Extension", "Détail", "Date"]],
        hide_index=True,
        use_container_width=True,
    )


# ============================================================
# APPLICATION
# ============================================================


def main():
    onepiece_path = GAME_DB_PATHS["onepiece"]
    if not onepiece_path.is_file():
        st.error(
            f"Base One Piece introuvable : {onepiece_path.name}. "
            "Place les bases statiques de cartes dans le dossier de l'application."
        )
        st.stop()

    try:
        init_app_tables()
    except Exception as exc:
        st.error(f"Connexion à la base application impossible : {exc}")
        st.stop()

    if "user_id" not in st.session_state:
        login_page()
        return

    user_id = st.session_state["user_id"]
    username = st.session_state["username"]

    # Crédit automatique des gains passifs écoulés depuis le dernier rerun.
    sync_coin_generator(user_id)

    balance = get_wallet_balance(user_id)

    # Navigation principale en haut de l'application. Le radio horizontal est
    # stylé comme une vraie barre d'onglets afin de conserver une seule page
    # active à la fois sans charger toutes les vues simultanément.
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"] {
            display: none !important;
        }
        .st-key-top_nav_container {
            border-bottom: 1px solid rgba(128,128,128,.28);
            padding-bottom: .35rem;
            margin-bottom: 1.1rem;
        }
        .st-key-top_nav_container [data-testid="stRadio"] > div {
            display: flex !important;
            flex-wrap: wrap !important;
            gap: .25rem !important;
            align-items: center !important;
        }
        .st-key-top_nav_container [data-testid="stRadio"] label {
            position: relative;
            margin: 0 !important;
            padding: .55rem .9rem !important;
            border-radius: 8px 8px 0 0 !important;
            border-bottom: 2px solid transparent !important;
            cursor: pointer !important;
            transition: background .15s ease, border-color .15s ease;
        }
        .st-key-top_nav_container [data-testid="stRadio"] label:hover {
            background: rgba(128,128,128,.10) !important;
        }
        .st-key-top_nav_container [data-testid="stRadio"] label:has(input:checked) {
            background: rgba(128,128,128,.13) !important;
            border-bottom-color: currentColor !important;
        }
        .st-key-top_nav_container [data-testid="stRadio"] input {
            display: none !important;
        }
        .st-key-top_nav_container [data-testid="stRadio"] label > div:first-child {
            display: none !important;
        }
        @media (max-width: 900px) {
            .st-key-top_nav_container [data-testid="stRadio"] label {
                padding: .45rem .6rem !important;
                font-size: .9rem !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    pages = [
        "Booster",
        "Cartedex",
        "Decks",
        "Amis",
        "Jouer",
        "Jeux",
        "Portefeuille",
    ]

    with st.container(key="top_nav_container"):
        brand_col, account_col, logout_col = st.columns([5.5, 2.3, 1.2], vertical_alignment="center")
        with brand_col:
            st.markdown("### TCG Booster Simulator")
        with account_col:
            st.markdown(f"**{username}**")
            st.caption(f"Solde : {format_coins(balance, show_eur=False)}")
        with logout_col:
            if st.button("Déconnexion", use_container_width=True, key="top_logout"):
                st.session_state.clear()
                st.rerun()

        page = st.radio(
            "Navigation",
            pages,
            horizontal=True,
            label_visibility="collapsed",
            key="main_navigation",
        )

    if page == "Booster":
        booster_page(user_id)
    elif page == "Cartedex":
        cartedex_page(user_id)
    elif page == "Decks":
        deck_page(user_id)
    elif page == "Amis":
        friends_page(user_id)
    elif page == "Jouer":
        play_page(user_id)
    elif page == "Jeux":
        games_page(user_id)
    elif page == "Portefeuille":
        wallet_page(user_id)


if __name__ == "__main__":
    main()
