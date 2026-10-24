param(
    [ValidateSet('start','stop','restart','mcp','locate','resolve')][string]$Action='start',
    [string]$Root,
    [string]$Registry=(Join-Path $env:LOCALAPPDATA 'Jiedu\installation.json')
)
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
function Read-Installation([string]$Path) {
    $resolved=[IO.Path]::GetFullPath($Path)
    $package=Join-Path $resolved '.jiedu\package.json'
    $manifest=Join-Path $resolved '.jiedu\workspace.json'
    if(-not (Test-Path -LiteralPath $package -PathType Leaf) -or -not (Test-Path -LiteralPath $manifest -PathType Leaf)){return $null}
    $layout=Get-Content -LiteralPath $package -Raw -Encoding UTF8 | ConvertFrom-Json
    $workspace=Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if($layout.layout -ne 'jiedu-self-contained-v1' -or $layout.program -ne 'program' -or -not $workspace.workspace_id){return $null}
    return @{root=$resolved;workspace_id=[string]$workspace.workspace_id}
}
function Save-Location($Installation) {
    $parent=Split-Path -Parent $Registry
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $record=@{version=1;workspace_id=$Installation.workspace_id;active_root=$Installation.root;search_parent=(Split-Path -Parent $Installation.root)}
    $temporary=$Registry+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($record|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Registry -Force
}
if($Action -eq 'locate') {
    if(-not $Root) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker=[Windows.Forms.FolderBrowserDialog]::new()
        $picker.Description='选择包含 .jiedu 的界渡文件夹'
        if($picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK){exit 0}
        $Root=$picker.SelectedPath
        $picker.Dispose()
    }
    $installation=Read-Installation $Root
    if(-not $installation){throw '所选目录不是完整的界渡实例。'}
    Save-Location $installation
    exit 0
}
if(-not (Test-Path -LiteralPath $Registry)){throw '请先使用“重新定位界渡”选择独立文件夹。'}
$saved=Get-Content -LiteralPath $Registry -Raw -Encoding UTF8 | ConvertFrom-Json
$installation=Read-Installation $saved.active_root
if($installation -and $installation.workspace_id -ne $saved.workspace_id){$installation=$null}
if(-not $installation) {
    $matches=@()
    # Search one recorded parent only; never recurse through user data or drives.
    if(Test-Path -LiteralPath $saved.search_parent -PathType Container) {
        foreach($folder in Get-ChildItem -LiteralPath $saved.search_parent -Directory -Force) {
            if($folder.Attributes -band [IO.FileAttributes]::ReparsePoint){continue}
            $candidate=Read-Installation $folder.FullName
            if($candidate -and $candidate.workspace_id -eq $saved.workspace_id){$matches+=,$candidate}
        }
    }
    if($matches.Count -eq 0){throw '界渡已移到其它目录，请重新选择位置。'}
    if($matches.Count -gt 1){throw '发现多个相同身份的界渡副本，请明确选择活动位置。'}
    $installation=$matches[0]
    Save-Location $installation
}
if($Action -eq 'resolve'){$installation.root;exit 0}
$control=Join-Path $installation.root '.jiedu'
$program=Join-Path $control 'program'
$python=Join-Path $control 'runtime\python\python.exe'
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw '界渡内置 Python 缺失。'}
# Old client environment entries must not redirect this explicit installation.
$env:KNOWLEDGE_BRIDGE_VAULT=$installation.root
$env:KNOWLEDGE_BRIDGE_LOCAL_DATA=Join-Path $control 'control'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
Push-Location -LiteralPath $program
try {
    if($Action -eq 'mcp'){& $python -I -X utf8 -m knowledge_bridge.cli mcp --transport stdio}
    else{& $python -I -X utf8 -m knowledge_bridge.desktop_launcher $Action}
    $result=$LASTEXITCODE
} finally {Pop-Location}
exit $result
