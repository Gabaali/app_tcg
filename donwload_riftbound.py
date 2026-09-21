import argparse
import mimetypes
import re
import sqlite3
import threading
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = "riftbound_tcg.sqlite"

IMAGE_ROOT = Path(
    "riftbound_images"
)

MAX_WORKERS = 4

TIMEOUT = 45


# ============================================================
# SESSION HTTP PAR THREAD
# ============================================================

thread_local = threading.local()


def get_session():

    if not hasattr(
        thread_local,
        "session"
    ):

        session = requests.Session()

        retry = Retry(
            total=4,
            connect=4,
            read=4,

            backoff_factor=1,

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
            max_retries=retry
        )

        session.mount(
            "https://",
            adapter
        )

        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/140 Safari/537.36"
            )
        })

        thread_local.session = (
            session
        )

    return thread_local.session


# ============================================================
# OUTILS
# ============================================================

def safe_filename(value):

    text = str(
        value or ""
    )

    # On garde l'information signed
    text = text.replace(
        "*",
        "_SIGNED"
    )

    text = text.replace(
        "/",
        "_"
    )

    text = re.sub(
        r'[<>:"\\|?]',
        "_",
        text
    )

    return text.strip()


def choose_extension(
    response,
    url
):

    content_type = (
        response.headers
        .get(
            "Content-Type",
            ""
        )
        .split(";")[0]
        .lower()
        .strip()
    )

    known = {
        "image/png":
            ".png",

        "image/jpeg":
            ".jpg",

        "image/webp":
            ".webp",

        "image/avif":
            ".avif",
    }

    if content_type in known:

        return known[
            content_type
        ]


    suffix = Path(
        urlsplit(
            url
        ).path
    ).suffix.lower()


    if suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".avif",
    }:

        if suffix == ".jpeg":
            return ".jpg"

        return suffix


    guessed = mimetypes.guess_extension(
        content_type
    )

    return guessed or ".png"


# ============================================================
# TÉLÉCHARGEMENT
# ============================================================

def download_card(
    card_uid,
    public_code,
    set_code,
    image_url,
    force=False,
):

    if not image_url:

        return (
            card_uid,
            None,
            "NO_URL"
        )


    set_folder = (
        IMAGE_ROOT
        /
        safe_filename(
            set_code
        )
    )

    set_folder.mkdir(
        parents=True,
        exist_ok=True
    )


    stem = safe_filename(
        public_code
        or card_uid
    )


    # ========================================================
    # DÉJÀ TÉLÉCHARGÉE
    # ========================================================

    existing = list(
        set_folder.glob(
            f"{stem}.*"
        )
    )


    if (
        existing
        and not force
    ):

        return (
            card_uid,
            str(
                existing[0]
            ),
            "EXISTS"
        )


    # ========================================================
    # DOWNLOAD
    # ========================================================

    try:

        session = get_session()

        response = session.get(
            image_url,
            timeout=TIMEOUT,
            stream=True
        )

        response.raise_for_status()


        content_type = (
            response.headers
            .get(
                "Content-Type",
                ""
            )
            .lower()
        )


        if (
            content_type
            and
            not content_type.startswith(
                "image/"
            )
        ):

            return (
                card_uid,
                None,
                f"NOT_IMAGE:{content_type}"
            )


        extension = choose_extension(
            response,
            image_url
        )


        destination = (
            set_folder
            /
            f"{stem}{extension}"
        )


        temporary = (
            destination
            .with_suffix(
                destination.suffix
                + ".part"
            )
        )


        with temporary.open(
            "wb"
        ) as file:

            for chunk in (
                response.iter_content(
                    chunk_size=1024 * 128
                )
            ):

                if chunk:

                    file.write(
                        chunk
                    )


        temporary.replace(
            destination
        )


        return (
            card_uid,
            str(
                destination
            ),
            "DOWNLOADED"
        )


    except Exception as exc:

        return (
            card_uid,
            None,
            f"ERROR:{exc}"
        )


# ============================================================
# ARGUMENTS
# ============================================================

def arguments():

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--set",
        dest="set_code",
        default=None,
        help=(
            "Seulement un set, "
            "exemple OGN"
        )
    )


    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Limiter le nombre d'images "
            "pour tester"
        )
    )


    parser.add_argument(
        "--no-variants",
        action="store_true",
        help=(
            "Ne télécharger que les "
            "impressions de base"
        )
    )


    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Retélécharger les images "
            "déjà présentes"
        )
    )


    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():

    args = arguments()


    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()


    query = """
    SELECT

        card_uid,
        public_code,
        set_code,
        image_full_url

    FROM cards

    WHERE
        active = 1

        AND image_full_url
            IS NOT NULL

        AND TRIM(
            image_full_url
        ) != ''
    """


    params = []


    if args.set_code:

        query += """
        AND UPPER(set_code) = ?
        """

        params.append(
            args.set_code.upper()
        )


    if args.no_variants:

        query += """
        AND is_variant = 0
        """


    query += """
    ORDER BY
        set_code,
        collector_number,
        public_code
    """


    rows = cursor.execute(
        query,
        params
    ).fetchall()


    if args.limit:

        rows = rows[
            :args.limit
        ]


    print(
        f"{len(rows)} images à traiter."
    )


    IMAGE_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )


    downloaded = 0
    existing = 0
    errors = 0


    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:


        futures = {

            executor.submit(
                download_card,
                card_uid,
                public_code,
                set_code,
                image_url,
                args.force,
            ):
            card_uid

            for (
                card_uid,
                public_code,
                set_code,
                image_url
            )
            in rows
        }


        for index, future in enumerate(
            as_completed(
                futures
            ),
            start=1
        ):

            (
                card_uid,
                local_path,
                status
            ) = future.result()


            if local_path:

                cursor.execute(
                    """
                    UPDATE cards

                    SET local_image_path = ?

                    WHERE card_uid = ?
                    """,
                    (
                        local_path,
                        card_uid
                    )
                )


            if status == "DOWNLOADED":

                downloaded += 1

            elif status == "EXISTS":

                existing += 1

            else:

                errors += 1

                print(
                    f"ERREUR "
                    f"{card_uid}: "
                    f"{status}"
                )


            if index % 50 == 0:

                conn.commit()

                print(
                    f"{index}/{len(rows)} "
                    f"| téléchargées {downloaded} "
                    f"| existantes {existing} "
                    f"| erreurs {errors}"
                )


    conn.commit()

    conn.close()


    print()
    print(
        "=" * 60
    )

    print(
        "TÉLÉCHARGEMENT TERMINÉ"
    )

    print(
        "=" * 60
    )

    print(
        f"Téléchargées   : {downloaded}"
    )

    print(
        f"Déjà présentes : {existing}"
    )

    print(
        f"Erreurs        : {errors}"
    )

    print(
        f"Dossier        : "
        f"{IMAGE_ROOT.resolve()}"
    )


if __name__ == "__main__":
    main()