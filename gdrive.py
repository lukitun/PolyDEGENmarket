"""Sync portfolio, plays, and strategies to Google Drive/Docs."""
import os
import json
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/docs",
]
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "gdrive_token.json")
CREDS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
FOLDER_ID_FILE = os.path.join(os.path.dirname(__file__), "gdrive_folder_id.txt")


def get_service(api="drive", version="v3"):
    """Get authenticated Google API service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build(api, version, credentials=creds)


def get_folder_id():
    """Get or create the Polymarkt folder in Google Drive."""
    if os.path.exists(FOLDER_ID_FILE):
        with open(FOLDER_ID_FILE) as f:
            return f.read().strip()

    service = get_service("drive", "v3")
    # Create folder
    metadata = {
        "name": "Polymarkt Trading Desk",
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder.get("id")

    with open(FOLDER_ID_FILE, "w") as f:
        f.write(folder_id)

    print(f"Created Google Drive folder: {folder_id}")
    return folder_id


def find_doc(service, name, folder_id):
    """Find a Google Doc by name in our folder."""
    query = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def create_or_update_doc(name, content):
    """Create or update a Google Doc (uploaded as plain text, converted to Docs)."""
    from googleapiclient.http import MediaInMemoryUpload
    drive = get_service("drive", "v3")
    folder_id = get_folder_id()

    doc_id = find_doc(drive, name, folder_id)
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")

    if doc_id:
        # Update existing file content
        drive.files().update(
            fileId=doc_id,
            media_body=media,
        ).execute()
        print(f"Updated: {name}")
    else:
        # Create new Google Doc from plain text
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [folder_id],
        }
        doc = drive.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()
        doc_id = doc.get("id")
        print(f"Created: {name} ({doc_id})")

    return doc_id


def generate_portfolio_report():
    """Generate a text report from ledger data."""
    from ledger import _load
    ledger = _load()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append(f"POLYMARKT PORTFOLIO — {now}")
    lines.append("=" * 50)
    lines.append(f"Initial Deposit: ${ledger['initial_deposit']:.2f}")
    lines.append(f"Current Funds:   ${ledger['funds']:.2f}")
    lines.append(f"Total PnL:       ${ledger['pnl_total']:+.2f}")
    lines.append(f"Max Single Bet:  ${ledger['funds'] * 0.20:.2f} (20%)")

    open_cost = sum(b["cost"] for b in ledger["open_bets"])
    lines.append(f"In Open Bets:    ${open_cost:.2f}")
    lines.append(f"Total Value:     ${ledger['funds'] + open_cost:.2f}")
    lines.append("")

    if ledger["open_bets"]:
        lines.append(f"OPEN POSITIONS ({len(ledger['open_bets'])})")
        lines.append("-" * 50)

        # Try to get live prices
        live_prices = {}
        try:
            from monitor import get_midpoint
            for b in ledger["open_bets"]:
                if b.get("token_id"):
                    mid = get_midpoint(b["token_id"])
                    if mid:
                        live_prices[b["id"]] = mid
        except:
            pass

        total_unrealized = 0
        for b in ledger["open_bets"]:
            lines.append(f"#{b['id']} {b['market']}")
            entry = b['price']
            size = b['size']
            cost = b['cost']
            current = live_prices.get(b['id'], 0)
            rules = b.get("rules", {})

            if current:
                value = current * size
                pnl = value - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                total_unrealized += pnl
                emoji = "UP" if pnl >= 0 else "DOWN"
                lines.append(f"   {b['side']} @ {entry} x {size} = ${cost:.2f}")
                lines.append(f"   NOW: ${current:.4f}  |  Value: ${value:.2f}  |  PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) {emoji}")
                if rules:
                    lines.append(f"   Stop: {rules.get('stop_loss', '?')}  |  TP1: {rules.get('take_profit_1', '?')}  |  TP2: {rules.get('take_profit_2', '?')}")
            else:
                lines.append(f"   {b['side']} @ {entry} x {size} = ${cost:.2f}")

            lines.append(f"   Date: {b['timestamp'][:10]}")
            if b.get("notes"):
                lines.append(f"   Notes: {b['notes']}")
            lines.append("")

        if total_unrealized != 0:
            lines.append(f"Unrealized PnL: ${total_unrealized:+.2f}")
            lines.append(f"Portfolio + Unrealized: ${ledger['funds'] + open_cost + total_unrealized:.2f}")
            lines.append("")

    if ledger["closed_bets"]:
        lines.append(f"CLOSED POSITIONS ({len(ledger['closed_bets'])})")
        lines.append("-" * 50)
        for b in ledger["closed_bets"][-10:]:
            pnl = b.get("pnl", 0)
            lines.append(f"#{b['id']} {b['market']} -> {b['status']} (${pnl:+.2f})")
        lines.append("")

    wins = sum(1 for b in ledger["closed_bets"] if b.get("pnl", 0) > 0)
    total = len(ledger["closed_bets"])
    if total > 0:
        lines.append(f"Win Rate: {wins}/{total} ({wins/total*100:.0f}%)")

    return "\n".join(lines)


def generate_plays_report():
    """Compile all play plans into one document."""
    plays_dir = os.path.join(os.path.dirname(__file__), "plays")
    lines = []
    lines.append(f"ACTIVE PLAY PLANS — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 50)
    lines.append("")

    if os.path.exists(plays_dir):
        for fname in sorted(os.listdir(plays_dir)):
            if fname.endswith(".md"):
                fpath = os.path.join(plays_dir, fname)
                with open(fpath) as f:
                    content = f.read()
                lines.append(content)
                lines.append("\n" + "=" * 50 + "\n")

    return "\n".join(lines)


def generate_strategies_report():
    """Read strategies file."""
    strat_file = os.path.join(os.path.dirname(__file__), "STRATEGIES.md")
    if os.path.exists(strat_file):
        with open(strat_file) as f:
            return f.read()
    return "No strategies file found."


def generate_intel_report():
    """Generate intel report by running intel.py full."""
    import subprocess

    result = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "intel.py"), "full"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout if result.stdout else "Intel report unavailable."


def sync_all():
    """Sync everything to Google Drive."""
    print("Syncing to Google Drive...")
    print()

    # 1. Portfolio status
    report = generate_portfolio_report()
    create_or_update_doc("Polymarkt — Portfolio Status", report)

    # 2. Play plans
    plays = generate_plays_report()
    create_or_update_doc("Polymarkt — Play Plans", plays)

    # 3. Strategies
    strategies = generate_strategies_report()
    create_or_update_doc("Polymarkt — Strategies", strategies)

    # 4. Rules
    rules_file = os.path.join(os.path.dirname(__file__), "CLAUDE.md")
    if os.path.exists(rules_file):
        with open(rules_file) as f:
            rules = f.read()
        create_or_update_doc("Polymarkt — Session Rules", rules)

    # 5. Intel report
    print("Generating intel report...")
    intel = generate_intel_report()
    create_or_update_doc("Polymarkt — Intel Report", intel)

    print("\nSync complete!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        # Just authenticate
        get_service()
        get_folder_id()
        print("Authenticated and folder ready.")
    else:
        sync_all()
