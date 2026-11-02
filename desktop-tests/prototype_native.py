"""Opt-in Windows WebDriver smoke test against an *installed prototype*.

Only a new, explicitly supplied fixture directory is accepted. A synthetic image
is created for the native file picker; no production installation is touched.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.desktop.launcher import stop_host
from app.desktop.headless import _read_bridge_token
from app.config import Settings
from app.host.secrets import ALLOWED_SECRETS
from app.runtime import InstanceLock
from measure_prototype import process_tree_private_bytes


def main():
    if os.name != 'nt':
        raise SystemExit('The native prototype test requires Windows.')
    executable = Path(os.environ['PUBLISHER_TEST_APPLICATION']).resolve(strict=True)
    native_driver = Path(os.environ['PUBLISHER_TEST_EDGE_DRIVER']).resolve(strict=True)
    driver_binary = Path(os.environ['PUBLISHER_TEST_TAURI_DRIVER']).resolve(strict=True)
    root = Path(os.environ['PUBLISHER_NATIVE_FIXTURE'])
    if not root.is_absolute() or root.exists():
        raise SystemExit('PUBLISHER_NATIVE_FIXTURE must name a new absolute directory.')
    root.mkdir(parents=True)
    root = root.resolve()
    (root / '.edizora-prototype').write_text('isolated test profile', encoding='utf-8')
    image_path = root / 'fixture.png'
    Image.new('RGB', (32, 32), '#de8055').save(image_path)
    second_image_path = root / 'fixture2.png'
    if os.environ.get('PUBLISHER_NATIVE_MULTI_PICKER') == '1':
        Image.new('RGB', (32, 32), '#57a3a0').save(second_image_path)
    screenshots = Path(os.environ.get('PUBLISHER_TEST_SCREENSHOTS', str(root / 'screenshots')))
    screenshots.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
    env.update(EDIZORA_PROTOTYPE_ROOT=str(root), MSEDGEDRIVER_TELEMETRY_OPTOUT='1')
    process = subprocess.Popen([str(driver_binary), '--port', str(port), '--native-driver', str(native_driver)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    session = None
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=150, trust_env=False)

    def call(method, path, body=None):
        response = client.request(method, path, json=body)
        data = response.json().get('value')
        if response.status_code >= 400:
            raise AssertionError(f'WebDriver {path}: {data}')
        return data

    def invoke(command, args=None):
        value = call('POST', f'/session/{session}/execute/async', {
            'script': "const [command, args, done] = arguments; window.__TAURI_INTERNALS__.invoke(command, args).then(value => done({value}), error => done({error: String(error)}));",
            'args': [command, args or {}],
        })
        if 'error' in value:
            raise AssertionError(f'{command}: {value["error"]}')
        return value['value']

    def wait_phase(expected):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = invoke('desktop_status')
            if status['phase'] == expected:
                return
            if status['phase'] == 'failed':
                raise AssertionError('Desktop backend failed; check the fixture log')
            time.sleep(.2)
        raise AssertionError(f'Timed out waiting for {expected}')

    def screenshot(name):
        encoded = call('GET', f'/session/{session}/screenshot')
        (screenshots / name).write_bytes(base64.b64decode(encoded))

    def wait_ui(script, description, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = call('POST', f'/session/{session}/execute/sync', {'script': script, 'args': []})
            if value:
                return value
            time.sleep(.2)
        raise AssertionError(f'Timed out waiting for UI: {description}')

    def submit_form(selector, values):
        return call('POST', f'/session/{session}/execute/sync', {'script': """
          const [selector, values] = arguments;
          const element = document.querySelector(selector);
          const form = element && element.form;
          if (!form) throw Error('Form is unavailable');
          for (const [name, value] of Object.entries(values)) {
            const input = form.elements.namedItem(name);
            if (!input) throw Error('Missing form field: ' + name);
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles:true}));
            input.dispatchEvent(new Event('change', {bubbles:true}));
          }
          form.requestSubmit();
          return true;
        """, 'args': [selector, values]})

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('tauri-driver exited before it became ready')
            try:
                call('GET', '/status')
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(.1)
        response = call('POST', '/session', {'capabilities': {'alwaysMatch': {
            'browserName': 'wry', 'tauri:options': {'application': str(executable)},
        }}})
        session = response['sessionId']
        call('POST', f'/session/{session}/timeouts', {'script': 120000})
        deadline = time.monotonic() + 30
        page = None
        while time.monotonic() < deadline:
            page = call('POST', f'/session/{session}/execute/sync', {
                'script': 'return {url:location.href, ready:document.readyState, bridge:!!window.__TAURI_INTERNALS__}', 'args': []})
            if page['bridge'] and page['ready'] != 'loading':
                break
            time.sleep(.2)
        else:
            raise AssertionError(f'Tauri page did not become ready: {page}')
        wait_phase('setup')
        wait_ui("return !!document.querySelector('input[name=repeat]')", 'setup form')
        screenshot('01-setup.png')
        setup_exit = os.environ.get('PUBLISHER_NATIVE_EXIT_DURING_SETUP')
        if setup_exit in {'before', '1'}:
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const [submit] = arguments;
              if (submit) {
                const form = document.querySelector('input[name=repeat]').form;
                for (const [name, value] of Object.entries({admin:'1234567', password:'isolated-password-123', repeat:'isolated-password-123'})) {
                  form.elements.namedItem(name).value = value;
                }
                form.requestSubmit();
              }
              [...document.querySelectorAll('button')].find(item => item.textContent.trim() === 'Выйти и остановить').click();
              return true;
            """, 'args': [setup_exit == '1']})
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    call('GET', f'/session/{session}/url')
                except (AssertionError, httpx.HTTPError):
                    break
                time.sleep(.2)
            else:
                raise AssertionError('Application did not exit after setup request')
            with InstanceLock(root / 'desktop-setup.lock'), InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
                pass
            if (root / 'control/node.json').exists():
                with sqlite3.connect((root / 'data/database.db').as_uri() + '?mode=ro', uri=True) as connection:
                    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            print('PASS: shutdown at first setup leaves no running host or runtime', flush=True)
            return
        submit_form('input[name=repeat]', {'admin': '1234567', 'password': 'isolated-password-123',
                                           'repeat': 'isolated-password-123'})
        wait_phase('ready')
        wait_ui("return !!document.querySelector('input[autocomplete=current-password]')", 'login form')
        screenshot('02-login.png')

        if os.environ.get('PUBLISHER_CHECK_MINIMIZED') == '1':
            print('MINIMIZE_READY', flush=True)
            deadline = time.monotonic() + 90
            while invoke('desktop_is_visible'):
                if time.monotonic() >= deadline:
                    raise AssertionError('The native window did not minimize')
                time.sleep(.1)
            time.sleep(1)
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const original=window.setTimeout;
              window.__minimizedPolls=0;
              const wrapped=function(handler,delay,...args){if(delay===5000)window.__minimizedPolls++;return original.call(this,handler,delay,...args);};
              window.setTimeout=wrapped;
              if(window.setTimeout!==wrapped)throw Error('Cannot instrument status polling');
              return true;
            """, 'args': []})
            time.sleep(6)
            count = call('POST', f'/session/{session}/execute/sync', {'script': 'return window.__minimizedPolls', 'args': []})
            if count:
                raise AssertionError(f'The minimized window scheduled {count} status polls')
            assert subprocess.run([str(executable)], env=env, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
            deadline = time.monotonic() + 15
            while not invoke('desktop_is_visible'):
                if time.monotonic() >= deadline:
                    raise AssertionError('Repeated launch did not restore the minimized window')
                time.sleep(.1)
            wait_ui('return window.__minimizedPolls > 0', 'poll after minimized window opens', timeout=10)

        if os.environ.get('PUBLISHER_CHECK_BACKGROUND_POLL') == '1':
            invoke('desktop_shutdown')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    call('GET', f'/session/{session}/url')
                except (AssertionError, httpx.HTTPError):
                    break
                time.sleep(.2)
            else:
                raise AssertionError('The initial shell did not finish shutdown')
            try:
                call('DELETE', f'/session/{session}')
            except (AssertionError, httpx.HTTPError):
                pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    _, children = process_tree_private_bytes(process.pid)
                    if not any(item['name'].lower() == executable.name.lower() for item in children):
                        break
                except OSError:
                    pass
                time.sleep(.2)
            else:
                raise AssertionError('The initial shell process did not exit')
            response = call('POST', '/session', {'capabilities': {'alwaysMatch': {
                'browserName': 'wry', 'tauri:options': {'application': str(executable), 'args': ['--background']},
            }}})
            session = response['sessionId']
            call('POST', f'/session/{session}/timeouts', {'script': 120000})
            wait_ui('return !!window.__TAURI_INTERNALS__', 'background bridge')
            wait_phase('ready')
            if invoke('desktop_is_visible'):
                raise AssertionError('Background startup unexpectedly showed the window')
            time.sleep(1)
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const original=window.setTimeout;
              window.__backgroundPolls=0;
              const wrapped=function(handler,delay,...args){if(delay===5000)window.__backgroundPolls++;return original.call(this,handler,delay,...args);};
              window.setTimeout=wrapped;
              if(window.setTimeout!==wrapped)throw Error('Cannot instrument status polling');
              return true;
            """, 'args': []})
            time.sleep(6)
            count = call('POST', f'/session/{session}/execute/sync', {'script': 'return window.__backgroundPolls', 'args': []})
            if count:
                raise AssertionError(f'The background window scheduled {count} status polls')
            assert subprocess.run([str(executable)], env=env, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
            wait_ui('return window.__backgroundPolls > 0', 'poll after background window opens', timeout=10)

        def api(path, method='GET', body=None):
            return invoke('desktop_request', {'method': method, 'path': '/api/v1' + path, 'body': body,
                'key': str(uuid4()) if method != 'GET' else None})

        assert api('/channels')['status'] == 401
        if os.environ.get('PUBLISHER_NATIVE_MULTI_PICKER') == '1':
            try:
                invoke('desktop_pick_files')
            except AssertionError:
                pass
            else:
                raise AssertionError('The native picker was available before sign-in')
        submit_form('input[autocomplete=current-password]', {'admin': '1234567', 'password': 'isolated-password-123'})
        wait_ui("return !!document.querySelector('input[name=alias]')", 'workspace')
        if os.environ.get('PUBLISHER_NATIVE_MULTI_PICKER') == '1':
            call('POST', f'/session/{session}/execute/sync', {'script': """
              window.__multiPicker = {};
              window.__TAURI_INTERNALS__.invoke('desktop_pick_files').then(value => { window.__multiPicker = {value}; }, error => { window.__multiPicker = {error:String(error)}; });
              return true;
            """, 'args': []})
            print('MULTI_PICKER_READY ' + json.dumps([str(image_path), str(second_image_path)], ensure_ascii=False), flush=True)
            selection = wait_ui("return window.__multiPicker && (window.__multiPicker.value || window.__multiPicker.error) ? window.__multiPicker : null", 'multiple file selection', timeout=120)
            assert 'error' not in selection, selection
            selected = selection['value']
            assert {file['name'] for file in selected} == {'fixture.png', 'fixture2.png'}, selected
            assert all(file['size'] > 0 and set(file) == {'id', 'name', 'size'} for file in selected), selected
            first = invoke('desktop_upload', {'id': selected[0]['id'], 'requestId': str(uuid4())})
            assert first['status'] == 201, first
            invoke('desktop_release_files', {'ids': [selected[1]['id']]})
            try:
                invoke('desktop_upload', {'id': selected[1]['id'], 'requestId': str(uuid4())})
            except AssertionError:
                pass
            else:
                raise AssertionError('A released file handle was still usable')
            print('PASS: native multi-select, streaming upload and file handle release', flush=True)
        session_response = api('/auth/session')
        assert session_response['status'] == 200 and 'csrf_token' not in session_response['body'] and 'token' not in session_response['body'], session_response
        submit_form('input[name=alias]', {'alias': 'fixture', 'title': 'Fixture channel', 'chat': '-1001234567'})
        wait_ui("return [...document.querySelectorAll('li')].some(item => item.textContent === 'Fixture channel')", 'created channel')
        screenshot('03-workspace.png')
        assert len(api('/channels')['body']['items']) == 1
        assert api('/publications?limit=20')['status'] == 200

        if os.environ.get('PUBLISHER_NATIVE_SEEDED_MEDIA') == '1':
            # Debugging shortcut for the media protocol only. This does not
            # exercise the native picker or Tauri upload command.
            origin = json.loads((root / 'control/host.json').read_text(encoding='utf-8'))['origin']
            token = _read_bridge_token(root / 'control')
            with httpx.Client(base_url=origin, trust_env=False, timeout=30,
                              headers={'Origin': origin, 'X-Edizora-Bridge': token}) as fixture_client:
                login = fixture_client.post('/api/v1/auth/password', json={'telegram_id': 1234567, 'password': 'isolated-password-123'})
                login.raise_for_status()
                fixture_client.headers['X-CSRF-Token'] = login.json()['csrf_token']
                with image_path.open('rb') as image_file:
                    response = fixture_client.post('/api/v1/assets/uploads', files={'file': ('fixture.png', image_file, 'image/png')})
                response.raise_for_status()
                uploaded = {'status': response.status_code, 'body': response.json()}
                fixture_client.post('/api/v1/auth/logout', json={}).raise_for_status()
        else:
            # Trigger the application button, then choose the synthetic image
            # in the actual Windows file picker.
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const button = [...document.querySelectorAll('button')].find(item => item.textContent.trim() === 'Выбрать изображение');
              if (!button) throw Error('Upload button unavailable');
              button.click(); return true;
            """, 'args': []})
            print(f'PICKER_READY {image_path}', flush=True)
            media_url = wait_ui("const image=document.querySelector('img.proto-preview'); return image && image.naturalWidth === 32 ? image.src : null", 'uploaded image', timeout=120)
            with sqlite3.connect((root / 'data/database.db').as_uri() + '?mode=ro', uri=True) as connection:
                asset_id = connection.execute('SELECT id FROM media_assets').fetchone()[0]
            uploaded = {'status': 201, 'body': {'url': f'/api/v1/assets/{asset_id}/content'}}
            screenshot('04-upload.png')
        assert uploaded['status'] == 201, uploaded
        if os.environ.get('PUBLISHER_NATIVE_SEEDED_MEDIA') == '1':
            media_url = invoke('desktop_media', {'path': uploaded['body']['url']})
        image_result = call('POST', f'/session/{session}/execute/async', {'script': """
          const [url, done] = arguments; const image = new Image();
          image.onload = () => done({width:image.naturalWidth, height:image.naturalHeight});
          image.onerror = () => done({error:'media unavailable'}); image.src = url;
        """, 'args': [media_url]})
        assert image_result == {'width': 32, 'height': 32}, image_result
        call('POST', f'/session/{session}/execute/sync', {'script': 'window.__beforeLogout = true; return true', 'args': []})
        call('POST', f'/session/{session}/execute/sync', {'script': """
          const button = [...document.querySelectorAll('button')].find(item => item.textContent.trim() === 'Выйти');
          if (!button) throw Error('Logout button unavailable');
          button.click(); return true;
        """, 'args': []})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                page = call('POST', f'/session/{session}/execute/sync', {
                    'script': 'return !!window.__TAURI_INTERNALS__ && !window.__beforeLogout', 'args': []})
                if page:
                    break
            except (AssertionError, httpx.HTTPError):
                pass
            time.sleep(.1)
        else:
            raise AssertionError('WebView document was not cleared after logout')
        assert api('/channels')['status'] == 401
        try:
            invoke('desktop_media', {'path': uploaded['body']['url']})
        except AssertionError:
            pass
        else:
            raise AssertionError('A signed-out session was given a new media reference')
        revoked = call('POST', f'/session/{session}/execute/async', {'script': """
          const [url, done] = arguments; const image = new Image();
          image.onload = () => done(false); image.onerror = () => done(true); image.src = url;
        """, 'args': [media_url]})
        assert revoked
        if os.environ.get('PUBLISHER_NATIVE_TRAY_CLICK') == '1':
            wait_ui("return !!document.querySelector('input[autocomplete=current-password]')", 'login form after logout')
            call('POST', f'/session/{session}/execute/sync', {'script': "localStorage.removeItem('edizora-prototype-tray-notice'); return true", 'args': []})
            print('CLOSE_READY', flush=True)
            wait_ui("return !!document.querySelector('[role=dialog] #tray-title')", 'first close explanation')
            wait_ui("return document.activeElement && document.activeElement.textContent.trim() === 'Скрыть в трей'", 'dialog focus')
            screenshot('05-tray-notice.png')
            call('POST', f'/session/{session}/execute/sync', {'script': """
              [...document.querySelectorAll('[role=dialog] button')].find(item => item.textContent.trim() === 'Скрыть в трей').click();
              return true;
            """, 'args': []})
        else:
            invoke('desktop_hide')
        deadline = time.monotonic() + 15
        while invoke('desktop_is_visible'):
            if time.monotonic() >= deadline:
                raise AssertionError('Window did not hide to the tray')
            time.sleep(.1)
        if os.environ.get('PUBLISHER_CHECK_HIDDEN') == '1':
            # WebView2 can keep document.hidden=false when its native window
            # is hidden. Count scheduled status polls rather than trusting it.
            time.sleep(1)
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const original=window.setTimeout;
              window.__statusPolls=0;
              const wrapped=function(handler,delay,...args){if(delay===5000)window.__statusPolls++;return original.call(this,handler,delay,...args);};
              window.setTimeout=wrapped;
              if(window.setTimeout!==wrapped)throw Error('Cannot instrument status polling');
              return true;
            """, 'args': []})
            time.sleep(6)  # More than one visible-window polling interval.
            count = call('POST', f'/session/{session}/execute/sync', {'script': 'return window.__statusPolls', 'args': []})
            if count:
                raise AssertionError(f'The hidden window issued {count} status polls')
        assert api('/channels')['status'] == 401  # The host is still running after hiding.
        native_tray = os.environ.get('PUBLISHER_NATIVE_TRAY_ICON') == '1'
        if native_tray and os.environ.get('PUBLISHER_NATIVE_TRAY_CLICK') != '1':
            raise AssertionError('Native tray testing also requires PUBLISHER_NATIVE_TRAY_CLICK=1')
        if native_tray:
            print('TRAY_OPEN_READY', flush=True)
            deadline = time.monotonic() + 90
            while not invoke('desktop_is_visible'):
                if time.monotonic() >= deadline:
                    raise AssertionError('The tray icon did not reopen the window')
                time.sleep(.1)
        assert subprocess.run([str(executable)], env=env, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
        deadline = time.monotonic() + 15
        while not invoke('desktop_is_visible'):
            if time.monotonic() >= deadline:
                raise AssertionError('Repeated launch did not restore the window')
            time.sleep(.1)
        if os.environ.get('PUBLISHER_CHECK_HIDDEN') == '1':
            wait_ui('return window.__statusPolls > 0', 'poll after reopening', timeout=10)
        if os.environ.get('PUBLISHER_NATIVE_TRAY_CLICK') == '1':
            print('CLOSE_AGAIN_READY', flush=True)
            deadline = time.monotonic() + 15
            while invoke('desktop_is_visible'):
                if time.monotonic() >= deadline:
                    raise AssertionError('A later close did not hide the window')
                time.sleep(.1)
            if native_tray:
                print('TRAY_QUIT_READY', flush=True)
            else:
                assert subprocess.run([str(executable)], env=env, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
                deadline = time.monotonic() + 15
                while not invoke('desktop_is_visible'):
                    if time.monotonic() >= deadline:
                        raise AssertionError('Repeated launch did not reopen the window')
                    time.sleep(.1)
        if not native_tray:
            call('POST', f'/session/{session}/execute/sync', {'script': """
              const button = [...document.querySelectorAll('button')].find(item => item.textContent.trim() === 'Выйти и остановить');
              if (!button) throw Error('Shutdown button unavailable');
              button.click(); return true;
            """, 'args': []})
        deadline = time.monotonic() + (90 if native_tray else 60)
        while True:
            try:
                with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
                    break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Application did not drain the fixture host and runtime') from None
                time.sleep(.2)
        while time.monotonic() < deadline:
            try:
                call('GET', f'/session/{session}/url')
            except (AssertionError, httpx.HTTPError):
                break
            time.sleep(.2)
        else:
            raise AssertionError('Application window did not exit after its backend drained')
        upload_check = 'seeded upload' if os.environ.get('PUBLISHER_NATIVE_SEEDED_MEDIA') == '1' else 'native upload'
        print(f'PASS: setup, authenticated bridge, read, mutation, {upload_check}, media, logout, single instance, drained shutdown', flush=True)
    finally:
        if (root / 'control/host.json').exists():
            try:
                try:
                    with InstanceLock(root / 'control/host.lock'):
                        pass
                except RuntimeError:
                    stop_host(root)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        with InstanceLock(root / 'control/host.lock'):
                            break
                    except RuntimeError:
                        time.sleep(.2)
            except Exception:
                print('Fixture backend could not be stopped through its normal drain.', file=sys.stderr)
        if session:
            try:
                call('DELETE', f'/session/{session}')
            except Exception:
                pass
        client.close()
        process.terminate()
        process.wait(timeout=15)


if __name__ == '__main__':
    main()
