<#
    Downloads a Windows build of iperf3 into tools\iperf3 so the build can bundle it.

    Source: https://github.com/ar51an/iperf3-win-builds (unmodified upstream iperf3,
    BSD-3-Clause). The binaries are Cygwin builds, so cygwin1.dll (LGPLv3) comes with
    them. Read tools\iperf3\README.txt before redistributing the result.
#>
[CmdletBinding()]
param(
    [string] $Destination = (Join-Path $PSScriptRoot 'iperf3')
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

Write-Host 'Looking up the latest iperf3 Windows build...'
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/ar51an/iperf3-win-builds/releases/latest' `
    -Headers @{ 'User-Agent' = 'WifiSurveyor' }

$asset = $release.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1
if (-not $asset) { throw 'No zip asset found on the latest release. Download it by hand instead.' }

$zip = Join-Path $env:TEMP $asset.name
Write-Host "Downloading $($asset.name)..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers @{ 'User-Agent' = 'WifiSurveyor' }

$extract = Join-Path $env:TEMP ('iperf3-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -Path $zip -DestinationPath $extract -Force

# The zip nests the files in a versioned folder; take what we need from wherever it landed.
foreach ($name in 'iperf3.exe', 'cygwin1.dll', 'LICENSE', 'LICENSE.txt', 'COPYING') {
    Get-ChildItem -Path $extract -Filter $name -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 |
        ForEach-Object { Copy-Item $_.FullName -Destination $Destination -Force }
}

Remove-Item $zip, $extract -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path (Join-Path $Destination 'iperf3.exe')) {
    Write-Host "Done. iperf3 is in $Destination and will be copied into the build output."
    if (Test-Path (Join-Path $Destination 'cygwin1.dll')) {
        Write-Host 'Note: this build includes cygwin1.dll (LGPLv3). See README.txt before redistributing.'
    }
    if (-not (Get-ChildItem -Path $Destination -Filter 'LICENSE*')) {
        Write-Warning 'No LICENSE file was found in the archive. Fetch it from https://github.com/esnet/iperf and put it here: the BSD licence requires it to travel with the binary.'
    }
} else {
    Write-Warning 'iperf3.exe was not found in the archive. Download it manually into tools\iperf3.'
}
