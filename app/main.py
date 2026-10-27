import asyncio

from app.bootstrap import bootstrap
from app.config import Settings
from app.logging_config import configure_logging
from app.runtime import InstanceLock,assert_legacy_entrypoint_allowed


async def main() -> None:
    assert_legacy_entrypoint_allowed()
    settings = Settings()
    configure_logging(settings.log_level)
    with InstanceLock(settings.storage_path / ".publisher.lock"):
        application = await bootstrap(settings)
        await application.run()


if __name__ == "__main__":
    asyncio.run(main())
