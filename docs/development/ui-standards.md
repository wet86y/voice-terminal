# UI（用户界面）资产与开发规范

本规范适用于主程序、独立组件管理页、设备设置、悬浮窗、启动与维护页面，以及界面样例。界面保持**简洁、克制、直观**：优先复用已有组件、配色和布局，每个区域突出当前任务，反馈清楚且不过度装饰。新增界面与修改既有界面时均须遵守；历史页面中的局部硬编码不作为新增样式的依据。

## 先找资产，再写界面

1. 先查看下表中的对应公共组件，再查看最接近的正式页面。已有组件能通过属性、插槽或组合满足需求时直接复用。
2. 颜色、字号、控件尺寸和反馈从共享设计系统取值；页面局部样式只描述该页面特有的布局和内容。
3. 确需增加公共能力时，扩展对应基础组件；新增有独立语义的组件放入 `apps/frontend/src/interface`（公共界面目录）。业务调用、权限及持久化继续留在既有业务层。
4. 同一功能的正式页与样例页使用同一组件。样例只注入展示数据和事件，不复制正式控件，也不连接真实设备或配置。
5. 验证默认态、悬停、按下、键盘焦点、禁用、处理中、选中或展开状态，以及长内容和小窗口。只运行与改动相关的业务回归，不为样式验证触发下载、删除、迁移或模型运行。

## 资产目录与权威入口

以下路径均相对仓库根目录。英文组件名是源码标识，中文说明描述复用场景。

