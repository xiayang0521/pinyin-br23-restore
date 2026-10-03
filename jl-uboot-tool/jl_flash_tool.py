#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杰理 BR23 固件备份 / 恢复工具（菜单入口）
适用于 AC6956C8 / AC695N 等 1MB 内置 NOR Flash 的 BR23 方案。
"""

import datetime
import sys
from pathlib import Path

# 保证中文控制台正常显示（exe 默认 GBK 代码页，统一切到 UTF-8）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import smart_restore as sr

TIMEOUT = 180


def pause(msg="按回车键返回菜单..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def print_header():
    print()
    print("=" * 60)
    print("       杰理 BR23 固件 备份 / 恢复 工具")
    print("       适用 AC6956C8 / AC695N (1MB Flash)")
    print("=" * 60)
    print("  使用前请让设备进入 UBOOT 下载模式")
    print("  （电脑识别为 BR23 UBOOT1.00）")
    print()


def action_backup():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"firmware_backup_{stamp}.bin"
    print()
    print("【备份固件】把设备整片 1MB Flash 读到电脑。")
    print("此操作只读，不会改动设备。")
    print(f"备份文件将保存在本程序所在文件夹，默认文件名：{default_name}")
    raw = input("直接回车用默认名，或输入自定义文件名 > ").strip()
    name = raw or default_name
    if not name.lower().endswith(".bin"):
        name += ".bin"

    out = sr.APP_ROOT / name
    print("-" * 60)
    sr.backup_firmware(out, TIMEOUT)
    print()
    print("备份完成。请妥善保存该 .bin 及其 .sha256 校验文件。")


def pick_image():
    """让用户选择要校验/恢复的镜像，返回 Path 或 None。"""
    default = sr.DEFAULT_BACKUP
    print()
    if default.is_file():
        print(f"找到默认镜像：{default.name}（位于本程序文件夹）")
        raw = input("直接回车使用它，或输入其它 .bin 文件路径 > ").strip()
        if not raw:
            return default
    else:
        print("本程序文件夹未找到 factory_backup.bin。")
        raw = input("请输入要使用的 .bin 文件完整路径 > ").strip()
        if not raw:
            return None

    # 支持拖拽文件到窗口（可能带引号）
    p = Path(raw.strip().strip('"').strip("'"))
    if not p.is_absolute():
        p = (sr.APP_ROOT / p)
    return p


def action_verify():
    img = pick_image()
    if img is None:
        print("未选择镜像。")
        return
    print("-" * 60)
    print(f"【只读校验】镜像：{img}")
    code = sr.verify_firmware(img, TIMEOUT)
    print()
    if code == 0:
        print("设备内容与镜像完全一致，无需刷写。")
    else:
        print("设备内容与镜像存在差异，可使用菜单第 3 项恢复。")


def action_restore():
    img = pick_image()
    if img is None:
        print("未选择镜像。")
        return

    print("-" * 60)
    print("【恢复固件】警告：此操作将擦除并重写设备 Flash，完成后自动重启！")
    print(f"将使用镜像：{img}")
    print()
    print("请确认：")
    print("  1) 设备确实是杰理 BR23 / AC6956C8 拼音机")
    print("  2) 镜像文件正确、设备电量充足、数据线连接稳定")
    confirm = input('确认恢复请输入 YES（大写）后回车，其它输入取消 > ').strip()
    if confirm != "YES":
        print("已取消，未做任何改动。")
        return

    code = sr.restore_firmware(img, TIMEOUT)
    print()
    if code == 0:
        print("固件恢复成功，设备已重启。")
    else:
        print(f"恢复未成功完成（错误码 {code}）。请保持设备连接，")
        print("把屏幕信息发给开发者后再处理，切勿随意断电。")


def main():
    while True:
        print_header()
        print("  1. 备份固件（只读，安全）")
        print("  2. 校验固件（只读，安全）")
        print("  3. 恢复固件（会擦写，谨慎）")
        print("  0. 退出")
        print()
        try:
            choice = input("请输入选项数字后回车 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        try:
            if choice == "1":
                action_backup()
                pause()
            elif choice == "2":
                action_verify()
                pause()
            elif choice == "3":
                action_restore()
                pause()
            elif choice == "0":
                print("再见。")
                return 0
            else:
                print("无效选项，请重新输入。")
                pause()
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f"[错误] {e}")
            pause()
        except KeyboardInterrupt:
            print("\n操作已中断。")
            pause()


if __name__ == "__main__":
    sys.exit(main())
