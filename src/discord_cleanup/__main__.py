import sys


def main() -> None:
    if "--usernames" in sys.argv:
        from discord_cleanup.cli.usernames import run_username_checker, run_username_cli_args

        extra = [arg for arg in sys.argv[1:] if arg != "--usernames" and arg != "--cli"]
        if extra:
            raise SystemExit(run_username_cli_args([arg for arg in sys.argv[1:] if arg != "--cli"]))
        else:
            run_username_checker()
        return
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from discord_cleanup.cli.main import main as cli_main
        cli_main()
    else:
        from discord_cleanup.ui.app import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
