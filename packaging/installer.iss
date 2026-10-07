; Inno Setup 安裝檔設定。由 build.ps1 呼叫：ISCC /DAppVersion=x.y.z packaging\installer.iss
; 本檔含中文，必須存成「UTF-8 含 BOM」，否則 ISCC 會用系統字碼頁讀成亂碼。
#define AppName "AI 工廠"
#define AppExe "AIFactory.exe"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{0FC20121-3FB0-4245-AC46-88339E175F50}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=deven951130
AppPublisherURL=https://github.com/deven951130/ai-factory
; 裝在使用者自己的資料夾，不需要系統管理員權限
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\AI Factory
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=AIFactory-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 程式還開著時，安裝 / 解除安裝會先請使用者關閉
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\AIFactory\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 更新版本時清掉舊版留下的程式檔（使用者資料在 %LOCALAPPDATA%\AIFactory，不受影響）
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// 解除安裝時詢問是否一併刪除帳號登入與設定；選「是」之後重新安裝就是全新狀態。
// .claude-accounts 是 0.1.x 版放 Claude 帳號登入的資料夾（新版放在 AIFactory\data\accounts）。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, LegacyDir: String;
begin
  if (CurUninstallStep <> usPostUninstall) or UninstallSilent then
    Exit;
  DataDir := ExpandConstant('{localappdata}\AIFactory');
  LegacyDir := ExpandConstant('{%USERPROFILE}\.claude-accounts');
  if MsgBox('要一併刪除 AI 工廠的帳號登入與所有設定嗎？' + #13#10#13#10 +
            '是：刪除所有 AI 帳號的登入資訊與金鑰、AI 節點、生產線與紀錄，下次安裝就是全新狀態。' + #13#10 +
            '否：保留，重新安裝後可以直接使用。' + #13#10#13#10 +
            '會刪除的資料夾：' + #13#10 + DataDir + #13#10 + LegacyDir,
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(DataDir, True, True, True);
    DelTree(LegacyDir, True, True, True);
  end;
end;
