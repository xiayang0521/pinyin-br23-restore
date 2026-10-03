#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BR23 拼音机固件备份 / 恢复工具
===============================

双击运行的中文菜单程序。

功能：
  1. 备份固件    —— 读取设备整片 1MB Flash 存成 .bin
  2. 恢复固件    —— 只读预检 → 人工确认 → 写入 → 校验 → 复位
  3. 查看设备信息 —— 进 UBOOT 并显示芯片/固件信息（只读）
  4. 只读预检    —— 只分析不写入，看清恢复会做���么
  0. 退出

无需管理员权限，无需 Pico 硬件。
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import br23core

APP_NAME = "BR23 拼音机固件工具"
APP_VERSION = "1.0"

# ---------------------------------------------------------------- 路径定位

def app_dir():
    """程序所在目录（打包成 exe 后为 exe 所在目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_tool_dir():
    """定位 jl-uboot-tool（源码树内 或 exe 同级的 _internal 内）"""
    base = app_dir()
    candidates = [
        base / "jl-uboot-tool",
        base / "_internal" / "jl-uboot-tool",
        base.parent / "救砖" / "jl-uboot-tool",       # 源码树布局
    ]
    for c in candidates:
        if (c / "scsiio").is_dir() and (c / "data").is_dir():
            return c
    # 打包后 _internal 里可能是扁平结构，直接搜
    for root in (base, base / "_internal"):
        if not root.is_dir():
            continue
        for d in root.rglob("scsiio"):
            parent = d.parent
            if (parent / "data").is_dir():
                return parent
    return None


def find_fw_package_dir():
    """定位固件包目录（用于固件身份鉴定），找不到就跳过鉴定"""
    base = app_dir()
    for c in [base / "固件包_ebook_V0.6.3",
              base.parent / "固件包_ebook_V0.6.3",
              base / "_internal" / "固件包_ebook_V0.6.3"]:
        if c.is_dir():
            return c
    for root in (base, base.parent):
        if root.is_dir():
            hits = sorted(root.glob("固件包*"))
            if hits:
                return hits[0]
    return None


APP_DIR = app_dir()
LOG_DIR = APP_DIR / "logs"
SNAPSHOT_DIR = APP_DIR / "backups"

# ---------------------------------------------------------------- 终端输出

class Term:
    """控制台输出。windowed 打包后 sys.stdout 可能为 None，需容错。"""

    def __init__(self):
        self.fp = None
        self._fix_codepage()
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.fp = open(LOG_DIR / ("session_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")),
                           "w", encoding="utf-8", errors="replace")
        except Exception:
            self.fp = None

    def _fix_codepage(self):
        """把控制台代码页切到 UTF-8，否则中文在 cmd 里是乱码。
        chcp 命令通过 ctypes 调 kernel32.SetConsoleOutputCP，
        避免用 os.system 弹一个黑框。"""
        if sys.stdout is None or os.name != "nt":
            return
        try:
            import ctypes
            # 65001 = CP_UTF8
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            # CP_INPUT = 1：输入缓冲区也要，否则 input() 读中文会乱码
            ctypes.windll.kernel32.SetConsoleCP(65001)
            # 让 Python 的标准流也按 UTF-8 重新绑定
            for stream in (sys.stdout, sys.stderr, sys.stdin):
                if stream is not None and hasattr(stream, "reconfigure"):
                    try:
                        stream.reconfigure(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
        except Exception:
            pass

    def write(self, text=""):
        if self.fp:
            try:
                self.fp.write(text + "\n")
                self.fp.flush()
            except Exception:
                pass
        if sys.stdout is not None:
            try:
                print(text, flush=True)
            except Exception:
                pass

    def raw(self, text):
        """无换行输出（进度条）"""
        if sys.stdout is not None:
            try:
                print(text, end="", flush=True)
            except Exception:
                pass

    def clear(self):
        if sys.stdout is not None:
            try:
                print("\033[2J\033[H", end="", flush=True)
            except Exception:
                pass

    def close(self):
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


TERM = Term()


def log(msg=""):
    TERM.write(msg)


def head(title):
    TERM.write("")
    TERM.write("=" * 66)
    TERM.write(" " + title)
    TERM.write("=" * 66)


def sub(title):
    TERM.write("")
    TERM.write("-" * 66)
    TERM.write(" " + title)
    TERM.write("-" * 66)


# ---------------------------------------------------------------- 交互

def pause(msg="按回车键返回菜单 ..."):
    if sys.stdin is not None:
        try:
            input(msg)
        except (EOFError, KeyboardInterrupt):
            TERM.write("")
    else:
        TERM.write(msg)
        time.sleep(1.2)


def ask_yes(prompt):
    if sys.stdin is None:
        return False
    try:
        ans = input(prompt).strip().upper()
        return ans == "Y" or ans == "YES"
    except (EOFError, KeyboardInterrupt):
        TERM.write("")
        return False


def ask_confirm_danger(prompt):
    """高危操作确认：必须输入完整 YES"""
    if sys.stdin is None:
        return False
    try:
        ans = input(prompt).strip()
        return ans == "YES"
    except (EOFError, KeyboardInterrupt):
        TERM.write("")
        return False


# ---------------------------------------------------------------- 核心流程

def connect_device(require_uboot=False):
    """扫描 + 进 UBOOT + 起 loader。返回 Device 或 None"""
    tool = find_tool_dir()
    if tool is None:
        log("[错误] 找不到 jl-uboot-tool 组件，无法工作。")
        log("       请确认 exe 完整（_internal 目录是否齐全）。")
        return None
    try:
        mods = br23core.bind_modules(tool)
    except Exception as e:
        log(f"[错误] 加载组件失败: {type(e).__name__}: {e}")
        return None

    dev = br23core.Device(mods, log, logfile=TERM.fp)
    if not dev.connect(require_uboot=require_uboot):
        dev.close()
        return None
    return dev


def op_backup():
    """功能 1：备份整片固件"""
    head("功能 1　备份固件")
    log("")
    log("  将读取设备整片 1MB Flash 并保存为 .bin 文件。")
    log("  读取过程不会修改设备任何内容。")
    log("")

    default_name = "backup_%s.bin" % datetime.now().strftime("%Y%m%d_%H%M%S")
    log(f"  提示：备份文件建议与固件包放在一起，便于日后核对。")
    log("")

    dev = connect_device()
    if dev is None:
        return 1

    try:
        log("")
        ok, info = dev.read_info()
        if not ok:
            log("[错误] 设备信息读取失败，中止（未做任何修改）。")
            return 2

        log("")
        data = dev.read_flash()

        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        out = SNAPSHOT_DIR / default_name
        out.write_bytes(data)

        log("")
        sub("备份完成")
        log(f"  文件    : {out}")
        log(f"  大小    : {len(data):,} 字节")
        log(f"  SHA256  : {br23core.sha256(data)}")

        # 顺手做个固件鉴定
        fw = find_fw_package_dir()
        if fw:
            ident = br23core.firmware_identity(data, fw)
            if ident["matched"]:
                log("")
                log(f"  固件鉴定: 与 {ident['matched'].name} 比对，"
                    f"{ident['total']} 字节中差 {ident['diff_bytes']} 字节")
                if ident["diff_bytes"] <= 64:
                    log("            → 设备当前运行的就是该固件")
                else:
                    log("            → 与该文件差异较大，不是同一份固件")
        return 0
    except KeyboardInterrupt:
        log("")
        log("[已中断]")
        return 130
    except Exception as e:
        log(f"[错误] {type(e).__name__}: {e}")
        return 3
    finally:
        dev.close()
        log("")
        log("  设备仍处于 UBOOT 模式（loader 在 SRAM，断电即失效）。")
        log("  断电重新上电即可回到正常模式。")


def read_and_analyze(dev, image=None):
    """读取设备并做分析。返回 (current, plan, identity) 或 None"""
    log("")
    ok, info = dev.read_info()
    if not ok:
        log("[错误] 设备信息读取失败。")
        return None
    log("")
    current = dev.read_flash()

    if image is None:
        return current, None, None

    plan = br23core.analyze(current, image, allow_loader_write=False)
    fw = find_fw_package_dir()
    identity = br23core.firmware_identity(current, fw) if fw else {}
    return current, plan, identity


def print_precheck(current, plan, identity, info, image=None, image_path=None):
    sub("只读预检报告")
    log(f"  设备回读大小   : {len(current):,} 字节")
    log(f"  设备 SHA256    : {br23core.sha256(current)}")
    if info.get("flash"):
        log(f"  Flash 芯片     : {info['flash']}")
    if image is not None:
        log(f"  目标镜像       : {image_path}")
        log(f"  镜像大小       : {len(image):,} 字节")
        log(f"  目标 SHA256    : {br23core.sha256(image)}")
    log("")

    if plan is None:
        log("  （本次仅读取设备，未指定目标镜像）")
        log("")
        return

    total = br23core.FLASH_SIZE // br23core.SECTOR
    log(f"  4KB 扇区统计   : 共 {total} | 一致 {plan['same']} | 不同 {len(plan['diff'])}")
    if plan["diff"]:
        log(f"    设备空白/镜像有内容 : {len(plan['dev_blank'])}")
        log(f"    设备有/镜像空白     : {len(plan['bk_blank'])}")
        log(f"    双方有数据但不同     : {len(plan['both'])}")
    log("")
    log(f"  loader 区 0x0-0x{br23core.LOADER_END:X} : "
        f"{'与镜像一致，无需写入' if plan['loader_identical'] else '与镜像不一致（将被保护跳过）'}")

    if identity and identity.get("matched"):
        log("")
        log(f"  固件身份鉴定   : 设备当前固件与 {identity['matched'].name} "
            f"仅差 {identity['diff_bytes']}/{identity['total']} 字节")
        if identity["diff_bytes"] <= 64:
            log(f"    → 差异集中在 0x{identity['first']:06X}-0x{identity['last']:06X}，属签名/CRC 尾")
            log(f"    → **设备当前运行的就是该固件**")
        else:
            log("    → 差异较大，设备固件与该文件不是同一份")

    if plan["diff"]:
        log("")
        log("  差异区间:")
        for s, e in br23core.compress_ranges(plan["diff"]):
            tag = "  [设备空白]" if s in plan["dev_blank"] else ""
            log(f"    0x{s:06X} - 0x{e-1:06X}  ({(e-s)//1024:4d} KB){tag}")
    log("")
    log(f"  预计写入扇区数 : {len(plan['to_write'])} / {total}")
    log("")


def pick_default_image():
    """在程序目录/固件包/上级目录里找一个可用的 .bin 镜像"""
    cands = []
    for pat in ("factory_backup.bin", "*.bin"):
        cands.extend(sorted(APP_DIR.glob(pat)))
    fw = find_fw_package_dir()
    if fw:
        cands.extend(sorted(fw.rglob("jl_isd.bin")))
    cands.extend(sorted(APP_DIR.parent.glob("恢复出厂/*.bin")))
    seen, out = set(), []
    for c in cands:
        if c in seen or not c.is_file():
            continue
        seen.add(c)
        if c.stat().st_size == br23core.FLASH_SIZE:
            out.append(c)
    return out


def op_precheck():
    """功能 4：只读预检（可选镜像对比）"""
    head("功能 4　只读预检（不写入）")
    log("")
    log("  读取设备并分析，**不会写入任何内容**。")
    log("")

    cands = pick_default_image()
    image = image_path = None
    if cands:
        log("  找到以下可用的 1MB 镜像文件：")
        for i, c in enumerate(cands[:10], 1):
            log(f"    {i}. {c.name}")
        log("")
        if ask_yes(f"  选择镜像进行对比分析？(y/n) [n]: "):
            try:
                idx = int(input("  输入序号 [1]: ").strip() or "1")
                if 1 <= idx <= len(cands[:10]):
                    image_path = cands[idx - 1]
            except (ValueError, EOFError, KeyboardInterrupt):
                pass

    dev = connect_device()
    if dev is None:
        return 1
    try:
        image = image_path.read_bytes() if image_path else None
        if image is not None:
            ok, err = br23core.validate_image(image)
            if not ok:
                log(f"[错误] 镜像不可用: {err}")
                return 1
        result = read_and_analyze(dev, image)
        if result is None:
            return 2
        current, plan, identity = result
        print_precheck(current, plan, identity, {}, image, image_path)

        if plan is not None:
            level, warns = br23core.risk_assessment(plan, identity or {})
            if warns:
                sub("风险提示")
                tag = {"none": "无", "ok": "低", "warn": "中", "high": "高"}[level]
                log(f"  风险等级：{tag}")
                for w in warns:
                    log(f"  ! {w}")
        return 0
    except KeyboardInterrupt:
        log("")
        log("[已中断]")
        return 130
    except Exception as e:
        log(f"[错误] {type(e).__name__}: {e}")
        return 3
    finally:
        dev.close()
        log("")
        log("  设备仍处于 UBOOT 模式。断电重新上电可回到正常模式。")


def op_restore():
    """功能 2：恢复固件（预检 → 确认 → 写入 → 校验 → 复位）"""
    head("功能 2　恢复固件")
    log("")
    log("  流程：读取设备 → 只读预检 → 你确认 → 写入 → 校验 → 复位")
    log("  写入前会自动保存回滚快照。")
    log("")

    cands = pick_default_image()
    if not cands:
        log("[错误] 附近没找到 1MB 的 .bin 镜像文件。")
        log("       请把要恢复的固件 .bin 放到本程序目录下再试。")
        return 1

    log("  可用镜像：")
    for i, c in enumerate(cands[:10], 1):
        log(f"    {i}. {c.name}   ({c.stat().st_size:,} 字节)")
    log("")
    try:
        idx = int(input("  请选择要写入的镜像序号 [1]: ").strip() or "1")
    except (ValueError, EOFError, KeyboardInterrupt):
        log("")
        log("已取消。")
        return 130
    if not (1 <= idx <= len(cands[:10])):
        log("序号无效，已取消。")
        return 130
    image_path = cands[idx - 1]

    image = image_path.read_bytes()
    ok, err = br23core.validate_image(image)
    if not ok:
        log(f"[错误] 镜像不可用: {err}")
        return 1
    log(f"  已选择: {image_path}")

    # ---- 连接并预检 ----
    dev = connect_device()
    if dev is None:
        return 1

    try:
        log("")
        log("  正在读取设备当前内容（写入前预检）...")
        ok, info = dev.read_info()
        if not ok:
            log("[错误] 设备信息读取失败，中止（未做任何写入）。")
            return 2
        current = dev.read_flash()
        plan = br23core.analyze(current, image, allow_loader_write=False)
        fw = find_fw_package_dir()
        identity = br23core.firmware_identity(current, fw) if fw else {}
        print_precheck(current, plan, identity, info, image, image_path)

        if plan["identical"]:
            sub("无需写入")
            log("  设备 Flash 与目标镜像完全一致，未做任何写入。")
            return 0

        # 回滚快照
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snap = SNAPSHOT_DIR / ("snapshot_%s.bin" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        snap.write_bytes(current)
        log(f"  回滚快照: {snap}")

        # 风险评估
        level, warns = br23core.risk_assessment(plan, identity)
        sub("风险评估")
        tag = {"none": "无", "ok": "低", "warn": "中", "high": "高"}[level]
        log(f"  风险等级：{tag}")
        for w in warns:
            log(f"  ! {w}")
        log("")
        log(f"  将写入 {len(plan['to_write'])} 个 4KB 扇区（{len(plan['to_write'])*4096:,} 字节）")
        if plan["skipped_loader"]:
            log(f"  loader 区 {len(plan['skipped_loader'])} 个扇区将被保护跳过")
        log("")

        if level == "high":
            log("  ⚠  高风险操作！请确认你清楚后果。")
        log("  回滚方式：若恢复后异常，可重新运行本工具选择该快照再恢复一次。")
        log("")

        if not ask_confirm_danger("  确认写入？请输入 YES 继续（其它任何输入均取消）："):
            log("")
            log("  已取消，未做任何写入。")
            return 130

        # ---- 写入 ----
        head("正在写入")
        ok, failed = dev.write_plan(image, plan["to_write"])
        if not ok:
            log("")
            log(f"[写入失败] 从扇区 0x{failed[0]:06X} 起未完成。")
            log("           设备保持 UBOOT 状态，**未复位**。")
            log(f"           可用回滚快照恢复: {snap}")
            return 4

        # ---- 校验 ----
        head("写后校验")
        ok, bad = dev.verify(image)
        if not ok:
            log("")
            log("[校验失败] 全片内容与目标镜像不一致。")
            if bad:
                log(f"         差异扇区 {len(bad)} 个，首个 0x{bad[0]:06X}")
            log("         设备保持 UBOOT 状态，**未复位**。")
            log(f"         可用回滚快照恢复: {snap}")
            return 5

        # ---- 复位 ----
        head("完成")
        log(f"  已写入 {len(plan['to_write'])} 个扇区并通过全片 SHA256 校验。")
        log(f"  回滚快照: {snap}")
        log("")
        dev.reset()
        log("  设备应已自动重启进入正常模式。")
        return 0

    except KeyboardInterrupt:
        log("")
        log("[已中断] 设备未复位。若已开始写入，请先用「功能 4 只读预检」确认状态。")
        return 130
    except Exception as e:
        log("")
        log(f"[异常] {type(e).__name__}: {e}")
        log("        设备未复位。若已开始写入，请先用「功能 4 只读预检」确认状态。")
        return 6
    finally:
        dev.close()


def op_device_info():
    """功能 3：查看设备信息（只读）"""
    head("功能 3　查看设备信息（只读）")
    log("")
    dev = connect_device()
    if dev is None:
        return 1
    try:
        log("")
        ok, info = dev.read_info()
        if not ok:
            return 2
        log("")
        sub("读取整片固件（只读）...")
        data = dev.read_flash()
        log("")
        sub("设备信息")
        log(f"  设备路径   : {dev.entry['path']}")
        log(f"  厂商/产品 : {dev.entry['vendor']} / {dev.entry['product']} {dev.entry['rev']}")
        log(f"  Flash 芯片 : {info.get('flash', '未知')}")
        log(f"  Flash 大小 : {len(data):,} 字节 (1MB)")
        log(f"  Flash SHA256: {br23core.sha256(data)}")

        fw = find_fw_package_dir()
        if fw:
            ident = br23core.firmware_identity(data, fw)
            if ident["matched"]:
                log("")
                log(f"  固件鉴定   : 与 {ident['matched'].name} 比对，"
                    f"差 {ident['diff_bytes']}/{ident['total']} 字节")
                if ident["diff_bytes"] <= 64:
                    log("              → 设备当前运行的就是该固件")

        sub("Flash 内容分布")
        blank = b"\xff" * br23core.SECTOR
        nz = [a for a in range(0, br23core.FLASH_SIZE, br23core.SECTOR)
              if data[a:a + br23core.SECTOR] != blank]
        for s, e in br23core.compress_ranges(nz):
            log(f"  0x{s:06X} - 0x{e-1:06X}  ({(e-s)//1024:4d} KB) 有数据")
        if not nz:
            log("  整片空白（芯片可能已擦除）")
        return 0
    except KeyboardInterrupt:
        log("")
        log("[已中断]")
        return 130
    except Exception as e:
        log(f"[错误] {type(e).__name__}: {e}")
        return 3
    finally:
        dev.close()
        log("")
        log("  设备仍处于 UBOOT 模式。断电重新上电可回到正常模式。")


# ---------------------------------------------------------------- 菜单

MENU = """
  1. 备份固件          读取设备整片 1MB 存成 .bin（只读）
  2. 恢复固件          预检 → 确认 → 写入 → 校验 → 复位
  3. 查看设备信息      进 UBOOT 看芯片与固件信息（只读）
  4. 只读预检          只分析不写入，看清恢复会做什么
  0. 退出
