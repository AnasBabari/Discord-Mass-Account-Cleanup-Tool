from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.models.usernames import (
    CandidatePreferences,
    RequestBudget,
    UsernameProgress,
    UsernameScan,
    UsernameStatus,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.transport.http_transport import RequestsTransport
from discord_cleanup.usernames.candidates import parse_custom_names, preview_candidates
from discord_cleanup.usernames.public_client import PUBLIC_CHECK_URL, PublicUsernameClient
from discord_cleanup.usernames.scanner import UsernameScanner
from discord_cleanup.usernames.store import UsernameScanStore


def _choose_scan(store: UsernameScanStore, account_id: str) -> UsernameScan | None:
    scans = store.list_scans(account_id)
    if not scans:
        print("No saved username scans for this account.")
        return None
    for index, scan in enumerate(scans, 1):
        print(
            f"  [{index}] {scan.created_at} | {scan.mode} | {scan.checked:,}/{scan.total:,} checked | "
            f"{scan.available:,} available | net:{scan.network_checks:,} cache:{scan.cache_hits:,} | {scan.status}"
        )
    value = input("Saved scan number (or q): ").strip()
    if value.isdigit() and 1 <= int(value) <= len(scans):
        return scans[int(value) - 1]
    return None


def _show_results(store: UsernameScanStore, scan: UsernameScan, account_id: str) -> None:
    offset = 0
    total_avail, fresh_avail = store.availability_counts(scan.id, account_id)
    while True:
        results, total = store.results(scan.id, account_id, offset=offset)
        print(
            f"\nAvailable usernames: {total:,} ({fresh_avail:,} fresh, {max(0, total_avail - fresh_avail):,} historical at the listed check time)"
        )
        for result in results:
            prov = f" ({result.provenance_label()})" if (result.is_cached or result.is_stale) else ""
            print(f"  {result.username} | {result.display_status()} | {result.checked_at}{prov}")
        if offset + 100 >= total or input("Enter n for the next page, or Enter to return: ").strip().lower() != "n":
            return
        offset += 100


def _print_diagnostics(store: UsernameScanStore, account_id: str, route: str) -> None:
    summary = store.diagnostics_summary(account_id, route=route)
