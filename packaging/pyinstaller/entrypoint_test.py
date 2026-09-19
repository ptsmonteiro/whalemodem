"""PyInstaller entrypoint: thin wrapper around whale.test_cli's CLI.

``main()`` returns the exit status (0 pass, 1 failed on air, 2 could not
start), so it is passed to ``sys.exit`` the way the console script does.
"""

import sys

from whale.test_cli import main

if __name__ == "__main__":
    sys.exit(main())
