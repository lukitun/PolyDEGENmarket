"""Alert system -- write critical alerts to a log file.

Alerts are written to logs/alerts.log with timestamps and severity levels.
Other modules call alert() to record events like:
- Stop loss triggered
- Take profit hit
- Position resolved
- Proxy failure
- Price threshold crossed

Usage:
    python3 alerts.py                  # Show recent alerts
    python3 alerts.py tail [N]         # Show last N alerts (default: 20)
    python3 alerts.py clear            # Clear alert log
    python3 alerts.py test             # Write a test alert
"""
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
ALERT_FILE = os.path.join(LOGS_DIR, "alerts.log")

# Severity levels
CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"
TRADE = "TRADE"


def alert(message, severity=INFO, source="system"):
    """Write an alert to the log file.

    Args:
        message: Alert message text
        severity: One of CRITICAL, WARNING, INFO, TRADE
        source: Which module generated the alert (e.g., "monitor", "proxy", "execute")
    """
    os.makedirs(LOGS_DIR, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] [{severity}] [{source}] {message}\n"

    with open(ALERT_FILE, "a") as f:
        f.write(line)

    # Also print to stdout for immediate visibility
    if severity == CRITICAL:
        print(f"*** ALERT: {message} ***")


def get_recent(n=20):
    """Get the last N alerts."""
    if not os.path.exists(ALERT_FILE):
        return []

    with open(ALERT_FILE) as f:
        lines = f.readlines()

    return lines[-n:]


def show_alerts(n=20):
    """Print recent alerts."""
    alerts = get_recent(n)
    if not alerts:
        print("No alerts. All quiet.")
        return

    print(f"Last {min(n, len(alerts))} alerts (from {ALERT_FILE}):")
    print("-" * 70)
    for line in alerts:
        print(line.rstrip())
    print("-" * 70)
    print(f"Total alerts in log: {_count_alerts()}")


def _count_alerts():
    """Count total lines in alert file."""
    if not os.path.exists(ALERT_FILE):
        return 0
    with open(ALERT_FILE) as f:
        return sum(1 for _ in f)


def clear_alerts():
    """Clear the alert log."""
    if os.path.exists(ALERT_FILE):
        os.remove(ALERT_FILE)
    print("Alert log cleared.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tail"

    if cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_alerts(n)
    elif cmd == "clear":
        clear_alerts()
    elif cmd == "test":
        alert("Test alert -- system is working", severity=INFO, source="test")
        print("Test alert written to log.")
    else:
        print("Usage:")
        print("  python3 alerts.py                  # Show recent alerts")
        print("  python3 alerts.py tail [N]          # Show last N alerts")
        print("  python3 alerts.py clear             # Clear alert log")
        print("  python3 alerts.py test              # Write a test alert")
