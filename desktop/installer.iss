; Compile with /DPayload=<reviewed payload> /DOutput=<release directory>.
#ifndef Payload
  #error Payload must be explicitly supplied
#endif
#ifndef Output
  #error Output must be explicitly supplied
#endif
#ifndef RuntimePayload
  #define RuntimePayload Payload + "\.jiedu\runtime\python"
#endif
#ifndef ResourceBaseURL
  #error ResourceBaseURL must be explicitly supplied
#endif
#ifndef RuntimeSHA256
  #error RuntimeSHA256 must be explicitly supplied
#endif
#ifndef RuntimeArchiveBytes
  #error RuntimeArchiveBytes must be explicitly supplied
#endif
#ifndef RuntimeInstallBytes
  #error RuntimeInstallBytes must be explicitly supplied
#endif
#define RuntimeAsset "jiedu-python-3.13.16.zip"
#ifndef VectorSHA256
  #error VectorSHA256 must be explicitly supplied
#endif
#ifndef OCRSHA256
  #error OCRSHA256 must be explicitly supplied
#endif
#ifndef VectorArchiveBytes
  #error VectorArchiveBytes must be explicitly supplied
#endif
#ifndef OCRArchiveBytes
  #error OCRArchiveBytes must be explicitly supplied
#endif
#ifndef VectorInstallBytes
  #error VectorInstallBytes must be explicitly supplied
#endif
#ifndef OCRInstallBytes
  #error OCRInstallBytes must be explicitly supplied
#endif
#define VectorAsset "jiedu-bge-m3-5617a9f61b02.zip"
#define OCRAsset "jiedu-ocr-rapidocr-3.9.2.zip"
#define VersionHandle FileOpen(Payload + "\.jiedu\program\VERSION")
#define ReleaseVersion Trim(FileRead(VersionHandle))
#expr FileClose(VersionHandle)
#if Pos("-rc.", ReleaseVersion) > 0
  #define ReleaseFileVersion StringChange(ReleaseVersion, "-rc.", ".")
#else
  #define ReleaseFileVersion ReleaseVersion + ".0"
