"""Native Windows build in a new directory; no administrator rights requested."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import venv

from rew_app_metadata import APP_VERSION


def main():
    if os.name != 'nt':
        raise SystemExit('Budowa wymaga Windows; nie jest to cross-kompilacja.')
    root = Path(__file__).resolve().parent
    base = Path(tempfile.mkdtemp(prefix='rewdo_20_34_dev1_build_'))
    environment = base / 'venv'
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / 'Scripts/python.exe'
    log = base / 'build.log'
    with log.open('w', encoding='utf-8') as stream:
        for command in (
            [str(python), '-m', 'pip', 'install', '-r', str(root/'requirements.txt')],
            [str(python), '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', str(base/'dist'),
             '--workpath', str(base/'work'), str(root/'REWDO_onedir.spec')],
        ):
            print('Budowa REWDO', APP_VERSION, '— log:', log, flush=True)
            subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, check=True)
    target = base / 'dist/REWDO_GUI_ONEDIR'
    executable = target / 'REWDO_GUI.exe'
    subprocess.run([str(executable), '--startup-diagnostic'], check=True)
    subprocess.run([str(executable), '--smoke-test'], check=True)
    shutil.copy2(root/'INSTRUKCJA_v20_34_dev1.md', target/'INSTRUKCJA.md')
    shutil.copy2(root/'BUILD_IDENTITY.json', target/'BUILD_IDENTITY.json')
    print('Nowy folder aplikacji:', target)
    print('Sprawdź ręcznie Windows, skalowanie 100% / 150%, import API i pełną analizę czasu. Testy startu nie zastępują odbioru GUI.')


if __name__ == '__main__':
    main()