| 资产或入口 | 职责与复用要求 |
| --- | --- |
| [design.css（共享设计系统）](../../apps/frontend/src/interface/design.css) | 配色、字号、尺寸、表单、设置布局、弹窗与响应式规则的权威入口；正式页、管理页、悬浮窗与样例均加载。 |
| [startup.css（最小共享界面层）](../../apps/desktop/boot/startup.css) | 独立启动、维护、故障页的样式，以及全程序的公共按钮反馈。完整界面由共享设计系统导入；独立入口可直接加载或内嵌，无需先加载界面框架。 |
| [application.css（产品运行状态布局）](../../apps/frontend/src/product/application.css) | 组件运行、降级提示等专用内容；新增普通设置页不以此建立第二套配色。 |
| [floating-window.css（悬浮窗布局）](../../apps/frontend/src/product/floating-window.css) | 透明窗口与字幕布局，复用主界面的球体、颜色和反馈。 |
| [AppFrame.vue（应用框架）](../../apps/frontend/src/interface/AppFrame.vue)、[Workbench.vue（工作台）](../../apps/frontend/src/interface/Workbench.vue) | 应用外壳、三栏工作台与会话操作。已有区域通过模型和事件扩展。 |
| [SettingsFrame.vue（设置框架）](../../apps/frontend/src/interface/SettingsFrame.vue)、[SettingsPageHeader.vue（设置页标题）](../../apps/frontend/src/interface/SettingsPageHeader.vue)、[settings.ts（设置分区定义）](../../apps/frontend/src/interface/settings.ts) | 导航、页面标题、说明及标题右侧操作。页面名称和分区归属沿用同一份定义。 |
| [UiButton.vue（通用按钮）](../../apps/frontend/src/interface/UiButton.vue) | 普通操作的首选，提供主要、次要、危险及忙碌状态。选择器、图标操作、媒体控制等有专用语义的原生按钮同样继承公共反馈。 |
| [UiDialog.vue（通用模态弹窗）](../../apps/frontend/src/interface/UiDialog.vue) | 所有业务确认、编辑、版本、资源和图片查看弹窗的公共外壳；负责模态隔离、标题、内容滚动、操作区与关闭焦点恢复。 |
| [DatePicker.vue（日期选择器）](../../apps/frontend/src/interface/DatePicker.vue)、[TimePicker.vue（时间选择器）](../../apps/frontend/src/interface/TimePicker.vue)、[WeekdayPicker.vue（星期选择器）](../../apps/frontend/src/interface/WeekdayPicker.vue) | 输入与选择面板、键盘导航、选中状态和边界定位。继续使用 Popover（非模态弹出面板），不改成业务确认框。 |
| [SilenceTimeoutControl.vue（静默时长）](../../apps/frontend/src/interface/SilenceTimeoutControl.vue)、[WakeTaskSchedule.vue（唤醒计划）](../../apps/frontend/src/interface/WakeTaskSchedule.vue)、[WakeTaskTabs.vue（任务切换）](../../apps/frontend/src/interface/WakeTaskTabs.vue) | 时长、定时规则、待执行与归档视图，保留既有输入校验和状态语义。 |
| [InstructionEditorRow.vue（指令编辑条目）](../../apps/frontend/src/interface/InstructionEditorRow.vue)、[ModelPoolEntry.vue（模型条目）](../../apps/frontend/src/interface/ModelPoolEntry.vue)、[ModelAssignmentField.vue（模型分配）](../../apps/frontend/src/interface/ModelAssignmentField.vue)、[ModelAccordion.vue（模型折叠卡片）](../../apps/frontend/src/interface/ModelAccordion.vue) | 条目标题、启停、展开编辑、分配及折叠布局。不要在新页面重做同类卡片。 |
| [LocalComponents.vue（组件列表）](../../apps/frontend/src/interface/LocalComponents.vue)、[ManagedResourceActions.vue（资源操作）](../../apps/frontend/src/interface/ManagedResourceActions.vue)、[ManagedResourceProgress.vue（资源进度）](../../apps/frontend/src/interface/ManagedResourceProgress.vue) | 组件状态、下载、导入、重置、进度与取消，复用正式资源模型及分发入口。 |
| [AppUpdateCard.vue（应用更新）](../../apps/frontend/src/interface/AppUpdateCard.vue)、[ReleaseNotes.vue（更新日志）](../../apps/frontend/src/interface/ReleaseNotes.vue)、[LanVersionDialog.vue（局域网版本同步）](../../apps/frontend/src/interface/LanVersionDialog.vue)、[WorkspaceLocationCard.vue（工作区位置）](../../apps/frontend/src/interface/WorkspaceLocationCard.vue) | 程序版本、更新记录、设备版本同步与工作区操作。 |
| [FixedProviderSettings.vue（固定供应商设置）](../../apps/frontend/src/interface/FixedProviderSettings.vue)、[FixedProviderUsage.vue（供应商用量）](../../apps/frontend/src/interface/FixedProviderUsage.vue) | 凭据输入、保存状态与用量展示。 |
| [TtsVoiceLibrary.vue（语音音色库）](../../apps/frontend/src/interface/TtsVoiceLibrary.vue)、[TtsSynthesisPreferences.vue（语音合成偏好）](../../apps/frontend/src/interface/TtsSynthesisPreferences.vue)、[OnlineTtsVoiceSettings.vue（在线音色设置）](../../apps/frontend/src/interface/OnlineTtsVoiceSettings.vue)、[ReferenceAudioPlayer.vue（参考音频播放器）](../../apps/frontend/src/interface/ReferenceAudioPlayer.vue)、[SpeakerRecordingFlow.vue（声纹录制流程）](../../apps/frontend/src/interface/SpeakerRecordingFlow.vue) | 音色编辑、合成选项、试听、录音和参考素材。使用已有状态模型，避免新增重复播放器或录制操作。 |
| [ConversationMessage.vue（对话消息）](../../apps/frontend/src/interface/ConversationMessage.vue)、[CaptureImages.vue（图片附件与查看）](../../apps/frontend/src/interface/CaptureImages.vue)、[VoiceOrb.vue（语音球体）](../../apps/frontend/src/interface/VoiceOrb.vue) | 消息、图片缩放与语音状态视觉；保持正文、附件及状态动效语义。 |
| [OperationNotice.vue（操作提示）](../../apps/frontend/src/product/OperationNotice.vue)、[ApplicationStartup.vue（启动反馈）](../../apps/frontend/src/product/ApplicationStartup.vue)、[ApplicationMotion.vue（应用进出场）](../../apps/frontend/src/product/ApplicationMotion.vue) | 保存反馈、错误与启动退出阶段。普通成功反馈使用现有提示，不新增阻断式确认框。 |
| [app-icon.svg（应用图标源文件）](../../apps/frontend/src/assets/app-icon.svg) | 应用标识的源资源；派生图标使用 `scripts/generate-app-icons.mjs`（图标生成入口）。不在页面复制修改标识。 |