#endif
[Setup]
AppId={{6DA20431-B9C2-4C8E-A814-86A059B235D1}
AppName=界渡
AppVersion={#ReleaseVersion}
AppVerName=界渡 {#ReleaseVersion}
VersionInfoVersion={#ReleaseFileVersion}
VersionInfoTextVersion={#ReleaseFileVersion}
VersionInfoProductVersion={#ReleaseFileVersion}
VersionInfoProductTextVersion={#ReleaseVersion}
AppPublisher=界渡开源项目
DefaultDirName={code:DefaultInstallDirectory}
DefaultGroupName=界渡
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#Output}
OutputBaseFilename=Jiedu-{#ReleaseVersion}-windows-x64-setup
Compression=lzma2/fast
ArchiveExtraction=full
SolidCompression=no
LZMANumBlockThreads=2
WizardStyle=modern dark
DisableProgramGroupPage=yes
AllowNoIcons=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\jiedu-v1.ico
ChangesAssociations=yes
SetupIconFile={#Payload}\.jiedu\program\desktop\icons\icon.ico
LicenseFile={#Payload}\.jiedu\program\LICENSE
SetupLogging=yes

[Languages]
Name: chinesesimp; MessagesFile: compiler:Languages\ChineseSimplified.isl
Name: english; MessagesFile: compiler:Default.isl

[Files]
; All large assets use the selected installation drive as temporary cache.
Source: "{#ResourceBaseURL}/{#RuntimeAsset}"; DestDir: "{app}\.jiedu\installer-cache"; DestName: "{#RuntimeAsset}"; ExternalSize: {#RuntimeArchiveBytes}; Hash: "{#RuntimeSHA256}"; Flags: external download ignoreversion deleteafterinstall
Source: "{app}\.jiedu\installer-cache\{#RuntimeAsset}"; DestDir: "{app}\.jiedu\runtime\python"; ExternalSize: {#RuntimeInstallBytes}; Hash: "{#RuntimeSHA256}"; Flags: external extractarchive recursesubdirs ignoreversion
; Download to the selected installation drive, verify the pinned archive, then
; extract it. deleteafterinstall removes both ZIPs and empty cache directories.
Source: "{#ResourceBaseURL}/{#VectorAsset}"; DestDir: "{app}\.jiedu\installer-cache"; DestName: "{#VectorAsset}"; ExternalSize: {#VectorArchiveBytes}; Hash: "{#VectorSHA256}"; Flags: external download ignoreversion deleteafterinstall
Source: "{app}\.jiedu\installer-cache\{#VectorAsset}"; DestDir: "{app}\.jiedu\program\resources\models\bge-m3"; ExternalSize: {#VectorInstallBytes}; Hash: "{#VectorSHA256}"; Flags: external extractarchive recursesubdirs ignoreversion
Source: "{#ResourceBaseURL}/{#OCRAsset}"; DestDir: "{app}\.jiedu\installer-cache"; DestName: "{#OCRAsset}"; ExternalSize: {#OCRArchiveBytes}; Hash: "{#OCRSHA256}"; Flags: external download ignoreversion deleteafterinstall
Source: "{app}\.jiedu\installer-cache\{#OCRAsset}"; DestDir: "{app}\.jiedu\program\resources\ocr"; ExternalSize: {#OCRInstallBytes}; Hash: "{#OCRSHA256}"; Flags: external extractarchive recursesubdirs ignoreversion
Source: "{#Payload}\界渡.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\program\desktop\icons\icon.ico"; DestDir: "{app}"; DestName: "jiedu-v1.ico"; Flags: ignoreversion
Source: "{#Payload}\jiedu-install.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\package.json"; DestDir: "{app}\.jiedu"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\install-files.json"; DestDir: "{app}\.jiedu"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\program\*"; DestDir: "{app}\.jiedu\program"; Excludes: "config.json,AGENTS.md,__pycache__,*.pyc,data,knowledge-space"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Payload}\prerequisites\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView

Source: "{#Payload}\ThirdPartyNotices.zip"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Obsolete third-party launchers used absolute build paths; portable .cmd entries replace them.
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\cffi-gen-src.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\dotenv.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\f2py.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\fastapi.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\hf.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\httpx.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\httpx2.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\huggingface-cli.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\idna.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\isympy.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\jsonschema.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\markdown-it.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\mcp.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\numba"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\numpy-config.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\pygmentize.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\pywin32_postinstall.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\pywin32_testall.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\tiny-agents.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\torchfrtrace.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\torchrun.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\tqdm.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\transformers.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\typer.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\uvicorn.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\watchfiles.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\watchmedo.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\websockets.exe"
Type: files; Name: "{app}\.jiedu\runtime\python\Scripts\wheel.exe"

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Icons]
Name: "{group}\界渡"; Filename: "{app}\界渡.exe"; WorkingDir: "{app}"; IconFilename: "{app}\jiedu-v1.ico"; AppUserModelID: "local.jiedu.desktop"
Name: "{autodesktop}\界渡"; Filename: "{app}\界渡.exe"; WorkingDir: "{app}"; IconFilename: "{app}\jiedu-v1.ico"; AppUserModelID: "local.jiedu.desktop"; Tasks: desktopicon
Name: "{group}\卸载界渡"; Filename: "{uninstallexe}"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备桌面界面组件，需要联网…"; Flags: waituntilterminated runhidden; Check: NeedsWebViewAndValidated
Filename: "{app}\界渡.exe"; Description: "启动界渡"; Flags: nowait postinstall skipifsilent; Check: InstallationValidated

[Code]
const InstallIdentity = 'jiedu-consumer-install-v1';
function WindowsDriveType(Root: String): Cardinal;
external 'GetDriveTypeW@kernel32.dll stdcall';

function NeedsWebView: Boolean;
var V: String;
begin
  Result := True;
  if RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) then
    if (V <> '') and (V <> '0.0.0.0') then Result := False;
  if RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) then
    if (V <> '') and (V <> '0.0.0.0') then Result := False;
end;

function DefaultInstallDirectory(Param: String): String;
begin
  if WindowsDriveType('D:\') = 3 then Result := 'D:\Apps\Jiedu'
  else Result := ExpandConstant('{localappdata}\Programs\Jiedu');
end;

function ValidateDestination: String;
var Target, Marker: String; Files: TFindRec; NonEmpty: Boolean;
begin
  Result := '';
  Target := ExpandConstant('{app}');
  if Length(Target) <= 3 then begin Result := '请选择专用安装文件夹，不能选择磁盘根目录。'; Exit; end;
  Marker := AddBackslash(Target) + 'jiedu-install.txt';
  { A manual installer must never overwrite a live or partially installed tree.
    The verified full-update helper moves the previous tree to its rollback
    directory before invoking Setup, so its destination is empty. }
  if FileExists(Marker) or
     FileExists(AddBackslash(Target) + '.jiedu\program\VERSION') or
     FileExists(AddBackslash(Target) + '.jiedu\package.json') or
     FileExists(AddBackslash(Target) + '界渡.exe') then begin
    Result := '此目录已有界渡，不能直接覆盖。请打开原来的界渡，在设置中检查更新。若旧版无法更新，请安装到新的空文件夹，再连接原工作区；保留旧目录，连接和权限需在新版中核对。';
    Exit;
  end;
  NonEmpty := False;
  if FindFirst(AddBackslash(Target) + '*', Files) then begin
    try
      repeat
        if (Files.Name <> '.') and (Files.Name <> '..') then NonEmpty := True;
      until not FindNext(Files);
    finally FindClose(Files); end;
  end;
  if NonEmpty then Result := '此目录已有文件。为保护资料，请选择空文件夹；安装后可在界渡中连接原有知识库。';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var Error: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Error := ValidateDestination;
    if Error <> '' then begin
      Log('Destination rejected: ' + Error);
      { Silent validation is handled by PrepareToInstall, which exits without
        opening a custom dialog or trapping the unattended wizard on this page. }
      if not WizardSilent then begin
        MsgBox(Error, mbError, MB_OK);
        Result := False;
      end;
    end;
  end;
end;

var PreviousProgramVersion: String;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  FreeBytes, TotalBytes, RequiredBytes: Int64;
  TargetDrive, OldVersionFile: String;
  OldVersionContents: AnsiString;
begin
  Result := ValidateDestination;
  if Result <> '' then Exit;
  PreviousProgramVersion := '';
  OldVersionFile := ExpandConstant('{app}\.jiedu\program\VERSION');
  if FileExists(OldVersionFile) and LoadStringFromFile(OldVersionFile, OldVersionContents) then
    PreviousProgramVersion := Trim(OldVersionContents);
  TargetDrive := ExtractFileDrive(ExpandConstant('{app}')) + '\';
  RequiredBytes := {#RuntimeArchiveBytes} + {#VectorArchiveBytes} + {#OCRArchiveBytes} +
    {#RuntimeInstallBytes} + {#VectorInstallBytes} + {#OCRInstallBytes} + 536870912;
  if not GetSpaceOnDisk64(TargetDrive, FreeBytes, TotalBytes) then begin
    Result := '无法读取所选安装盘的可用空间。'; Exit;
  end;
  if FreeBytes < RequiredBytes then begin
    Result := '所选安装盘空间不足；需要同时容纳程序、临时下载包与离线资源。'; Exit;
  end;
  if not GetSpaceOnDisk64(GetTempDir, FreeBytes, TotalBytes) or (FreeBytes < 268435456) then
    Result := '系统临时盘空间不足，无法安全启动安装器。';
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := '安装界渡桌面程序与独立运行环境，无需预先安装 Python 或 Git。' + #13#10#13#10 +
    '知识库由你在首次启动时选择。升级和卸载保留已有知识、原始档案、配置与同步进度。' + #13#10#13#10 +
    '安装时需联网自动下载并校验向量模型与 OCR；临时下载放在你选定的安装盘，完成后清理。安装完成后两项可离线使用。管家生成模型不附带。';
end;

var
  InstallAttempted, InstallValidated: Boolean;

function InstallationValidated: Boolean;
begin
  Result := InstallValidated;
end;

function NeedsWebViewAndValidated: Boolean;
begin
  Result := InstallValidated and NeedsWebView;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ProgramDir, PythonExe, Arguments: String;
  ResultCode: Integer;
begin
  if CurStep = ssInstall then InstallAttempted := True;
  if CurStep = ssPostInstall then begin
    ProgramDir := ExpandConstant('{app}\.jiedu\program');
    PythonExe := ExpandConstant('{app}\.jiedu\runtime\python\python.exe');
    Arguments := '-I -B -m knowledge_bridge.full_installer_update verify-installation --program "' + ProgramDir + '"';
    InstallValidated := Exec(PythonExe, Arguments, ProgramDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
    if InstallValidated then begin
      Arguments := '-I -B -m knowledge_bridge.update_notice record-install --program "' + ProgramDir + '"';
      if PreviousProgramVersion <> '' then
        Arguments := Arguments + ' --previous "' + PreviousProgramVersion + '"';
      if not (Exec(PythonExe, Arguments, ProgramDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0)) then
        Log('Installation succeeded, but the update notice state could not be recorded; the app will show an unconfirmed notice status.');
    end;
    if not InstallValidated then Log('Required offline resources failed final file-by-file validation; Setup exit code will be nonzero.');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and InstallAttempted and not InstallValidated then begin
    WizardForm.FinishedHeadingLabel.Caption := '安装未完成';
    WizardForm.FinishedLabel.Caption := '向量模型或 OCR 资源没有通过完整校验。界渡不会启动。请重新运行安装器并重试下载。';
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if InstallAttempted and not InstallValidated then Result := 7
  else Result := 0;
end;
