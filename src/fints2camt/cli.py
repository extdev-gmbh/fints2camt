"""Command line: ``fints2camt [--config PATH] {setup,run,approve,check}``.

``run`` prints one summary line on stdout (for cron and systemd logs) and exits with:

* 0 – every account delivered
* 1 – an account was not delivered, or another error
* 2 – configuration, secret or environment missing or invalid
* 3 – operator action needed: run ``fints2camt approve`` (or ``setup``)
* 4 – the bank rejected the PIN or locked the access: fix the PIN, then ``approve``

Details go to stderr. Neither the PIN, a TAN nor a token is ever printed or logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, commands, healthcheck
from .config import Config, load_config
from .errors import ConfigError, Fints2CamtError

CONFIG_ENV = "FINTS2CAMT_CONFIG"
DEFAULT_CONFIG = "config.toml"

log = logging.getLogger("fints2camt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fints2camt",
        description="Fetch bank transactions via FinTS, produce camt.053 and deliver them.",
    )
    parser.add_argument(
        "-c", "--config", type=Path,
        default=Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG)),
        help=f"configuration file (default: ${CONFIG_ENV} or {DEFAULT_CONFIG})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug details (never secrets)")
    parser.add_argument("--version", action="version", version=f"fints2camt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("setup", help="interactive first login: TAN method, first approval, state")
    sub.add_parser("run", help="the scheduled run: fetch, convert, deliver, exit")
    sub.add_parser("approve", help="interactive: approve the bank access again (pushTAN)")
    sub.add_parser("check", help="validate configuration and reachability, without fetching")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # python-fints logs whole bank messages at debug level, httpx every request URL
    # (health-check push URLs carry a secret): keep both quiet.
    for name in ("fints", "mt940", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _finish(args.command, None, commands.Outcome(commands.EXIT_CONFIG, f"configuration error – {exc}"))
        return commands.EXIT_CONFIG

    try:
        if args.command == "run":
            outcome = commands.run(config)
            _finish("run", config, outcome)
            return outcome.exit_code
        if args.command == "setup":
            return commands.setup(config)
        if args.command == "approve":
            return commands.approve(config)
        return commands.check(config)
    except (KeyboardInterrupt, EOFError):
        print("aborted", file=sys.stderr)
        return commands.EXIT_FAILED
    except Fints2CamtError as exc:
        code = commands.EXIT_CONFIG if isinstance(exc, ConfigError) else commands.EXIT_FAILED
        _finish(args.command, config, commands.Outcome(code, f"failed – {exc}"))
        return code
    except Exception as exc:  # noqa: BLE001 – the run must end with a summary and a ping
        log.debug("unexpected error", exc_info=True)
        _finish(args.command, config, commands.Outcome(commands.EXIT_FAILED, f"failed – unexpected {type(exc).__name__}"))
        return commands.EXIT_FAILED


def _finish(command: str, config: Config | None, outcome: commands.Outcome) -> None:
    """Print the run's summary line; after ``run``, ping the health check."""
    stream = sys.stdout if command == "run" else sys.stderr
    print(f"fints2camt {command}: {outcome.summary}", file=stream, flush=True)
    if command == "run" and config is not None:
        healthcheck.ping(config.healthcheck, outcome.ok)


if __name__ == "__main__":
    sys.exit(main())
