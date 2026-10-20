"""GitHub announcements for IRC."""

import argparse
import asyncio
import logging
from pathlib import Path


def main() -> None:
    """Run the configured webhook relay."""
    from .config import ConfigError, load_config
    from .web import serve

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        parser.exit(2, f"Configuration error: {error}\n")
    if args.check_config:
        print("Configuration valid")
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        return
    except Exception:
        parser.exit(1, "Service stopped because of an unexpected runtime failure\n")