系统文件选择器、系统消息框和窗口标题栏使用 Windows（操作系统）的原生反馈。DSH（代理执行宿主）插件使用宿主的按钮与菜单组件；不要注入主程序全局样式覆盖宿主。上述边界不影响本项目自行绘制的启动、维护和业务界面。

## 视觉基线

### 配色

| 共享变量 | 当前值 | 中文用途 |
| --- | --- | --- |
| `--bg` | `#0e1118` | 页面底色。 |
| `--panel` | `#181c25` | 主面板与弹窗。 |
| `--panel-soft` | `#202530` | 次级表面与附件。 |
| `--line`、`--line-strong` | `#ffffff12`、`#ffffff24` | 分隔线、可操作边界。 |
| `--text` | `#edf0f7` | 主文字。 |
| `--muted`、`--subtle` | `#a3adbf`、`#8995aa` | 次要说明、辅助信息。 |
| `--accent` | `#b2baff` | 主要操作、选中与键盘焦点。 |
| `--mint` | `#88dbc3` | 就绪、成功、启用提示。 |
| `--danger` | `#efa0b1` | 危险操作与错误。 |

新增样式使用 `var()`（共享变量引用），透明叠色优先使用 `color-mix()`（颜色混合）。已有语义色足够时不新增近似颜色。状态同时提供文字、图标或可访问状态，不仅靠颜色区分。

### 字体、尺寸与密度

字体沿用 Inter（西文字体）、Segoe UI（系统字体）、Microsoft YaHei（微软雅黑）与系统回退，不增加远程字体依赖。

| 共享变量 | 当前值 | 中文用途 |
| --- | --- | --- |
| `--font-caption` | `0.625rem`，默认 10 像素 | 辅助说明、标签。 |
| `--font-body` | `0.75rem`，默认 12 像素 | 正文、输入与普通操作。 |
| `--font-section` | `0.875rem`，默认 14 像素 | 任务与区域标题。 |
| `--font-heading` | `1.25rem`，默认 20 像素 | 页面、工作台和弹窗标题。 |
| `--font-metric` | `1.375rem`，默认 22 像素 | 指标数字。 |
| `--control-height` | `2.5rem`，默认 40 像素 | 主要操作最小高度。 |
| `--compact-control-height` | `2rem`，默认 32 像素 | 次要操作最小高度。 |
| `--radius` | `18px` | 面板与弹窗圆角。 |
| `--duration` | `160ms` | 常规颜色、显隐过渡。 |

`rem`（根字号单位）响应用户字体设置。缩小窗口时调整列数、折行和留白，不按视口压缩字号、控件或整张页面。表单沿用已有 42 像素输入高度、9 像素圆角；小型日历格、媒体图标等沿用对应专用组件尺寸。

优先继承组件内边距与间距；新增组合沿用 8、12、16、20、24 像素的既有节奏。普通按钮圆角 10 像素，主要图标沿用 14 或 16 像素、1.6 像素描边的 SVG（矢量图形）风格。文字用清楚的动作表达；图标按钮必须有中文可访问名称。

### 页面与表单

- 设置页复用设置框架、页面标题、`.settings-stack`（设置分组）、`.form-field`（字段容器）、`.panel`（面板）等现有结构。不要复制侧栏、标题或整套控件样式。
- 分组按照用户任务组织，标题与短说明足够时不再叠加装饰标签。一个操作区最多突出一个主要操作；次要操作用轮廓或文字按钮，危险操作用危险色。
- 开关使用已有 `.switch-row`（开关行）、`.instruction-toggle`（指令开关）或音色库开关结构；保持原生复选框、名称、状态与焦点轮廓。
- 使用 Grid（网格布局）、Flex（弹性布局）和已有容器断点。可伸缩内容设置最小宽度为零；长路径、设备名和错误说明折行；确需截断的文本保留完整可访问内容或提示。
- 动效只解释状态变化。减少动态效果时保留颜色反馈和文字状态，关闭位移、旋转及不必要的动画。

