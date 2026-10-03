# BR23 拼音机固件工具

面向杰理 **AC6956C8（BR23 / AC695N 系列）拼音机**的 Windows 固件**备份 / 恢复 / 只读预检**工具。通过杰理 USB Loader V2 协议与设备通信，无需拆机、无需额外下载器即可读取整片 1MB Flash 或写入镜像。

> 本仓库只发布**工具程序与源码**，不随附任何第三方厂商固件。恢复所需的 `.bin` 镜像请使用你自己从设备备份、来源明确的文件。

## 适用设备

| 项目 | 说明 |
| --- | --- |
| 芯片 | 杰理 AC6956C8（BR23 / AC695N） |
| Flash | 内置 1MB NOR Flash（`0x100000`），4KB 扇区 |
| UBOOT 标识 | 枚举为 `BR23 UBOOT1.00` |
| 系统 | Windows 10 / 11（x64），无需管理员权限 |

## 功能

```
  1. 备份固件      读取整片 1MB 存成 .bin（只读）
  2. 恢复固件      预检 → 确认 → 写入 → 校验 → 复位
  3. 查看设备信息  进 UBOOT 查看芯片与固件信息（只读）
  4. 只读预检      只分析不写入，看清恢复会做什么
  0. 退出
```

- **备份（只读）**：整片 1MB 读取到 `backups\backup_日期时间.bin`，不修改设备。
- **恢复**：写前自动存回滚快照；必须输入完整 `YES` 才执行；逐扇区写后回读、失败重试 3 次；写完全片 SHA256 校验，不一致自动补刷差异扇区；任何失败都不复位，设备留在 UBOOT 便于继续处理。
- **设备信息 / 预检**：只读分析 Flash 分布与风险等级，绝不写入。

## 快速开始

1. 到 [Releases](../../releases) 下载最新的 `BR23-Firmware-Tool-v1.0-portable.zip`。
2. 解压到任意目录（**整个目录一起保留，不能只拷 exe**，`_internal` 里是 USB 通信组件和 loader）。
3. 双击 `BR23固件工具.exe`，按中文菜单操作。

目录结构：

```
BR23固件工具\
├── BR23固件工具.exe   主程序
├── _internal\         运行组件（不可删除）
├── backups\           备份与回滚快照（运行后生成）
└── logs\              运行日志（运行后生成）
```

详细说明见 [BR23固件工具-使用说明.md](BR23固件工具-使用说明.md)。

## 安全设计

- loader 只上传到芯片 **SRAM**（断电即失效），不碰 Flash。
- 镜像安全闸门：全 `0xFF` / 全 `0x00` / 大小不符的镜像直接拒绝。
- loader 保护区 `0x0–0x1000` 默认不写。
- 幂等：设备与目标镜像一致时零写入。
- 换固件 / 降级 / 涉及 loader 区会给出**高风险**提示，需再次确认。
- BR23 loader 实测禁用 `0xFC15 / 0xFC16 / 0xFC09`（会超时挂死 USB），工具只使用安全命令子集。

## 从源码运行

```bash
python -m pip install -r requirements.txt
python br23_tool.py
```

`br23core.py` 通过 `bind_modules()` 动态加载 `jl-uboot-tool/` 下的 `scsiio`、`jltech` 与 `data/`，因此仓库目录结构需保持完整。

## 从源码重新打包

```bash
python -m pip install pyinstaller pyyaml crcmod
python -m PyInstaller "BR23固件工具.spec" --noconfirm
```

产物在 `dist\BR23固件工具\`。采用 onedir + console 模式，`scsiio`/`jltech`/`data` 以明文整树落盘到 `_internal\jl-uboot-tool\`。固件身份鉴定为可选功能：若仓库根存在 `固件包_ebook_V0.6.3\`（内含 `jl_isd.bin`）会自动收入，缺失则打包与运行时自动跳过。

## 源码结构

| 文件 | 作用 |
| --- | --- |
| `br23_tool.py` | 菜单主程序、终端编码、流程编排 |
| `br23core.py` | 核心库：设备连接、读写、镜像校验、风险分析 |
| `BR23固件工具.spec` | PyInstaller 打包配置 |
| `jl-uboot-tool/` | 杰理 USB/UART 底层通信库与 loader 文件 |

## 免责声明

刷写 Flash 存在让设备无法启动（变砖）的风险。使用前请务必先**备份**，并确认目标镜像来源可靠。作者不对因使用或误用本工具造成的任何设备损坏、数据丢失承担责任。本工具不提供、不转发任何受版权保护的厂商固件。

## 第三方组件署名

- [`jl-uboot-tool`](jl-uboot-tool/) 基于 Andrey Grigoryev（kagaimiq）的开源项目，采用 **MIT License**（见 [jl-uboot-tool/LICENSE](jl-uboot-tool/LICENSE)），Copyright (c) 2023 Andrey Grigoryev。
