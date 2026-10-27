from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp

from app.logging_config import configure_logging
from app.news.facade import NewsSourceFacade
from app.news.home_runtime import HomeNewsWorker
from app.news.http import PublicOnlyResolver
from app.news.llm_factory import create_news_client, list_model_ids
from app.news.vps_client import VpsNewsApiClient
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError
from app.services.model_selection import ModelSelection

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process news drafts through local LM Studio or Ollama",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("bot-settings.toml"),
        help="TOML file containing the [home_worker] section",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Lease at most one task and exit",
    )
    return parser


async def main(*, config_file: Path, once: bool = False) -> None:
    settings = HomeWorkerSettings(_config_file=config_file)
    configure_logging(settings.log_level)
    await run_worker(settings, once=once)


async def run_worker(
    settings: HomeWorkerSettings, *, once: bool = False,
    model_selection: ModelSelection | None = None,
) -> None:
    extractor_connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    async with (
        aiohttp.ClientSession(trust_env=False) as session,
        aiohttp.ClientSession(
            trust_env=False, connector=extractor_connector,
        ) as extractor_session,
    ):
        extractor = NewsSourceFacade.from_session(extractor_session)
        api = VpsNewsApiClient(
            session,
            base_url=settings.vps_api_url,
            token=settings.token.get_secret_value(),
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            source_types=settings.source_types,
            model=settings.model_name,
            timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
        )
        was_ready: tuple[str, bool] | None = None

        async def model_ready() -> bool:
            nonlocal was_ready
            current = model_selection.worker_settings() if model_selection is not None else settings
            try:
                ready = bool(current.model_name) and current.model_name in await list_model_ids(session, current)
            except LlmError:
                ready = False
            if ready:
                # Only replace the client between jobs, never during a rewrite.
                if worker.ollama is None or worker.ollama.model != current.model_name:
                    worker.ollama = create_news_client(session, current)
                api.model = current.model_name
            if (current.model_name, ready) != was_ready:
                if ready:
                    logger.info("local_model_ready provider=%s model=%s", current.llm_provider, current.model_name)
                else:
                    logger.warning("local_model_unavailable_news_waiting provider=%s", settings.llm_provider)
                was_ready = (current.model_name, ready)
            return ready

        worker = HomeNewsWorker(
            api=api,
            extractor=extractor,
            ollama=None,
            poll_interval_seconds=settings.poll_interval_seconds,
            ready=model_ready,
        )
        if once:
            await worker.run_once()
        else:
            await worker.run_forever()


def run() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(main(config_file=args.config, once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
