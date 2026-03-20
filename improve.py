"""Codebase improvement agent -- audit and report issues.

Scans the trading codebase for bugs, risks, and improvement opportunities.
Run after every session or before pushing to the public repo.

Usage:
    python3 improve.py              # Full audit
    python3 improve.py quick        # Quick check (safety-critical only)
    python3 improve.py reconcile    # Ledger vs on-chain reconciliation
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
GITIGNORE_FILE = os.path.join(BASE_DIR, ".gitignore")
PY_FILES = [f for f in os.listdir(BASE_DIR) if f.endswith(".py") and f != "improve.py"]

# Files/patterns that must NEVER be in git
SENSITIVE_PATTERNS = [
    r"0x[a-fA-F0-9]{64}",  # Private keys
    r"credentials\.json",
    r"gdrive_token",
    r"POLYMARKET_PRIVATE_KEY\s*=\s*0x[a-fA-F0-9]",
]

SENSITIVE_FILES = [
    ".env", "ledger.json", "credentials.json", "gdrive_token.json",
    "gdrive_folder_id.txt", "monitor.log", "monitor_state.json",
    "watchlist.json", "equity_history.json", "news_alerts.json",
]


def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return None


def check_security():
    """Check that no secrets can leak to the public repo."""
    issues = []

    # Check .gitignore exists and covers sensitive files
    if not os.path.exists(GITIGNORE_FILE):
        issues.append("CRITICAL: .gitignore is missing!")
        return issues

    with open(GITIGNORE_FILE) as f:
        gitignore = f.read()

    for sf in SENSITIVE_FILES:
        if sf not in gitignore:
            issues.append(f"CRITICAL: {sf} is not in .gitignore")

    # Check git-tracked files for secrets
    try:
        import subprocess
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=BASE_DIR, text=True
        ).strip().split("\n")

        for filepath in tracked:
            if filepath in SENSITIVE_FILES:
                issues.append(f"CRITICAL: {filepath} is tracked by git -- must be removed!")

            full_path = os.path.join(BASE_DIR, filepath)
            if not os.path.exists(full_path) or not filepath.endswith((".py", ".md", ".txt", ".json", ".example")):
                continue

            with open(full_path) as f:
                try:
                    content = f.read()
                except Exception:
                    continue

            for pattern in SENSITIVE_PATTERNS:
                if re.search(pattern, content):
                    # Allow patterns in .example files and improve.py itself
                    if filepath.endswith(".example") or filepath == "improve.py":
                        continue
                    issues.append(f"CRITICAL: {filepath} may contain secrets (matched: {pattern[:30]}...)")
    except Exception as e:
        issues.append(f"WARNING: Could not check git files: {e}")

    return issues


def check_ledger_integrity():
    """Verify ledger data is consistent."""
    issues = []
    ledger = load_ledger()
    if not ledger:
        issues.append("WARNING: No ledger.json found")
        return issues

    funds = ledger.get("funds", 0)
    initial = ledger.get("initial_deposit", 0)
    pnl = ledger.get("pnl_total", 0)
    open_bets = ledger.get("open_bets", [])
    closed_bets = ledger.get("closed_bets", [])

    # Float precision check
    funds_str = str(funds)
    if len(funds_str) > 10 and "." in funds_str and len(funds_str.split(".")[1]) > 4:
        issues.append(f"WARNING: Float precision drift in funds: {funds} -- should be rounded")

    # Negative funds check
    if funds < -0.001:
        issues.append(f"CRITICAL: Negative funds: ${funds:.6f} -- ledger is corrupted")

    # Negative or zero size check on open bets
    for bet in open_bets:
        size = bet.get("size", 0)
        if size <= 0:
            issues.append(f"CRITICAL: Bet #{bet.get('id')} has non-positive size ({size}) -- should be closed")
        cost = bet.get("cost", 0)
        if cost < 0:
            issues.append(f"CRITICAL: Bet #{bet.get('id')} has negative cost (${cost}) -- ledger corrupted")

    # Position sizing check (20% rule)
    for bet in open_bets:
        cost = bet.get("cost", 0)
        pct = (cost / initial * 100) if initial > 0 else 0
        if pct > 22:  # 2% tolerance for price movement
            issues.append(f"RISK: Bet #{bet.get('id')} '{bet.get('market', '')[:40]}' is {pct:.1f}% of initial deposit (limit: 20%)")

    # Check for bets without token_id (can't monitor or sell)
    for bet in open_bets:
        if not bet.get("token_id"):
            issues.append(f"WARNING: Bet #{bet.get('id')} has no token_id -- cannot monitor or sell")

    # Check for bets without rules (no stop-loss protection)
    for bet in open_bets:
        if not bet.get("rules"):
            issues.append(f"WARNING: Bet #{bet.get('id')} '{bet.get('market', '')[:40]}' has no monitor rules set")

    # Check for stale positions (no activity in 30+ days)
    now = datetime.now(timezone.utc)
    for bet in open_bets:
        ts = bet.get("timestamp", "")
        if ts:
            try:
                bet_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_days = (now - bet_time).days
                if age_days > 30:
                    issues.append(f"INFO: Bet #{bet.get('id')} '{bet.get('market', '')[:40]}' is {age_days} days old -- review if still valid")
            except (ValueError, TypeError):
                pass

    # Check closed bets have PnL recorded
    for bet in closed_bets:
        if bet.get("pnl") is None and bet.get("status") in ("CLOSED", "WON", "LOST"):
            # Check if PnL exists in trade history
            has_trade_pnl = any(
                t.get("original_bet_id") == bet.get("id") and t.get("pnl") is not None
                for t in ledger.get("trades", [])
                if t.get("action") in ("SELL", "RESOLUTION")
            )
            if not has_trade_pnl:
                issues.append(f"WARNING: Closed bet #{bet.get('id')} '{bet.get('market', '')[:40]}' has no PnL recorded anywhere")

    # Duplicate market detection (multiple bets on same token)
    token_bets = {}
    for bet in open_bets:
        tid = bet.get("token_id", "")
        if tid:
            token_bets.setdefault(tid, []).append(bet)
    for tid, bets in token_bets.items():
        if len(bets) > 1:
            total_cost = sum(b.get("cost", 0) for b in bets)
            ids = [b.get("id") for b in bets]
            pct = (total_cost / initial * 100) if initial > 0 else 0
            issues.append(
                f"INFO: {len(bets)} bets on same market (IDs: {ids}), "
                f"combined cost ${total_cost:.2f} ({pct:.1f}% of deposit)"
            )

    # Combined position sizing check (all bets on same token must stay under 20%)
    for tid, bets in token_bets.items():
        if len(bets) > 1:
            total_cost = sum(b.get("cost", 0) for b in bets)
            pct = (total_cost / initial * 100) if initial > 0 else 0
            if pct > 22:
                ids = [b.get("id") for b in bets]
                issues.append(
                    f"RISK: Combined position on same market (IDs: {ids}) is {pct:.1f}% "
                    f"of initial deposit (limit: 20%)"
                )

    return issues


def _is_inside_main_guard(lines, line_idx):
    """Check if a line is inside an 'if __name__ == "__main__"' block."""
    # Walk backwards looking for the __main__ guard
    for j in range(line_idx - 1, max(0, line_idx - 200), -1):
        stripped = lines[j].strip()
        if re.match(r'if\s+__name__\s*==\s*["\']__main__["\']', stripped):
            return True
        # If we hit a top-level def/class before finding __main__, we're not in it
        if re.match(r'^(def |class )', lines[j]) and not lines[j].startswith(' '):
            return False
    return False


def check_code_quality():
    """Scan Python files for common issues."""
    issues = []

    for pyfile in PY_FILES:
        filepath = os.path.join(BASE_DIR, pyfile)
        if not os.path.exists(filepath):
            continue

        with open(filepath) as f:
            lines = f.readlines()

        for i, line in enumerate(lines, 1):
            # Bare except
            if re.match(r'\s*except\s*:', line):
                issues.append(f"CODE: {pyfile}:{i} -- bare 'except:' hides errors, use 'except Exception:'")

            # Hardcoded API keys (not in .env)
            if re.search(r'api_key\s*=\s*["\'][a-zA-Z0-9]{10,}', line, re.IGNORECASE):
                issues.append(f"CODE: {pyfile}:{i} -- hardcoded API key, move to .env")

            # httpx requests without timeout (check next 10 lines for multiline calls)
            if re.search(r'httpx\.(get|post|put|delete)\(', line):
                snippet = "".join(lines[i-1:min(i+10, len(lines))])
                if 'timeout' not in snippet:
                    issues.append(f"CODE: {pyfile}:{i} -- HTTP request without timeout, can hang forever")

            # requests library without timeout
            if re.search(r'requests\.(get|post|put|delete)\(', line):
                snippet = "".join(lines[i-1:min(i+10, len(lines))])
                if 'timeout' not in snippet:
                    issues.append(f"CODE: {pyfile}:{i} -- HTTP request without timeout, can hang forever")

            # Print statements that actually interpolate the KEY variable
            # (not just mentioning the word KEY in a user-facing error message)
            if re.search(r'print\(.*\bKEY\b', line) and not re.search(r'print\(["\']', line):
                # This catches f-strings or concatenation that include the KEY variable
                issues.append(f"CODE: {pyfile}:{i} -- possible private key exposure in print statement")

            # Unreachable code: return after except block that also returns at SAME indent
            # Pattern: except block ends with return, then a return follows at same indent level
            if re.match(r'\s+return\b', line) and i >= 3:
                current_indent = len(line) - len(line.lstrip())
                prev_lines = [l.rstrip() for l in lines[max(0, i-4):i-1]]
                for j, pl in enumerate(reversed(prev_lines)):
                    if pl.strip() == '':
                        continue
                    prev_indent = len(pl) - len(pl.lstrip())
                    if re.match(r'\s+return\b', pl) and prev_indent == current_indent:
                        # Both returns at same indent -- check if prev is in an except block
                        for k in range(j+1, len(prev_lines)):
                            pk = prev_lines[len(prev_lines)-1-k].strip()
                            if pk == '':
                                continue
                            if re.match(r'except\b', pk):
                                issues.append(
                                    f"CODE: {pyfile}:{i} -- possibly unreachable return after except block that returns"
                                )
                            break
                    break

            # time.mktime used on UTC timestamps (should be calendar.timegm)
            if 'time.mktime' in line and 'published' in line.lower():
                issues.append(
                    f"CODE: {pyfile}:{i} -- time.mktime() uses local timezone; "
                    f"use calendar.timegm() for UTC timestamps from RSS feeds"
                )

            # Unguarded sys.argv access (IndexError risk)
            argv_match = re.search(r'sys\.argv\[(\d+)\]', line)
            if argv_match and _is_inside_main_guard(lines, i - 1):
                idx = int(argv_match.group(1))
                if idx >= 2:
                    # Check if there's a length guard in the preceding 15 lines
                    preceding = "".join(lines[max(0, i-16):i-1])
                    if f'len(sys.argv)' not in preceding and f'sys.argv) <' not in preceding:
                        issues.append(
                            f"CODE: {pyfile}:{i} -- sys.argv[{idx}] accessed without bounds check (IndexError risk)"
                        )

    return issues


def check_reconciliation():
    """Check ledger vs on-chain positions and USDC balance."""
    issues = []
    ledger = load_ledger()
    if not ledger:
        return ["Cannot reconcile: no ledger.json"]

    # Check that all open bets have required fields
    for bet in ledger.get("open_bets", []):
        required = ["id", "market", "side", "price", "size", "cost", "token_id"]
        missing = [f for f in required if not bet.get(f)]
        if missing:
            issues.append(f"DATA: Bet #{bet.get('id', '?')} missing fields: {missing}")

    # Try to check on-chain USDC balance
    try:
        from positions import get_balance
        on_chain = get_balance()
        ledger_funds = ledger.get("funds", 0)
        diff = round(on_chain - ledger_funds, 2)
        if abs(diff) > 0.50:
            issues.append(
                f"RISK: Ledger funds ${ledger_funds:.2f} vs on-chain USDC ${on_chain:.2f} "
                f"(diff: ${diff:+.2f}) -- run 'ledger.py sync'"
            )
        elif abs(diff) > 0.05:
            issues.append(
                f"WARNING: Ledger funds ${ledger_funds:.2f} vs on-chain USDC ${on_chain:.2f} "
                f"(diff: ${diff:+.2f})"
            )
        else:
            issues.append(f"INFO: Ledger/on-chain USDC in sync (${on_chain:.2f})")
    except Exception as e:
        issues.append(f"INFO: Could not check on-chain balance: {e}")

    # Try to check on-chain positions
    try:
        import httpx
        from positions import get_address, DATA_API
        address = get_address()
        resp = httpx.get(
            f"{DATA_API}/positions",
            params={"user": address},
            timeout=15,
        )
        resp.raise_for_status()
        on_chain_positions = resp.json()

        ledger_tokens = set(
            b.get("token_id", "") for b in ledger.get("open_bets", [])
            if b.get("token_id")
        )

        on_chain_tokens = set()
        for p in on_chain_positions:
            asset = p.get("asset", "")
            size = float(p.get("size", 0))
            if size > 0.01 and asset:
                on_chain_tokens.add(asset)

        # Positions in ledger but not on-chain
        ledger_only = ledger_tokens - on_chain_tokens
        for tid in ledger_only:
            bet_names = [
                b.get("market", "?")[:40]
                for b in ledger.get("open_bets", [])
                if b.get("token_id") == tid
            ]
            issues.append(
                f"WARNING: Ledger has position not found on-chain: {', '.join(bet_names)} "
                f"(token: {tid[:16]}...)"
            )

        # Positions on-chain but not in ledger
        chain_only = on_chain_tokens - ledger_tokens
        for tid in chain_only:
            matching = [p for p in on_chain_positions if p.get("asset") == tid]
            for p in matching:
                title = p.get("title", tid[:20])
                size = p.get("size", "?")
                issues.append(
                    f"WARNING: On-chain position not in ledger: {title} (size: {size})"
                )

        if not ledger_only and not chain_only:
            issues.append(f"INFO: Ledger positions match on-chain ({len(ledger_tokens)} positions)")

    except Exception as e:
        issues.append(f"INFO: Could not reconcile positions: {e}")

    return issues


def check_plays():
    """Verify all open bets have corresponding play files."""
    issues = []
    ledger = load_ledger()
    if not ledger:
        return issues

    plays_dir = os.path.join(BASE_DIR, "plays")
    if not os.path.exists(plays_dir):
        issues.append("WARNING: plays/ directory missing")
        return issues

    play_files = [f for f in os.listdir(plays_dir) if f.endswith(".md") and f != "example_play.md"]

    open_markets = set()
    for bet in ledger.get("open_bets", []):
        market = bet.get("market", "")
        open_markets.add(market)

    if not play_files and open_markets:
        issues.append(f"WARNING: {len(open_markets)} open bets but no play files in plays/")

    return issues


def check_proxy_health():
    """Check if the configured proxy is reachable."""
    issues = []
    env_file = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_file):
        return issues

    with open(env_file) as f:
        env_content = f.read()

    # Check if a proxy is configured
    import re as _re
    proxy_match = _re.search(r'^SOCKS_PROXY\s*=\s*(.+)$', env_content, _re.MULTILINE)
    if not proxy_match:
        return issues

    proxy_url = proxy_match.group(1).strip()
    if not proxy_url or proxy_url.startswith("#"):
        return issues

    # Try to test the proxy
    try:
        from proxy_client import check_proxy_health as _check_proxy, PROXY_LIST
        ok, latency = _check_proxy(proxy_url, timeout=8)
        if ok:
            issues.append(f"INFO: Proxy healthy ({proxy_url}, {latency:.0f}ms)")
        else:
            issues.append(f"RISK: Proxy unreachable ({proxy_url}) -- trades will fail!")
            if len(PROXY_LIST) > 1:
                issues.append(f"INFO: {len(PROXY_LIST) - 1} fallback proxies configured")
            else:
                issues.append(f"WARNING: No fallback proxies. Set SOCKS_PROXY_LIST in .env")
    except Exception as e:
        issues.append(f"WARNING: Could not test proxy: {e}")

    return issues


def check_cron_health():
    """Check cron jobs are correctly configured."""
    issues = []
    try:
        import subprocess
        crontab = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)

        # Check that all polymarkt cron entries use cd /root/polymarkt or absolute paths
        for line in crontab.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Only check polymarkt-related entries
            if "polymarkt" not in line.lower() and "equity" not in line.lower():
                continue
            # Check if it uses a bare python3 command without cd
            if "python3 " in line and "cd /root/polymarkt" not in line and "/root/polymarkt/" not in line:
                issues.append(f"WARNING: Cron entry may not find files (no cd or absolute path): {line[:80]}")
    except Exception:
        pass  # No crontab or crontab command not available

    return issues


def check_data_files():
    """Check health of data files (watchlist, equity history)."""
    issues = []

    # Check watchlist
    wl_file = os.path.join(BASE_DIR, "watchlist.json")
    if os.path.exists(wl_file):
        try:
            with open(wl_file) as f:
                wl = json.load(f)
            markets = wl.get("markets", [])
            snapshots = wl.get("snapshots", [])
            if markets and not snapshots:
                issues.append("INFO: Watchlist has markets but no snapshots -- run 'watchlist.py snapshot'")
        except json.JSONDecodeError:
            issues.append("WARNING: watchlist.json is corrupted")

    # Check equity history
    eq_file = os.path.join(BASE_DIR, "equity_history.json")
    if os.path.exists(eq_file):
        try:
            with open(eq_file) as f:
                eq = json.load(f)
            snapshots = eq.get("snapshots", [])
            if snapshots:
                latest = snapshots[-1]
                ts = latest.get("timestamp", "")
                try:
                    snap_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - snap_time).total_seconds() / 3600
                    if age_hours > 24:
                        issues.append(f"INFO: Equity snapshot is {age_hours:.0f}h old -- run 'equity.py snapshot'")
                except (ValueError, TypeError):
                    pass
        except json.JSONDecodeError:
            issues.append("WARNING: equity_history.json is corrupted")

    return issues


def run_audit(mode="full"):
    """Run full codebase audit."""
    print("=" * 60)
    print(f"CODEBASE AUDIT -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    all_issues = []
    sections = [
        ("SECURITY", check_security),
        ("LEDGER INTEGRITY", check_ledger_integrity),
        ("PLAY FILES", check_plays),
    ]

    if mode == "full":
        sections.append(("CODE QUALITY", check_code_quality))
        sections.append(("PROXY HEALTH", check_proxy_health))
        sections.append(("CRON HEALTH", check_cron_health))
        sections.append(("DATA FILES", check_data_files))

    if mode == "reconcile":
        sections = [("RECONCILIATION", check_reconciliation)]

    for name, check_fn in sections:
        print(f"\n[{name}]")
        issues = check_fn()
        if issues:
            for issue in issues:
                severity = issue.split(":")[0]
                print(f"  {issue}")
            all_issues.extend(issues)
        else:
            print("  All clear.")

    # Summary
    critical = sum(1 for i in all_issues if i.startswith("CRITICAL"))
    risks = sum(1 for i in all_issues if i.startswith("RISK"))
    warnings = sum(1 for i in all_issues if i.startswith("WARNING"))
    info = sum(1 for i in all_issues if i.startswith("INFO"))
    code = sum(1 for i in all_issues if i.startswith("CODE"))
    data = sum(1 for i in all_issues if i.startswith("DATA"))

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {critical} critical, {risks} risk, {warnings} warnings, {info} info, {code} code, {data} data")

    if critical > 0:
        print("*** FIX CRITICAL ISSUES BEFORE PUSHING TO PUBLIC REPO ***")
    elif risks > 0:
        print("Review risk items before next trade.")
    else:
        print("Safe to push. Safe to trade.")
    print("=" * 60)

    return all_issues


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode not in ("full", "quick", "reconcile"):
        print("Usage: python3 improve.py [full|quick|reconcile]")
        sys.exit(1)
    issues = run_audit(mode)
    sys.exit(1 if any(i.startswith("CRITICAL") for i in issues) else 0)
