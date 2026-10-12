# -*- coding: utf-8 -*-
"""br23core 一体化固件（jl_isd）相关纯函数与写入偏移的单元测试。

运行：在仓库根目录执行 `pytest`。
"""
import sys
import time
from pathlib import Path

import pytest

# 仓库未打包成可安装包，这里把根目录加入导入路径以便直接 import br23core。
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import br23core  # noqa: E402


# 构造一份合法的 2 扇区一体化固件：头部为魔数，其余填充非空白数据。
def make_valid_isd() -> bytes:
    first_sector = bytearray(br23core.ISD_MAGIC)
    first_sector += b"\x11" * (br23core.SECTOR - len(br23core.ISD_MAGIC))
    second_sector = b"\x22" * br23core.SECTOR
    return bytes(first_sector) + second_sector


# ------------------------------------------------ validate_isd

def test_validate_isd_合法文件通过() -> None:
    ok, err = br23core.validate_isd(make_valid_isd())
    assert ok
    assert err is None


def test_validate_isd_空文件拒绝() -> None:
    ok, _err = br23core.validate_isd(b"")
    assert not ok


def test_validate_isd_非扇区对齐拒绝() -> None:
    bad = br23core.ISD_MAGIC + b"\x00" * 10
    ok, err = br23core.validate_isd(bad)
    assert not ok
    assert "整数倍" in err


def test_validate_isd_魔数错误拒绝() -> None:
    bad = b"\x00" * (2 * br23core.SECTOR)
    ok, err = br23core.validate_isd(bad)
    assert not ok
    assert "魔数" in err


def test_validate_isd_全空白拒绝() -> None:
    bad = br23core.ISD_MAGIC + b"\xff" * (2 * br23core.SECTOR - 4)
    ok, _err = br23core.validate_isd(bad)
    assert not ok


def test_validate_isd_越界拒绝() -> None:
    # 长度恰好使 ISD_BASE + len 超过 Flash 容量。
    oversized_len = br23core.FLASH_SIZE - br23core.ISD_BASE + br23core.SECTOR
    bad = bytearray(oversized_len)
    bad[:4] = br23core.ISD_MAGIC
    ok, err = br23core.validate_isd(bytes(bad))
    assert not ok
    assert "超出" in err


# ------------------------------------------------ analyze_isd

def test_analyze_isd_窗口一致时无需写入() -> None:
    isd = make_valid_isd()
    plan = br23core.analyze_isd(isd, isd)
    assert plan["identical"] is True
    assert plan["to_write"] == []
    assert plan["same"] == 2


def test_analyze_isd_设备空白扇区计入dev_blank且地址带物理偏移() -> None:
    isd = make_valid_isd()
    window = b"\xff" * br23core.SECTOR + isd[br23core.SECTOR:]
    plan = br23core.analyze_isd(window, isd)
    expected_addr = br23core.ISD_BASE
    assert plan["dev_blank"] == [expected_addr]
    assert plan["to_write"] == [expected_addr]


def test_analyze_isd_输出地址为物理地址而非零基索引() -> None:
    isd = make_valid_isd()
    window = b"\xff" * (2 * br23core.SECTOR)
    plan = br23core.analyze_isd(window, isd)
    assert plan["diff"] == [br23core.ISD_BASE,
                            br23core.ISD_BASE + br23core.SECTOR]


# ------------------------------------------------ isd_risk_assessment

def test_isd_risk_一致时等级为none() -> None:
    isd = make_valid_isd()
    plan = br23core.analyze_isd(isd, isd)
    level, _warns = br23core.isd_risk_assessment(plan)
    assert level == "none"


def test_isd_risk_小范围差异为warn() -> None:
    isd = make_valid_isd()
    window = b"\xff" * br23core.SECTOR + isd[br23core.SECTOR:]
    plan = br23core.analyze_isd(window, isd)
    level, _warns = br23core.isd_risk_assessment(plan)
    assert level == "warn"


def test_isd_risk_大范围差异升级为high() -> None:
    # 3 个扇区中 2 个不同，差异占比 2/3 > 1/2。
    isd = make_valid_isd() + b"\x33" * br23core.SECTOR
    window = b"\xff" * (2 * br23core.SECTOR) + isd[2 * br23core.SECTOR:]
    plan = br23core.analyze_isd(window, isd)
    level, _warns = br23core.isd_risk_assessment(plan)
    assert level == "high"


# ------------------------------------------------ Device.write_plan 基地址

def test_write_plan_使用base时按物理地址取正确扇区() -> None:
    """验证 jl_isd 从 0 索引的缓冲能通过 base 映射到物理地址 0x1000。"""
    device = object.__new__(br23core.Device)
    captured: list[tuple[int, bytes]] = []

    def fake_write_sector(addr: int, data: bytes) -> bool:
        captured.append((addr, bytes(data)))
        return True

    device.write_sector = fake_write_sector
    device.log = lambda text: None
    device.log_raw = lambda text: None
    device.log_only_file = lambda text: None

    isd = make_valid_isd()
    physical_addrs = [br23core.ISD_BASE]
    ok, failed = device.write_plan(isd, physical_addrs,
                                   base=br23core.ISD_BASE)
    assert ok is True
    assert failed == []
    written_addr, written_data = captured[0]
    assert written_addr == br23core.ISD_BASE
    assert written_data == isd[:br23core.SECTOR]


def test_write_plan_默认base0保持整片恢复行为() -> None:
    device = object.__new__(br23core.Device)
    captured: list[bytes] = []
    device.write_sector = lambda addr, data: captured.append(bytes(data)) or True
    device.log = lambda text: None
    device.log_raw = lambda text: None
    device.log_only_file = lambda text: None

    image = b"\x44" * br23core.SECTOR
    ok, _failed = device.write_plan(image, [0])
    assert ok is True
    assert captured[0] == image


# ------------------------------------------------ Device.read_window

def test_read_window_拼接返回且不越界() -> None:
    device = object.__new__(br23core.Device)
    device.chunk_size = lambda: 256
    device.log = lambda text="": None

    class FakeLoader:
        def flash_read(self, addr: int, size: int) -> bytes:
            return bytes([addr % 256]) * size

    device.ldr = FakeLoader()
    size = 1024
    result = device.read_window(br23core.ISD_BASE, size)
    assert len(result) == size


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
