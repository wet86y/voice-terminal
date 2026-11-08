# 本地语音独立参考登记与默认选择

## 模型、参考录音和默认选择

GPT-SoVITS（两阶段语音合成模型）的权重导出、精度转换和推理引擎编译不把某段参考录音训练进权重。AnifLive（本地推理框架）的完整转换入口仍要求一套参考条件，用于生成可启动的包及执行实际合成验收。本项目将这套验收条件与用户的默认音色选择分开。

每段来源录音独立保存名称、稳定编号、标签、转写、语言、来源及就绪状态。默认选择只引用其中一段已就绪录音：设置默认时保留原记录，并在运行时情绪候选中排除该编号及相同素材；更换默认后，原录音重新参与普通候选。取消默认不会删除录音、素材或历史版本。未登记默认时，正式合成明确提示缺少默认参考，不回退到包内验收录音；仍可对独立录音进行显式试听。

作者推荐只在首次参考登记中消费一次。推荐必须指向本次来源清单中的唯一录音，且转写、语言、音频校验均通过。后续同步、任务重试、懒初始化和用户修改都不能重新套用推荐。没有推荐、字段冲突、路径歧义或推荐录音未就绪时，保留“默认合成参考未登记”，提示用户通过录音卡片上的开关选择。

既有音色版本及用户选择保持原样。没有新增状态字段的旧记录沿用原有基础参考语义；新增独立参考策略由转换输入和模型收据交接，避免参考资料登记之前的音色列表读取把验收录音错误初始化成默认。切换默认只增加音色版本，不重建模型引擎。

## 持久字段及交接

| 字段 | 中文说明 |
| --- | --- |
| `reference_policy: independent-v1` | 独立参考策略，写入转换输入、构建身份及应用模型收据；验收条件不直接产生默认音色 |
| `validation_audio` | 本轮转换验收录音的来源路径，与默认选择无绑定关系 |
| `recommendation_audio` | 已解析且通过校验的一次性作者推荐路径 |
| `recommendation_status` | 推荐匹配、歧义或未登记状态 |
| `default_reference_registered` | 当前音色是否已登记默认参考；明确为假时禁止隐式回退；旧记录缺省保持原语义 |
| `default_reference_id` | 当前选中的独立参考编号 |
| `references` | 独立参考记录集合，包含所选默认录音；传入情绪兼容层时排除默认项 |
| `default_reference_choice` | 模型音色库中的默认选择登记来源，防止同步再次套用作者推荐 |

包内验收录音继续用于可追溯构建与验证，不被当作未登记模型的可用默认音色。兼容组件把“默认未登记”视为合法资料状态，不因此归档整个音色库。补充的验收录音也必须有可见的独立记录，不能变成隐藏素材。

## 公开来源与有限格式适配

截至 2026-11-08，已读取上游源码、多个作者的模型卡及小型配置。没有发现适用于所有微调模型包的默认场景标准。以下支持依据完整字段组合和明确配置上下文建立，不将任意同名字符串当作参考条件。

| 来源结构 | 可识别的录音／转写／语言 | 推荐依据与限制 |
| --- | --- | --- |
| GPT-SoVITS 旧接口参数 | `refer_wav_path`（录音）、`prompt_text`（转写）、`prompt_language`（参考语言） | 普通请求参数不等于默认声明；完整的 `default_refer_path`／`default_refer_text`／`default_refer_language`（默认三元组）或明确默认标记可作为推荐 |
| GPT-SoVITS 新接口参数 | `ref_audio_path`（录音）、`prompt_text`（转写）、`prompt_lang`（参考语言） | 只有显式默认／推荐标记或明确选择器才产生推荐；`text_lang`（目标语言）不代替参考语言 |
| 作者场景及参考清单 | `prompt`（参考参数对象）、`references`（清单）、`file`（清单录音）、`profile`（关联场景） | 唐语歌的 `selected_final`（选定场景）、`final_selected`（最终选定状态）是作者约定；按清单与场景的关系解析 |
| 已识别的语音提供方配置 | `gpt_sovits`／`gpt_sovits_tts`（引擎块）内的 `reference_audio`（录音）、`prompt_text`（转写）、`prompt_language`／`prompt_lang`（参考语言） | 同一对象内解释；不从无关提供方或目标合成文本提取参考条件 |
| minimal-inference（精简推理实现）的 `voices.json`（语音配置） | 根映射 `语音名称 → 配置` 中的 `gpt_path`／`sovits_path`（配对权重）、`ref_audio`（录音）、`ref_text`（转写）、`ref_lang`（参考语言） | 该实现请求默认使用根键 `default`（默认语音）；必须完整识别此结构，不能把其他命令中的同名文本文件参数当转写 |
| `.list`（标注清单）、同名 `.lab`／`.txt`／`.text`（旁注） | 按完整来源路径配对录音；清单可携带参考语言 | 普通旁注不包含语言和默认选择，不猜语言或把第一条记录设为默认 |

依据：[旧接口源码](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api.py)、[新接口源码](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)、[唐语歌参考清单](https://huggingface.co/shiqianyan/TangYuGe-GPT-SoVITS-v2ProPlus/blob/main/manifests/references.yaml)、[Warashi 配置说明](https://github.com/inni918/warashi/blob/main/docs/custom-voice-gpt-sovits.md)、[精简推理配置实现](https://github.com/GPT-SoVITS-Devel/GPT-SoVITS_minimal_inference/blob/master/api_server.py)。

相对路径仅在本次来源清单中解析。来源根与配置目录两种解释都存在且指向不同文件时，标记歧义。旧设备绝对路径不用于读取外部文件，只可在已扫描来源中按完整相对路径后缀唯一映射；无法映射时等待用户处理。中文、英文名称及已知单语代码可以归一化；多语自动识别标记不能凭文本猜成某个固定参考语言。

[AI-Vtuber 的真实配置](https://github.com/Ikaros-521/AI-Vtuber/blob/main/config.json)同时存有多个互斥接口版本对象，配置本身没有明确选用版本。本项目不把这些快照扁平合并。对同一录音声明不同转写或语言的来源也保留冲突，阻止自动默认选择。

## 没有默认推荐的实际模型包

- [Sayuki 微调模型](https://huggingface.co/wsffdg/GPT-SoVITS-v2ProPlus-Finetuned-Sayuki-in-Hokai)要求调用方自备录音和逐字稿。
- [Shiki2023 示例模型](https://huggingface.co/Shiki2023/GPT-SoVits-mcp-Sample-v2ProPlus)仅提供配对权重，没有参考资料。
- [Laura 微调模型](https://huggingface.co/TwinPeaksTownie/GPT_SoVITS_LAURA_v1)提供录音并在说明中建议使用，但训练配置没有完整参考转写与语言映射。
- [Verina 微调模型](https://huggingface.co/huggingkot/Verina-GPT-SoVITS-v2ProPlus-DPO-EN-JA-ZH)包含多语合成演示；输出样例及对应正文不能直接作为参考录音与准确转写。
- [魔搭的 Shorekeeper 模型](https://www.modelscope.cn/models/Richopera/Shorekeeper-zh-gpt-sovits-v2proplus)要求用户提供参考片段及准确转写，没有统一默认推荐字段。

这些模型包没有明确默认声明并不表示权重无效。程序不执行模型仓库脚本，不从自然语言说明中猜默认，不把目标合成正文或生成结果样例自动当作参考条件。
