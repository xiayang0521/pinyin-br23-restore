; ============================================================================
; BR23 拼音机固件工具 —— NSIS 安装脚本
; ============================================================================
; 与 BR23_setup.iss（Inno Setup）等价的 NSIS 版本，可在 Linux 下用
; makensis 交叉编译 Windows 安装包（Wine 无法运行 32 位的 Inno Setup）。
;
; 设计原则（与 iss 保持一致）：
; 1. 按用户安装到 %LocalAppData%\Programs —— 免 UAC、以正常完整性运行
; 2. 产物来自 PyInstaller onedir 目录 dist\BR23固件工具\*
; 3. 卸载时保留用户数据（backups\ 与 logs\ 在 $INSTDIR 下，删除时目录非空即保留）
; ============================================================================

Unicode true
RequestExecutionLevel user

!define APP_NAME      "BR23 拼音机固件工具"
!define APP_SHORT     "BR23固件工具"
!define APP_VERSION   "1.1.0"
!define APP_PUBLISHER "Pinyin-Machine"
!define APP_EXE       "BR23固件工具.exe"
!define APP_URL       "https://github.com/xiayang0521/pinyin-br23-restore"

Name "${APP_NAME}"
OutFile "Setup-BR23-Firmware-Tool-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_SHORT}"

!include "MUI2.nsh"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "运行 ${APP_NAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ---------------------------------------------------------------------------
Section "主程序" SEC01
  SetOutPath "$INSTDIR"
  ; PyInstaller onedir 产物：exe + _internal\（含 jl-uboot-tool 组件）
  File /r "..\dist\${APP_SHORT}\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
      "NoRepair" 1
SectionEnd

Section /o "桌面快捷方式" SEC02
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
SectionEnd

; ---------------------------------------------------------------------------
Section "Uninstall"
  Delete "$INSTDIR\${APP_EXE}"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\uninstall.exe"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  ; 非递归删目录：用户备份(backups)与日志(logs)会自动保留
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd
