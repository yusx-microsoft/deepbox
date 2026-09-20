"""Command dispatcher for the user-installed ``agentbridge`` launcher."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from agentbridge.product import NAME

from . import client


HELP = f"""usage: {NAME} <command> [options]

Commands:
  connect    Connect this machine to an {NAME} server
  doctor     Check server reachability and connector credentials
  status     Show local connector status
  project    Manage connector-local project paths
  skill      Install and manage connector-local Agent Skills
  context    Inspect/release an abandoned native writer guard (local only)
  upgrade    Refresh the installed connector explicitly

Run '{NAME} <command> --help' for connector command options.
"""


class CommandError(ValueError):
    """Raised when a top-level agentbridge command is not recognized."""


def connector_argv(argv: Sequence[str]) -> list[str] | None:
    """Translate a agentbridge command into the existing connector argument shape.

    ``None`` means that top-level help should be printed instead of starting the
    connector. Leading options remain supported for compatibility with the old
    ``deepbox-connect`` launcher.
    """

    args = list(argv)
    if not args or args[0] in {"help", "-h", "--help"}:
        return None

    command, rest = args[0], args[1:]
    if command == "connect":
        return rest
    if command == "doctor":
        return ["--doctor", *rest]
    if command == "status":
        return ["--status", *rest]
    if command in {"project", "skill"}:
        return args
    if command == "upgrade":
        raise CommandError(
            f"upgrade must be run through the installed {NAME} command"
        )
    if command.startswith("-"):
        return args
    raise CommandError(f"unknown command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "context":
        return _context_command(list(args[1:]))
    try:
        routed = connector_argv(args)
    except CommandError as exc:
        print(f"{NAME}: {exc}", file=sys.stderr)
        print(HELP, file=sys.stderr, end="")
        return 2

    if routed is None:
        print(HELP, end="")
        return 0

    asyncio.run(client.main(routed))
    return 0


def _context_command(argv: list[str]) -> int:
    """Explicit local recovery; never connects, probes a CLI or changes history."""
    import argparse
    import json
    import os
    from .local_store import default_state_root
    from .native_writer import status, recover_native_writer, NativeWriterError
    from .runtimes import get_for_surface, UnknownRuntimeError

    parser = argparse.ArgumentParser(prog="agentbridge context")
    parser.add_argument("action", choices=("status", "release"))
    parser.add_argument("runtime", help="runtime family, e.g. claude-code or copilot-cli")
    parser.add_argument("session_id", help="unchanged AgentBridge/native session ID")
    parser.add_argument("--owner", help="exact owner from local status output")
    parser.add_argument("--confirm-writer-stopped", action="store_true",
                        help="attest that the previous CLI and any writers are stopped")
    args = parser.parse_args(argv)
    try:
        adapter = get_for_surface(args.runtime, "structured")
    except UnknownRuntimeError:
        adapter = None
    if adapter is None or adapter.context_control is None:
        print("This runtime does not support native conversation recovery.", file=sys.stderr)
        return 2
    root = os.path.join(default_state_root(), "native-writers")
    try:
        if args.action == "status":
            result = status(root, adapter.family_id, args.session_id)
        else:
            # Confirmation is human attestation, not PID liveness detection.
            recover_native_writer(
                root, adapter.family_id, args.session_id,
                expected_owner=args.owner,
                confirm_writer_stopped=args.confirm_writer_stopped)
            result = {"released": True}
    except NativeWriterError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
