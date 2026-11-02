"""Cold startup must not load optional Telegram/AI work before it is needed."""

import subprocess
import sys
import textwrap


def _python(code):
    subprocess.run([sys.executable, '-c', textwrap.dedent(code)], check=True, timeout=30)


def test_host_and_runtime_imports_defer_telegram():
    _python('''
        import sys
        import app.desktop.headless
        import app.web
        import app.api.runtime
        assert 'aiogram' not in sys.modules
        assert 'app.worker_v1' not in sys.modules
        assert 'app.services.translation_service' not in sys.modules
        assert 'app.services.download_service' not in sys.modules
    ''')


def test_lazy_package_exports_preserve_the_public_api():
    _python('''
        import sys
        import app.news
        from app.news.worker_config import HomeWorkerSettings
        assert 'app.news.facade' not in sys.modules
        from app.application import PlatformError, PlatformService
        from app.application.platform import PlatformError as ExistingError
        from app.news import *
        assert PlatformError is ExistingError
        assert PlatformService.__name__ == 'PlatformService'
        assert HomeWorkerSettings.__name__ == 'HomeWorkerSettings'
        assert NewsSourceFacade.__name__ == 'NewsSourceFacade'
        assert callable(classify_news_input)
    ''')


def test_idle_ai_manager_does_not_load_extractors():
    _python('''
        import asyncio
        import sys
        from app.desktop import ai
        assert 'aiohttp' not in sys.modules
        ai.SecretStore = lambda _control: None
        ai.profiles = lambda _store: []

        async def check():
            task = asyncio.create_task(ai.run_connections(None, None))
            await asyncio.sleep(.05)
            assert 'app.news.facade' not in sys.modules
            assert 'app.worker_v1' not in sys.modules
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(check())
    ''')


async def test_configured_ai_still_starts_worker(monkeypatch):
    import asyncio
    from types import ModuleType, SimpleNamespace

    import pytest
    import aiohttp
    from app.desktop import ai

    started = asyncio.Event()

    class Worker:
        def __init__(self, *, api, settings, session, extractor):
            self.settings = settings
            assert api.credential == 'fixture-credential'
            assert extractor == 'fixture-extractor'

        async def run_once(self):
            assert await self.model_catalog() == ['fixture-model']
            started.set()
            return False

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    facade = ModuleType('app.news.facade')
    facade.NewsSourceFacade = SimpleNamespace(from_session=lambda *args, **kwargs: 'fixture-extractor')
    http = ModuleType('app.news.http')
    http.PublicOnlyResolver = object
    worker = ModuleType('app.worker_v1')
    worker.RegisteredWorker = Worker
    monkeypatch.setitem(sys.modules, 'app.news.facade', facade)
    monkeypatch.setitem(sys.modules, 'app.news.http', http)
    monkeypatch.setitem(sys.modules, 'app.worker_v1', worker)
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(aiohttp, 'TCPConnector', lambda **kwargs: None)
    monkeypatch.setattr(ai, 'SecretStore', lambda _control: None)
    monkeypatch.setattr(ai, 'profiles', lambda _store: [{
        'id': 'fixture-id', 'name': 'Fixture', 'base_url': 'http://127.0.0.1:1234',
        'model': 'fixture-model', 'credential': 'fixture-credential',
    }])
    monkeypatch.setattr(ai.Connection, 'settings', lambda self: SimpleNamespace(model_name=self.model))
    task = asyncio.create_task(ai.run_connections(None, None))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
