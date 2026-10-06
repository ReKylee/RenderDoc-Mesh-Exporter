"""An offline gallery: classic scripts and embedded image bytes avoid file:// fetches."""
import base64
import json
import shutil
from pathlib import Path


def safe_json(value):
    return json.dumps(value,separators=(',',':'),ensure_ascii=True).replace('<','\\u003c')


def build_gallery(root,catalog,blobs):
    root=Path(root);dest=root/'viewer';dest.mkdir(exist_ok=True);(dest/'models').mkdir(exist_ok=True)
    source=Path(__file__).parent
    bundle=source/'vendor'/'viewer.bundle.js'
    if not bundle.is_file():raise FileNotFoundError('Missing bundled Three.js viewer. Build the extension before exporting.')
    shutil.copyfile(bundle,dest/'viewer.bundle.js')
    license_file=source/'vendor'/'THIRD_PARTY_LICENSES.txt'
    if license_file.exists():shutil.copyfile(license_file,dest/license_file.name)
    image_data={}
    for model in catalog['models']:
        for binding in model['textures']:
            file=binding.get('image')
            if file and file not in image_data:
                image_data[file]='data:image/png;base64,'+base64.b64encode((root/file).read_bytes()).decode('ascii')
    for name,blob in blobs.items():
        (dest/'models'/(name+'.js')).write_text('window.MeshData['+safe_json(name)+']='+safe_json(base64.b64encode(blob).decode('ascii'))+';',encoding='utf8')
    (dest/'catalog.js').write_text('window.MeshData={};window.MeshCatalog='+safe_json(catalog)+';window.MeshImages='+safe_json(image_data)+';',encoding='utf8')
    shutil.copyfile(source/'viewer.html',dest/'index.html')
    shutil.copyfile(source/'viewer_server.py',dest/'server.py')
    (root/'index.html').unlink(missing_ok=True)
    launcher='@echo off\r\nsetlocal\r\nset "ROOT=%~dp0."\r\nset "SERVER=%ROOT%\\viewer\\server.py"\r\nwhere py >nul 2>nul\r\nif %errorlevel%==0 (\r\n  py -3 "%SERVER%" "%ROOT%" %*\r\n  exit /b %errorlevel%\r\n)\r\nwhere python >nul 2>nul\r\nif %errorlevel%==0 (\r\n  python "%SERVER%" "%ROOT%" %*\r\n  exit /b %errorlevel%\r\n)\r\nwhere python3 >nul 2>nul\r\nif %errorlevel%==0 (\r\n  python3 "%SERVER%" "%ROOT%" %*\r\n  exit /b %errorlevel%\r\n)\r\necho Python 3 was not found on PATH.\r\nexit /b 1\r\n'
    (root/'viewer.cmd').write_text(launcher,encoding='ascii')
    (root/'viewer.bat').write_text(launcher,encoding='ascii')
    powershell_launcher='''param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ViewerArgs
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Server = Join-Path $Root "viewer\\server.py"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $Server $Root @ViewerArgs
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $Server $Root @ViewerArgs
    exit $LASTEXITCODE
}
if (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 $Server $Root @ViewerArgs
    exit $LASTEXITCODE
}
throw "Python 3 was not found on PATH."
'''
    (root/'viewer.ps1').write_text(powershell_launcher,encoding='utf8')
