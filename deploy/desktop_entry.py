"""PyInstaller entry; the packaged executable dispatches its own child modules."""
import multiprocessing
import os
import runpy
import sys

multiprocessing.freeze_support()
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r', encoding='utf-8')

if len(sys.argv) >= 3 and sys.argv[1] == '--publisher-module':
    module = sys.argv[2]
    if module not in {'app.api.runtime', 'app.desktop.launcher'}:
        raise SystemExit('Unsupported child module')
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    if module == 'app.api.runtime':
        from app.desktop.launcher import configure_logs, root_path
        configure_logs(root_path())
    runpy.run_module(module, run_name='__main__')
else:
    from app.desktop.launcher import run
    run()
