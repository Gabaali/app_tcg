# Images d'affichage

## Dos de carte

- Source : https://github.com/33Shin/optcg-simulator/blob/main/public/assets/imgs/back.webp
- Original : `one_piece_card_back.webp`.
- Version PNG sans changement de dimensions : `one_piece_card_back.png` (644 × 900).
- Utilisation : dos de l'animation d'ouverture uniquement.

## DON!!

Les DON!! de `terminal_cards` n'ont pas de numéro utilisable et sont absentes
de la table `cards`. Elles nécessitent donc un mapping séparé.

`don_images.json` associe leur `card_key` stable à une image locale, sa source
et la page de la carte. Les correspondances proviennent d'une égalité exacte
du nom dans la page d'extension de https://onepiece.app ; les images sont
hébergées par TCGplayer. Aucun rapprochement approximatif entre variantes
n'est effectué. Les identifiants `terminal_id` ne sont pas persistés.

Au téléchargement : 174 images sur 186 DON!!, avec toutes les DON!! OP01 à
OP16 couvertes. Les 12 images refusées par le serveur (EB03 et OP17) figurent
dans `don_images_unavailable.json`. Aucun autre artwork ne les remplace.

Pour actualiser : `python download_display_assets.py` depuis la racine du
projet. Pillow est nécessaire uniquement pour recréer le PNG s'il manque.
Le téléchargement ne modifie pas la base SQLite ni les collections.

Les illustrations et marques appartiennent à leurs titulaires respectifs.
