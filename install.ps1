param([string]$ExtensionDirectory = (Join-Path $env:APPDATA 'qrenderdoc\extensions'))
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$destination = Join-Path $ExtensionDirectory 'mesh_exporter'
if (-not (Test-Path (Join-Path $source 'extension.json'))) { throw 'Run this installer from the mesh_exporter package.' }
if (-not (Test-Path (Join-Path $source 'vendor\viewer.bundle.js'))) { throw 'The packaged Three.js viewer is missing.' }
New-Item -ItemType Directory -Force -Path $ExtensionDirectory | Out-Null
if (Test-Path $destination) {
    $item = Get-Item -LiteralPath $destination -Force
    if ($item.LinkType -eq 'Junction' -and [string]$item.Target -eq $source) {
        Write-Host "Already installed: $destination"
        exit 0
    }
    throw "Destination already exists; nothing was replaced: $destination"
}
New-Item -ItemType Junction -Path $destination -Target $source | Out-Null
Write-Host "Installed: $destination"
Write-Host 'In RenderDoc: Tools > Manage Extensions > Mesh Exporter > Load.'
Write-Host 'Then use Tools > Mesh Exporter > Export meshes and textures...'
