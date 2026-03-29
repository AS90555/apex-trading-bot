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

# Aktuelle Crontab sichern
crontab -l > /tmp/apex_crontab_backup.txt 2>/dev/null || true
echo "   Backup gesichert: /tmp/apex_crontab_backup.txt"

# APEX-Block aus bestehender Crontab entfernen (falls vorhanden)
grep -v "APEX" /tmp/apex_crontab_backup.txt > /tmp/apex_crontab_clean.txt 2>/dev/null || true

# APEX Cron-Jobs hinzufügen
# WICHTIG: Alle Zeiten in Europe/Berlin Timezone
# Server muss auf Berlin-Zeit laufen ODER Zeiten entsprechend anpassen!

cat >> /tmp/apex_crontab_clean.txt << EOF

# ============================================================
# APEX Trading Bot – Cron Jobs
# Alle Zeiten: Europe/Berlin
# Server-Timezone prüfen: timedatectl
# ============================================================

# ─── TOKYO SESSION (Mo-Fr 02:00–03:30 Berlin) ────────────────
0  2 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/pre_market.py tokyo >> $LOG/tokyo.log 2>&1
15 2 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/save_opening_range.py >> $LOG/tokyo.log 2>&1
30 2 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/tokyo.log 2>&1
45 2 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/tokyo.log 2>&1
0  3 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/tokyo.log 2>&1
30 3 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/session_summary.py tokyo >> $LOG/tokyo.log 2>&1

# ─── EU SESSION (Mo-Fr 08:30–10:30 Berlin) ───────────────────
30 8 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/pre_market.py eu >> $LOG/eu.log 2>&1
0  9 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/save_opening_range.py >> $LOG/eu.log 2>&1
15 9 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/eu.log 2>&1
30 9 * * 1-5  cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/eu.log 2>&1
0  10 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/eu.log 2>&1
30 10 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/session_summary.py eu >> $LOG/eu.log 2>&1

# ─── USA SESSION (Mo-Fr 21:00–23:00 Berlin) ──────────────────
0  21 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/pre_market.py us >> $LOG/us.log 2>&1
30 21 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/save_opening_range.py >> $LOG/us.log 2>&1
45 21 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/us.log 2>&1
0  22 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/us.log 2>&1
15 22 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/us.log 2>&1
45 22 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/autonomous_trade.py >> $LOG/us.log 2>&1
0  23 * * 1-5 cd $REPO_DIR && $PYTHON $SCRIPTS/daily_closeout.py >> $LOG/daily.log 2>&1

# ─── POSITION MONITOR (alle 30 Min, Mo-So) ───────────────────
*/30 * * * *  cd $REPO_DIR && $PYTHON $SCRIPTS/position_monitor.py >> $LOG/monitor.log 2>&1

# ─── WEEKEND MOMO (AVAX) ─────────────────────────────────────
0  23 * * 5   cd $REPO_DIR && $PYTHON $SCRIPTS/weekend_momo.py --check >> $LOG/weekend.log 2>&1
5  0  * * 6   cd $REPO_DIR && $PYTHON $SCRIPTS/weekend_momo.py --entry >> $LOG/weekend.log 2>&1
0  21 * * 0   cd $REPO_DIR && $PYTHON $SCRIPTS/weekend_momo.py --exit  >> $LOG/weekend.log 2>&1

# ─── LOG ROTATION (täglich 04:00) ────────────────────────────
0  4  * * *   find $LOG -name "*.log" -size +5M -exec truncate -s 1M {} \;

EOF

crontab /tmp/apex_crontab_clean.txt
echo "   ✅ Crontab eingerichtet ($(grep -c 'APEX\|scripts' /tmp/apex_crontab_clean.txt) Zeilen)"

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
