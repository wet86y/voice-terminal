# 0.9.3 构建、本机安装与公开发行验收

验证日期：2026-10-19。GitHub（代码托管平台）于香港时间 20:32:57 发布 `v0.9.3`。标签指向源码提交 [`337b36e9dd10ada6119d2893838c8fb3e14e1423`](https://github.com/wet86y/voice-terminal/commit/337b36e9dd10ada6119d2893838c8fb3e14e1423)，公开最新发行入口及更新清单均返回 `0.9.3`。

## 正式程序与安装器

本机正式程序使用构建编号 `product-4bd00c49aff3e1f3`，显示版本为 `0.9.3`。统一程序包含 347 个固定文件，大小 103,513,901 字节，SHA-256（完整文件摘要）为 `cf7fedc4e9cdf26c560b6b258c35812c5cb9e905f3d1e6feb30dcb68cffedd08`。

普通分发安装器 `VoiceTerminalSetup.exe` 大小为 112,884,224 字节，SHA-256 为 `08fad57bd8ab807fcaa72d9d07f1c3ad763c2e25cba05a883d2659d51c5aea49`。安装器构建戳为优化构建且 `qualification=false`（隔离验收入口关闭）；安装器内嵌的程序包摘要与本机正式程序一致。

| GitHub 发行资产 | 大小 | SHA-256（完整文件摘要） |
| --- | ---: | --- |
| `VoiceTerminalSetup.exe`（普通分发安装器） | 112,884,224 字节 | `08fad57bd8ab807fcaa72d9d07f1c3ad763c2e25cba05a883d2659d51c5aea49` |
| `delivery.json`（交付记录） | 724 字节 | `6fce2c280d27c131bb5d7d143e5997cc5eea4c9c036efefb6f234934a7059d43` |
| `update.json`（更新清单） | 2,055 字节 | `506f7e1cdba49059010f997839fc6176813637299e315e7e8e136814db23bc5f` |
| `SHA256SUMS.txt`（摘要列表） | 247 字节 | `cebae226a3457999674f9cb5fcd1adb191aa6c77c8bcb4a13dbb44cdf7397bf6` |

四项发行资产均从[公开发行页](https://github.com/wet86y/voice-terminal/releases/tag/v0.9.3)实际回下载，逐项比对本机资产的完整摘要并一致。GitHub 资产元数据的大小和摘要也匹配；公开 latest（最新发行）入口及 `update.json`（更新清单）均返回 `0.9.3`。本机已安装程序和安装器共用同一构建编号与程序包摘要。

## 构建和安装

- `npm run frontend:build`（前端类型检查与生产构建）通过。
- `scripts/publish-desktop.ps1`（本机程序发布脚本）完成桌面宿主、启动入口、程序包组装，并事务替换 `.local`（本机正式程序目录）内的程序本体。安装脚本确认用户数据、模型、运行环境和组件登记得到保留。
- `scripts/distribute-desktop.ps1 -SkipProductBuild`（复用刚生成的程序包构建分发安装器）通过。分发器构建戳中的嵌入摘要与程序包及本机安装记录一致。
- `scripts/publish-github-release.ps1 -SkipBuild`（复用已构建安装器发布 GitHub 发行版）通过，发行脚本检查了版本、构建戳、清单和安装器大小及摘要。
- 本轮发行使用的功能回归记录见[GitHub 节点与四路下载验收](github-node-selection-20261019.md)和[关于页与工作区验收](about-workspace-acceleration-20261019.md)：前端相关交互 24 项、原生公共模块 135 项、桌面宿主与权限 27 项通过；11 项既有手动验收保持忽略。0.9.3 发行阶段重新执行了前端生产构建和上述正式构建流程，没有重复运行整套测试。

新功能说明见[v0.9.3 发行说明](../releases/v0.9.3.md)，当前正式资产摘要及版本指针见[当前交付记录](../releases/current-release.md)。
