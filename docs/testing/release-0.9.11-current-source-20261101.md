# Voice Terminal v0.9.11 当前源码同版本重发记录（2026-11-01）

## 更新日志合并

上次公开发行于 2026-11-01 17:55（香港时间），标签 `v0.9.11` 指向 `4682ab829f1d65045abfac6b291a9561c5b85d44`，程序构建为 `product-5b6d10523a7c0bd9`。本次按同版本重发流程撤回该发行，并以当前源码提交 `99248053521d01f6436bf583ed89def3dcba442b` 重新构建和发布。

`docs/releases/v0.9.11.md`（公开及程序内共用的版本说明）保留上次公开发行中的全部条目，包括自定义指令、双主语音后端、共享密钥、后台执行宿主、工具调用和运行状态修复；并合入本机更新后尚未公开的三项界面变化：播放目标草稿与单独保存、MiniMax 音色名称随标识输入在线查询、移除声纹页过时的 CAM++（声纹模型）迁移提示。新发行正文与该文件一致。

## 旧发行撤回与旧版交接

撤回前将旧发行记录、正文和四项资产保存到 `.local/tmp/release-v0.9.11-before-reissue-20261101-2002`（本机旧发行归档目录）。旧标签指向 `4682ab829f1d65045abfac6b291a9561c5b85d44`；发行和标签撤回后，GitHub（代码托管平台）最新入口回退到 `v0.9.10`，新发行完成后重新指向 `v0.9.11`。

从旧 `VoiceTerminalSetup.exe`（普通分发安装器）中恢复其内嵌程序包，构建为 `product-5b6d10523a7c0bd9`，程序包大小 105,074,760 字节，SHA-256（内容摘要）为 `700990906573921a7a2193f20191eece7aaaf7aa8a82aa3f922164c02a018322`；摘要与旧 `delivery.json`（交付记录）一致。使用该旧包运行 `scripts/check-app-update-handoff.py`（真实应用更新交接验收脚本），旧程序仍运行及已退出两种场景均更新成功。每轮核对 378 个固定文件，保留 4 个虚构数据与组件哨兵文件，目标均为 `product-ca0f135039cecc6f`；隔离工作区和进程按脚本流程回收。

## 当前源码构建与发行资产

源码 `99248053521d01f6436bf583ed89def3dcba442b` 已推送至 `origin/main`（GitHub 远程主分支）。`npm run desktop:dist`（正式分发构建）准备程序包并生成普通安装器，没有更新本机程序。新 `v0.9.11` 标签及公开发行于 2026-11-01 20:21:34（香港时间）发布，标签、远程 `main` 与构建源码均指向该提交。

| 项目 | 本轮结果 |
| --- | --- |
| 正式程序构建 | `0.9.11 / product-ca0f135039cecc6f` |
| 固定文件数 | 378 个 |
| 程序包大小 | 105,076,078 字节 |
| 程序包 SHA-256（内容摘要） | `a966ddc173dffcb6cce90a52d0b92f3fccf2a2ea7bf86ab06ab73ec376058c29` |
| 普通安装器大小 | 115,230,208 字节 |
| 安装器 SHA-256（内容摘要） | `a837ae6f06506c24e4c0d89a482b75104bcd6c3cb6fa7cdd6d4ff910b4befa81` |
| 发行页 | <https://github.com/wet86y/voice-terminal/releases/tag/v0.9.11> |

## 公开核验

| 文件 | 字节数 | SHA-256（内容摘要） |
| --- | ---: | --- |
| `VoiceTerminalSetup.exe` | 115,230,208 | `a837ae6f06506c24e4c0d89a482b75104bcd6c3cb6fa7cdd6d4ff910b4befa81` |
| `delivery.json`（交付记录） | 893 | `52b38fbc94a17f7d1f66414b71e4dc82f21780559e424ace41d728daaa55b697` |
| `update.json`（更新清单） | 3,064 | `7e4cf0cfa24bab14d3191672a75ef8721c677298d565cf60ad2d6279f9511703` |
| `SHA256SUMS.txt`（摘要列表） | 247 | `5b20b09be50ca4651654efeb55d12aaf2548fa6ba30ac884e9a44aff0fff1c44` |

新发行正文与合并后的版本说明完全一致；GitHub latest（最新发行）入口为 `v0.9.11`，`git ls-remote`（远程引用查询）确认 `main` 和 `v0.9.11` 标签均指向 `99248053521d01f6436bf583ed89def3dcba442b`。`gh release download`（发行资产下载）在内置核验及单独重试时均未完成：首轮返回 `unexpected EOF`（意外结束），重试长时间停留在零字节安装器下载。为避免再次上传，改用四项资产的公开 HTTPS（加密传输）下载地址取回完整文件，再将大小及 SHA-256 分别与本机文件和 GitHub 资产元数据摘要核对，全部一致。失败暂存保留于 `.local/tmp/release-public-cea18678a668443180d14303d3537808` 和 `.local/tmp/release-public-2f716d1cafc54aff9080c23112631269`；直接回下载及校验结果记录于 `.local/cache/reports/release-public-latest.json`（最近公开发行核验报告）。

核验后，本文件与[当前交付记录](../releases/current-release.md)作为纯文档补录另行提交和推送；`v0.9.11` 标签仍绑定实际构建提交 `99248053521d01f6436bf583ed89def3dcba442b`。