## 按钮反馈契约

普通新按钮优先使用通用按钮；保留现有原生按钮和专用复合控件，它们自动继承同一套反馈。不要为接入反馈重写点击事件，也不要新增鼠标事件业务分发。

| 状态 | 统一行为 |
| --- | --- |
| 默认 | 保留主要、次要、危险、选中、展开的原有语义。 |
| Hover（悬停） | 仅支持悬停的设备显示轻微叠色与提亮；不加光晕、浮起或波纹。 |
| Active（按下） | 轻微加深、向下位移 1 像素；释放后恢复。`.active`（持久选中类）与 `:active`（按下伪类）分开使用。 |
| Focus visible（键盘焦点） | 保留清晰的淡紫色轮廓；日期、时间等紧凑控件沿用内部焦点边线。 |
| Disabled（禁用） | 降低透明度，不出现悬停或按下反馈，不触发业务。优先使用原生禁用属性。 |
| Busy（处理中） | 显示正在执行的动作或现有加载提示，禁止重复提交。通用按钮的忙碌属性同时禁用按钮。 |
| Reduced motion（减少动态效果） | 不位移、不渐变移动，保留即时的颜色差异。 |

反馈变量在最小共享界面层维护：`--button-feedback-duration`（反馈时长，120 毫秒）、`--button-feedback-color`（悬停叠色来源）、`--button-hover-wash`（悬停薄层）、`--button-press-wash`（按下薄层）、`--button-press-offset`（按下位移）、`--button-hover-brightness`（悬停亮度）、`--button-press-brightness`（按下亮度）。只有具有明确语义的公共变体可覆盖这些变量，不在页面另写一套悬停和按下规则。

反馈不改写按钮的 `transform`（原有变换）、`box-shadow`（状态阴影）或尺寸，保留日期今日标记与选择器居中定位。悬浮球透明区域不绘制矩形底色；拖动期间不显示按钮按下反馈。开关轨道共用亮度反馈，滑块和音频进度条保留自己的运动机制，不套用按钮位移。

`aria-disabled`（无障碍禁用状态）本身不会阻止点击；确需用于非原生控件时，还必须在正式事件入口阻止操作。`aria-busy`（无障碍忙碌状态）也不代替业务并发保护。

## 弹窗契约

业务弹窗必须复用通用模态弹窗，不新增自建遮罩、焦点循环或随意增大的层级值。其原生 `dialog`（模态对话框）进入浏览器顶层，背景不可交互，避免被页面滚动、变换或通知层遮挡。

| 部分 | 统一行为 |
| --- | --- |
| 遮罩 | `--dialog-backdrop`（模态遮罩），不额外叠加模糊和装饰。 |
| 容器 | 面板底色、共享边线和圆角；`--dialog-padding`（内容边距）默认 24 像素，小窗口为 16 像素。 |
| 宽度 | `--dialog-width`（普通宽度上限）500 像素；`wide`（宽弹窗属性）使用 900 像素上限。资源清单可沿用 640 像素，图片查看沿用 1500 像素；均受窗口边界约束。 |
| 标题 | `title`（标题）必填；`kicker`（上方说明）仅在补充上下文时提供，不重复标题。`titleId`（标题标识）默认自动生成，不硬编码通用重复标识。 |
| 内容 | 默认插槽放内容，公共容器保持自然文档流，不给任意插槽强加网格行高；长说明与列表在 `.dialog-body`（内容区）滚动，标题及操作区保持可见。列表条目保持内容高度，不为容纳全部条目而收缩；普通列表不重复限高或建立第二个纵向滚动区。编辑器与图片等确需独立滚动时，在专用组件内明确约束。 |
| 操作 | `actions`（底部操作插槽）统一对齐、间距和分隔线；取消、返回在前，主要或危险确认在后，文案直接写操作。没有操作时不显示空页脚。 |
| 工具 | `header-actions`（标题操作插槽）用于图片查看等专用工具；保留公共外壳，只扩展内容布局。 |
| 初始焦点 | 聚焦内容容器，先读标题与说明，不自动聚焦危险确认。 |
| 键盘 | Tab（制表键）与 Shift+Tab（反向制表组合键）限制在最上层模态区域；Escape（退出键）请求关闭最上层，内部选择面板先按浏览器规则关闭。 |
| 遮罩关闭 | 按下与释放都在遮罩时才请求关闭；从内容拖动到外部不误关。 |
| 忙碌 | `busy`（处理中属性）阻止退出键、遮罩关闭；调用方同时禁用不可重复执行的操作。可取消任务保留正式取消入口。 |
| 关闭 | 通过 `close`（关闭请求事件）交给调用方收尾；关闭后恢复触发控件焦点，已移除或禁用的控件不强行聚焦。 |

