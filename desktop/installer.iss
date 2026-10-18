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
Source: "{#Payload}\界渡.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\program\desktop\icons\icon.ico"; DestDir: "{app}"; DestName: "jiedu-v1.ico"; Flags: ignoreversion
Source: "{#Payload}\jiedu-install.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\package.json"; DestDir: "{app}\.jiedu"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\install-files.json"; DestDir: "{app}\.jiedu"; Flags: ignoreversion
Source: "{#Payload}\.jiedu\program\*"; DestDir: "{app}\.jiedu\program"; Excludes: "config.json,AGENTS.md,__pycache__,*.pyc,data,knowledge-space"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RuntimePayload}\*"; DestDir: "{app}\.jiedu\runtime\python"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
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
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备桌面界面组件，需要联网…"; Flags: waituntilterminated runhidden; Check: NeedsWebView
Filename: "{app}\界渡.exe"; Description: "启动界渡"; Flags: nowait postinstall skipifsilent

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
var Target, Marker: String; Contents: AnsiString; Files: TFindRec; NonEmpty: Boolean;
begin
  Result := '';
  Target := ExpandConstant('{app}');
  if Length(Target) <= 3 then begin Result := '请选择专用安装文件夹，不能选择磁盘根目录。'; Exit; end;
  Marker := AddBackslash(Target) + 'jiedu-install.txt';
  if FileExists(Marker) then begin
    if not LoadStringFromFile(Marker, Contents) or (Trim(Contents) <> InstallIdentity) then
      Result := '此目录不是可识别的界渡安装目录，请选择一个空文件夹。';
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

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := ValidateDestination;
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := '安装界渡桌面程序与独立运行环境，无需预先安装 Python 或 Git。' + #13#10#13#10 +
    '知识库由你在首次启动时选择。升级和卸载保留已有知识、原始档案、配置与同步进度。' + #13#10#13#10 +
    '桌面界面需要 Microsoft Edge WebView2。OCR 与本地 AI 模型属于可选资源，使用时再准备。';
end;
