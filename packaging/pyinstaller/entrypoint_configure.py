"""PyInstaller entrypoint: thin wrapper around whale.config_tui's CLI."""

from whale.config_tui import main

if __name__ == "__main__":
    main()
