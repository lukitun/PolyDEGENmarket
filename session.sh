#!/bin/bash
# Polymarkt automated session check
# Runs monitoring, intel, and snapshots

cd /root/polymarkt

echo "=========================================="
echo "POLYMARKT AUTO-CHECK $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "=========================================="

# Check positions against stop/TP rules
echo ""
echo "[MONITOR]"
python3 monitor.py check 2>&1

# Quick intel (news alerts)
echo ""
echo "[NEWS ALERTS]"
python3 news_monitor.py 2>&1

# Oil + crypto prices
echo ""
echo "[PRICES]"
python3 intel.py commodities 2>&1

# Record equity snapshot
echo ""
echo "[EQUITY]"
python3 equity.py snapshot 2>&1

# Watchlist alerts
echo ""
echo "[WATCHLIST]"
python3 watchlist.py alerts 2>&1

echo ""
echo "=========================================="
echo "Auto-check complete"
echo "=========================================="
