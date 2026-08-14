import sys


def main() -> None:
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from discord_cleanup.cli.main import main as cli_main
        cli_main()
    else:
        from discord_cleanup.ui.app import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
