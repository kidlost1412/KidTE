; Số phiên bản do build.ps1 truyền vào (/DAppVersion=1.4.<commit>), lấy từ file VERSION
; + số commit. Không ghi cứng ở đây.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef PackageProfile
  #define PackageProfile "cpu"
#endif
#ifndef ArtifactLabel
  #define ArtifactLabel ""
#endif
#if ArtifactLabel != ""
  #define ArtifactSuffix "-" + ArtifactLabel
#else
  #define ArtifactSuffix ""
#endif
#if PackageProfile != "cpu" && PackageProfile != "gpu"
  #error PackageProfile must be cpu or gpu
#endif
#define AppName "Kid TE"
#define ProjectRoot SourcePath + "..\"

[Setup]
AppId={{8CF58DC0-2347-4A62-BE48-5C1104E6740F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
; MỘT thư mục cho tất cả: người dùng chọn thư mục gốc (mặc định D:\KidTE nếu máy có
; ổ D), bên trong là  App\  (chương trình, thay mới mỗi lần cập nhật) và  DuLieu\
; (khách hàng, trí nhớ AI, video, sao lưu, model chép lời). config._runtime_root tìm
; DuLieu cạnh thư mục App. Chuyển máy / sao lưu tay = chép nguyên thư mục này.
DefaultDirName={code:DefaultRoot}
UsePreviousAppDir=no
AppendDefaultDirName=no
DirExistsWarning=no
DisableDirPage=no
DefaultGroupName=Kid TE
PrivilegesRequired=lowest
OutputDir={#ProjectRoot}PhatHanh
OutputBaseFilename=KidTE-Setup-{#AppVersion}-{#PackageProfile}{#ArtifactSuffix}
Compression=lzma2
; Hai khối nén song song (khoảng bốn lõi), vẫn chừa tài nguyên cho phiên LIVE.
LZMANumBlockThreads=2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\App\KidTE.exe
; Bộ gỡ cài nằm trong App để thư mục gốc chỉ còn App + DuLieu.
UninstallFilesDir={app}\App
SetupIconFile={#ProjectRoot}assets\kid-te.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 tro len. Noi thang thay vi de nguoi dung cai duoc roi hong luc
; chay: ban dong goi khong mang theo cac DLL UCRT (api-ms-win-crt-*,
; ucrtbase.dll) vi tu Windows 10 chung la mot phan cua he dieu hanh. Ung dung
; cung da can WebView2 va faster-whisper/ctranslate2, deu khong nham Win 7/8.1.
MinVersion=10.0

[Languages]
Name: "vi"; MessagesFile: "compiler:Default.isl"

[Messages]
SelectDirLabel3=Kid TE sẽ đặt TOÀN BỘ chương trình và dữ liệu (khách hàng, trí nhớ AI, video, sao lưu, model chép lời) trong thư mục này.
SelectDirBrowseLabel=Nên chọn ổ còn nhiều chỗ trống (model chép lời 0,2–3 GB, video tải lên). Bấm Next để tiếp tục.

[Files]
Source: "{#ProjectRoot}dist\KidTE\*"; DestDir: "{app}\App"; Flags: ignoreversion recursesubdirs createallsubdirs

; Cùng AppId với bản cũ: thay tên chương trình và lối tắt, giữ nguyên DuLieu.
[InstallDelete]
#if PackageProfile == "cpu"
; These files belong to the former bundled runtime, not DuLieu or external packs.
Type: filesandordirs; Name: "{app}\App\_internal\nvidia"
#endif
Type: files; Name: "{app}\App\LiveRe.exe"
Type: files; Name: "{app}\App\LiveReVideo.exe"
Type: files; Name: "{autodesktop}\Live Re.lnk"
Type: files; Name: "{autoprograms}\Live Re.lnk"
Type: files; Name: "{autoprograms}\Live Re - thư mục dữ liệu.lnk"

[Dirs]
Name: "{app}\DuLieu"

[Icons]
Name: "{autoprograms}\Kid TE"; Filename: "{app}\App\KidTELauncher.exe"
Name: "{autoprograms}\Kid TE - thư mục dữ liệu"; Filename: "{app}\DuLieu"
Name: "{autodesktop}\Kid TE"; Filename: "{app}\App\KidTELauncher.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng Kid TE trên Desktop"; GroupDescription: "Lối tắt:"

[Run]
Filename: "{app}\App\KidTELauncher.exe"; Description: "Mở Kid TE"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Luôn xoá chương trình. Dữ liệu chỉ xoá khi người dùng đồng ý (hỏi ở [Code]).
Type: filesandordirs; Name: "{app}\App"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8CF58DC0-2347-4A62-BE48-5C1104E6740F}_is1';

function PreviousRoot(): String;
var
  Path, Candidate: String;
  Depth: Integer;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', Path) then
    Exit;
  Path := RemoveBackslash(Path);
  if Path = '' then
    Exit;
  // Chỉ tin bản ghi cài đặt nếu chương trình thật sự còn ở đó.
  if FileExists(AddBackslash(Path) + 'KidTE.exe') or
     FileExists(AddBackslash(Path) + 'LiveRe.exe') then
  begin
    Result := ExtractFileDir(Path);
    Exit;
  end;
  if FileExists(AddBackslash(Path) + 'App\KidTE.exe') or
     FileExists(AddBackslash(Path) + 'App\LiveRe.exe') then
  begin
    Result := Path;
    Exit;
  end;
  // Registry có thể còn trỏ vào bản thử nghiệm đã bị xoá. Tìm DuLieu thật
  // trong các thư mục cha để cập nhật tại chỗ, không tạo cây cài đặt rỗng.
  Candidate := Path;
  for Depth := 0 to 6 do
  begin
    if Length(Candidate) <= 3 then
      Break;
    if DirExists(AddBackslash(Candidate) + 'DuLieu') then
    begin
      Result := Candidate;
      Exit;
    end;
    Candidate := ExtractFileDir(Candidate);
  end;
end;

function DefaultRoot(Param: String): String;
begin
  Result := PreviousRoot();
  if Result <> '' then
    Exit;
  // Ổ D (nếu có và không phải ổ Windows) để dữ liệu không chiếm ổ hệ thống.
  if DirExists('D:\') and (UpperCase(Copy(ExpandConstant('{win}'), 1, 1)) <> 'D') then
    Result := 'D:\KidTE'
  else
    Result := ExpandConstant('{localappdata}\KidTE');
end;

function CanWrite(Dir: String): Boolean;
var
  Probe: String;
begin
  Result := False;
  if not ForceDirectories(Dir) then
    Exit;
  Probe := AddBackslash(Dir) + '.ghi-thu.tmp';
  if SaveStringToFile(Probe, 'x', False) then
  begin
    DeleteFile(Probe);
    Result := True;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID <> wpSelectDir then
    Exit;
  Dir := WizardDirValue();
  if (Pos(UpperCase(ExpandConstant('{commonpf}')), UpperCase(Dir)) = 1) or
     (Pos(UpperCase(ExpandConstant('{commonpf32}')), UpperCase(Dir)) = 1) then
  begin
    MsgBox('Không cài vào Program Files: Kid TE cần ghi dữ liệu ngay cạnh chương trình. ' +
           'Hãy chọn một thư mục khác, ví dụ D:\KidTE.', mbError, MB_OK);
    Result := False;
  end
  else if not CanWrite(Dir) then
  begin
    MsgBox('Không ghi được vào thư mục này. Hãy chọn thư mục khác.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Data := ExpandConstant('{app}\DuLieu');
  if DirExists(Data) and not UninstallSilent() then
  begin
    if MsgBox('Xoá luôn DỮ LIỆU của Kid TE?' + #13#10 + Data + #13#10#13#10 +
              'Gồm khách hàng, hộp thư, trí nhớ AI, video, bản sao lưu và model chép lời. ' +
              'Không khôi phục lại được.' + #13#10#13#10 +
              'Chọn "No" để giữ lại (cài lại vào đúng thư mục này sẽ dùng tiếp).',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(Data, True, True, True);
  end;
  RemoveDir(ExpandConstant('{app}'));
end;
