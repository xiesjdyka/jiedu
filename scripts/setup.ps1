param([string]$PythonExecutable='python',[string]$CacheDirectory)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(-not $CacheDirectory){$CacheDirectory=Join-Path $root '.setup-cache'}
$CacheDirectory=[IO.Path]::GetFullPath($CacheDirectory)
New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null
$oldTemp=$env:TEMP;$oldTmp=$env:TMP
try {
    $env:TEMP=Join-Path $CacheDirectory 'temp';$env:TMP=$env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    & $PythonExecutable -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"; print(sys.version.split()[0])'
    if($LASTEXITCODE -ne 0){throw 'Python is unavailable or too old.'}
    $venv=Join-Path $root '.venv312'
    $python=Join-Path $venv 'Scripts/python.exe'
    if(-not(Test-Path -LiteralPath $python)){
        & $PythonExecutable -m venv $venv
        if($LASTEXITCODE -ne 0){throw 'Virtual environment creation failed.'}
    }
    & $python -m pip --isolated --cache-dir $CacheDirectory install --no-deps --index-url https://pypi.org/simple -r (Join-Path $root 'requirements.lock.txt')
    if($LASTEXITCODE -ne 0){throw 'Dependency installation failed. Existing workspace and configuration were not changed.'}
    & $python -m pip --isolated --cache-dir $CacheDirectory install --index-url https://pypi.org/simple --no-deps -e $root
    if($LASTEXITCODE -ne 0){throw 'Source registration failed.'}
    & $python -m pip check
    if($LASTEXITCODE -ne 0){throw 'Dependency validation failed.'}
    & $python (Join-Path $PSScriptRoot 'check_environment.py')
    if($LASTEXITCODE -ne 0){throw 'Installed versions or source revisions differ from the release lock.'}
    $config=Join-Path $root 'config.json'
    if(-not(Test-Path -LiteralPath $config)){Copy-Item -LiteralPath (Join-Path $root 'config.example') -Destination $config}
    Write-Host 'Installation complete. Start with start.bat; initialize a new empty workspace before connecting your own data.'
    Write-Host 'Models and optional OCR resources are not installed by this script.'
} finally {$env:TEMP=$oldTemp;$env:TMP=$oldTmp}


