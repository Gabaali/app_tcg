import argparse
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import (
    parse_qs,
    urljoin,
    urlparse
)

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError
)


# ============================================================
# CONFIG
# ============================================================

DB_PATH = "onepiece_tcg.sqlite"

BASE_SITE = "https://poneglyph.one"

OUTPUT_DIR = Path("images_poneglyph")

PAGE_TIMEOUT = 45_000
DOWNLOAD_TIMEOUT = 20_000

DEFAULT_DELAY = 1.0


# ============================================================
# OUTILS
# ============================================================

def safe_filename(value):
    value = str(value or "").strip()

    value = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        value
    )

    value = re.sub(
        r"\s+",
        "_",
        value
    )

    return value[:150]


def normalize_extension(filename):
    suffix = Path(filename).suffix.lower()

    valid = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".avif"
    }

    if suffix in valid:

        if suffix == ".jpeg":
            return ".jpg"

        return suffix

    return ".png"


# ============================================================
# SQLITE
# ============================================================

def prepare_database(conn):

    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS poneglyph_images (

        base_id TEXT NOT NULL,

        variant_index INTEGER NOT NULL,

        variant_label TEXT,

        page_url TEXT NOT NULL,

        source_url TEXT,

        local_path TEXT,

        status TEXT,

        downloaded_at TEXT,

        PRIMARY KEY (
            base_id,
            variant_index
        )
    );


    CREATE INDEX IF NOT EXISTS
        idx_poneglyph_base_id
    ON poneglyph_images(
        base_id
    );


    CREATE INDEX IF NOT EXISTS
        idx_poneglyph_status
    ON poneglyph_images(
        status
    );
    """)

    conn.commit()


# ============================================================
# CARTES À TRAITER
# ============================================================

def get_card_ids(
    conn,
    card=None,
    prefix=None,
    all_cards=False,
    limit=None
):

    cursor = conn.cursor()

    if card:

        rows = cursor.execute(
            """
            SELECT DISTINCT base_id
            FROM cards
            WHERE base_id = ?
            """,
            (card,)
        ).fetchall()

    elif prefix:

        rows = cursor.execute(
            """
            SELECT DISTINCT base_id
            FROM cards

            WHERE base_id LIKE ?

            ORDER BY base_id
            """,
            (
                prefix.upper() + "-%",
            )
        ).fetchall()

    elif all_cards:

        rows = cursor.execute(
            """
            SELECT DISTINCT base_id
            FROM cards

            WHERE
                base_id IS NOT NULL
                AND TRIM(base_id) != ''

            ORDER BY base_id
            """
        ).fetchall()

    else:

        return []


    ids = [
        row[0]
        for row in rows
    ]


    if limit:
        ids = ids[:limit]


    return ids


# ============================================================
# DÉCOUVERTE DES VARIANTES
# ============================================================

def discover_variant_indexes(page, base_id):
    """
    Cherche toutes les URLs contenant :

        ?variant=0
        ?variant=1
        ...

    Si rien n'est trouvé, on considère variant=0.
    """

    indexes = {0}


    # --------------------------------------------------------
    # URLs présentes dans les balises <a>
    # --------------------------------------------------------

    try:

        hrefs = page.locator(
            "a"
        ).evaluate_all(
            """
            els => els
                .map(el => el.href)
                .filter(Boolean)
            """
        )

    except Exception:

        hrefs = []


    for href in hrefs:

        try:

            parsed = urlparse(
                href
            )

            if (
                f"/cards/{base_id}"
                not in parsed.path
            ):
                continue


            query = parse_qs(
                parsed.query
            )


            if "variant" in query:

                value = int(
                    query["variant"][0]
                )

                indexes.add(
                    value
                )

        except Exception:
            pass


    # --------------------------------------------------------
    # Recherche aussi directement dans le HTML.
    #
    # Utile si les variantes sont générées par JS.
    # --------------------------------------------------------

    try:

        html = page.content()

        matches = re.findall(
            r"variant(?:=|%3D)(\d+)",
            html,
            flags=re.IGNORECASE
        )

        for value in matches:

            indexes.add(
                int(value)
            )

    except Exception:
        pass


    return sorted(
        indexes
    )


# ============================================================
# NOM DE LA VARIANTE
# ============================================================

def detect_variant_label(page, variant_index):
    """
    Poneglyph affiche par exemple :

        Image: Standard

    On tente de récupérer ce texte.
    """

    try:

        body = page.locator(
            "body"
        ).inner_text()


        match = re.search(
            r"(?mi)^Image:\s*(.+?)\s*$",
            body
        )


        if match:

            value = (
                match
                .group(1)
                .strip()
            )

            if value:
                return value[:100]


    except Exception:
        pass


    return f"variant_{variant_index}"


# ============================================================
# TROUVER LE BOUTON DOWNLOAD
# ============================================================

def find_download_control(page):

    # Plusieurs possibilités :
    #
    # <a>Download image</a>
    # <button>Download image</button>

    locator = page.locator(
        "a, button"
    ).filter(
        has_text=re.compile(
            r"^\s*Download image\s*$",
            re.IGNORECASE
        )
    )


    if locator.count() == 0:

        # fallback plus souple

        locator = page.get_by_text(
            "Download image",
            exact=True
        )


    if locator.count() == 0:
        return None


    return locator.first


# ============================================================
# TÉLÉCHARGEMENT DIRECT VIA HREF
# ============================================================

def download_href(
    context,
    href,
    page_url,
    destination_base
):

    absolute_url = urljoin(
        page_url,
        href
    )


    response = context.request.get(
        absolute_url,
        timeout=DOWNLOAD_TIMEOUT
    )


    if not response.ok:

        raise RuntimeError(
            f"HTTP {response.status}"
        )


    content_type = (
        response.headers
        .get("content-type", "")
        .lower()
    )


    if (
        content_type
        and "image" not in content_type
        and "octet-stream" not in content_type
    ):

        raise RuntimeError(
            f"Content-Type inattendu: "
            f"{content_type}"
        )


    parsed = urlparse(
        absolute_url
    )


    extension = normalize_extension(
        Path(parsed.path).name
    )


    destination = (
        destination_base
        .with_suffix(extension)
    )


    destination.write_bytes(
        response.body()
    )


    return (
        destination,
        absolute_url
    )


# ============================================================
# TÉLÉCHARGEMENT VIA LE BOUTON DU SITE
# ============================================================

def download_using_button(
    page,
    control,
    destination_base
):

    with page.expect_download(
        timeout=DOWNLOAD_TIMEOUT
    ) as info:

        control.click()


    download = info.value


    suggested = (
        download.suggested_filename
        or "card.png"
    )


    extension = normalize_extension(
        suggested
    )


    destination = (
        destination_base
        .with_suffix(extension)
    )


    download.save_as(
        str(destination)
    )


    return (
        destination,
        None
    )


# ============================================================
# TÉLÉCHARGEMENT D'UNE VARIANTE
# ============================================================

def process_variant(
    page,
    context,
    conn,
    base_id,
    variant_index,
    force=False
):

    cursor = conn.cursor()


    page_url = (
        f"{BASE_SITE}/cards/"
        f"{base_id}"
        f"?variant={variant_index}"
    )


    # --------------------------------------------------------
    # Existe déjà ?
    # --------------------------------------------------------

    existing = cursor.execute(
        """
        SELECT
            local_path,
            status

        FROM poneglyph_images

        WHERE
            base_id = ?
            AND variant_index = ?
        """,
        (
            base_id,
            variant_index
        )
    ).fetchone()


    if (
        existing
        and not force
        and existing[0]
        and Path(existing[0]).exists()
    ):

        print(
            f"    v{variant_index}: déjà présente"
        )

        return "EXISTS"


    # --------------------------------------------------------
    # Charger la fiche
    # --------------------------------------------------------

    page.goto(
        page_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )


    try:

        page.wait_for_function(
            """
            () => {
                const text =
                    document.body.innerText || '';

                return (
                    text.includes('Download image') ||
                    text.includes('Copy card number')
                );
            }
            """,
            timeout=20_000
        )

    except PlaywrightTimeoutError:

        pass


    page.wait_for_timeout(
        1000
    )

    variant_label = detect_variant_label(
        page,
        variant_index
    )


    print(
        f"    v{variant_index}: "
        f"{variant_label}"
    )


    # --------------------------------------------------------
    # Bouton Download image
    # --------------------------------------------------------

    control = find_download_control(
        page
    )


    if control is None:

        cursor.execute(
            """
            INSERT OR REPLACE
            INTO poneglyph_images (

                base_id,
                variant_index,

                variant_label,

                page_url,

                status,

                downloaded_at
            )

            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                base_id,
                variant_index,

                variant_label,

                page_url,

                "NO_DOWNLOAD_BUTTON",

                datetime.now(
                    timezone.utc
                ).isoformat()
            )
        )

        conn.commit()

        print(
            "      Pas de bouton Download image."
        )

        return "NO_DOWNLOAD_BUTTON"


    # --------------------------------------------------------
    # Dossier
    # --------------------------------------------------------

    folder = (
        OUTPUT_DIR
        / safe_filename(base_id)
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )


    filename = (
        f"v{variant_index:02d}_"
        f"{safe_filename(variant_label)}"
    )


    destination_base = (
        folder
        / filename
    )


    # --------------------------------------------------------
    # Vérifier si le contrôle contient déjà un href.
    # --------------------------------------------------------

    href = None

    try:

        href = control.get_attribute(
            "href"
        )

    except Exception:
        pass


    destination = None
    source_url = None


    # --------------------------------------------------------
    # Méthode 1 : lien direct
    # --------------------------------------------------------

    if (
        href
        and not href.startswith("#")
        and not href.lower().startswith(
            "javascript:"
        )
        and not href.lower().startswith(
            "data:"
        )
    ):

        try:

            (
                destination,
                source_url
            ) = download_href(
                context,
                href,
                page_url,
                destination_base
            )

        except Exception as exc:

            print(
                "      href direct échoué :",
                exc
            )


    # --------------------------------------------------------
    # Méthode 2 : bouton générant un vrai téléchargement
    # --------------------------------------------------------

    if destination is None:

        try:

            (
                destination,
                source_url
            ) = download_using_button(
                page,
                control,
                destination_base
            )

        except PlaywrightTimeoutError:

            print(
                "      Le bouton n'a pas généré "
                "de téléchargement."
            )

        except Exception as exc:

            print(
                "      Erreur téléchargement :",
                exc
            )


    # --------------------------------------------------------
    # Échec
    # --------------------------------------------------------

    if destination is None:

        cursor.execute(
            """
            INSERT OR REPLACE
            INTO poneglyph_images (

                base_id,
                variant_index,

                variant_label,

                page_url,

                status,

                downloaded_at
            )

            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                base_id,
                variant_index,

                variant_label,

                page_url,

                "DOWNLOAD_FAILED",

                datetime.now(
                    timezone.utc
                ).isoformat()
            )
        )

        conn.commit()

        return "ERROR"


    # --------------------------------------------------------
    # Succès
    # --------------------------------------------------------

    cursor.execute(
        """
        INSERT OR REPLACE
        INTO poneglyph_images (

            base_id,
            variant_index,

            variant_label,

            page_url,
            source_url,

            local_path,

            status,

            downloaded_at
        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            base_id,
            variant_index,

            variant_label,

            page_url,
            source_url,

            str(destination),

            "DOWNLOADED",

            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )


    conn.commit()


    print(
        f"      -> {destination}"
    )


    return "DOWNLOADED"


# ============================================================
# TRAITEMENT D'UNE CARTE
# ============================================================

def process_card(
    page,
    context,
    conn,
    base_id,
    delay,
    force=False
):

    print()
    print("=" * 60)
    print(base_id)

    initial_url = (
        f"{BASE_SITE}/cards/"
        f"{base_id}"
        f"?variant=0"
    )

    try:

        response = page.goto(
            initial_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        if response:
            print(
                f"  HTTP : {response.status}"
            )

        print(
            f"  URL  : {page.url}"
        )

        print(
            f"  Title: {page.title()}"
        )

        # ----------------------------------------------------
        # Poneglyph charge les données après le HTML initial.
        #
        # On attend jusqu'à 20 secondes que le contenu réel
        # de la carte apparaisse.
        # ----------------------------------------------------

        try:

            page.wait_for_function(
                """
                () => {
                    const text =
                        document.body.innerText || '';

                    return (
                        text.includes('Download image') ||
                        text.includes('Copy card number') ||
                        text.includes('Variants') ||
                        text.includes('Variant')
                    );
                }
                """,
                timeout=20_000
            )

        except PlaywrightTimeoutError:

            print(
                "  Timeout pendant le chargement "
                "du contenu dynamique."
            )


        # petite marge supplémentaire
        page.wait_for_timeout(
            1500
        )


    except Exception as exc:

        print(
            "  ERREUR page :",
            exc
        )

        return


    # ========================================================
    # DIAGNOSTIC
    # ========================================================

    try:

        body = page.locator(
            "body"
        ).inner_text()

    except Exception as exc:

        print(
            "  Impossible de lire la page :",
            exc
        )

        return


    # On ne teste PLUS uniquement "Download image".
    #
    # La présence du numéro de carte suffit à montrer
    # que la fiche est chargée.

    if base_id.lower() not in body.lower():

        print(
            "  Fiche non chargée correctement."
        )

        print()
        print(
            "  Début du contenu reçu :"
        )

        print(
            body[:1000]
        )

        # Capture automatique pour debug
        debug_dir = Path(
            "debug_poneglyph"
        )

        debug_dir.mkdir(
            exist_ok=True
        )

        screenshot_path = (
            debug_dir
            / f"{safe_filename(base_id)}.png"
        )

        html_path = (
            debug_dir
            / f"{safe_filename(base_id)}.html"
        )

        try:

            page.screenshot(
                path=str(screenshot_path),
                full_page=True
            )

            html_path.write_text(
                page.content(),
                encoding="utf-8"
            )

            print()
            print(
                f"  Screenshot : "
                f"{screenshot_path}"
            )

            print(
                f"  HTML       : "
                f"{html_path}"
            )

        except Exception as exc:

            print(
                "  Erreur debug :",
                exc
            )

        return


    print(
        "  Fiche chargée."
    )


    # ========================================================
    # VARIANTES
    # ========================================================

    variants = discover_variant_indexes(
        page,
        base_id
    )


    print(
        "  Variantes détectées :",
        variants
    )


    # ========================================================
    # TRAITEMENT
    # ========================================================

    for variant_index in variants:

        try:

            process_variant(
                page,
                context,
                conn,
                base_id,
                variant_index,
                force
            )

        except Exception as exc:

            print(
                f"    ERREUR v"
                f"{variant_index}: "
                f"{exc}"
            )


        time.sleep(
            delay
        )

def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            "Télécharge les images proposées "
            "par Poneglyph."
        )
    )


    group = (
        parser
        .add_mutually_exclusive_group(
            required=True
        )
    )


    group.add_argument(
        "--card",
        help="Une seule carte, ex: OP05-119"
    )


    group.add_argument(
        "--prefix",
        help="Toutes les cartes d'un préfixe, ex: OP05"
    )


    group.add_argument(
        "--all",
        action="store_true",
        help="Toutes les cartes locales"
    )


    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limiter le nombre de numéros traités"
    )


    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Pause entre variantes"
    )


    parser.add_argument(
        "--headful",
        action="store_true",
        help="Afficher Chromium"
    )


    parser.add_argument(
        "--force",
        action="store_true",
        help="Retélécharger les images existantes"
    )


    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_args()


    conn = sqlite3.connect(
        DB_PATH
    )


    prepare_database(
        conn
    )


    card_ids = get_card_ids(
        conn,

        card=(
            args.card.upper()
            if args.card
            else None
        ),

        prefix=(
            args.prefix.upper()
            if args.prefix
            else None
        ),

        all_cards=args.all,

        limit=args.limit
    )


    if not card_ids:

        print(
            "Aucune carte à traiter."
        )

        conn.close()
        return


    print()
    print(
        f"{len(card_ids)} numéros "
        f"de cartes à traiter."
    )


    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=not args.headful
        )


        context = browser.new_context(
            accept_downloads=True,

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/140 Safari/537.36"
            )
        )


        page = context.new_page()


        for position, base_id in enumerate(
            card_ids,
            start=1
        ):

            print()
            print(
                f"[{position}/{len(card_ids)}]"
            )


            try:

                process_card(
                    page,
                    context,
                    conn,
                    base_id,
                    args.delay,
                    args.force
                )

            except Exception as exc:

                print(
                    f"ERREUR {base_id}: "
                    f"{exc}"
                )


        browser.close()


    conn.close()


    print()
    print(
        "Terminé."
    )

    print(
        "Images :",
        OUTPUT_DIR.resolve()
    )


if __name__ == "__main__":
    main()