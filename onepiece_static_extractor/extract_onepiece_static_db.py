from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
from pathlib import Path

# Tables "vivantes" de l'application : elles ne doivent PAS partir sur GitHub.
# Cette liste reprend les tables utilisateur/économie/decks/amis/matchs/quiz
# utilisées par l'application.
DYNAMIC_TABLES = {
    "app_users",
    "user_collection",
    "pack_openings",
    "opening_cards",
    "user_wallets",
    "wallet_transactions",
    "booster_prices",
    "coin_generators",
    "slot_spins",
    "trivia_runs",
    "trivia_answers",
    "decks",
    "deck_cards",
    "friend_requests",
    "friendships",
    "game_invites",
    "multiplayer_matches",
    "matches",
    "match_players",
    "match_board_cards",
    "match_invitations",
}

# Préfixes qui indiquent très probablement une table liée aux joueurs.
DYNAMIC_PREFIXES = (
    "user_",
    "friend_",
    "match_",
    "multiplayer_",
    "wallet_",
    "app_",
)

# Tables statiques attendues / connues côté One Piece.
KNOWN_STATIC_TABLES = {
    "cards",
    "sets",
    "pull_rates",
    "terminal_cards",
    "card_drop_estimates",
    "don_images",
    "don_image_manifest",
    "don_image_overrides",
}


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_dynamic_table(name: str) -> bool:
    low = name.casefold()
    if low in {x.casefold() for x in DYNAMIC_TABLES}:
        return True
    return any(low.startswith(prefix) for prefix in DYNAMIC_PREFIXES)


def list_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [r[0] for r in rows]


def list_views(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'view'
          AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def schema_sql(conn: sqlite3.Connection, obj_type: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (obj_type, name),
    ).fetchone()
    return row[0] if row else None


def table_indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = ?
          AND sql IS NOT NULL
        ORDER BY name
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def table_triggers(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'trigger'
          AND tbl_name = ?
          AND sql IS NOT NULL
        ORDER BY name
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def view_is_safe(sql: str, included_tables: set[str], excluded_tables: set[str]) -> bool:
    """
    Garde uniquement les vues qui ne mentionnent pas explicitement une table
    utilisateur exclue. Ce test est volontairement prudent.
    """
    low = sql.casefold()
    for table in excluded_tables:
        if re.search(rf"\b{re.escape(table.casefold())}\b", low):
            return False
    return True


def copy_static_database(
    source: Path,
    target: Path,
    *,
    overwrite: bool = False,
    include_views: bool = True,
) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Base source introuvable : {source}")

    if source.resolve() == target.resolve():
        raise ValueError("La base source et la base cible doivent être différentes.")

    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"{target} existe déjà. Utilise --overwrite pour la remplacer."
            )
        target.unlink()

    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row

    all_tables = list_user_tables(src)
    included = [t for t in all_tables if not is_dynamic_table(t)]
    excluded = [t for t in all_tables if is_dynamic_table(t)]

    if not included:
        raise RuntimeError("Aucune table statique détectée.")

    print("=" * 72)
    print("EXTRACTION DE LA BASE ONE PIECE STATIQUE")
    print("=" * 72)
    print(f"Source : {source.resolve()}")
    print(f"Cible  : {target.resolve()}")
    print()

    print("Tables EXCLUES (données joueurs / application) :")
    if excluded:
        for name in excluded:
            print(f"  - {name}")
    else:
        print("  (aucune)")
    print()

    print("Tables COPIÉES dans onepiece_cards.sqlite :")
    for name in included:
        marker = " [connue statique]" if name in KNOWN_STATIC_TABLES else ""
        print(f"  - {name}{marker}")
    print()

    unknown = [t for t in included if t not in KNOWN_STATIC_TABLES]
    if unknown:
        print("NOTE : tables non reconnues mais considérées statiques car elles ne")
        print("ressemblent pas à des tables utilisateur :")
        for name in unknown:
            print(f"  - {name}")
        print()

    dst = sqlite3.connect(target)
    try:
        dst.execute("PRAGMA foreign_keys = OFF")
        dst.execute("PRAGMA journal_mode = DELETE")
        dst.execute("PRAGMA synchronous = FULL")

        src.execute("PRAGMA query_only = ON")

        # Copie des tables + données.
        for table in included:
            create_sql = schema_sql(src, "table", table)
            if not create_sql:
                print(f"[SKIP] Schéma introuvable pour {table}")
                continue

            print(f"[TABLE] {table}")
            dst.execute(create_sql)

            columns = [
                row["name"]
                for row in src.execute(f"PRAGMA table_info({qident(table)})").fetchall()
            ]
            if not columns:
                continue

            col_sql = ", ".join(qident(c) for c in columns)
            placeholders = ", ".join("?" for _ in columns)

            cursor = src.execute(f"SELECT {col_sql} FROM {qident(table)}")
            insert_sql = (
                f"INSERT INTO {qident(table)} ({col_sql}) "
                f"VALUES ({placeholders})"
            )

            count = 0
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                dst.executemany(insert_sql, [tuple(row) for row in batch])
                count += len(batch)

            print(f"        {count} lignes")

            for sql in table_indexes(src, table):
                dst.execute(sql)

            # Les triggers ne sont copiés que s'ils ne mentionnent pas une table dynamique.
            for sql in table_triggers(src, table):
                if view_is_safe(sql, set(included), set(excluded)):
                    dst.execute(sql)
                else:
                    print("        trigger ignoré (référence données dynamiques)")

        # Copie prudente des vues.
        if include_views:
            for view_name, view_sql in list_views(src):
                if view_is_safe(view_sql, set(included), set(excluded)):
                    try:
                        dst.execute(view_sql)
                        print(f"[VIEW ] {view_name}")
                    except sqlite3.Error as exc:
                        print(f"[SKIP ] vue {view_name}: {exc}")

        dst.commit()

        # Vérifications.
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Échec integrity_check : {integrity}")

        copied_tables = list_user_tables(dst)
        leaked = [t for t in copied_tables if is_dynamic_table(t)]
        if leaked:
            raise RuntimeError(
                "Des tables dynamiques ont été copiées par erreur : "
                + ", ".join(leaked)
            )

        print()
        print("=" * 72)
        print("TERMINÉ")
        print("=" * 72)
        print(f"Intégrité SQLite : {integrity}")
        print(f"Tables copiées   : {len(copied_tables)}")
        print(f"Fichier créé     : {target.resolve()}")
        print()
        print("Tu peux mettre CE fichier sur GitHub.")
        print("Garde la base source onepiece_tcg.sqlite en sauvegarde locale tant que")
        print("la migration des données joueurs vers Turso n'est pas terminée.")

    except Exception:
        dst.rollback()
        dst.close()
        if target.exists():
            target.unlink()
        raise
    finally:
        try:
            dst.close()
        except Exception:
            pass
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extrait les tables statiques de onepiece_tcg.sqlite vers "
            "onepiece_cards.sqlite, sans copier les données utilisateurs."
        )
    )
    parser.add_argument(
        "source",
        nargs="?",
        default="onepiece_tcg.sqlite",
        help="Base source (défaut: onepiece_tcg.sqlite)",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="onepiece_cards.sqlite",
        help="Base cible (défaut: onepiece_cards.sqlite)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Remplace la base cible si elle existe déjà.",
    )
    parser.add_argument(
        "--no-views",
        action="store_true",
        help="Ne copie aucune vue SQLite.",
    )
    args = parser.parse_args()

    copy_static_database(
        Path(args.source),
        Path(args.target),
        overwrite=args.overwrite,
        include_views=not args.no_views,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
