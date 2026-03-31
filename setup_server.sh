#!/bin/bash
# ============================================================
# APEX Trading Bot – Server Setup
# Ubuntu 24.04 LTS (aarch64)
# ============================================================
# Einmalig ausführen nach git clone auf dem Server:
#   chmod +x setup_server.sh && ./setup_server.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "============================================================"
echo "APEX Server Setup"
echo "Verzeichnis: $REPO_DIR"
echo "============================================================"

# ─── 1. System-Abhängigkeiten ────────────────────────────────
echo ""
echo "[1/5] System-Pakete aktualisieren..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git curl

# ─── 2. Python Virtual Environment ───────────────────────────
echo ""
echo "[2/5] Python Virtual Environment erstellen..."
cd "$REPO_DIR"
python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip -q
pip install requests python-dotenv -q

echo "   ✅ Abhängigkeiten installiert"
deactivate

# ─── 3. Verzeichnisse anlegen ─────────────────────────────────
echo ""
echo "[3/5] Verzeichnisse anlegen..."
mkdir -p "$REPO_DIR/data"
mkdir -p "$REPO_DIR/logs"
mkdir -p "$REPO_DIR/config"
echo "   ✅ data/, logs/, config/ erstellt"

# ─── 4. Config-Dateien prüfen ─────────────────────────────────
echo ""
echo "[4/5] Config-Dateien prüfen..."

if [ ! -f "$REPO_DIR/config/.env.bitget" ]; then
    echo "   ⚠️  ACHTUNG: config/.env.bitget fehlt!"
    echo "   → Erstelle die Datei manuell:"
    echo ""
    echo "   nano $REPO_DIR/config/.env.bitget"
    echo ""
    echo "   Inhalt:"
    echo "   BITGET_API_KEY=dein_key"
    echo "   BITGET_SECRET_KEY=dein_secret"
    echo "   BITGET_PASSPHRASE=deine_passphrase"
else
    echo "   ✅ config/.env.bitget vorhanden"
fi

if [ ! -f "$REPO_DIR/.env.telegram" ]; then
    echo "   ⚠️  ACHTUNG: .env.telegram fehlt!"
    echo "   → Erstelle die Datei manuell:"
    echo ""
    echo "   nano $REPO_DIR/.env.telegram"
    echo ""
    echo "   Inhalt:"
    echo "   TELEGRAM_BOT_TOKEN=dein_token"
    echo "   TELEGRAM_CHAT_ID=deine_chat_id"
else
    echo "   ✅ .env.telegram vorhanden"
fi

# ─── 5. Crontab einrichten ────────────────────────────────────
echo ""
echo "[5/5] Crontab einrichten..."

PYTHON="$REPO_DIR/venv/bin/python3"
LOG="$REPO_DIR/logs"
SCRIPTS="$REPO_DIR/scripts"

# Crontab aus Template installieren (idempotent – ersetzt immer komplett)
crontab "$REPO_DIR/crontab_template.txt"
echo "   ✅ Crontab installiert ($(grep -c '^[^#]' "$REPO_DIR/crontab_template.txt") Jobs)"

echo ""
echo "============================================================"
echo "✅ Setup abgeschlossen!"
echo "============================================================"
echo ""
echo "Nächste Schritte:"
echo ""
echo "  1. Timezone prüfen:"
echo "     timedatectl"
echo "     sudo timedatectl set-timezone Europe/Berlin"
echo ""
echo "  2. API-Keys eintragen:"
echo "     nano $REPO_DIR/config/.env.bitget"
echo "     nano $REPO_DIR/.env.telegram"
echo ""
echo "  3. Bot testen:"
echo "     cd $REPO_DIR"
echo "     source venv/bin/activate"
echo "     python scripts/bitget_client.py"
echo "     python scripts/pre_market.py eu"
echo ""
echo "  4. Live schalten (wenn alles OK):"
echo "     → In config/bot_config.py: DRY_RUN = False setzen"
echo ""
echo "  5. Crontab kontrollieren:"
echo "     crontab -l"
echo ""
echo "  6. Logs beobachten:"
echo "     tail -f $REPO_DIR/logs/eu.log"
echo ""
