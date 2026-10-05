"""Compatibility for ``python -m store_vision``; delegates to the one CLI."""

from store_vision.cli import main


if __name__ == "__main__":
    main()
