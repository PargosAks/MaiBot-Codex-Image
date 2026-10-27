# Windows PowerShell 5.1+ / PowerShell 7. Does not sign into Codex or change MaiBot code.
[CmdletBinding()]
param(
    [string]$MaiBotDirectory,
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE 'MaiBot-Codex-Image'),
    [string[]]$AllowedUser = @(),
    [string]$CodexBinary,
    [string]$UvBinary,
    [int]$Port = 8765,
    [switch]$CreateInstance,
    [switch]$NoGrantCommand
)
$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath($InstallDirectory)
New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
if (-not $UvBinary) {
    $existingUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($existingUv) { $UvBinary = $existingUv.Source }
}
if (-not $UvBinary) {
    $uvFolder = Join-Path $installRoot 'bootstrap'
    New-Item -ItemType Directory -Force -Path $uvFolder | Out-Null
    $UvBinary = Join-Path $uvFolder 'uv.exe'
    if (-not (Test-Path -LiteralPath $UvBinary)) {
        $installer = Join-Path $uvFolder 'uv-installer.ps1'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.13.6/install.ps1' -OutFile $installer
        $savedUnmanaged = $env:UV_UNMANAGED_INSTALL
        try {
            $env:UV_UNMANAGED_INSTALL = $uvFolder
            & $installer
        } finally { $env:UV_UNMANAGED_INSTALL = $savedUnmanaged }
    }
}
$env:UV_CACHE_DIR = Join-Path $installRoot 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $installRoot 'python'
$arguments = @('run','--no-project','--python','3.13',(Join-Path $PSScriptRoot 'deploy.py'),
    '--install-directory',$installRoot,'--uv',$UvBinary,'--port',[string]$Port)
if ($MaiBotDirectory) { $arguments += @('--maibot-directory',$MaiBotDirectory) }
foreach ($allowedIdentity in $AllowedUser) { $arguments += @('--allowed-user',$allowedIdentity) }
if ($CodexBinary) { $arguments += @('--codex-binary',$CodexBinary) }
if ($CreateInstance) { $arguments += '--create-instance' }
if ($NoGrantCommand) { $arguments += '--no-grant-command' }
& $UvBinary @arguments
if ($LASTEXITCODE -ne 0) { throw 'Installation did not complete. Review the error above; existing data was retained.' }
