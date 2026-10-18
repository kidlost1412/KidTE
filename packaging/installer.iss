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
; Không để Restart Manager tự đóng KidTELauncher.exe: Job Object sẽ giết luôn lõi
; giữa phiên LIVE. [Code] kiểm mutex KidTE.Running và hỏi người dùng tự đóng.
CloseApplications=no

[Languages]
Name: "vi"; MessagesFile: "compiler:Default.isl"

[Messages]
SelectDirLabel3=Kid TE sẽ đặt TOÀN BỘ chương trình và dữ liệu (khách hàng, trí nhớ AI, video, sao lưu, model chép lời) trong thư mục này.
SelectDirBrowseLabel=Nên chọn ổ còn nhiều chỗ trống (model chép lời 0,2–3 GB, video tải lên). Bấm Next để tiếp tục.

[Files]
; Extracted only for the setup-time WebView2 prerequisite check; not installed with the app.
Source: "{#ProjectRoot}packaging\ensure-webview2.ps1"; Flags: dontcopy
; Merged install tree = runtime (KidTE.exe, _internal) + app (backend, plugins, docs, VERSION) +
; extensions (extension, extension-shopee) + KidTELauncher.exe. The signed bootstrap-request.json lists
; runtime/app/extensions, so the launcher registers them as the first generation (no download).
Source: "{#ProjectRoot}dist\KidTE\*"; DestDir: "{app}\App"; Flags: ignoreversion recursesubdirs createallsubdirs

; Cùng AppId với bản cũ: thay tên chương trình và lối tắt, giữ nguyên DuLieu.
[InstallDelete]
; Legacy optional payloads are replaced by versioned packages. DuLieu and
; App/packages (including the installed GPU pack) are preserved.
; Split layout: Kid TE code now lives in App\backend (the app package); the monolithic
; build froze it inside _internal\backend.
Type: filesandordirs; Name: "{app}\App\_internal\backend"
Type: filesandordirs; Name: "{app}\App\Captcha"
Type: filesandordirs; Name: "{app}\App\tools"
Type: files; Name: "{app}\App\KidTEVideo.exe"
Type: filesandordirs; Name: "{app}\App\_internal\faster_whisper"
Type: filesandordirs; Name: "{app}\App\_internal\ctranslate2"
Type: filesandordirs; Name: "{app}\App\_internal\onnxruntime"
Type: filesandordirs; Name: "{app}\App\_internal\av"
Type: filesandordirs; Name: "{app}\App\_internal\av.libs"
Type: filesandordirs; Name: "{app}\App\_internal\tokenizers"
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
  // Tạo bởi KidTELauncher.exe và KidTE.exe (instance_lock.RUNNING_MUTEX).
  RunningMutex = 'KidTE.Running.8CF58DC0';
  MinFreeBytes = 10737418240; // 10 GB: gói tự tải + model chép lời + video

var
  FreshInstall: Boolean;

