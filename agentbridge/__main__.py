"""Run the connector CLI as ``python -m agentbridge``."""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    # Import only when invoked: importing this entry point never starts a client.
    from connector.cli import main as connector_main

    return connector_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
