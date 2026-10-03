# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec —— BR23 拼音机固件备份/恢复工具

关键点（来自实战经验）：
1. onedir 模式（不是 onefile）：启动快、无需解压到临时目录、不会被安全软件误杀
2. console=True：这是命令行交互程序，必须有控制台才能输入菜单选择
3. jl-uboot-tool 需要**明文落盘**（scsiio/jltech 是动态 import，
   data/ 下的 yaml 和 loaderblobs 是运行期读取），用 Tree 收进 _internal
4. 固件包里的 jl_isd.bin 也要带上，用于固件身份鉴定
"""
import os
from pathlib import Path

from PyInstaller.building.datastruct import Tree

# SPECPATH 由 PyInstaller 注入，spec 自己所在目录
ROOT = Path(SPECPATH).resolve()
# 仓库内布局：依赖 jl-uboot-tool 与 spec 同目录，可独立重新打包
TOOL_SRC = ROOT / "jl-uboot-tool"
# 固件身份鉴定为可选增强；仓库不随附第三方固件，缺失时自动跳过
FW_SRC = ROOT / "固件包_ebook_V0.6.3"

print("spec root :", ROOT)
print("tool src  :", TOOL_SRC, "exists" if TOOL_SRC.is_dir() else "MISSING")
print("fw src    :", FW_SRC, "exists" if FW_SRC.is_dir() else "MISSING")

# jl-uboot-tool 明文落盘：scsiio/ jltech/ 是动态 import 的包，
# data/ 下的 yaml 与 loaderblobs 是运行期读取。
# 注意 Tree 的元组顺序是 (源, 目标, 类型) —— 写反会在 COLLECT 期报
# "attempting to store file outside of the dist directory"。
trees = []
if TOOL_SRC.is_dir():
    for sub in ("scsiio", "jltech"):
        d = TOOL_SRC / sub
        if d.is_dir():
            trees.append(Tree(str(d), prefix=f"jl-uboot-tool/{sub}"))
    data = TOOL_SRC / "data"
    if data.is_dir():
        trees.append(Tree(str(data), prefix="jl-uboot-tool/data"))

# 固件包：只收 jl_isd.bin（固件鉴定用），不收整个包（几百 MB）
fw_datas = []
if FW_SRC.is_dir():
    for f in FW_SRC.rglob("jl_isd.bin"):
        rel = f.relative_to(FW_SRC)
        fw_datas.append((str(f), f"固件包_ebook_V0.6.3/{rel.as_posix()}"))

datas = trees + fw_datas
print("trees     :", len(trees))
print("fw datas  :", len(fw_datas))
for s, d in fw_datas:
    print("   DATA ->", d)

a = Analysis(
    ["br23_tool.py"],
    # pathex 必须包含 jl-uboot-tool，否则 scsiio/jltech 无法被静态分析到
    pathex=[str(ROOT), str(TOOL_SRC)],
    binaries=[],
    datas=fw_datas,          # 只能是 (src, dest) 二元组
    hiddenimports=[
        # scsiio 内部按平台选实现，PyInstaller 静态分析看不到
        "scsiio",
        "scsiio.win32port",
        "scsiio.common",
        "jltech.uboot",
        "jltech.crc",
        "jltech.cipher",
        "crcmod",       # jltech/crc.py 依赖
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除用不到的大包，显著减小体积
        "numpy", "scipy", "pandas", "matplotlib", "PIL", "PyQt5", "PyQt6",
        "PySide2", "PySide6", "tkinter", "unittest", "pydoc_data",
        "test", "lib2to3", "distutils", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BR23固件工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=True：菜单交互必须有控制台
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # scsiio/ jltech/ data/ 整树明文落盘。
    # Analysis 没有 trees 参数（会被 _kwargs 静默吞掉），Tree 必须直接给 COLLECT。
    *trees,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BR23固件工具",
)
