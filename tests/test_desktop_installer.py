"""Opt-in install/upgrade/uninstall acceptance with registry registration disabled."""
import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from app.desktop.launcher import SetupInput, initialize

INSTALLER = os.environ.get('PUBLISHER_TEST_INSTALLER')
pytestmark = pytest.mark.skipif(not INSTALLER, reason='Set PUBLISHER_TEST_INSTALLER to an AcceptanceInstall build')


async def test_install_upgrade_backup_and_uninstall_preserve_data(tmp_path):
    program, state, temporary = tmp_path / 'program', tmp_path / 'state', tmp_path / 'temp'
    temporary.mkdir()
    await initialize(state, SetupInput(admin_id=1234567, password='isolated-test-password', password_repeat='isolated-test-password'))
    sentinel = state / 'data/user-material.txt'
    sentinel.write_text('preserve this user material', encoding='utf-8')
    environment = {**os.environ, 'PUBLISHER_DESKTOP_ROOT': str(state), 'TEMP': str(temporary), 'TMP': str(temporary)}
    arguments = [str(Path(INSTALLER).resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NOICONS', '/TASKS=', '/NORESTART',
        '/DIR=' + str(program), '/LOG=' + str(tmp_path / 'install.log')]

    def execute(command):
        return subprocess.run(command, env=environment, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW, check=True)

    await asyncio.to_thread(execute, arguments)
    assert (program / 'Publisher.exe').is_file()
    assert (program / '_internal/frontend/dist/index.html').is_file()
    assert not (program / '.env').exists()
    await asyncio.to_thread(execute, arguments)
    assert sentinel.read_text(encoding='utf-8') == 'preserve this user material'
    backups = list((state / 'update-backups').glob('*/backup.json'))
    assert len(backups) == 1
    await asyncio.to_thread(execute, [str(program / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LOG=' + str(tmp_path / 'uninstall.log')])
    for _ in range(100):
        if not (program / 'Publisher.exe').exists():
            break
        await asyncio.sleep(.1)
    assert not (program / 'Publisher.exe').exists()
    assert sentinel.read_text(encoding='utf-8') == 'preserve this user material'
