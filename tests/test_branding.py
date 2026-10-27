"""Keep installations and old release archives usable across the product rename."""
import json
from pathlib import Path
import zipfile

import pytest

from app import apply_environment_aliases
from app.branding import default_data_root
from app.deployment.release import build_release, verify_release
from app.runtime import assert_legacy_entrypoint_allowed


def test_branded_environment_takes_precedence_and_does_not_override_child_settings(monkeypatch):
    monkeypatch.setenv('EDIZORA_CONTROL_DIR', 'selected-control')
    monkeypatch.setenv('PUBLISHER_CONTROL_DIR', 'old-control')
    apply_environment_aliases()
    import os
    assert os.environ['PUBLISHER_CONTROL_DIR'] == 'selected-control'
    # Launcher changes must not be undone when a child imports app again.
    monkeypatch.setenv('PUBLISHER_CONTROL_DIR', 'isolated-desktop-control')
    apply_environment_aliases()
    assert os.environ['PUBLISHER_CONTROL_DIR'] == 'isolated-desktop-control'


def test_existing_data_is_reused_without_moving_it(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert default_data_root() == tmp_path / 'Edizora'
    legacy = tmp_path / 'Publisher'
    (legacy / 'control').mkdir(parents=True)
    (legacy / 'control/node.json').write_text('{}')
    (tmp_path / 'Edizora').mkdir()  # An empty new directory must not hide old data.
    assert default_data_root() == legacy
    assert (legacy / 'control/node.json').read_text() == '{}'
    (tmp_path / 'Edizora/control').mkdir()
    (tmp_path / 'Edizora/control/node.json').write_text('{}')
    assert default_data_root() == tmp_path / 'Edizora'


@pytest.mark.parametrize('directory', ['Edizora', 'Publisher'])
def test_legacy_bot_cannot_start_beside_either_managed_installation(monkeypatch, tmp_path, directory):
    import app.runtime as runtime
    monkeypatch.delenv('PUBLISHER_CONTROL_DIR', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(runtime, 'managed_checkout_marker', lambda: tmp_path / 'no-checkout-marker')
    control = tmp_path / directory / 'control'
    control.mkdir(parents=True)
    (control / 'node.json').write_text('{}')
    with pytest.raises(RuntimeError, match='Edizora'):
        assert_legacy_entrypoint_allowed()


def test_old_release_prefix_remains_verifiable(tmp_path):
    source = tmp_path / 'source'
    (source / 'frontend/dist').mkdir(parents=True)
    (source / 'frontend/dist/index.html').write_text('<main>test</main>')
    result = build_release(source, tmp_path / 'releases')
    assert result['release_id'].startswith('edizora-')
    manifest = verify_release(Path(result['path']))
    manifest['release_id'] = manifest['release_id'].replace('edizora-', 'publisher-', 1)
    legacy_archive = tmp_path / 'legacy.zip'
    with zipfile.ZipFile(result['path']) as original, zipfile.ZipFile(legacy_archive, 'w') as legacy:
        for name in original.namelist():
            data = json.dumps(manifest).encode() if name == 'release-manifest.json' else original.read(name)
            legacy.writestr(name, data)
    assert verify_release(legacy_archive)['release_id'] == manifest['release_id']
