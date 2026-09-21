from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None

try:
    import turso_serverless
except ImportError as exc:
    raise SystemExit(
        "turso_serverless n'est pas installé. Lance : pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parent
SOURCE_DB = ROOT / "onepiece_tcg.sqlite"
SECRETS_FILE = ROOT / ".streamlit" / "secrets.toml"

# Ordre choisi pour respecter les dépendances entre tables.
TABLES = [
    "app_users",
    "user_wallets",
    "wallet_transactions",
    "user_collection",
    "pack_openings",
    "opening_cards",
    "coin_generators",
    "slot_spins",
    "booster_prices",
    "decks",
    "deck_cards",
    "friend_requests",
    "friendships",
    "game_invites",
    "multiplayer_matches",
    "match_players",
    "match_board_cards",
    "trivia_runs",
    "trivia_answers",
]


def read_credentials() -> tuple[str, str]:
    url = str(os.environ.get("TURSO_DATABASE_URL", "") or "").strip()
    token = str(os.environ.get("TURSO_AUTH_TOKEN", "") or "").strip()

    if url and token:
        return url, token

    if SECRETS_FILE.exists() and tomllib is not None:
        with SECRETS_FILE.open("rb") as f:
            data = tomllib.load(f)
        url = str(data.get("TURSO_DATABASE_URL", "") or "").strip()
        token = str(data.get("TURSO_AUTH_TOKEN", "") or "").strip()

    if not url or not token:
        raise SystemExit(
            "Identifiants Turso introuvables.\n"
            "Crée .streamlit/secrets.toml avec TURSO_DATABASE_URL et "
            "TURSO_AUTH_TOKEN, ou définis ces variables d'environnement."
        )
    return url, token


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def source_create_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] if row and row[0] else None


def source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    ]


def target_count(conn, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def main() -> int:
    if not SOURCE_DB.exists():
        print(f"Source introuvable : {SOURCE_DB}")
        print("Ce script sert uniquement à migrer les anciennes données joueurs.")
        print("Si tu veux repartir de zéro, tu n'as pas besoin de le lancer.")
        return 1

    url, token = read_credentials()

    print("=" * 72)
    print("MIGRATION onepiece_tcg.sqlite -> TURSO")
    print("=" * 72)
    print(f"Source : {SOURCE_DB}")
    print(f"Turso  : {url}")
    print()
    print("La base source n'est pas modifiée.")
    print()

    source = sqlite3.connect(SOURCE_DB)
    target = turso_serverless.connect(url, auth_token=token)

    try:
        try:
            target.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass

        total_inserted = 0

        for table in TABLES:
            if not table_exists(source, table):
                print(f"[SKIP] {table}: absente de la base locale")
                continue

            ddl = source_create_sql(source, table)
            if not ddl:
                print(f"[SKIP] {table}: schéma introuvable")
                continue

            print(f"[TABLE] {table}")

            # Crée la table distante si nécessaire.
            target.execute(ddl)

            columns = source_columns(source, table)
            if not columns:
                print("        aucune colonne")
                continue

            col_sql = ", ".join(quote_ident(c) for c in columns)
            placeholders = ", ".join("?" for _ in columns)
            insert_sql = (
                f"INSERT OR IGNORE INTO {quote_ident(table)} ({col_sql}) "
                f"VALUES ({placeholders})"
            )

            rows = source.execute(
                f"SELECT {col_sql} FROM {quote_ident(table)}"
            ).fetchall()

            before = target_count(target, table)

            # Insertion ligne par ligne : plus lente mais robuste pour un script
            # de migration exécuté une seule fois.
            for row in rows:
                target.execute(insert_sql, tuple(row))

            target.commit()
            after = target_count(target, table)
            inserted = max(0, after - before)
            total_inserted += inserted

            print(
                f"        local={len(rows)} | distant avant={before} | "
                f"distant après={after} | ajoutées={inserted}"
            )

        try:
            target.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass

        target.commit()

        print()
        print("=" * 72)
        print("MIGRATION TERMINÉE")
        print("=" * 72)
        print(f"Lignes ajoutées au total : {total_inserted}")
        print()
        print("Tu peux maintenant lancer app.py avec APP_DATABASE_MODE='turso'.")
        print("Ne supprime pas encore onepiece_tcg.sqlite : garde-la en sauvegarde.")
        return 0
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    raise SystemExit(main())
