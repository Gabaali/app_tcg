# Import de scans sans SAMPLE

- Scans disponibles telecharges : 908.
- Cartes du simulateur reliees exactement : 709 / 3798 (DON!! incluses dans le total).
- Lignes de cards mises a jour via clean_image_path : 507 / 4844.
- Ces nombres se recoupent ; ne pas les additionner.
- Sauvegarde avant import : `backups\before_clean_images_20260918_093345_707600.sqlite`.

Source : https://poneglyph.one/scans ; API publique https://api.poneglyph.one/v1/cards/batch.
Seules les images explicitement classees `images.scan` ont ete importees.
Les fichiers ont ete valides comme images et plusieurs scans ont ete controles visuellement.
Les correspondances du simulateur utilisent le produit TCGplayer exact, le numero de carte et le card_key stable.
Les suffixes _p1/_p2 ne sont pas assimiles aux indices de variantes de cette API.

| Extension | Scans relies dans le simulateur |
| --- | ---: |
| EB01 | 1 |
| EB03 | 63 |
| OP02 | 2 |
| OP03 | 1 |
| OP04 | 1 |
| OP05 | 3 |
| OP06 | 47 |
| OP07 | 96 |
| OP08 | 84 |
| OP09 | 4 |
| OP10 | 3 |
| OP11 | 32 |
| OP12 | 104 |
| OP13 | 26 |
| OP14 | 93 |
| OP15 | 144 |
| PRB01 | 3 |
| PRB02 | 2 |

Les extensions absentes de ce tableau ne disposent pas de nouvelle correspondance exacte importee.
Les images originales et les 174 images DON!! precedemment associees sont conservees en recours.
Les visuels de stock TCGplayer, Limitless et Poneglyph testes peuvent encore porter SAMPLE.
Les reconstructions communautaires ne sont pas utilisees car certains fichiers portent le numero de base tout en representant une autre illustration.