"""

OPS = {
    "1": op_backup,
    "2": op_restore,
    "3": op_device_info,
    "4": op_precheck,
}


def main():
    log("")
    log("=" * 66)
    log(f" {APP_NAME}  v{APP_VERSION}")
    log(" 杰理 BR23 / AC6956C 固件备份与恢复")
    log("=" * 66)
    log("")
    log(" 本工具不会烧录固件以外的任何东西。")
    log(" 恢复前会自动预检并保存回滚快照。")
    log(f" 程序目录: {APP_DIR}")
    log(f" 日志目录: {LOG_DIR}")

    while True:
        log("")
        log(MENU)
        try:
            choice = input(" 请选择 [0]: ").strip() or "0"
        except (EOFError, KeyboardInterrupt):
            log("")
            log("再见。")
            break

        if choice == "0":
            log("")
            log("再见。")
            break
        fn = OPS.get(choice)
        if fn is None:
            log("无效选项，请重新输入。")
            continue
        try:
            fn()
        except KeyboardInterrupt:
            log("")
            log("[已取消]")
        except Exception as e:
            log("")
            log(f"[未预期错误] {type(e).__name__}: {e}")
        pause()

    TERM.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        TERM.write(f"\n[fatal] {type(e).__name__}: {e}")
        TERM.close()
        sys.exit(1)
