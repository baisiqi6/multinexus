#!/usr/bin/env python3
"""MultiNexus bridge entrypoint.

Two modes:

1. Legacy / per-agent::

       python multinexus.py --agent host-claude
       python multinexus.py --config agents.toml --agent host-claude

   One process hosts one ``DiscordClient`` (1 agent = 1 process). Kept for
   backward compatibility.

2. Per-platform bridge::

       python multinexus.py --platform discord
       python multinexus.py --config agents.toml --platform discord

   One process hosts a ``DiscordBridge`` containing N ``DiscordClient``
   instances (one per ``[[agents]]`` entry). All agents on Discord share a
   single process and a single in-process mention map.
"""

import argparse
import asyncio
import logging
import sys

from multinexus.client import DiscordBridge, DiscordClient
from multinexus.config import load_all_configs_for_platform, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nexus")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MultiNexus bridge (per-platform) or per-agent runner (legacy).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to agents.toml (default: $DISCORD_AGENTS_CONFIG or ./agents.toml)",
    )
    parser.add_argument(
        "--platform",
        choices=("discord", "kook"),
        default=None,
        help=(
            "Bridge mode: host all agents in 1 process for the given platform. "
            "Mutually exclusive with --agent."
        ),
    )
    parser.add_argument(
        "--agent",
        default=None,
        help=(
            "Legacy mode: host 1 Discord client for this agent. "
            "Mutually exclusive with --platform."
        ),
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    if args.platform and args.agent:
        log.error("--platform and --agent are mutually exclusive.")
        sys.exit(2)

    if args.platform == "kook":
        log.error("KOOK bridge launcher is in multinexus/kook/__main__.py, not here.")
        sys.exit(2)

    if args.platform == "discord":
        configs = load_all_configs_for_platform(
            config_path=args.config, require_token=True,
        )
        log.info(
            "Starting MultiNexus bridge: platform=discord agents=%s",
            [c.id for c in configs],
        )
        bridge = DiscordBridge(configs)
        try:
            asyncio.run(bridge.start())
        except KeyboardInterrupt:
            log.info("Shutting down bridge")
            asyncio.run(bridge.close())
        return

    if args.agent:
        # Legacy: 1 process 1 client.
        import os
        argv = []
        if args.config:
            argv += ["--config", args.config]
        else:
            # load_config reads DISCORD_AGENTS_CONFIG from env when --config
            # is not passed; replicate that by setting it.
            os.environ.setdefault("DISCORD_AGENTS_CONFIG", "agents.toml")
        argv += ["--agent", args.agent]
        config = load_config(argv)
        log.info(
            "Starting MultiNexus: agent=%s adapter=%s display_name=%s",
            config.id,
            config.adapter,
            config.display_name or config.id,
        )
        client = DiscordClient(config)
        try:
            asyncio.run(client.start(config.token))
        except KeyboardInterrupt:
            log.info("Shutting down agent %s", config.id)
            asyncio.run(client.close())
        return

    log.error("Must specify either --platform (discord|kook) or --agent <id>.")
    sys.exit(2)


if __name__ == "__main__":
    main()
