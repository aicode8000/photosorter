#!/bin/sh
# ============================================================
# convert-nef.sh — Extraction JPEG embarqué NEF → JPG
#
# Deux modes :
#
#   Mode QNAP (archive année/mois) :
#     ./convert-nef.sh 2024                → année entière, séquentiel
#     ./convert-nef.sh 2024 03             → mars 2024, séquentiel
#     ./convert-nef.sh 2024 --jobs 4       → année entière, 4 workers
#     ./convert-nef.sh 2024 03 --jobs 4    → mars 2024, 4 workers
#     Arbo   : $MEDIA_ROOT/YYYY/MM/NEF/*.NEF
#     Sortie : $MEDIA_ROOT/YYYY/MM/JPG/*.jpg
#
#   Mode dossier générique (n'importe quel dossier, ex: Mac) :
#     ./convert-nef.sh --dir /chemin/vers/nef
#     ./convert-nef.sh --dir /chemin/vers/nef --out /chemin/vers/jpg --jobs 4
#     Sortie par défaut : <dir>/JPG/*.jpg (créé si absent)
#
# Principe : extrait le JPEG natif Nikon embarqué dans le NEF
# via exiftool -JpgFromRaw (aucune reconversion, qualité maximale)
#
# Skip si DSC_xxxx.jpg OU DSC_xxxx_DxO.jpg existe déjà dans le dossier JPG.
#
# Prérequis :
#   Mac             : brew install exiftool
#   QNAP (Entware)  : opkg install perl-image-exiftool
# ============================================================

MEDIA_ROOT="/share/Media"
JOBS=1
YEAR=""
MONTH=""
MODE="qnap"
SRC_DIR=""
OUT_DIR=""
LOG=""

# ── Arguments ────────────────────────────────────────────────
if [ -z "$1" ]; then
    echo "Usage:"
    echo "  $0 YYYY [MM] [--jobs N]                       (mode QNAP, arbo année/mois)"
    echo "  $0 --dir DOSSIER [--out DOSSIER] [--jobs N]    (mode dossier générique)"
    exit 1
fi

if [ "$1" = "--dir" ]; then
    MODE="dir"
    shift
    SRC_DIR="$1"
    shift
else
    YEAR="$1"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --jobs)
            shift
            JOBS="$1"
            ;;
        --out)
            shift
            OUT_DIR="$1"
            ;;
        --log)
            shift
            LOG="$1"
            ;;
        *)
            [ "$MODE" = "qnap" ] && MONTH="$1"
            ;;
    esac
    shift
done

case "$JOBS" in
    ''|*[!0-9]*)
        echo "[ERREUR] --jobs doit être un entier positif (reçu: '$JOBS')"
        exit 1
        ;;
esac

if [ "$MODE" = "dir" ]; then
    if [ -z "$SRC_DIR" ] || [ ! -d "$SRC_DIR" ]; then
        echo "[ERREUR] Dossier source introuvable : $SRC_DIR"
        exit 3
    fi
    [ -n "$OUT_DIR" ] || OUT_DIR="$SRC_DIR/JPG"
    [ -n "$LOG" ] || LOG="$OUT_DIR/convert-nef.log"
    mkdir -p "$OUT_DIR"
else
    [ -n "$LOG" ] || LOG="/var/log/convert-nef.log"
fi

# ── Vérification outils ──────────────────────────────────────
if ! command -v exiftool >/dev/null 2>&1; then
    echo "[ERREUR] exiftool introuvable."
    echo "  → Mac : brew install exiftool"
    echo "  → QNAP: opkg install perl-image-exiftool"
    exit 2
fi

# ── Liste des mois (mode QNAP uniquement) ────────────────────
if [ "$MODE" = "qnap" ]; then
    if [ -n "$MONTH" ]; then
        MONTH_LIST="$MONTH"
    else
        MONTH_LIST=$(ls -1 "$MEDIA_ROOT/$YEAR/" 2>/dev/null)
    fi

    if [ -z "$MONTH_LIST" ]; then
        echo "[ERREUR] Aucun dossier trouvé dans $MEDIA_ROOT/$YEAR/" | tee -a "$LOG"
        exit 3
    fi
fi

# ── Fichiers temporaires pour compteurs (safe avec xargs -P) ─
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/convert-nef.XXXXXX")
CONV_FILE="$TMP_DIR/converted"
SKIP_FILE="$TMP_DIR/skipped"
ERR_FILE="$TMP_DIR/errors"
touch "$CONV_FILE" "$SKIP_FILE" "$ERR_FILE"

# ── Worker script (invoqué par xargs, sh POSIX pur) ──────────
WRAPPER="$TMP_DIR/worker.sh"
cat > "$WRAPPER" << 'WORKER_EOF'
#!/bin/sh
NEF="$1"
LOG="$2"
CONV_FILE="$3"
SKIP_FILE="$4"
ERR_FILE="$5"
OUT_DIR_ARG="$6"

if [ -n "$OUT_DIR_ARG" ]; then
    JPG_DIR="$OUT_DIR_ARG"
