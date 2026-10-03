#!/usr/bin/env python3
"""
杰理 BR23 / AC6956C8 固件工具核心库（备份 / 只读校验 / 恢复）

功能：
  1. 备份：读取整片 1MB Flash 保存为 .bin，并计算 SHA256
  2. 校验：只读整片、与镜像逐扇区比对，不擦写/不复位
  3. 恢复：
       - 写入范围 0x1000..1MB（loader 区自动保留）
       - 0x0-0x1000 loader 区：先比对，一致则保留，不一致才擦写
       - 主区域按 4KB 扇区擦写，单扇区写入失败自动重试
       - 写后全片回读 SHA256 校验，不一致自动补刷差异扇区
       - 校验通过后复位重启

命令行用法：
    python smart_restore.py [镜像路径]                 # 实际恢复
    python smart_restore.py --dry-run [镜像路径]      # 只读校验
    python smart_restore.py --backup-to out.bin       # 备份整片
"""

import argparse
import hashlib
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import yaml


def _app_root():
    """应用基目录：冻结(exe)模式为 exe 所在目录，源码模式为脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _data_root():
    """随包数据 data 目录：冻结模式在 PyInstaller 解压目录，源码模式在脚本旁。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", _app_root()))
        return base / "data"
    return _app_root() / "data"


APP_ROOT = _app_root()
script_dir = APP_ROOT
dataroot = _data_root()


def find_default_backup():
    """定位默认镜像 factory_backup.bin：优先 exe/脚本同目录，再找常见位置。"""
    candidates = [
        APP_ROOT / "factory_backup.bin",
        APP_ROOT.parent / "factory_backup.bin",
        APP_ROOT.parent / "恢复出厂" / "factory_backup.bin",
        APP_ROOT.parent.parent / "恢复出厂" / "factory_backup.bin",
    ]
    for c in candidates:
        if c.is_file():
            return c
    root = APP_ROOT.parent
    if root.is_dir():
        found = sorted(root.rglob("factory_backup.bin"))
        if found:
            return found[0]
    return candidates[0]


DEFAULT_BACKUP = find_default_backup()

FLASH_SIZE = 0x100000          # 1MB
LOADER_END = 0x1000            # loader/uboot 保护区边界
SECTOR = 0x1000                # 4KB 扇区
MAX_RETRY = 3                  # 单扇区写入重试次数


def find_device_wait(timeout=180):
    """等待并返回第一个 UBOOT 设备路径。"""
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from jldevfind import find_jl_devices

    print("等待 BR23 UBOOT1.00 设备接入...")
    for attempt in range(1, timeout + 1):
        devs = find_jl_devices()
        if devs:
            print(f"\n已找到设备: {devs[0]['name']}")
            return devs[0]["path"]
        print(f"\r尚未发现设备，已等待 {attempt}/{timeout} 秒...", end="", flush=True)
        time.sleep(1)
    print("\n[错误] 等待超时，未发现 UBOOT 设备。")
    return None


