"""Standalone agentd launcher.

Run as: python -m multinexus.agentd --config agents.toml --agent <id>

One agentd process per agent identity. Claims jobs from coordinate runtime,
executes via adapter, reports results.
"""

import argparse
import asyncio
import logging
import signal
import sys

from ..config import load_config
from .coordinate_client import (
    CoordinateRuntimeError,
    normalize_claim_reap_policy,
    normalize_recovery_reason,
)
from .worker import AgentdWorker

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MultiNexus agentd — coordinate worker")
    parser.add_argument("--config", default="agents.toml")
    parser.add_argument("--agent", required=True, help="Agent ID to run agentd for")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Job poll interval (seconds)")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--recoverable",
        action="store_true",
        default=False,
        help="OPERATOR RECOVERY MODE: also claim timed_out+recoverable jobs to resume them. "
             "Normal workers must NOT set this; it prevents automatic reclaim of stuck jobs.",
    )
    parser.add_argument(
        "--recovery-reason",
        default="",
        help="Evidence: human-readable reason for recovery mode (e.g. prior-process-crashed).",
    )
    parser.add_argument(
        "--prior-process-stopped",
        action="store_true",
        default=False,
        help="Evidence: the prior process handling this agent was confirmed stopped before recovery.",
    )
    parser.add_argument(
        "--reap-mode",
        choices=["global", "none"],
        default="global",
        help="Claim reap policy: 'global' (default, legacy) or 'none' (requires --reap-reason).",
    )
    parser.add_argument(
        "--reap-reason",
        default=None,
        help="Sealed reason for reap_mode=none; must be non-blank, stripped-stable, no control chars.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.recoverable:
        try:
            args.recovery_reason = normalize_recovery_reason(args.recovery_reason)
        except CoordinateRuntimeError as exc:
            parser.error(str(exc))
        if not args.prior_process_stopped:
            parser.error("--recoverable requires --prior-process-stopped")
    else:
        if args.recovery_reason != "":
            parser.error("--recovery-reason requires --recoverable")
        if args.prior_process_stopped:
            parser.error("--prior-process-stopped requires --recoverable")

    # Validate reap policy before config load or any subprocess can run.
    try:
        _, args.reap_reason = normalize_claim_reap_policy(
            reap_mode=args.reap_mode,
            reap_reason=args.reap_reason,
        )
    except CoordinateRuntimeError as exc:
        parser.error(str(exc))

    config = load_config(
        ["--config", args.config, "--agent", args.agent],
        require_token=False,
    )

    worker = AgentdWorker(config)
    loop = asyncio.new_event_loop()

    def _shutdown():
        log.info("Shutting down agentd for %s", config.id)
        worker.stop()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)

    try:
        log.info(
            "agentd worker starting: agent=%s recoverable=%s reap_mode=%s",
            config.id,
            args.recoverable,
            args.reap_mode,
        )
        run_kwargs = {
            "poll_interval": args.poll_interval,
            "recoverable": args.recoverable,
            "recovery_reason": args.recovery_reason,
            "prior_process_stopped": args.prior_process_stopped,
        }
        if args.reap_mode == "none":
            run_kwargs.update(
                reap_mode=args.reap_mode,
                reap_reason=args.reap_reason,
            )
        loop.run_until_complete(worker.run(**run_kwargs))
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        worker.stop()
        loop.close()


if __name__ == "__main__":
    main()
