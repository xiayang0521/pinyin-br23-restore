#!/usr/bin/env python3
"""
一键恢复原厂固件（杰理 AC6956C8 / BR23 拼音机）

用法:
    python restore_factory.py [备份文件路径]

不传备份文件路径时，默认使用上级目录的 factory_backup.bin。
脚本会自动等待并选择 BR23 UBOOT1.00 设备（两个 LUN 时取第一个），
自动上传 loader，整片擦写 Flash，然后复位重启。
"""

import subprocess
import sys
import time
from pathlib import Path

script_dir = Path(__file__).resolve().parent

# 默认备份文件：救砖目录下的 factory_backup.bin
DEFAULT_BACKUP = script_dir.parent / "factory_backup.bin"

EXPECTED_SIZE = 1024 * 1024  # 1MB，AC6956C8 整片内置 NOR Flash


def main():
    backup = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_BACKUP

    print("=" * 56)
    print(" 杰理拼音机原厂固件恢复工具 (BR23 / AC6956C8)")
    print("=" * 56)

    # 1) 检查备份文件
    if not backup.is_file():
        print(f"[错误] 找不到备份文件: {backup}")
        return 1

    size = backup.stat().st_size
    print(f"备份文件: {backup}")
    print(f"文件大小: {size} 字节 ({size / 1024 / 1024:.2f} MB)")

    if size != EXPECTED_SIZE:
        print(f"[警告] 文件不是标准的 1MB (1048576 字节)，请确认文件正确！")
        answer = input("仍然继续？输入 yes 继续，其他键退出: ").strip().lower()
        if answer != "yes":
            return 1

    # 2) 查找 UBOOT 设备
    sys.path.insert(0, str(script_dir))
    from jldevfind import find_jl_devices

    print()
    print("等待 BR23 UBOOT1.00 设备接入...")
    print("(请先用 SuperMini 救砖器让拼音机进入下载模式，并拆掉 GPIO4/GPIO5)")

    devpath = None
    for attempt in range(1, 181):  # 最多等待 3 分钟
        devs = find_jl_devices()
        if devs:
            # 优先选 UBOOT1.00；两个 LUN 时取第一个
            devpath = devs[0]["path"]
            print(f"\n已找到设备: {devs[0]['name']}")
            break
        print(f"\r尚未发现设备，已等待 {attempt} 秒...", end="", flush=True)
        time.sleep(1)

    if devpath is None:
        print("\n[错误] 等待超时，未发现 UBOOT 设备。")
        return 1

    # 3) 调用 jluboottool 整片写回，然后复位
    print()
    print("开始整片擦写 Flash（先擦除后写入，约需 1-3 分钟）...")
    print("-" * 56)

    result = subprocess.run(
        [
            sys.executable,
            str(script_dir / "jluboottool.py"),
            "--device",
            devpath,
            f"write 0 {backup}",
            "reset",
        ],
        cwd=str(script_dir),
    )

    print("-" * 56)

    if result.returncode != 0:
        print("[失败] 写入过程出错，请检查接线后重试（可重新进入下载模式）。")
        return result.returncode

    print()
    print("[成功] 原厂固件已恢复，设备已复位重启。")
    print("如果屏幕没有正常启动，请断开电源重新上电一次。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(1)
