#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
br23core —— 杰理 BR23 / AC6956C 固件备份与恢复核心库
=====================================================

被 GUI 主程序和命令行共用。职责：
  * 扫描设备、固件 USB 模式软触发 UBOOT1.00
  * 上传 loader 到 SRAM（只写内存，不碰 Flash）
  * 只读信息采集、整片 Flash 回读
  * 差异分析、固件身份鉴定
  * 逐扇区擦写 + 校验（全片 SHA256）

重要限制（实测）
----------------
* BR23 loader **不支持** `version()` (0xFC15)、`maskrom_id()` (0xFC16)、
  `chip_key()` (0xFC09)。调用会 SCSI 超时并**挂死整条 USB 通道**。
  本库只使用：0xFC0A / 0xFC14 / 0xFD05 / 0xFB01 / 0xFB04 / 0xFC0C
* 设备卷必须用 `GENERIC_READ|GENERIC_WRITE` 打开（scsiio 已如此），非管理员即可
* 每次进入 UBOOT 后设备路径都会变（HarddiskVolumeNN 递增），必须重新扫描
* loader blob 本身已 MengLi 加密，mem_write 原样写入即可（见 chips.yaml 的
  `memory-rw-mengli-crypt` quirk）
"""
import hashlib
import sys
import time
from collections.abc import Mapping
from pathlib import Path

FLASH_SIZE = 0x100000          # 1MB 内置 NOR Flash
SECTOR = 0x1000                # 4KB 扇区
LOADER_END = 0x1000            # loader/boot 区保护边界
MAX_CHUNK = 256                # BR23 loader I/O 缓冲（实测 256 最稳）
READ_RETRY = 3
WRITE_RETRY = 3
LOADER_ARG = 0x0001            # target=1(SPI NOR) | clkdiv=0 | spimode=0(3-wire)

# jl_isd.bin 是配合杰理烧录工具生成的“一体化固件”：头部为 ISD 封装/段表，
# 内部 uboot/app 等段已加密。真机实测它从物理地址 0x1000 起逐字节裸放
# （头部魔数恰在 0x1000，isd_config.ini 的 EOFFSET=1 佐证），故复用
# LOADER_END 作为落盘起点；刷写只动这个窗口，boot 区与其后的资源区不碰。
ISD_BASE = LOADER_END
ISD_MAGIC = bytes.fromhex("46e4bf1d")   # jl_isd 头部 4 字节魔数（真机锚点）

JL_PRODUCTS = ("UBOOT", "UDISK", "DEVICE")
DEV_TYPE = {
    0x00: "None", 0x01: "SDRAM", 0x02: "SD card", 0x03: "SPI NOR flash on SPI0",
    0x04: "SPI NAND on SPI0", 0x05: "OTP", 0x16: "SPI NOR on SPI1", 0x17: "SPI NAND on SPI1",
}

BAR = "=" * 66
SUB = "-" * 66


# ---------------------------------------------------------------- 日志

class Logger:
    """同时输出到控制台和可选日志文件；供 GUI 挂到回调上"""

    def __init__(self, callback=None, logfile=None):
        self.callback = callback
        self.fp = None
        if logfile:
            try:
                self.fp = open(logfile, "w", encoding="utf-8", errors="replace")
            except Exception:
                self.fp = None

    def __call__(self, text=""):
        try:
            print(text, flush=True)
        except Exception:
            pass
        if self.fp:
            try:
                self.fp.write(text + "\n")
                self.fp.flush()
            except Exception:
                pass
        if self.callback:
            try:
                self.callback(text)
            except Exception:
                pass

    def close(self):
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


# ---------------------------------------------------------------- 依赖注入

def bind_modules(tool_dir):
    """把 jl-uboot-tool 加进 sys.path 并导入其模块。

    tool_dir: jl-uboot-tool 目录（含 scsiio/ jltech/ data/）
    打包成 exe 后由程序定位；源码运行时传入仓库路径。
    返回 dict，调用方传给 Device。
    """
    tool_dir = Path(tool_dir)
    if not tool_dir.is_dir():
        raise RuntimeError(f"找不到 jl-uboot-tool 目录: {tool_dir}")
    sys.path.insert(0, str(tool_dir))

    import yaml
    from scsiio import SCSIDev
    from scsiio.common import SCSIException
    from jltech.uboot import JL_UBOOT, JL_LoaderV2

    return {
        "yaml": yaml,
        "SCSIDev": SCSIDev,
        "SCSIException": SCSIException,
        "JL_UBOOT": JL_UBOOT,
        "JL_LoaderV2": JL_LoaderV2,
        "dataroot": tool_dir / "data",
    }


# ---------------------------------------------------------------- 设备层

class Device:
    """封装 BR23 设备会话：扫描 -> 进 UBOOT -> 起 loader -> 读写 Flash"""

    def __init__(self, mods, log, logfile=None):
        self.SCSIDev = mods["SCSIDev"]
        self.SCSIException = mods["SCSIException"]
        self.JL_UBOOT = mods["JL_UBOOT"]
        self.JL_LoaderV2 = mods["JL_LoaderV2"]
        self.yaml = mods["yaml"]
        self.dataroot = mods["dataroot"]
        # log 是一个可调用对象（如 br23_tool.log 或 Logger 实例），
        # 只负责输出文本；日志文件句柄单独持有，不依赖 log 对象的属性。
        self.log = log
        self.log_fp = logfile
        self.dev = None        # SCSIDev
        self.ldr = None        # JL_LoaderV2
        self.entry = None      # 当前设备信息 dict

    # ---------------- 扫描 ----------------

    def _scan_paths(self):
        paths = []
        for i in range(180):
            paths.append("\\\\.\\HarddiskVolume%d" % i)
        for i in range(16):
            paths.append("\\\\.\\PhysicalDrive%d" % i)
        for i in range(26):
            paths.append("\\\\.\\%s:" % chr(ord("A") + i))
        return paths

    def inquiry(self, dev):
        data = bytearray(36)
        dev.execute(b"\x12\x00\x00\x24\x00", None, data)
        return (
            str(data[8:16], "ascii").strip(),
            str(data[16:32], "ascii").strip(),
            str(data[32:36], "ascii").strip(),
        )

    def scan(self, verbose=True):
        """扫描所有 JL 设备，返回 [dict(path,vendor,product,rev), ...]"""
        found = []
        for path in self._scan_paths():
            try:
                with self.SCSIDev(path) as d:
                    v, p, r = self.inquiry(d)
            except Exception:
                continue
            if p.upper().startswith(JL_PRODUCTS):
                e = {"path": path, "vendor": v, "product": p, "rev": r}
                found.append(e)
                if verbose:
                    self.log(f"  [发现] {path} -> {v} | {p} | {r}")
        return found

    def find_uboot(self, verbose=False):
        for e in self.scan(verbose=verbose):
            if e["vendor"].upper() == "BR23" and e["product"] == "UBOOT1.00":
                return e
        return None

    # ---------------- 进 UBOOT ----------------

    def soft_trigger(self, fw_entry):
        """向固件 UDISK 发 0xFC0A，促使其复位进 UBOOT1.00"""
        self.log(f"  向 {fw_entry['path']} 发送 JL 下载协议命令 0xFC0A ...")
        for attempt in range(1, 4):
            try:
                with self.SCSIDev(fw_entry["path"]) as d:
                    _v, product, _r = self.inquiry(d)
                    if product == "UBOOT1.00":
                        self.log("  设备已是 UBOOT1.00")
                        return True
                    self.JL_LoaderV2(d).online_device()
                    self.log(f"  第 {attempt} 次命令已发送（设备应答正常）")
                    return True
            except self.SCSIException:
                self.log(f"  第 {attempt} 次: 无标准应答（设备无反应或正在重启）")
                return True
            except OSError as e:
                self.log(f"  第 {attempt} 次: 打开失败（{e}）")
                time.sleep(1.0)
        return False

    def wait_uboot(self, timeout=40):
        self.log(f"  等待 BR23 UBOOT1.00 重新枚举（最多 {timeout} 秒）...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            e = self.find_uboot()
            if e:
                self.log(f"  [成功] 已进入 UBOOT: {e['path']} -> {e['vendor']} | {e['product']} | {e['rev']}")
                return e
            time.sleep(0.5)
        return None

    # ---------------- 起 loader ----------------

    def connect(self, require_uboot=False):
        """完整连接：扫描 →（必要时）软触发 UBOOT → 上传 loader 到 SRAM。
        成功返回 True，self.dev / self.ldr / self.entry 就绪。"""
        self.log("  扫描磁盘设备 ...")
        entries = self.scan(verbose=True)
        if not entries:
            self.log("  [错误] 未发现任何 JL 设备。请检查 USB 线是否插好、设备是否开机。")
            return False

        uboot = next((e for e in entries if e["product"] == "UBOOT1.00"), None)
        if uboot is None:
            if require_uboot:
                self.log("  [错误] 设备不在 UBOOT 模式，且未允许自动软触发。")
                return False
            fwmode = next((e for e in entries if e["product"] in ("UDISK", "DEVICE")), None)
            if fwmode is None:
                self.log("  [错误] 发现的 JL 设备均非 BR23：")
                for e in entries:
                    self.log(f"          {e['path']} -> {e['vendor']} | {e['product']} | {e['rev']}")
                return False
            self.log(f"  设备处于固件 USB 模式（{fwmode['vendor']} {fwmode['product']}），软触发进 UBOOT ...")
            if not self.soft_trigger(fwmode):
                self.log("  [错误] 进入 UBOOT 命令发送失败")
                return False
            uboot = self.wait_uboot()
            if uboot is None:
                self.log("  [错误] 40 秒内未以 UBOOT1.00 重新枚举")
                return False
        else:
            self.log("  设备已在 UBOOT 模式")

        # 重新扫一次拿最新路径
        live = self.find_uboot()
        if live is None:
            self.log("  [错误] UBOOT 设备在准备阶段消失")
            return False

        spec = self.yaml.safe_load(open(self.dataroot / "usb-loaders.yaml"))["br23"]
        addr = spec["address"]
        block_size = spec.get("blocksize", 512)
        blob = (self.dataroot / spec["file"]).read_bytes()

        self.dev = self.SCSIDev(live["path"])
        v, product, r = self.inquiry(self.dev)
        self.entry = {"path": live["path"], "vendor": v, "product": product, "rev": r}
        self.log(f"  会话设备: {live['path']} -> {v} | {product} | {r}")

        ub = self.JL_UBOOT(self.dev)
        self.log(f"  上传 loader 到 SRAM 0x{addr:X}（{len(blob)} 字节 / 块 {block_size}，仅写内存不碰 Flash）...")
        off = 0
        while off < len(blob):
            ub.mem_write(addr + off, blob[off:off + block_size])
            off += block_size

        self.log(f"  跳转执行 loader（参数 0x{LOADER_ARG:04X} = SPI NOR / div0 / 3-wire）...")
        ub.mem_jump(addr, LOADER_ARG)
        time.sleep(1.0)

        self.ldr = self.JL_LoaderV2(self.dev)
        last = None
        for attempt in range(1, 4):
            try:
                self.ldr.flash_read(0, 1)      # 1 字节预热读（只读）
                self.log("  loader 已就绪（1 字节预热读成功）")
                return True
            except Exception as e:
                last = e
                self.log(f"  预热读第 {attempt} 次失败: {type(e).__name__}")
                time.sleep(0.6)
        self.close()
        self.log(f"  [错误] loader 启动后无响应: {last}")
        return False

    def close(self):
        try:
            if self.dev:
                self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.ldr = None

    # ---------------- 读取 ----------------

    def chunk_size(self):
        try:
            n = self.ldr.usb_buffer_size()
            if n:
                return max(1, min(int(n), MAX_CHUNK))
        except Exception:
            pass
        return MAX_CHUNK

    def read_info(self):
        """只读采集 Flash 芯片信息。返回 (ok, info)"""
        info = {}
        try:
            ondev = self.ldr.online_device()
            info["flash"] = "ID 0x%06X (%s)" % (
                ondev["id"], DEV_TYPE.get(ondev["type"], "type=0x%02X" % ondev["type"]))
            self.log(f"  Flash 芯片: {info['flash']}")
        except Exception as e:
            self.log(f"  [错误] Flash 信息读取失败: {type(e).__name__}: {e}")
            return False, info
        try:
            info["buffer"] = self.ldr.usb_buffer_size()
            self.log(f"  USB 缓冲大小: {info['buffer']} 字节")
        except Exception:
            info["buffer"] = None
        return True, info

    def read_flash(self, progress=None):
        """整片回读 1MB Flash（只读）。progress(pct) 用于回调进度。"""
        chunk = self.chunk_size()
        self.log(f"  开始整片回读（0x0 - 0x{FLASH_SIZE:X}，块 {chunk} 字节）...")
        data = bytearray()
        addr = 0
        t0 = time.time()
        next_mark = 0
        last_pct = -1
        while addr < FLASH_SIZE:
            n = min(chunk, FLASH_SIZE - addr)
            got = None
            for attempt in range(1, READ_RETRY + 1):
                try:
                    got = self.ldr.flash_read(addr, n)
                    break
                except Exception as e:
                    if attempt == READ_RETRY:
                        self.log("")
                        raise RuntimeError(
                            f"回读 0x{addr:06X} 失败（重试 {READ_RETRY} 次）: {type(e).__name__}: {e}\n"
                            f"       已完成 {addr} 字节（{addr * 100 // FLASH_SIZE}%）")
                    self.log(f"  回读 0x{addr:06X} 第 {attempt} 次失败（{type(e).__name__}），重试...")
                    time.sleep(0.4)
            data += got
            addr += n
            if addr >= next_mark:
                rate = addr / 1024.0 / max(time.time() - t0, 0.001)
                pct = addr * 100 // FLASH_SIZE
                line = (f"\r  [{time.strftime('%H:%M:%S')}] 回读 {pct:3d}%  "
                        f"({addr/1024:.0f} KB / 1024 KB, {rate:.0f} KB/s)      ")
                self.log_raw(line)
                if pct // 25 != last_pct // 25:
                    last_pct = pct
                    self.log_only_file(f"[{time.strftime('%H:%M:%S')}] 回读 {pct}% ({addr/1024:.0f} KB)")
                    if progress:
                        progress(pct)
                next_mark += 0x20000
        self.log_raw("\n")
        self.log(f"  整片回读完成：{len(data)} 字节，耗时 {time.time() - t0:.1f} 秒")
        return bytes(data)

    # ---------------- 窗口读取（jl_isd 专用，避免读取整片 1MB） ----------------

    def _read_retry(self, addr, size):
        """读取指定地址区间，带 READ_RETRY 次重试。失败抛出 RuntimeError。

        抽成独立方法是因为整片回读与 jl_isd 窗口回读共用同一套重试规则，
        避免在两处重复实现异常处理。
        """
        got = None
        for attempt in range(1, READ_RETRY + 1):
            try:
                got = self.ldr.flash_read(addr, size)
                break
            except Exception as e:
                if attempt == READ_RETRY:
                    raise RuntimeError(
                        f"回读 0x{addr:06X} 失败（重试 {READ_RETRY} 次）: "
                        f"{type(e).__name__}: {e}") from e
                self.log(f"  回读 0x{addr:06X} 第 {attempt} 次失败"
                         f"（{type(e).__name__}），重试...")
                time.sleep(0.4)
        return got

    def read_window(self, base, size, progress=None):
        """只读回读 [base, base+size) 区间。jl_isd 只需这一小段，不必读全片。"""
        chunk = self.chunk_size()
        self.log(f"  回读窗口 0x{base:X} - 0x{base + size:X}"
                 f"（{size // 1024} KB，块 {chunk} 字节）...")
        data = bytearray()
        addr = base
        t0 = time.time()
        while addr < base + size:
            n = min(chunk, base + size - addr)
            data += self._read_retry(addr, n)
            addr += n
            if progress:
                progress((addr - base) * 100 // size)
        self.log(f"  窗口回读完成：{len(data)} 字节，"
                 f"耗时 {time.time() - t0:.1f} 秒")
        return bytes(data)

    # ---------------- 写入 ----------------

    def write_sector(self, addr, data):
        """擦除并写入一个 4KB 扇区，写后回读校验。返回 bool"""
        try:
            self.ldr.flash_erase_sector(addr)
            off = 0
            while off < SECTOR:
                n = min(SECTOR - off, MAX_CHUNK)
                self.ldr.flash_write(addr + off, data[off:off + n])
                off += n
            got = bytearray()
            while len(got) < SECTOR:
                n = min(MAX_CHUNK, SECTOR - len(got))
                got += self.ldr.flash_read(addr + len(got), n)
            if bytes(got) == data:
                return True
            self.log(f"  扇区 0x{addr:06X} 回读校验不一致")
            return False
        except Exception as e:
            self.log(f"  扇区 0x{addr:06X} 写入异常: {type(e).__name__}: {e}")
            return False

    def write_plan(self, image, plan_addrs, progress=None, base=0):
        """按 plan_addrs 列表逐扇区写入。返回 (成功?, 剩余未完成列表)

        base 为 image 在 Flash 上的起始偏移：jl_isd 缓冲区从 0 索引，
        但其物理地址从 ISD_BASE 起，故取数据时用 image[addr-base]。
        整片恢复时 base=0，行为与原先完全一致。
        """
        if not plan_addrs:
            return True, []
        total = len(plan_addrs)
        t0 = time.time()
        for idx, addr in enumerate(plan_addrs, 1):
            data = image[addr - base:addr - base + SECTOR]
            ok = False
            for attempt in range(1, WRITE_RETRY + 1):
                if self.write_sector(addr, data):
                    ok = True
                    break
                if attempt < WRITE_RETRY:
                    self.log(f"  扇区 0x{addr:06X} 第 {attempt} 次失败，重试")
                    time.sleep(0.4)
            if not ok:
                self.log_raw("\n")
                self.log(f"[失败] 扇区 0x{addr:06X} 重试 {WRITE_RETRY} 次仍失败")
                return False, plan_addrs[idx - 1:]
            if idx % 8 == 0 or idx == total:
                rate = idx * SECTOR / 1024.0 / max(time.time() - t0, 0.001)
                self.log_raw(f"\r  [{time.strftime('%H:%M:%S')}] 写入: {idx}/{total} 扇区 "
                             f"({idx*100//total:3d}%, {rate:.0f} KB/s)   ")
                self.log_only_file(f"[{time.strftime('%H:%M:%S')}] 写入 {idx}/{total} 扇区 ({idx*100//total}%)")
                if progress:
                    progress(idx * 100 // total)
        self.log_raw("\n")
        self.log(f"  写入完成：{total} 个扇区，耗时 {time.time() - t0:.1f} 秒")
        return True, []

    def verify(self, image, rounds=3):
        """全片回读 SHA256 校验；不一致自动补刷差异扇区。
        返回 (成功?, 差异扇区列表 or None)"""
        for round_no in range(1, rounds + 1):
            self.log("")
            self.log(f"  写后校验（第 {round_no} 轮）...")
            rb = self.read_flash()
            actual = hashlib.sha256(rb).hexdigest()
            expect = hashlib.sha256(image).hexdigest()
            self.log(f"    回读 SHA256 : {actual}")
            self.log(f"    目标 SHA256 : {expect}")
            if actual == expect:
                self.log("    [校验通过] 全片一致。")
                return True, None
            bad = [a for a in range(0, FLASH_SIZE, SECTOR)
                   if rb[a:a + SECTOR] != image[a:a + SECTOR]]
            self.log(f"    [校验失败] {len(bad)} 个扇区不一致")
            if not bad:
                self.log("    扇区级比对一致但哈希不同，疑似回读错位，不做补刷")
                return False, None
            if round_no == rounds:
                return False, bad
            self.log(f"    补刷 {len(bad)} 个差异扇区 ...")
            fail = []
            for i, addr in enumerate(bad, 1):
                data = image[addr:addr + SECTOR]
                ok = False
                for attempt in range(1, WRITE_RETRY + 1):
                    if self.write_sector(addr, data):
                        ok = True
                        break
                    time.sleep(0.4)
                if not ok:
                    fail.append(addr)
                if i % 8 == 0 or i == len(bad):
                    self.log_raw(f"\r    补刷: {i}/{len(bad)} 扇区      ")
            self.log_raw("\n")
            if fail:
                self.log(f"    补刷失败 {len(fail)} 个扇区")
                return False, fail
        return False, None

    def verify_isd(self, isd, rounds=3):
        """jl_isd 窗口写后校验：只回读 ISD_BASE 起的 len(isd) 字节。

        与整片 verify 的区别：校验范围被严格限制在一体化固件窗口内，
        boot 区和其后的资源/字库区不参与，避免把“本就不写”的区域误判为差异。
        不一致时按扇区自动补刷。返回 (成功?, 差异扇区列表 or None)。
        """
        end = ISD_BASE + len(isd)
        for round_no in range(1, rounds + 1):
            self.log("")
            self.log(f"  写后校验（第 {round_no} 轮，窗口 "
                     f"0x{ISD_BASE:X}-0x{end:X}）...")
            rb = self.read_window(ISD_BASE, len(isd))
            actual = hashlib.sha256(rb).hexdigest()
            expect = hashlib.sha256(isd).hexdigest()
            self.log(f"    回读 SHA256 : {actual}")
            self.log(f"    目标 SHA256 : {expect}")
            if actual == expect:
                self.log("    [校验通过] 一体化固件窗口一致。")
                return True, None

            # 差异扇区以物理地址表示；read_window 返回值从 ISD_BASE 起，
            # 故第 k 个扇区对应物理地址 ISD_BASE + k*SECTOR。
            sector_count = (len(isd) + SECTOR - 1) // SECTOR
            bad = [ISD_BASE + k * SECTOR for k in range(sector_count)
                   if rb[k * SECTOR:(k + 1) * SECTOR]
                   != isd[k * SECTOR:(k + 1) * SECTOR]]
            self.log(f"    [校验失败] {len(bad)} 个扇区不一致")
            if not bad:
                self.log("    扇区级比对一致但哈希不同，疑似回读错位，不做补刷")
                return False, None
            if round_no == rounds:
                return False, bad
            self.log(f"    补刷 {len(bad)} 个差异扇区 ...")
            ok, failed = self.write_plan(isd, bad, base=ISD_BASE)
            if not ok:
                self.log(f"    补刷失败，首个未完成扇区 0x{failed[0]:06X}")
                return False, failed
        return False, None

    def reset(self):
        """复位重启"""
        try:
            self.ldr.run_app(1)
            self.log("  复位指令已发送，设备将重启")
            return True
        except Exception as e:
            self.log(f"  复位指令异常（{type(e).__name__}），请手动断电重新上电")
            return False

    # Logger 辅助：进度条等需要特殊处理的输出
    def log_raw(self, text):
        """无换行输出（进度条），只上控制台，不进日志文件"""
        try:
            print(text, end="", flush=True)
        except Exception:
            pass

    def log_only_file(self, text):
        """只写日志文件，不上控制台（用于进度条里程碑）"""
        fp = self.log_fp
        if fp is not None:
            try:
                fp.write(text + "\n")
                fp.flush()
            except Exception:
                pass


# ---------------------------------------------------------------- 分析

def sha256(b):
    return hashlib.sha256(b).hexdigest()


def compress_ranges(addrs):
    """把扇区地址列表压缩成连续区间 [(start, end), ...]"""
    if not addrs:
        return []
    addrs = sorted(set(addrs))
    out, start, prev = [], addrs[0], addrs[0]
    for a in addrs[1:]:
        if a == prev + SECTOR:
            prev = a
            continue
        out.append((start, prev + SECTOR))
        start = prev = a
    out.append((start, prev + SECTOR))
    return out


def analyze(current, image, allow_loader_write=False):
    """分析当前 flash 与目标镜像差异，返回写入计划"""
    blank = b"\xff" * SECTOR
    same, diff, dev_blank, bk_blank, both = 0, [], [], [], []
    for a in range(0, FLASH_SIZE, SECTOR):
        s, b = current[a:a + SECTOR], image[a:a + SECTOR]
        if s == b:
            same += 1
        else:
            diff.append(a)
            (dev_blank if s == blank else bk_blank if b == blank else both).append(a)

    to_write = [a for a in diff if a >= LOADER_END or allow_loader_write]
    skipped_loader = [a for a in diff if a < LOADER_END and not allow_loader_write]

    return {
        "same": same, "diff": diff, "dev_blank": dev_blank,
        "bk_blank": bk_blank, "both": both,
        "loader_identical": current[:SECTOR] == image[:SECTOR],
        "to_write": to_write, "skipped_loader": skipped_loader,
        "identical": not diff,
    }


def validate_isd(isd: bytes) -> tuple[bool, str | None]:
    """校验 jl_isd 一体化固件是否可安全写入。返回 (ok, 错误信息)。

    为什么要单独校验而不复用 validate_image：jl_isd 不是 1MB 整片镜像，
    它只覆盖 ISD_BASE 起的应用窗口，因此判据是“扇区对齐 + 落在 Flash 内
    + 头部魔数正确 + 非空白”，而不是长度恰为 1MB。
    """
    if not isd:
        return False, "文件为空"
    if len(isd) % SECTOR != 0:
        return False, (f"文件大小 {len(isd)} 不是 {SECTOR} 字节的整数倍，"
                       "无法按扇区对齐写入")
    if ISD_BASE + len(isd) > FLASH_SIZE:
        return False, (f"落盘范围 0x{ISD_BASE:X}-0x{ISD_BASE + len(isd):X} "
                       f"超出 Flash 容量 0x{FLASH_SIZE:X}")
    if isd[:len(ISD_MAGIC)] != ISD_MAGIC:
        return False, ("文件头不是 jl_isd 魔数 "
                       f"0x{ISD_MAGIC.hex()}，可能不是一体化固件")
    # 只跳过头部魔数检查主体：防止“魔数正确但其余全空白”的损坏文件蒙混过关，
    # 真实 jl_isd 的主体是加密段，绝不可能整片为 0xFF/0x00。
    body = isd[len(ISD_MAGIC):]
    if body and (body == b"\xff" * len(body) or body == b"\x00" * len(body)):
        return False, "除头部魔数外全部为空白（0xFF/0x00），文件疑似损坏，拒绝写入"
    return True, None


def analyze_isd(window: bytes, isd: bytes) -> dict[str, object]:
    """对比设备当前窗口与 jl_isd，生成写入计划。

    window 为设备从 ISD_BASE 回读的内容，长度应与 isd 相同；isd 从 0 索引，
    但输出的扇区地址统一换算为物理地址（ISD_BASE + k*SECTOR），这样后续
    write_plan / verify_isd 拿到的地址可直接用于设备，无需再做换算。
    """
    blank = b"\xff" * SECTOR
    same = 0
    diff: list[int] = []
    dev_blank: list[int] = []
    bk_blank: list[int] = []
    both: list[int] = []
    sector_count = len(isd) // SECTOR
    for k in range(sector_count):
        start = k * SECTOR
        device_sector = window[start:start + SECTOR]
        target_sector = isd[start:start + SECTOR]
        addr = ISD_BASE + start
        if device_sector == target_sector:
            same += 1
            continue
        diff.append(addr)
        if device_sector == blank:
            dev_blank.append(addr)
        elif target_sector == blank:
            bk_blank.append(addr)
        else:
            both.append(addr)

    return {
        "same": same,
        "diff": diff,
        "dev_blank": dev_blank,
        "bk_blank": bk_blank,
        "both": both,
        "to_write": diff,          # 整个窗口都在应用区，无 loader 保护问题
        "identical": not diff,
        "sector_count": sector_count,
    }


def isd_risk_assessment(plan: Mapping[str, object]) -> tuple[str, list[str]]:
    """评估刷写一体化固件的风险，返回 (等级 none/ok/warn/high, 提示列表)。"""
    diff = plan["diff"]
    if plan["identical"]:
        return "none", ["设备固件窗口与 jl_isd 完全一致，无需写入"]

    sector_count = int(plan["sector_count"])
    warns = [
        "本操作写入的是一体化固件（含加密 uboot/app），实质为【刷写/更换固件】，"
        "boot 保护区(0x0-0x1000)与资源区不受影响",
    ]
    level = "warn"
    if len(diff) / sector_count > 0.5:
        warns.append(f"差异扇区占比 {len(diff) / sector_count * 100:.0f}%，"
                     "写入范围较大")
        level = "high"
    return level, warns


def firmware_identity(current, fw_dir, quiet=True):
    """鉴定设备当前固件身份：与固件包里的 jl_isd.bin 比对"""
    result = {"matched": None, "diff_bytes": None, "total": None, "first": None, "last": None}
    fw_dir = Path(fw_dir)
    if not fw_dir.is_dir():
        return result
    candidates = list(fw_dir.rglob("jl_isd.bin"))
    if not candidates:
        return result
    best = None
    for isd_path in candidates:
        try:
            isd = isd_path.read_bytes()
        except Exception:
            continue
        seg = current[LOADER_END:LOADER_END + len(isd)]
        if len(seg) != len(isd):
            continue
        n = sum(1 for i in range(len(isd)) if isd[i] != seg[i])
        if best is None or n < best[0]:
            best = (n, isd_path)
    if best:
        n, isd_path = best
        isd = isd_path.read_bytes()
        diffs = [i for i in range(len(isd)) if isd[i] != current[LOADER_END + i]]
        result.update({
            "matched": isd_path, "diff_bytes": n, "total": len(isd),
            "first": LOADER_END + diffs[0] if diffs else None,
            "last": LOADER_END + diffs[-1] if diffs else None,
        })
        if not quiet:
            print(f"固件鉴定: 与 {isd_path.name} 比对，{len(isd)} 字节中差 {n} 字节")
    return result


def risk_assessment(plan, identity):
    """返回 (风险等级, 提示列表)。等级 none/ok/warn/high"""
    warns = []
    level = "ok"
    if plan["identical"]:
        return "none", ["设备 Flash 与目标镜像完全一致，无需写入"]

    if identity.get("matched") and identity["diff_bytes"] <= 64:
        warns.append(f"设备当前运行的是固件包 {identity['matched'].name}，"
                     f"而目标镜像是另一份固件 —— 本操作实质是【换固件/降级】，不是恢复出厂")
        level = "high"

    if not plan["loader_identical"] and not plan["skipped_loader"]:
        warns.append("loader 区与镜像不一致，已按保护策略跳过该区域")
        level = "high" if level == "ok" else level

    if plan["dev_blank"]:
        warns.append(f"{len(plan['dev_blank'])} 个扇区将被写入（原设备为空白 0xFF）")
    if plan["bk_blank"]:
        warns.append(f"{len(plan['bk_blank'])} 个扇区将被清空为 0xFF（原设备有内容）")
        if level == "ok":
            level = "warn"

    ratio = len(plan["diff"]) / (FLASH_SIZE // SECTOR)
    if ratio > 0.5:
        warns.append(f"差异扇区占比 {ratio*100:.0f}%，写入范围很大")
        if level == "ok":
            level = "warn"

    return level, warns


# ---------------------------------------------------------------- 镜像校验

def validate_image(image):
    """校验镜像是否可安全写入。返回 (ok, 错误信息)"""
    if len(image) != FLASH_SIZE:
        return False, f"镜像大小不是 {FLASH_SIZE} 字节，而是 {len(image)} 字节"
    if image == b"\xff" * FLASH_SIZE:
        return False, "镜像是全 0xFF（空白），拒绝写入"
    if image == b"\x00" * FLASH_SIZE:
        return False, "镜像是全 0x00，拒绝写入"
    return True, None
