; ============================================================================
; BR23 拼音机固件工具 —— Inno Setup 安装脚本
; ============================================================================
; 设计原则（来自实战经验）：
; 1. 按用户安装到 {localappdata}\Programs —— 免 UAC、绕安全软件降权沙箱，
;    进程以正常完整性（S-1-16-12288）运行，PyInstaller 的临时目录不会写炸
; 2. 冻结版产物来自 PyInstaller onedir 目录 dist\BR23固件工具\*
; 3. AppId 固定不变，升级/卸载识别靠它，切勿改动
; 4. 卸载器（unins000.exe/dat）由 ISCC 自动生成，卸载后可重装
; ============================================================================

#define AppName        "BR23 拼音机固件工具"
#define AppNameShort   "BR23固件工具"
#define AppVersion     "1.0.0"
#define AppPublisher   "Pinyin-Machine"
#define AppExe         "BR23固件工具.exe"
#define AppIcon        ""

; ---------------------------------------------------------------------------
; ISCC 编译参数注入：
;   ISCC.exe BR23_setup.iss /DAppVer=1.0.0 /DSourceRoot=<源码根> /DOutDir=<输出>
; ---------------------------------------------------------------------------
#ifndef AppVer
#define AppVer "1.0.0"
#endif
#ifndef SourceRoot
#define SourceRoot ".."
#endif
#ifndef OutDir
#define OutDir "output"
#endif

[Setup]
; --- 元信息 ---
AppId={{A8F3E2C1-4D5B-6E78-9F0A-1B2C3D4E5F60}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/xiayang0521/pinyin-br23-restore
AppSupportURL=https://github.com/xiayang0521/pinyin-br23-restore/issues
; --- 安装路径（按用户，免 UAC）---
DefaultDirName={localappdata}\Programs\{#AppNameShort}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; --- 安装器基本信息 ---
OutputBaseFilename=Setup-{#AppNameShort}-{#AppVer}
OutputDir={#OutDir}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; --- 架构：win10+ x64 ---
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
DisableProgramGroupPage=yes
; --- 许可（可选；没就去掉这两行）---
; LicenseFile=..\LICENSE

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller onedir 产物：exe + _internal\
; Source 通配符覆盖所有子目录（_internal\crcmod、_internal\jl-uboot-tool 等）
Source: "{#SourceRoot}\dist\BR23固件工具\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion onlyifdoesntexist

; 补充：源码目录下的 jl_isd.bin（若存在）和使用说明——方便用户刷机直接用
Source: "{#SourceRoot}\使用说明*.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SourceRoot}\jl_isd.bin"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SourceRoot}\FONT16.FNT"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 安装完成后给个打开主程序的勾选项
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理用户级配置（如果后续有配置文件）
; Type: filesandordirs; Name: "{userappdata}\{#AppNameShort}"