示例中的 `saving`（正在保存）、`confirmationOpen`（确认框打开状态）、`save`（正式保存操作）与 `closeConfirmation`（关闭确认框）由现有业务层提供：

```vue
<UiDialog :open="confirmationOpen" title="保存设置？" :busy="saving" @close="closeConfirmation">
  <p>保存后立即应用新的设置。</p>
  <template #actions>
    <UiButton variant="quiet" :disabled="saving" @click="closeConfirmation">取消</UiButton>
    <UiButton :busy="saving" @click="save">{{ saving ? '正在保存…' : '保存' }}</UiButton>
  </template>
</UiDialog>
```

`variant="quiet"`（次要按钮变体）降低取消操作的视觉权重；`disabled`（禁用属性）和 `busy`（忙碌属性）分别对应不可操作与正在处理。

日期、时间、星期使用既有非模态选择面板。启动、恢复与退出使用共享全屏启动页，保留阶段、进度与真实可用操作；不把业务模态框的关闭行为套到应用生命周期。系统选择器继续使用原生对话框。

## 验收与后续维护

- 通过[代理调试入口](../guides/agent-debug.md)观察正式控件、状态和业务回执；按钮悬停、按下、原生模态隔离等接口未覆盖的视觉细节，再补充渲染或电脑控制验收。
- 检查主工作台、设置、设备管理、资源弹窗、版本弹窗、图片查看、悬浮窗和独立启动样式；动态添加的条目与传送到文档根部的内容同样继承规则。
- 检查禁用、处理中、选中、展开、危险操作、键盘焦点、关闭后还焦点；长标题、长路径及窄窗口不能遮住操作，减少动态效果时仍能辨认反馈。
- 改公共弹窗的键盘与生命周期时运行对应行为回归；改共享样式时完成前端类型检查、相关组件测试和正式前端构建。真实渲染须使用实际业务组件及完整数据量，检查折叠与展开、首尾条目的文字完整性、滚动可达性和操作区可见性；仅确认控件存在或模拟长段落不足以证明列表布局正确。诊断和截图保存在 `.local/cache/reports`（受管报告目录）。
- `npm test`（常规回归）包含 `npm run frontend:check-layout`（真实弹窗布局检查）：使用本机 Edge（浏览器）渲染正式组件与固定数据，检查完整版本历史、资源清单、热词编辑器、工作区清理、版本同步、图片查看和长说明。可通过 `VOICE_TERMINAL_LAYOUT_BROWSER`（渲染引擎路径）指定兼容浏览器；不安装浏览器、不连接正式业务、不执行确认操作。
- 新增公共组件、共享变量或界面入口时，在本文件补充用途与复用路径。调整既有设计基线时同时修改源样式和本文，不建立第二份主题、按钮反馈或弹窗规则。

本轮梳理将普通按钮反馈集中到最小共享界面层，将资源确认、图片查看和样例确认迁移到公共弹窗，并移除相应分叉的悬停与弹窗外壳。该范围不要求重构既有业务数据流、原生系统界面或宿主插件样式。