else
    # Mode QNAP : .../YYYY/MM/NEF/x.NEF → JPG_DIR = .../YYYY/MM/JPG
    NEF_DIR=$(dirname "$NEF")
    MM_DIR=$(dirname "$NEF_DIR")
    JPG_DIR="$MM_DIR/JPG"
fi

BASENAME=$(basename "$NEF")
STEM="${BASENAME%.*}"
TARGET="$JPG_DIR/${STEM}.jpg"
TARGET_DXO="$JPG_DIR/${STEM}_DxO.jpg"

mkdir -p "$JPG_DIR"

# Skip si JPG natif déjà présent
if [ -f "$TARGET" ]; then
    echo "[SKIP] $STEM (${STEM}.jpg déjà présent)" | tee -a "$LOG"
    echo 1 >> "$SKIP_FILE"
    exit 0
fi

# Skip si variante DxO présente (traité par DxO PhotoLab)
if [ -f "$TARGET_DXO" ]; then
    echo "[SKIP] $STEM (${STEM}_DxO.jpg présent — traité DxO)" | tee -a "$LOG"
    echo 1 >> "$SKIP_FILE"
    exit 0
fi

echo "[CONV] $STEM → ${STEM}.jpg" | tee -a "$LOG"

# Extraction directe du JPEG natif Nikon embarqué dans le NEF
# -b          : sortie binaire brute sur stdout
# -JpgFromRaw : JPEG full-res produit par le processeur Nikon
# Redirection stdout → fichier cible (pas de delegate ImageMagick)
exiftool -b -JpgFromRaw "$NEF" > "$TARGET" 2>/dev/null

# Vérifier que le fichier est non-vide (extraction réussie)
if [ -s "$TARGET" ]; then
    echo "[OK]   → $TARGET" | tee -a "$LOG"
    echo 1 >> "$CONV_FILE"
else
    echo "[ERR]  Extraction vide ou échouée : $NEF" | tee -a "$LOG"
    echo 1 >> "$ERR_FILE"
    rm -f "$TARGET"
fi
WORKER_EOF
chmod +x "$WRAPPER"

# ── Scan des NEF (via ls glob, find bugué sur ce fs QNAP) ────
NEF_LIST="$TMP_DIR/nef_list.txt"
: > "$NEF_LIST"

echo "============================================" | tee -a "$LOG"
if [ "$MODE" = "dir" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Début — dossier: $SRC_DIR — sortie: $OUT_DIR — jobs: $JOBS" | tee -a "$LOG"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Début — année: $YEAR ${MONTH:+mois: $MONTH} — jobs: $JOBS" | tee -a "$LOG"
fi
echo "============================================" | tee -a "$LOG"

if [ "$MODE" = "dir" ]; then
    echo "[INFO] Scan : $SRC_DIR" | tee -a "$LOG"
    for NEF in "$SRC_DIR"/*.NEF "$SRC_DIR"/*.nef; do
        [ -f "$NEF" ] && echo "$NEF" >> "$NEF_LIST"
    done
else
    for MM in $MONTH_LIST; do
        NEF_DIR="$MEDIA_ROOT/$YEAR/$MM/NEF"
        [ -d "$NEF_DIR" ] || continue
        echo "[INFO] Scan : $YEAR/$MM" | tee -a "$LOG"
        for NEF in "$NEF_DIR"/*.NEF "$NEF_DIR"/*.nef; do
            [ -f "$NEF" ] && echo "$NEF" >> "$NEF_LIST"
        done
    done
fi

TOTAL=$(wc -l < "$NEF_LIST" | tr -d ' ')

if [ "$TOTAL" -eq 0 ]; then
    echo "[INFO] Aucun NEF trouvé." | tee -a "$LOG"
else
    echo "[INFO] $TOTAL NEF trouvés — lancement avec $JOBS worker(s)..." | tee -a "$LOG"
    xargs -I{} -P "$JOBS" \
        "$WRAPPER" "{}" "$LOG" "$CONV_FILE" "$SKIP_FILE" "$ERR_FILE" "$OUT_DIR" < "$NEF_LIST"
fi

# ── Comptage ─────────────────────────────────────────────────
CONVERTED=$(wc -l < "$CONV_FILE" | tr -d ' ')
SKIPPED=$(wc -l   < "$SKIP_FILE" | tr -d ' ')
ERRORS=$(wc -l    < "$ERR_FILE"  | tr -d ' ')

# ── Résumé ───────────────────────────────────────────────────
echo "============================================" | tee -a "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Terminé" | tee -a "$LOG"
echo "  NEF trouvés  : $TOTAL"     | tee -a "$LOG"
echo "  Convertis    : $CONVERTED" | tee -a "$LOG"
echo "  Ignorés (OK) : $SKIPPED"   | tee -a "$LOG"
echo "  Erreurs      : $ERRORS"    | tee -a "$LOG"
echo "============================================" | tee -a "$LOG"

# ── Nettoyage tmp ────────────────────────────────────────────
rm -rf "$TMP_DIR"

[ "$ERRORS" -eq 0 ]
