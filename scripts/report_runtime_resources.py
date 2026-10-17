"""汇总已完成的本地证据；不自动启用候选或推算整套应用的节省量。"""
import json
from pathlib import Path
import statistics

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
REPORTS = ROOT / ".local/cache/reports"


def read(name):
    return json.loads((REPORTS / name).read_text(encoding="utf-8-sig"))


def main():
    lines = ["# 运行资源优化实施与验收（2026-10-03）", "",
        "实现已接入项目，现用进程与全局程序运行环境未重启、未替换。大项优化候选保持关闭。完整应用性能门槛和用户开启监听后的麦克风现场验收尚未完成，不能将本报告写成生产性能验收通过。", "",
        "## 实现与默认状态", "",
        "| 项目 | 实现与当前状态 |", "| --- | --- |",
        "| 宿主资源策略 | 启动和重启前统一线程与下载缓存环境；默认保留原线程预算 |",
        "| 运行依赖 | Python（程序运行环境）3.12，113项业务依赖约束及两个张量框架组合；独立环境检查通过，保留现用环境 |",
        "| 主服务设备探测 | 查询读取已加载工作进程报告，必要时一次性隔离探测；主服务查询不加载张量框架或初始化图形处理器计算上下文 |",
        "| 4线程候选 | 按进程可选；完整应用启动、并发及精修性能未过门槛，默认关闭 |",
        "| 轻量检测候选 | 当前未量化权重导出、数值校验、200毫秒流式接口及失败回退已实现；默认原后端 |",
        "| 音频与缓存 | 所有者计量、原生完成租约、格式区分转换、范围复用及协商后的共享路径已实现；复用和共享候选关闭 |",
        "| 完整历史 | 有界读取缓存、完整记录落盘、去重索引、迟到精修、失败保留、可读文档及原成对归档兼容已实现；默认原内存路径 |",
        "| 图形处理器模型 | 流式识别与整句精修保持独立，未合并进程、未迁移推理引擎 |", "",
        "## 测量范围", "",
        "原约13.18 GiB（二进制吉字节）私有提交来自运行中的受管进程树参考快照，不是本轮完整应用持续负载峰值。下表是独立模型进程，不能直接相加推算13.18 GiB会减少多少。", "",
        "真实权重固定在本机模型目录，输入为本地示例录音。原源码来自改动前保存的后端快照。短测量通常12次，检测短测量5次；启动只有单次且受文件缓存与同时运行的持续回放影响，不能据此判定启动性能等价。提交和驻留为推理后取样最大值，未连续捕获每个瞬时峰值。", "",
        "| 模型 | 原预算/4线程中位耗时，毫秒 | 原预算/4线程私有提交，GiB | 原预算/4线程常驻内存，GiB | 输出一致 |",
        "| --- | --- | --- | --- | --- |"]
    for role, label in (("speaker", "声纹"), ("asr", "流式识别"), ("refine", "整句精修"), ("endpoint", "结束判断")):
        a, b = (read(f"resource-{role}-{variant}.json") for variant in ("baseline", "threads"))
        def peak(value, key):
            return max(row[key] for row in value["memory_samples"]) / 2**30
        lines.append(f"| {label} | {a['latency_ms']['p50']:.2f} / {b['latency_ms']['p50']:.2f} | "
                     f"{peak(a,'private_commit_bytes'):.3f} / {peak(b,'private_commit_bytes'):.3f} | "
                     f"{peak(a,'rss_bytes'):.3f} / {peak(b,'rss_bytes'):.3f} | {'是' if a['results']==b['results'] else '否'} |")
    lines += ["", "整句精修首轮候选中位耗时较高；两次30样本交错复测如下。尚不足以认定稳定退化或性能等价，分析进程继续保留原预算。", "",
              "| 复测 | 原预算中位，毫秒 | 4线程中位，毫秒 |", "| --- | --- | --- |"]
    for serial in (1, 2):
        a, b = (read(f"resource-refine-{variant}-repeat{serial}.json") for variant in ("baseline", "threads"))
        lines.append(f"| {serial} | {a['latency_ms']['p50']:.2f} | {b['latency_ms']['p50']:.2f} |")
    a, b = read("resource-vad-baseline.json"), read("resource-vad-onnx.json")
    lines += ["", "## 轻量检测功能与短测量", "",
              f"固定整段示例录音的中位耗时：原后端{a['latency_ms']['p50']:.2f}毫秒，候选{b['latency_ms']['p50']:.2f}毫秒。候选未导入 PyTorch（张量推理框架）。这是单模型处理一整段音频的耗时，不是应用端点延迟。", "",
              f"取样最大私有提交：原后端{max(x['private_commit_bytes'] for x in a['memory_samples'])/2**30:.3f} GiB，候选{max(x['private_commit_bytes'] for x in b['memory_samples'])/2**30:.3f} GiB；常驻内存分别{max(x['rss_bytes'] for x in a['memory_samples'])/2**30:.3f} GiB和{max(x['rss_bytes'] for x in b['memory_samples'])/2**30:.3f} GiB。二者运行环境不同，不能把差值视作完整主服务的已验收收益。", "",
              "当前权重导出在独立模型副本完成，原模型根目录未改动。1、5、20、30、65帧的模型输出及缓存对照通过，最大绝对数值差约0.0000141，未量化。", ""]
    original, candidate = read("resource-vad-functional-original.json"), read("resource-vad-functional-onnx.json")
    valid = [key for key in original if original[key]["error"] is None]
    same = sum(original[key]["events"] == candidate[key]["events"] for key in valid)
    lines += [f"42种固定输入组合覆盖长录音、20/40/200毫秒输入分块、极短输入、空最终刷新和重置。原后端正常完成{len(valid)}种，候选对应事件一致{same}种；候选错误数为{sum(bool(value['error']) for value in candidate.values())}。原后端另外6种极短输入组合报错，候选均正常处理。", "",
              "## 60分钟持续检测回放", "",
              "两组按20毫秒音频块连续回放同一录音，共同运行并同时承受其他验证负载；候选使用独立验证环境。此测试覆盖检测过程，不代表60分钟完整应用长会话。分位耗时是末8192个音频块；较小中位值包含等待模型凑满窗口的块。", "",
              "| 指标 | 原预算 | 4线程候选 |", "| --- | --- | --- |"]
    a, b = (read(f"resource-vad-{variant}-soak.json")["soak"] for variant in ("baseline", "threads"))
    for label, av, bv in (("持续秒数", f"{a['elapsed_seconds']:.2f}", f"{b['elapsed_seconds']:.2f}"),
                          ("处理音频块", str(a["frames"]), str(b["frames"])),
                          ("块处理95百分位，毫秒", f"{a['latency_ms']['p95']:.3f}", f"{b['latency_ms']['p95']:.3f}"),
                          ("块处理99百分位，毫秒", f"{a['latency_ms']['p99']:.3f}", f"{b['latency_ms']['p99']:.3f}")):
        lines.append(f"| {label} | {av} | {bv} |")
    for key, label in (("private_commit_bytes", "私有提交"), ("rss_bytes", "常驻内存")):
        def ends(value):
            samples = value["memory_samples"]
            return f"{samples[0][key]/2**30:.4f} / {samples[-1][key]/2**30:.4f}"
        lines.append(f"| {label}首次/末次取样，GiB | {ends(a)} | {ends(b)} |")
    difference = (a["memory_samples"][-1]["private_commit_bytes"] - b["memory_samples"][-1]["private_commit_bytes"]) / 2**30
    lines += ["", f"端点事件摘要一致：{a['event_digest']==b['event_digest']}。末次私有提交差约{difference:.3f} GiB；物理驻留没有对应幅度的稳定减少，系统对两组工作集进行了回收。输入由回放工具产生，不能将处理块数解释为真实麦克风与应用队列无丢帧。", "",
              "原估计全应用可减少2—3.6 GiB私有提交仍属于未确认估计，未计入已实现收益。", "",
              "## 完整历史验证", ""]
    a, b = read("resource-history-memory.json"), read("resource-history-disk.json")
    lines += [f"500轮纯文本历史，完整主模型历史摘要一致：{a['history_digest']==b['history_digest']}；迟到精修保留且不改写冻结正文。候选实际读取缓存{b['read_cache_bytes']}字节，不超过8 MiB（二进制兆字节）。", "",
              f"完整历史读取后的私有提交为{a['settled_private_commit_bytes']/2**20:.2f}/{b['settled_private_commit_bytes']/2**20:.2f} MiB，常驻内存为{a['settled_rss_bytes']/2**20:.2f}/{b['settled_rss_bytes']/2**20:.2f} MiB，依次为原路径/候选。此样本收益很小；读取缓存预算不是整个进程内存上限。", "",
              f"历史追加中位耗时为{statistics.median(a['commit_samples_ms']):.3f}/{statistics.median(b['commit_samples_ms']):.3f}毫秒。落盘有额外处理与写入开销，需完整应用确认是否超出原测量波动，候选保持关闭。", "",
              "真实数据库写入失败测试保留待写材料并成功重试；缓存逐出、迟到精修、重置、冻结历史、可读全文、长文本附件和归档标识兼容验证通过。重置不重放旧任务。持续磁盘故障时，未落盘材料仍需由活动进程保留，强制结束进程不能保证这些内存材料持久化；本次没有把该故障路径认作生产退出验收完成。", "",
              "## 回归与安装验证", ""]
    for name, label in (("resource-full-regression.txt", "现用环境完整后端回归"),
                        ("resource-isolated-regression.txt", "独立环境完整后端回归")):
        rows = (REPORTS / name).read_text(encoding="utf-8", errors="replace").splitlines()
        summaries = [row for row in rows if " passed" in row and " skipped" in row]
        lines.append(f"- {label}：{summaries[-1] if summaries else '尚未完成，不能填写通过'}。")
    lines += ["- 17项跳过对应已移除的旧影子代理链或旧被动进度邮箱；不是以局部通过代替全量。登记过的全量挂起本次未复现，使用90秒堆栈诊断门槛完成全量运行，没有宣称已定位历史挂起根因。",
              "- 新协议真实本机连接验证覆盖过期引用只在原生提交前回退、取消后继续读取、创建方关闭、接收进程正常结束及崩溃回收；也验证了虚拟环境实际业务进程身份。",
              "- 前端70项通过，类型检查、构建和差异检查通过。7项后端警告来自本机连接测试使用的已弃用第三方连接接口。",
              "- 两项旧启动脚本断言已改为检查现有宿主启动职责；没有向启动脚本填入已被替代的旧实现来让断言通过。",
              "- 独立环境113项清单无版本不匹配，包依赖一致性检查通过；安装解析器对照通过。环境来自现用分发文件副本加锁定缺失包安装，不宣称从空环境下载重装全部大型安装包。",
              "- 全局环境、当前模型权重与正在运行的应用保持现状。安装器默认创建独立候选目录，现用启动脚本不自动切换到候选环境。", "",
              "## 尚未达到的启用门槛", "",
              "完整应用在固定设备、相同音频、固定云端响应下的交错启动、并发识别、精修、声纹、首字、端点、首音、播放打断以及60分钟长会话对照仍需完成；完整应用停止、重置、崩溃和存储故障组合现场回归也未被单模型测试替代。用户开启监听后完成麦克风现场验收。", "",
              "因此4线程、轻量检测、音频复用与共享、完整历史落盘均未成为默认配置，原有效上下文和模型热态继续保留。本报告不声称已经降低正在运行的应用内存。", "",
              "策略及回退方法见 `docs/guides/runtime-resource-management.md`（资源管理说明）与 `config/runtime-resources.json`（默认资源策略）。原始证据为本目录中以 `resource-`（资源验证）开头的结构化结果及日志；`private_commit_bytes`（私有提交字节）、`rss_bytes`（常驻字节）、`latency_ms`（延迟毫秒）、`results`（原始模型结果）、`soak`（持续回放结果）分别保存对应原始指标。"]
    target = REPORTS / "运行资源优化实施与验收-20261003.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
