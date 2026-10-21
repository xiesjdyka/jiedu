param([Parameter(Mandatory=$true)][string]$BuildTools,[switch]$RefreshLock)
$ErrorActionPreference='Stop'
$BuildTools=(Resolve-Path -LiteralPath $BuildTools).Path
$vswhere='C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$vs=& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) {throw 'Visual C++ build tools are required.'}
$vc=(Get-ChildItem -LiteralPath "$vs\VC\Tools\MSVC" -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName
$sdk="$BuildTools\microsoft.windows.sdk.cpp\c"
$sdk64="$BuildTools\microsoft.windows.sdk.cpp.x64\c"
$sdkVersion=(Get-ChildItem -LiteralPath "$sdk\bin" -Directory | Where-Object Name -Match '^10\.' | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1).Name
$env:LIB="$vc\lib\x64;$sdk64\um\x64;$sdk64\ucrt\x64"
$env:INCLUDE="$vc\include;$sdk\Include\$sdkVersion\ucrt;$sdk\Include\$sdkVersion\shared;$sdk\Include\$sdkVersion\um;$sdk\Include\$sdkVersion\winrt"
$env:CARGO_HOME="$BuildTools\cargo"; $env:RUSTUP_HOME="$BuildTools\rustup"
$env:CARGO_TARGET_DIR="$BuildTools\target"; $env:TEMP="$BuildTools\tmp"; $env:TMP=$env:TEMP
$env:PATH="$BuildTools\cargo\bin;$vc\bin\Hostx64\x64;$sdk\bin\$sdkVersion\x64;"+$env:PATH
$env:RC="$sdk\bin\$sdkVersion\x64\rc.exe"
Push-Location (Join-Path $PSScriptRoot '..\desktop')
try {
    if($RefreshLock){& "$BuildTools\cargo\bin\cargo.exe" generate-lockfile; if($LASTEXITCODE -ne 0){throw 'Dependency resolution failed'}}
    & "$BuildTools\cargo\bin\cargo.exe" build --release --locked; if($LASTEXITCODE -ne 0){throw 'Desktop build failed'}
}
finally {Pop-Location}
