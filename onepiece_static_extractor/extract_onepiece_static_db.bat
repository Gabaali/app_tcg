@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   EXTRACTION ONE PIECE - BASE STATIQUE
echo ============================================================
echo.
echo Source : onepiece_tcg.sqlite
echo Cible  : onepiece_cards.sqlite
echo.
echo Les tables joueurs, portefeuille, decks, amis et matchs
echo ne seront PAS copiees.
echo.

py -3 extract_onepiece_static_db.py onepiece_tcg.sqlite onepiece_cards.sqlite --overwrite

echo.
if errorlevel 1 (
    echo ERREUR : extraction echouee.
) else (
    echo Extraction terminee.
)
echo.
pause
