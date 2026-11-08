/** ProductLocalTtsModelId（本地语音模型标识）。 */
export type ProductLocalTtsModelId = string
/** ProductOnlineTtsModelId（在线语音模型标识）：MiniMax Speech 2.8 Turbo（低时延）或 HD（高音质）。 */
export type ProductOnlineTtsModelId = 'speech-2.8-turbo' | 'speech-2.8-hd'
/** ProductMinimaxVoiceName（MiniMax 在线音色查询结果）：名称由服务端实时读取。 */
export interface ProductMinimaxVoiceName {
  voice_id: string // voice_id（音色标识）。
  voice_name: string | null // voice_name（MiniMax 在线返回的音色名称，无名称时为空）。
}
/** ProductTtsVoiceSelection（已启用音色版本）。 */
export interface ProductTtsVoiceSelection { voice_id: string; revision: number } // 音色标识、不可变版本。
/** ProductTtsSettings（语音配置）：后端、在线模型和音色、本地模型、推理步数及启用音色。 */
export interface ProductTtsSettings {
  speech_control_enabled?: boolean // speech_control_enabled（智能语音调节总开关），缺省开启。
  backend: 'minimax' | 'aniflive' // backend（后端）：aniflive 为本地引擎。
  online_model?: ProductOnlineTtsModelId // online_model（在线模型）：MiniMax Speech 2.8 Turbo 或 HD。
  online_voice_id?: string // online_voice_id（在线音色标识）：MiniMax 官方系统、克隆或生成音色 ID。
  local_model?: ProductLocalTtsModelId // local_model（本地模型）。
  local_model_revision?: string | null // local_model_revision（精确构建版本）。
  gpu_max_performance?: boolean // gpu_max_performance（显卡高性能模式）。
  aniflive_chunk_optimization?: boolean // aniflive_chunk_optimization（后续分块48个语义标记，关闭为96）。
  aniflive_first_audio_wait_ms?: number // aniflive_first_audio_wait_ms（首音实际到达后等待毫秒）。
  active_voice_by_model?: Record<ProductLocalTtsModelId, ProductTtsVoiceSelection | null> // active_voice_by_model（各模型启用版本）。
}
/** ProductLocalTtsStatus（本地语音状态）：模型加载及待切换目标。 */
export interface ProductLocalTtsStatus {
  backend: 'minimax' | 'aniflive' // backend（运行后端）。
  local_model: ProductLocalTtsModelId // local_model（选择模型）。
  loaded_model: ProductLocalTtsModelId | null // loaded_model（已加载模型）。
  load_state: string // load_state（加载状态）。
  error: string | null // error（错误说明）。
  voice_state?: string // voice_state（音色就绪状态）。
  voice_error?: string | null // voice_error（音色错误）。
  pending_selection?: { backend: 'minimax' | 'aniflive'; local_model: ProductLocalTtsModelId } | null // 待切换目标。
  active_voice_by_model?: Record<ProductLocalTtsModelId, ProductTtsVoiceSelection | null> // 各库启用选择。
  models: Array<{ id: ProductLocalTtsModelId; name: string; available: boolean; loaded: boolean; native_sample_rate: number }> // 模型标识、名称、可用、加载、采样率。
}
/** ProductTtsReference（情绪参考）：录音、配对文字、标记及准备状态。 */
export interface ProductTtsReference {
  kind?: 'emotion' // kind（参考类型）：此集合中的条目为情绪参考。
  id: string; name: string; tag: string; intensity: number // 参考编号、名称、情绪标记、强度档位。
  asset_id: string | null; source_asset_id?: string | null // 工作音频及原始音频编号。
  reference_text: string; reference_language: string // 配对转写及语言。
  status: 'ready' | 'pending'; error?: string // 状态和待完善原因。
  verification_source?: string; provenance?: Record<string, unknown> // 校验方式和来源。
}
/** ProductTtsVoice（音色）：源素材、正式样音、设计参数及启用状态。 */
export interface ProductTtsVoice {
  id: string; revision: number; name: string; source: string // 标识、版本、名称、来源。
  model: ProductLocalTtsModelId; is_default: boolean; enabled: boolean // 归属模型、默认项、启用状态。
  reference_asset_id: string; reference_text: string // 正式样音标识及转写。
  reference_language?: string // reference_language（参考录音语言）。
  duration_ms?: number; source_reference_duration_ms?: number // 目标和源素材时长（毫秒）。
  source_reference_asset_id?: string | null; source_reference_text?: string // 原始克隆素材及校对文本。
  design_description?: string; design_text?: string; inference_steps?: number // 设计描述、生成正文、推理步数。
  generation_recipe?: string // generation_recipe（样音生成配方版本）。
  references?: ProductTtsReference[] // references（情绪参考集合）。
  preview_guidance?: string; preview_intensity?: number // 试听用情绪标记与强度档位。
}
/** ProductTtsAsset（音频附件）：标识与毫秒时长。 */
export interface ProductTtsAsset { asset_id: string; duration_ms: number }
/** TtsReferenceImportJob（模型参考导入任务）。 */
export interface TtsReferenceImportJob { task_id: string; status: string; phase: string; error?: string | null; reference_registration?: { ready: number; pending: number } }
/** ProductTtsOperation（后台任务）：身份、模型、执行状态及结果。 */
export interface ProductTtsOperation {
  operation_id: string; model: ProductLocalTtsModelId | ProductOnlineTtsModelId; kind: string // 操作标识、本地或在线模型、任务种类。
  state: 'running' | 'succeeded' | 'failed' | 'cancelled'; error?: string | null // 状态和错误。
  result?: { asset_id?: string; duration_ms?: number; voice_id?: string; text?: string; voice?: Partial<ProductTtsVoice>;
    route?: string; fallback_reason?: string | null; punctuation_status?: string; punctuation_error?: string | null } // 实际识别路线、回退原因及标点状态。
}
/** TtsVoicePort（音色业务接口）：共享展示组件由正式页面注入，演示页无需连接。 */
export interface TtsVoicePort {
  ttsVoices(model: ProductLocalTtsModelId): Promise<{ voices: ProductTtsVoice[] }>
  createTtsVoice(model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>): Promise<{ voice: ProductTtsVoice }>
  updateTtsVoice(model: ProductLocalTtsModelId, id: string, voice: Partial<ProductTtsVoice>, revision: number): Promise<{ voice: ProductTtsVoice }>
  deleteTtsVoice(model: ProductLocalTtsModelId, id: string): Promise<unknown>
  selectTtsVoice(model: ProductLocalTtsModelId, selection: ProductTtsVoiceSelection | null): Promise<unknown>
  restoreDefaultTtsVoice(model: ProductLocalTtsModelId): Promise<unknown> // 恢复模型来源默认配置并保留历史版本。
  uploadTtsAsset(model: ProductLocalTtsModelId, file: File): Promise<{ asset: ProductTtsAsset }>
  uploadTtsSource(model: ProductLocalTtsModelId, file: File): Promise<{ asset: ProductTtsAsset }> // 保存可裁剪的原始录音。
  clipTtsAsset(model: ProductLocalTtsModelId, asset_id: string, start_ms: number, end_ms: number): Promise<{ asset: ProductTtsAsset }>
  startAnifLiveReferenceImport(model: ProductLocalTtsModelId): Promise<TtsReferenceImportJob> // 启动已有模型参考资料导入。
  getAnifLiveImport(task_id: string): Promise<TtsReferenceImportJob> // 参考导入进度。
  /** ttsAssetAudioUrl（参考音频地址）：供原生播放器读取音频并定位播放进度。 */
  ttsAssetAudioUrl(model: ProductLocalTtsModelId, asset_id: string): string
  startTtsTranscribe(model: ProductLocalTtsModelId, asset_id: string): Promise<ProductTtsOperation>
  startTtsPreview(model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>, text: string): Promise<ProductTtsOperation>
  startTtsPlay(model: ProductLocalTtsModelId, asset_id: string): Promise<ProductTtsOperation>
  getTtsOperation(id: string): Promise<ProductTtsOperation>
  cancelTtsOperation(id: string): Promise<ProductTtsOperation>
}
