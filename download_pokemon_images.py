import re
import sqlite3
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIG
# ============================================================

DB_PATH = "pokemon_tcg.sqlite"

IMAGE_ROOT = Path(
    "pokemon_images"
)

MAX_WORKERS = 6

TIMEOUT = 30


# ============================================================
# OUTILS
# ============================================================

def safe_filename(value):

    return re.sub(
        r'[<>:"/\\|?*]',
        "_",
        str(value),
    )


def create_session():

    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
    )

    adapter = HTTPAdapter(
        max_retries=retry,
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update({
        "User-Agent":
            "PokemonTCG-image-downloader/1.0"
    })

    return session


# ============================================================
# DOWNLOAD
# ============================================================

def download_card(
    row
):

    (
        card_id,
        set_id,
        image_url
    ) = row


    if not image_url:

        return (
            card_id,
            None,
            "NO_URL"
        )


    folder = (
        IMAGE_ROOT
        /
        safe_filename(
            set_id
        )
    )

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )


    destination = (
        folder
        /
        f"{safe_filename(card_id)}.webp"
    )


    if destination.exists():

        return (
            card_id,
            str(destination),
            "EXISTS"
        )


    session = create_session()


    try:

        response = session.get(
            image_url,
            timeout=TIMEOUT,
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
            and "image" not in content_type
        ):

            return (
                card_id,
                None,
                "NOT_IMAGE"
            )


        temporary = (
            destination
            .with_suffix(
                ".webp.part"
            )
        )


        temporary.write_bytes(
            response.content
        )


        temporary.replace(
            destination
        )


        return (
            card_id,
            str(destination),
            "DOWNLOADED"
        )


    except Exception as exc:

        return (
            card_id,
            None,
            f"ERROR: {exc}"
        )


# ============================================================
# SQLITE
# ============================================================

conn = sqlite3.connect(
    DB_PATH
)

cursor = conn.cursor()


rows = cursor.execute(
    """
    SELECT

        card_id,
        set_id,
        image_high_url

    FROM cards

    WHERE
        image_high_url IS NOT NULL

    ORDER BY
        set_id,
        card_id
    """
).fetchall()


print(
    f"{len(rows)} cartes "
    f"avec image."
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
            row
        ):
            row

        for row in rows
    }


    for index, future in enumerate(
        as_completed(
            futures
        ),
        start=1
    ):

        (
            card_id,
            path,
            status
        ) = future.result()


        if path:

            cursor.execute(
                """
                UPDATE cards

                SET local_image_path = ?

                WHERE card_id = ?
                """,

                (
                    path,
                    card_id
                )
            )


        if status == "DOWNLOADED":

            downloaded += 1


        elif status == "EXISTS":

            existing += 1


        else:

            errors += 1

            print(
                f"{card_id}: "
                f"{status}"
            )


        if index % 100 == 0:

            conn.commit()

            print(
                f"{index}/{len(rows)} "
                f"| téléchargées "
                f"{downloaded} "
                f"| existantes "
                f"{existing} "
                f"| erreurs "
                f"{errors}"
            )


conn.commit()

conn.close()


print()
print("=" * 60)
print("TERMINÉ")
print("=" * 60)

print(
    f"Téléchargées   : "
    f"{downloaded}"
)

print(
    f"Déjà présentes : "
    f"{existing}"
)

print(
    f"Erreurs        : "
    f"{errors}"
)

print(
    f"Dossier        : "
    f"{IMAGE_ROOT.resolve()}"
)