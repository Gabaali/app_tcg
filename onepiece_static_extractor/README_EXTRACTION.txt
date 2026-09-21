EXTRACTION ONE PIECE POUR GITHUB
================================

1. Copie ces deux fichiers dans le même dossier que :
       onepiece_tcg.sqlite

   - extract_onepiece_static_db.py
   - extract_onepiece_static_db.bat

2. Double-clique sur :
       extract_onepiece_static_db.bat

3. Le script crée :
       onepiece_cards.sqlite

4. Vérifie le résumé affiché dans la console.

5. Mets onepiece_cards.sqlite sur GitHub à la place de la base mixte
   onepiece_tcg.sqlite, une fois app.py adapté pour lire les cartes depuis
   onepiece_cards.sqlite.

Le script exclut notamment :
- app_users
- user_collection
- user_wallets
- wallet_transactions
- pack_openings
- opening_cards
- decks / deck_cards
- friend_requests / friendships
- game_invites
- matches / multiplayer_matches
- match_players
- match_board_cards
- trivia_runs / trivia_answers
- coin_generators
- slot_spins

Il copie les autres tables et leurs données, ainsi que leurs index.
Les vues/triggers qui semblent référencer une table utilisateur sont ignorés.

IMPORTANT
---------
Ne supprime pas onepiece_tcg.sqlite tant que tes données utilisateurs n'ont
pas été migrées vers Turso et vérifiées.