def safe_flash_read(loader, addr, length, retries=3):
    """读 Flash，偶发传输失败时自动重试。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return loader.flash_read(addr, length)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise last_err


def flash_read_all(loader, chunk):
    """读取整片 Flash，返回 bytes。"""
    buf = bytearray()
    addr = 0
    remaining = FLASH_SIZE
    while remaining > 0:
        n = min(remaining, chunk)
        buf += safe_flash_read(loader, addr, n)
        addr += n
        remaining -= n
    return bytes(buf)


def erase_sector(loader, addr):
    """擦除一个 4KB 扇区（统一扇区擦除，避免大块擦除误伤相邻扇区）。"""
    loader.flash_erase_sector(addr)


def write_sector(loader, addr, data, chunk):
    """擦除并写入一个 4KB 扇区，返回是否成功。"""
    assert len(data) == SECTOR
    try:
        erase_sector(loader, addr)
        off = 0
        while off < SECTOR:
            n = min(SECTOR - off, chunk)
            loader.flash_write(addr + off, data[off:off + n])
            off += n
        return True
    except Exception as e:
        print(f"\n  [扇区 0x{addr:06X} 写入异常] {e}")
        return False


def bring_up_loader(msc, product):
    """若设备处于 UBOOT1.00，则上传并启动 BR23 loader，返回 JL_LoaderV2。"""
    from jltech.uboot import JL_UBOOT, JL_LoaderV2

    if product == "UBOOT1.00":
        with open(dataroot / "usb-loaders.yaml", "r", encoding="utf-8") as f:
            usb_loaders = yaml.safe_load(f)
        spec = usb_loaders["br23"]

        loader_ub = JL_UBOOT(msc.dev)
        addr = spec["address"]
        block_size = spec.get("blocksize", 512)

        print(f"上传 loader 到 SRAM（0x{spec['address']:X}，块大小 {block_size}）...")
        with open(dataroot / spec["file"], "rb") as f:
            while True:
                block = f.read(block_size)
                if block == b"":
                    break
                loader_ub.mem_write(addr, block)  # BR23: blob 已 MengLi 加密，原样写入
                addr += len(block)

        loader_ub.mem_jump(spec["address"], 0x0001)
        print("loader 已在 SRAM 启动（未改动 Flash）。")

    # 跳转后稳定延时，避免紧接着发命令导致 WinError 31 半挂
    time.sleep(1.5)
    loader = JL_LoaderV2(msc.dev)

    # 1 字节预热读：确认 loader 已就绪（只读）
    for attempt in range(1, 4):
        try:
            loader.flash_read(0, 1)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(0.5)

    return loader


def get_chunk(loader):
    """获取 Flash I/O 块大小（BR23 loader 实际约 256 字节）。"""
    try:
        chunk = loader.usb_buffer_size()
        if not (1 <= chunk <= SECTOR):
            chunk = 256
    except Exception:
        chunk = 256
    return chunk


@contextmanager
def connect_device(timeout=180):
    """定位并打开 BR23 设备、启动 loader，yield (loader, chunk)。"""
    from jltech.uboot import JL_MSCDevice

    devpath = find_device_wait(timeout)
    if devpath is None:
        raise RuntimeError("未发现 UBOOT 设备")

    with JL_MSCDevice(devpath) as msc:
        vendor, product, prodrev = msc.inquiry()
        print(f"设备识别 : vendor={vendor} product={product} rev={prodrev}")
        if vendor.lower() != "br23":
            raise RuntimeError(f"检测到芯片为 {vendor}，本工具仅支持 BR23。")

        loader = bring_up_loader(msc, product)
        chunk = get_chunk(loader)
        print(f"Flash I/O 块大小: {chunk} 字节")
        yield loader, chunk


def load_image(path):
    """读取并校验镜像文件，返回 (image_bytes, sha256)。大小不符则抛错。"""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"找不到镜像文件: {p}")
    image = p.read_bytes()
    if len(image) != FLASH_SIZE:
        raise ValueError(f"文件大小 {len(image)} 不是 1MB ({FLASH_SIZE})，已拒绝。")
    return image, hashlib.sha256(image).hexdigest()


def do_backup(loader, chunk, out_path):
    """读取整片并保存，返回 (长度, sha256)。"""
    print("读取整片 Flash ...")
    data = flash_read_all(loader, chunk)
    digest = hashlib.sha256(data).hexdigest()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    # 同时写一份 .sha256 校验文件
    out.with_suffix(out.suffix + ".sha256").write_text(
        f"{digest}  {out.name}\n", encoding="utf-8"
    )

    print(f"已保存备份 : {out}")
    print(f"大小       : {len(data)} 字节 ({len(data) / 1024 / 1024:.2f} MB)")
    print(f"SHA256     : {digest}")
    return len(data), digest


def do_verify(loader, image, expected_hash, chunk):
    """只读校验：读整片、逐扇区比对、统计差异。返回哈希是否一致。"""
    print("-" * 60)
    print("[只读校验] 读取整片 Flash ...")
    readback = flash_read_all(loader, chunk)
    actual_hash = hashlib.sha256(readback).hexdigest()

    total_sectors = FLASH_SIZE // SECTOR
    same = 0
    diff_sectors = []
    blank_sectors = 0
    blank = b"\xff" * SECTOR

    for addr in range(0, FLASH_SIZE, SECTOR):
        sect = readback[addr:addr + SECTOR]
        if sect == blank:
            blank_sectors += 1
        if sect == image[addr:addr + SECTOR]:
            same += 1
        else:
            diff_sectors.append(addr)

    loader_same = readback[:LOADER_END] == image[:LOADER_END]

    print()
    print("校验结果")
    print("-" * 60)
    print(f"  设备回读大小 : {len(readback)} 字节")
    print(f"  设备 SHA256  : {actual_hash}")
    print(f"  镜像 SHA256  : {expected_hash}")
    print(f"  全片哈希一致 : {'是（已是该镜像，无需刷写）' if actual_hash == expected_hash else '否'}")
    print()
    print(f"  4KB 扇区总数 : {total_sectors}")
    print(f"  与镜像一致   : {same} 个")
    print(f"  与镜像不同   : {len(diff_sectors)} 个（恢复时需要刷写）")
    print(f"  空白(0xFF)   : {blank_sectors} 个")
    print()
    print(f"  loader 区 0x0-0x1000 : {'一致，将自动保留' if loader_same else '不一致（恢复时会擦写该扇区）'}")

    if diff_sectors:
        shown = ", ".join(f"0x{a:06X}" for a in diff_sectors[:16])
        more = "" if len(diff_sectors) <= 16 else f" ... 等共 {len(diff_sectors)} 个"
        print(f"  差异扇区     : {shown}{more}")

    print("-" * 60)
    print("[只读校验] 完成：未擦除、未写入、未复位。")
    return actual_hash == expected_hash


def do_restore(loader, image, expected_hash, chunk):
    """实际恢复：写 loader 区(按需)+主区，回读校验，通过后复位。返回 0/2/3。"""
    print("-" * 60)

    # 1) loader 保护区 0x0-0x1000
    print("检查 loader 区 0x0-0x1000 ...")
    cur_loader = loader.flash_read(0, LOADER_END)
    if cur_loader == image[:LOADER_END]:
        print("  loader 区与镜像一致，自动保留，跳过擦写。")
    else:
        print("  loader 区不一致，执行擦写...")
        if not write_sector(loader, 0, image[:SECTOR], chunk):
            print("[失败] loader 区写入失败。")
            return 2

    # 2) 主区域 0x1000..1MB
    print("-" * 60)
    print("写入主区域 0x1000..1MB（4KB 块 + 重试）...")
    total_sectors = (FLASH_SIZE - LOADER_END) // SECTOR
    done = 0
    for addr in range(LOADER_END, FLASH_SIZE, SECTOR):
        sector_data = image[addr:addr + SECTOR]
        success = False
        for attempt in range(1, MAX_RETRY + 1):
            if write_sector(loader, addr, sector_data, chunk):
                success = True
                break
            print(f"  扇区 0x{addr:06X} 第 {attempt} 次重试")
            time.sleep(0.3)

        if not success:
            print(f"\n[失败] 扇区 0x{addr:06X} 重试 {MAX_RETRY} 次仍失败。")
            return 2

        done += 1
        pct = done * 100 // total_sectors
        print(f"\r  进度: {done}/{total_sectors} 扇区 ({pct}%)", end="", flush=True)

    print()
    print("-" * 60)

    # 3) 全片回读 SHA256 校验
    print("全片回读校验中...")
    readback = flash_read_all(loader, chunk)
    actual_hash = hashlib.sha256(readback).hexdigest()
    print(f"回读 SHA256 : {actual_hash}")
    print(f"镜像 SHA256 : {expected_hash}")

    if actual_hash != expected_hash:
        print("[校验失败] 回读与镜像不一致，尝试补刷差异扇区...")
        fixed = 0
        for addr in range(0, FLASH_SIZE, SECTOR):
            if readback[addr:addr + SECTOR] != image[addr:addr + SECTOR]:
                write_sector(loader, addr, image[addr:addr + SECTOR], chunk)
                fixed += 1
        print(f"已补刷 {fixed} 个扇区，重新回读校验...")
        readback = flash_read_all(loader, chunk)
        actual_hash = hashlib.sha256(readback).hexdigest()
        if actual_hash != expected_hash:
            print("[错误] 补刷后校验仍不通过，请勿复位，请重新运行。")
            return 3

    print("[校验通过] 全片 SHA256 一致。")

    # 4) 复位
    print("-" * 60)
    try:
        loader.run_app(1)
        print("设备已复位重启。")
    except Exception as e:
        print(f"复位指令异常（可手动断电重上电）: {e}")

    print()
    print("[成功] 固件恢复完成。若屏幕未亮，请断电重新上电一次。")
    return 0


# -------- 对外的一站式入口（菜单程序 / CLI 共用） --------

def backup_firmware(out_path, timeout=180):
    with connect_device(timeout) as (loader, chunk):
        return do_backup(loader, chunk, out_path)


def verify_firmware(image_path, timeout=180):
    image, digest = load_image(image_path)
    with connect_device(timeout) as (loader, chunk):
        return 0 if do_verify(loader, image, digest, chunk) else 10


def restore_firmware(image_path, timeout=180):
    image, digest = load_image(image_path)
    with connect_device(timeout) as (loader, chunk):
        return do_restore(loader, image, digest, chunk)


def parse_args():
    p = argparse.ArgumentParser(description="杰理 BR23 固件 备份/校验/恢复 工具")
    p.add_argument("image", nargs="?", default=str(DEFAULT_BACKUP),
                   help="镜像文件路径（校验/恢复时使用，默认 factory_backup.bin）")
    p.add_argument("--dry-run", action="store_true",
                   help="只读校验：不擦除、不写入、不复位")
    p.add_argument("--backup-to", metavar="OUT",
                   help="备份整片 Flash 到指定文件")
    p.add_argument("--timeout", type=int, default=180,
                   help="等待设备接入秒数（默认 180）")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    if args.backup_to:
        print(" 固件备份 (BR23 / AC6956C8)")
    elif args.dry_run:
        print(" 只读校验 DRY-RUN (BR23 / AC6956C8)")
    else:
        print(" 固件恢复 (BR23 / AC6956C8)")
    print("=" * 60)

    try:
        if args.backup_to:
            backup_firmware(args.backup_to, args.timeout)
            return 0

        image_path = Path(args.image).resolve()
        print(f"镜像文件 : {image_path}")
        if args.dry_run:
            return verify_firmware(image_path, args.timeout)
        return restore_firmware(image_path, args.timeout)

    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"[错误] {e}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(1)