// C9: không bao giờ đóng/giết Kid TE thay người dùng (có thể đang LIVE).
// Trả True khi Kid TE đã tắt; False khi người dùng chọn huỷ.
function WaitForKidTEClosed(Action: String): Boolean;
begin
  Result := True;
  while CheckForMutexes(RunningMutex) do
  begin
    if SuppressibleMsgBox('Kid TE đang chạy.' + #13#10#13#10 +
        'Nếu đang LIVE, hãy đợi hết phiên LIVE. Sau đó đóng cửa sổ Kid TE ' +
        '(nếu không thấy cửa sổ: mở Kid TE rồi đóng, hoặc khởi động lại máy).' + #13#10#13#10 +
        'Bấm Retry khi đã đóng Kid TE để ' + Action + '; bấm Cancel để dừng.',
        mbInformation, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function WebView2FailureText(Probe: String): String;
begin
  if Pos('FAIL|download|', Probe) = 1 then
    Result := 'Không tải được WebView2: máy đang không có mạng hoặc mạng chặn trang của Microsoft.'
  else if Pos('FAIL|signature|', Probe) = 1 then
    Result := 'Tệp WebView2 tải về không có chữ ký hợp lệ của Microsoft nên đã bị bỏ để an toàn.'
  else if (Pos('FAIL|launch|', Probe) = 1) or (Pos('FAIL|verification|', Probe) = 1) then
    Result := 'Bộ cài WebView2 của Microsoft đã chạy nhưng chưa cài xong (có thể bị phần mềm diệt virus chặn).'
  else if Pos('FAIL|detection|', Probe) = 1 then
    Result := 'Không kiểm tra được WebView2 trên máy này.'
  else
    Result := 'Chưa xác nhận được WebView2 đã cài.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ScriptPath, ResultFile, PowerShellPath, Parameters: String;
  ProbeResult: AnsiString;
  ExecOk: Boolean;
  ExitCode: Integer;
begin
  Result := '';
  NeedsRestart := False;
  if not WaitForKidTEClosed('tiếp tục cài') then
  begin
    Result := 'Đã dừng cài đặt vì Kid TE vẫn đang chạy. Dữ liệu không bị thay đổi; hãy chạy lại bộ cài khi đã đóng Kid TE.';
    Exit;
  end;
  FreshInstall := not FileExists(ExpandConstant('{app}\App\KidTELauncher.exe')) and
                  not DirExists(ExpandConstant('{app}\DuLieu'));
  Log('Checking for the Microsoft WebView2 Runtime prerequisite.');

  ExtractTemporaryFile('ensure-webview2.ps1');
  ScriptPath := ExpandConstant('{tmp}\ensure-webview2.ps1');
  ResultFile := ExpandConstant('{tmp}\kidte-webview2-result.txt');
  DeleteFile(ResultFile);
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    AddQuotes(ScriptPath) + ' -ResultFile ' + AddQuotes(ResultFile);

  ExecOk := Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  ProbeResult := '';
  if FileExists(ResultFile) then
    LoadStringFromFile(ResultFile, ProbeResult);
  DeleteFile(ResultFile);

  if ExecOk and (ExitCode = 0) and (Pos('OK|', ProbeResult) = 1) then
  begin
    Log('WebView2 prerequisite: ' + ProbeResult);
    Result := '';
    Exit;
  end;

  if not ExecOk then
    ProbeResult := 'FAIL|launch|Windows PowerShell could not start.'
  else if ProbeResult = '' then
    ProbeResult := 'FAIL|setup|The prerequisite helper returned no result (exit code ' + IntToStr(ExitCode) + ').'
  else if ExitCode <> 0 then
    Log('WebView2 prerequisite helper exit code: ' + IntToStr(ExitCode));

  // Chuỗi thô (FAIL|stage|...) chỉ ghi vào nhật ký cài đặt, không hiện cho khách (C12).
  Log('WebView2 prerequisite failed: ' + ProbeResult);
  if SuppressibleMsgBox('Kid TE dùng Microsoft Edge WebView2 để mở cửa sổ ứng dụng.' + #13#10#13#10 +
      WebView2FailureText(ProbeResult) + #13#10#13#10 +
      'Bạn vẫn có thể cài tiếp: khi chưa có WebView2, Kid TE sẽ mở giao diện bằng trình duyệt ' +
      'và nhắc cách cài. Để cài WebView2 sau (khi có mạng), tải tại:' + #13#10 +
      'https://go.microsoft.com/fwlink/p/?LinkId=2124703' + #13#10 +
      'Máy không có mạng: cài bản "Evergreen Standalone Installer x64" chép từ máy khác, tại:' + #13#10 +
      'https://developer.microsoft.com/microsoft-edge/webview2/' + #13#10#13#10 +
      'Bấm Yes để cài tiếp Kid TE, No để dừng.',
      mbConfirmation, MB_YESNO, IDYES) = IDYES then
    Result := ''
  else
    Result := 'Đã dừng cài đặt. Hãy cài Microsoft Edge WebView2 Runtime rồi chạy lại bộ cài Kid TE.';
end;

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

function GetDriveType(lpRootPathName: String): UINT;
external 'GetDriveTypeW@kernel32.dll stdcall';

function DefaultRoot(Param: String): String;
begin
  Result := PreviousRoot();
  if Result <> '' then
    Exit;
  // Chỉ mặc định D: khi đó là ổ cố định, tránh chọn CD/DVD hoặc ổ rời.
  if (GetDriveType('D:\') = 3) and
     (UpperCase(Copy(ExpandConstant('{win}'), 1, 1)) <> 'D') then
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

function StartsWithDir(Dir, Prefix: String): Boolean;
begin
  Prefix := RemoveBackslash(Prefix);
  Result := (Prefix <> '') and
            ((CompareText(Dir, Prefix) = 0) or
             (Pos(UpperCase(AddBackslash(Prefix)), UpperCase(AddBackslash(Dir))) = 1));
end;

// C8: thư mục rủi ro cho SQLite + video: đồng bộ đám mây, ổ mạng, ổ tháo rời.
function RiskyDirReason(Dir: String): String;
var
  Drive: String;
  Kind: UINT;
begin
  Result := '';
  if Copy(Dir, 1, 2) = '\\' then
  begin
    Result := 'Đây là thư mục mạng (\\máy\thư mục). Mất mạng giữa LIVE sẽ làm hỏng dữ liệu.';
    Exit;
  end;
  if (Pos('\ONEDRIVE', UpperCase(Dir)) > 0) or
     StartsWithDir(Dir, GetEnv('OneDrive')) or
     StartsWithDir(Dir, GetEnv('OneDriveConsumer')) or
     StartsWithDir(Dir, GetEnv('OneDriveCommercial')) then
  begin
    Result := 'Thư mục này nằm trong OneDrive. OneDrive đồng bộ/khoá tệp liên tục có thể làm hỏng ' +
              'cơ sở dữ liệu khách hàng và chiếm hàng chục GB video.';
    Exit;
  end;
  if StartsWithDir(Dir, ExpandConstant('{userdesktop}')) or
     StartsWithDir(Dir, ExpandConstant('{userdocs}')) then
  begin
    Result := 'Desktop/Documents thường được OneDrive tự đồng bộ, dễ làm hỏng cơ sở dữ liệu và ' +
              'đầy dung lượng đám mây.';
    Exit;
  end;
  Drive := AddBackslash(ExtractFileDrive(Dir));
  Kind := GetDriveType(Drive);
  if Kind = 4 then
    Result := 'Ổ ' + Drive + ' là ổ mạng. Mất mạng giữa LIVE sẽ làm hỏng dữ liệu.'
  else if Kind = 2 then
    Result := 'Ổ ' + Drive + ' là ổ tháo rời (USB/thẻ nhớ). Rút ra khi đang chạy sẽ làm hỏng dữ liệu.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir, Reason: String;
  FreeBytes, TotalBytes: Int64;
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
    Exit;
  end;
  Reason := RiskyDirReason(Dir);
  if (Reason <> '') and
     (SuppressibleMsgBox(Reason + #13#10#13#10 +
        'Nên chọn thư mục trên ổ cứng của máy, ví dụ D:\KidTE.' + #13#10#13#10 +
        'Bấm Yes để vẫn cài vào đây, No để chọn thư mục khác.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDYES) <> IDYES) then
  begin
    Result := False;
    Exit;
  end;
  if GetSpaceOnDisk64(AddBackslash(ExtractFileDrive(Dir)), FreeBytes, TotalBytes) and
     (FreeBytes < MinFreeBytes) and
     (SuppressibleMsgBox('Ổ ' + ExtractFileDrive(Dir) + ' chỉ còn khoảng ' +
        IntToStr(FreeBytes div 1073741824) + ' GB trống.' + #13#10#13#10 +
        'Kid TE cần thêm khoảng 10 GB cho các gói tự tải, model chép lời và video. ' +
        'Ổ đầy giữa LIVE có thể làm hỏng dữ liệu.' + #13#10#13#10 +
        'Bấm Yes để vẫn cài vào đây, No để chọn ổ khác.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDYES) <> IDYES) then
  begin
    Result := False;
    Exit;
  end;
  if not CanWrite(Dir) then
  begin
    MsgBox('Không ghi được vào thư mục này. Hãy chọn thư mục khác.', mbError, MB_OK);
    Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := WaitForKidTEClosed('tiếp tục gỡ cài đặt');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and FreshInstall then
    SaveStringToFile(ExpandConstant('{app}\App\initial-components.json'),
      '{"schema_version":1,"status":"requested"}', False);
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
