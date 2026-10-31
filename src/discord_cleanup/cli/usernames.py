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
    rate_str = (
        f"{summary.effective_checks_per_minute:.2f}/min"
        if summary.effective_checks_per_minute is not None
        else "insufficient data"
    )
    print("\n--- Username Checker Diagnostics ---")
    print(f"  Scope / Source:          {account_id}")
    print(f"  Provider Health:         {summary.provider_health.state.label} ({summary.provider_health.reason or 'OK'})")
    print(f"  Network Requests Total:  {summary.network_requests_total:,}")
    print(f"  Definitive Net Checks:   {summary.definitive_network_checks:,}")
    print(f"  Cache Hits Reused:       {summary.total_cache_hits:,}")
    print(f"  Deduplicated Avoided:    {summary.total_deduplicated:,}")
    print(f"  Local Invalid Rejected:  {summary.total_local_invalid:,}")
    print(f"  Cumulative Wait Time:    {summary.total_wait_seconds:.1f}s")
    print(f"  Recent 429 Cooldowns:    {summary.recent_rate_limit_count}")
    print(f"  Effective Network Rate:  {rate_str}")


def build_username_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discord-cleanup --usernames",
        description="Public Discord 3- and 4-character username availability checker (no login required by default).",
    )
    parser.add_argument(
        "--usernames",
        action="store_true",
        help="Run the public username checker mode.",
    )
    parser.add_argument(
        "--check",
        "--names",
        dest="names",
        default="",
        help="Comma- or space-separated usernames to check non-interactively.",
    )
    parser.add_argument(
        "--input-file",
        dest="input_file",
        default="",
        help="Path to a text file containing candidate usernames.",
    )
    parser.add_argument(
        "--lengths",
        dest="lengths",
        choices=("3", "4", "both"),
        default="",
        help="Run generated candidate search for length 3, 4, or both.",
    )
    parser.add_argument(
        "--strategy",
        choices=("exhaustive", "randomized", "priority", "watchlist"),
        default="exhaustive",
        help="Candidate ordering strategy (default: exhaustive).",
    )
    parser.add_argument(
        "--charset",
        choices=("all", "letters", "alphanumeric", "no_punctuation"),
        default="all",
        help="Character set filter for generated candidates.",
    )
    parser.add_argument(
        "--pattern",
        default="any",
        help="Pattern preference (any, pronounceable, starts_with_letter, no_edge_punctuation, palindrome, or template e.g. CVC, LLL).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic seed for randomized traversal (default: 42).",
    )
    parser.add_argument(
        "--max-network-requests",
        type=int,
        default=None,
        help="Stop cleanly after this many live network requests.",
    )
    parser.add_argument(
        "--max-definitive-checks",
        type=int,
        default=None,
        help="Stop cleanly after this many definitive availability results.",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=None,
        help="Stop cleanly after this many seconds of active run time.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable reusing cached observations for this run.",
    )
    parser.add_argument(
        "--force-recheck",
        action="store_true",
        help="Force fresh live network checks even if cached observations exist.",
    )
    parser.add_argument(
        "--export",
        dest="export_path",
        default="",
        help="Export scan results to the specified CSV path after checking.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview candidate count and sample ordering without sending network requests.",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Print structured diagnostics and provider health summary and exit.",
    )
    parser.add_argument(
        "--reset-health",
        action="store_true",
        help="Reset circuit-breaker pause state while preserving active server cooldowns.",
    )
    parser.add_argument(
        "--store-path",
        dest="store_path",
        default="",
        help="Optional custom SQLite database path.",
    )
    return parser


