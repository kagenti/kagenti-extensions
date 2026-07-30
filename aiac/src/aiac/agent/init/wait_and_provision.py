"""aiac-init — the Agent pod's init container.

Gates Agent startup on NATS + IdP Configuration Service + PDP Policy Writer
health, then idempotently provisions the ``aiac-events`` JetStream stream.

``AIAC_RAG_INGEST_URL`` is treated as optional: the RAG pod doesn't exist yet
in this phase of the deployment, so its health check is skipped when the env
var is unset rather than blocking Agent startup forever.

Invoked as: ``python -m aiac.agent.init.wait_and_provision``
"""

import asyncio
import logging
import os
import socket
from urllib.parse import urlparse

import httpx
import nats
from tenacity import retry, stop_after_delay, wait_fixed

from aiac.agent.eventbus.stream import DEFAULT_NATS_URL, ensure_stream

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_RETRY_KWARGS = {"wait": wait_fixed(2), "stop": stop_after_delay(300), "reraise": True}


@retry(**_RETRY_KWARGS)
def _wait_for_tcp(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=2):
        pass


@retry(**_RETRY_KWARGS)
def _wait_for_http_health(base_url: str) -> None:
    httpx.get(f"{base_url}/health", timeout=2).raise_for_status()


async def _provision_stream(nats_url: str) -> None:
    nc = await nats.connect(nats_url)
    try:
        await ensure_stream(nc.jetstream())
    finally:
        await nc.close()


def main() -> None:
    nats_url = os.environ.get("NATS_URL", DEFAULT_NATS_URL)
    pdp_config_url = os.environ["AIAC_PDP_CONFIG_URL"]
    pdp_policy_url = os.environ["AIAC_PDP_POLICY_URL"]
    rag_ingest_url = os.environ.get("AIAC_RAG_INGEST_URL")

    nats_host_port = urlparse(nats_url)
    logger.info("waiting for NATS at %s:%s", nats_host_port.hostname, nats_host_port.port)
    _wait_for_tcp(nats_host_port.hostname, nats_host_port.port)

    logger.info("waiting for IdP Configuration Service at %s", pdp_config_url)
    _wait_for_http_health(pdp_config_url)

    logger.info("waiting for PDP Policy Writer at %s", pdp_policy_url)
    _wait_for_http_health(pdp_policy_url)

    if rag_ingest_url:
        logger.info("waiting for RAG Ingest Service at %s", rag_ingest_url)
        _wait_for_http_health(rag_ingest_url)
    else:
        logger.info(
            "AIAC_RAG_INGEST_URL not set; skipping RAG Ingest health check "
            "(Phase 3 dependency not yet deployed)"
        )

    logger.info("provisioning aiac-events JetStream stream")
    asyncio.run(_provision_stream(nats_url))
    logger.info("aiac-init complete")


if __name__ == "__main__":
    main()
