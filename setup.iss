; ============================================================
; Inno Setup script for CapTure
; ============================================================
; Produces a standard Windows installer:
;   1. Installs to %ProgramFiles%\CapTure
;   2. Creates Start Menu shortcuts
;   3. Optionally creates a Desktop shortcut
;   4. Registers an uninstaller
;
; Usage:
;   1. Build CapTure.exe first:  pyinstaller capture.spec --clean --noconfirm
;   2. Open this file in Inno Setup Compiler
;   3. Adjust #define paths below if needed
;   4. Compile → produces CapTure_Setup.exe
; ============================================================

#define AppName       "CapTure"
#define AppVersion    "0.1.0"
#define AppPublisher  "CapTure Team"
#define AppURL        "https://github.com/capture-app/capture"
#define AppExeName    "CapTure.exe"
#define OutputBase    "CapTure_Setup"

[Setup]
; NOTE: The AppId must be unique. Generate a new GUID for each release.
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Uncomment the line below and create a LICENSE.txt file if you want a license page:
; LicenseFile=LICENSE.txt
OutputDir=dist
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit only (CapTure targets x64 Windows)
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Require Windows 10+
MinVersion=10.0.19041

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startmenu";   Description: "Create a &Start Menu shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The main executable (build this first!)
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Optional: include documentation
; Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
; Source: "icon.png"; DestDir: "{app}"; Flags: ignoreversion

; Visual C++ Redistributable (optional — include if targeting machines without it)
; Download from https://aka.ms/vs/17/release/vc_redist.x64.exe
; Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not VCInstalled

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Optionally run VC++ redistributable installer
; Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Visual C++ Redistributable..."; Check: not VCInstalled

[Code]
{/* Check if Visual C++ Redistributable is already installed */}
function VCInstalled: Boolean;
var
  RegKey: string;
begin
  Result := False;
  { Check for VC++ 2015-2022 x64 redist }
  RegKey := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  if RegKeyExists(HKEY_LOCAL_MACHINE, RegKey) then
    Result := True
  else
  begin
    RegKey := 'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
    if RegKeyExists(HKEY_LOCAL_MACHINE, RegKey) then
      Result := True;
  end;
end;