def run_username_cli_args(
    argv: Sequence[str],
    *,
    store_path: str | Path | None = None,
    client: DiscordApiClient | PublicUsernameClient | None = None,
) -> int:
    """Execute non-interactive username CLI workflow when flags are supplied beyond `--usernames`."""
    parser = build_username_arg_parser()
    args = parser.parse_args(list(argv))
    resolved_store_path = args.store_path or store_path
    transport = RequestsTransport()
    owned_client: DiscordApiClient | PublicUsernameClient | None = None
    try:
        store = UsernameScanStore(resolved_store_path)
        active_client = client
        if active_client is None:
            owned_client = PublicUsernameClient(store, transport=transport)
            active_client = owned_client
        scanner = UsernameScanner(store, active_client)
        account_id = scanner.identify()
        store.recover_interrupted(account_id)
        route = PUBLIC_CHECK_URL if isinstance(active_client, PublicUsernameClient) else "/users/@me/pomelo-attempt"

        if args.reset_health:
            health = store.reset_provider_health(account_id, route=route)
            print(f"Provider health state: {health.state.label}")
            if not (args.names or args.input_file or args.lengths or args.diagnostics or args.preview):
                return 0

        if args.diagnostics:
            _print_diagnostics(store, account_id, route)
            return 0

        lengths: tuple[int, ...] = (int(args.lengths),) if args.lengths in ("3", "4") else (3, 4)
        prefs = CandidatePreferences(
            strategy=args.strategy if args.strategy != "watchlist" else "exhaustive",
            charset_mode=args.charset,
            pattern=args.pattern,
            seed=args.seed,
        )

        custom_raw = args.names
        if args.input_file:
            file_text = Path(args.input_file).read_text(encoding="utf-8")
            custom_raw = f"{custom_raw}\n{file_text}" if custom_raw else file_text
        names = parse_custom_names(custom_raw)

        mode = "custom" if (names or args.strategy == "watchlist") else "generated"
        if args.strategy == "watchlist" and not names:
            names = store.watchlist_candidates(
                account_id,
                provider="public" if isinstance(active_client, PublicUsernameClient) else "account",
                stale_only=not args.force_recheck,
            ) or [str(item["username"]) for item in store.list_watchlist(account_id)]

        if args.preview:
            prev = preview_candidates(mode, lengths, names, prefs, sample_size=15)
            print(
                f"Candidate preview ({prev.strategy_summary}): {prev.total_candidates:,} total "
                f"(sample: {', '.join(prev.sample_candidates)})"
            )
            return 0

        if mode == "custom" and not names:
            print("No candidate usernames provided.")
            return 1

        scan = store.create_scan(
            account_id,
            mode=mode,
            lengths=lengths,
            names=names,
            preferences=None if prefs.is_default_exhaustive() else prefs,
        )
        budget = RequestBudget(
            max_network_requests=args.max_network_requests,
            max_definitive_checks=args.max_definitive_checks,
            max_duration_seconds=args.max_duration,
        )

        last_wait_notice = -float("inf")

        def report(progress: UsernameProgress) -> None:
            nonlocal last_wait_notice
            if progress.result is not None and progress.result.status == UsernameStatus.AVAILABLE:
                tag = (
                    f" ({progress.result.provenance_label()})"
                    if (progress.result.is_cached or progress.result.is_stale)
                    else ""
                )
                print(f"AVAILABLE: {progress.result.username} | {progress.result.checked_at}{tag}", flush=True)
            elif progress.message.startswith("Rate limited"):
                if time.monotonic() - last_wait_notice >= 15:
                    print(progress.message, flush=True)
                    last_wait_notice = time.monotonic()

        final = scanner.run(
            scan.id,
            account_id,
            on_progress=report,
            budget=budget,
            use_cache=not args.no_cache and not args.force_recheck,
            force_recheck=args.force_recheck,
        )
        print(
            f"Scan {final.status}: {final.checked:,}/{final.total:,} checked, {final.available:,} available "
            f"(network={final.network_checks:,}, cached={final.cache_hits:,}, dedup={final.deduplicated_hits:,})."
        )
        if final.pause_reason and final.status != "completed":
            print(f"Pause reason: {final.pause_reason}")
        if final.error:
            print(final.error)
        if args.export_path:
            store.export_csv(final.id, account_id, args.export_path)
            print(f"Exported results to {args.export_path}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted; completed checks and cursor are saved.")
        return 130
    finally:
        pass
