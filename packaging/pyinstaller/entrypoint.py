"""PyInstaller entrypoint: thin wrapper around whale.vara_server's CLI."""

from whale.vara_server import main

if __name__ == "__main__":
    main()
