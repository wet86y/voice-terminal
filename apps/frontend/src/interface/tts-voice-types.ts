/** ProductLocalTtsModelId（本地语音模型标识）。 */
export type ProductLocalTtsModelId = 'voxcpm1' | 'voxcpm2'
/** ProductTtsVoiceSelection（已启用音色版本）。 */
export interface ProductTtsVoiceSelection { voice_id: string; revision: number } // 音色标识、不可变版本。
/** ProductTtsSettings（语音配置）：后端、本地模型、逐模型推理步数及启用音色。 */
export interface ProductTtsSettings {
  backend: 'minimax' | 'voxcpm1' // backend（后端）：voxcpm1 为本地兼容路由。
  local_model?: ProductLocalTtsModelId // local_model（本地模型）。
  inference_steps_by_model?: Record<ProductLocalTtsModelId, number> // inference_steps_by_model（逐模型推理步数）。
  active_voice_by_model?: Record<ProductLocalTtsModelId, ProductTtsVoiceSelection | null> // active_voice_by_model（各模型启用版本）。
}
/** ProductLocalTtsStatus（本地语音状态）：模型加载及待切换目标。 */
export interface ProductLocalTtsStatus {
  backend: 'minimax' | 'voxcpm1' // backend（运行后端）。
  local_model: ProductLocalTtsModelId // local_model（选择模型）。
  loaded_model: ProductLocalTtsModelId | null // loaded_model（已加载模型）。
  load_state: string // load_state（加载状态）。
  error: string | null // error（错误说明）。
  voice_state?: string // voice_state（音色就绪状态）。
  voice_error?: string | null // voice_error（音色错误）。
  pending_selection?: { backend: 'minimax' | 'voxcpm1'; local_model: ProductLocalTtsModelId } | null // 待切换目标。
  active_voice_by_model?: Record<ProductLocalTtsModelId, ProductTtsVoiceSelection | null> // 各库启用选择。
  models: Array<{ id: ProductLocalTtsModelId; name: string; available: boolean; loaded: boolean; native_sample_rate: number }> // 模型标识、名称、可用、加载、采样率。
}
/** ProductTtsVoice（音色）：源素材、正式样音、设计参数及启用状态。 */
export interface ProductTtsVoice {
  id: string; revision: number; name: string; source: string // 标识、版本、名称、来源。
  model: ProductLocalTtsModelId; is_default: boolean; enabled: boolean // 归属模型、默认项、启用状态。
  reference_asset_id: string; reference_text: string // 正式样音标识及转写。
  duration_ms?: number; source_reference_duration_ms?: number // 目标和源素材时长（毫秒）。
  source_reference_asset_id?: string | null; source_reference_text?: string // 原始克隆素材及校对文本。
  design_description?: string; design_text?: string; inference_steps?: number // 设计描述、生成正文、推理步数。
}
/** ProductTtsAsset（音频附件）：标识与毫秒时长。 */
export interface ProductTtsAsset { asset_id: string; duration_ms: number }
/** ProductTtsOperation（后台任务）：身份、模型、执行状态及结果。 */
export interface ProductTtsOperation {
  operation_id: string; model: ProductLocalTtsModelId; kind: string // 操作标识、模型、任务种类。
  state: 'running' | 'succeeded' | 'failed' | 'cancelled'; error?: string | null // 状态和错误。
  result?: ProductTtsAsset & { text?: string; voice?: Partial<ProductTtsVoice> } // 音频结果、精修文本、生成音色参数。
}
/** TtsVoicePort（音色业务接口）：共享展示组件由正式页面注入，演示页无需连接。 */
export interface TtsVoicePort {
  ttsVoices(model: ProductLocalTtsModelId): Promise<{ voices: ProductTtsVoice[] }>
  createTtsVoice(model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>): Promise<{ voice: ProductTtsVoice }>
  updateTtsVoice(model: ProductLocalTtsModelId, id: string, voice: Partial<ProductTtsVoice>, revision: number): Promise<{ voice: ProductTtsVoice }>
  deleteTtsVoice(model: ProductLocalTtsModelId, id: string): Promise<unknown>
  selectTtsVoice(model: ProductLocalTtsModelId, selection: ProductTtsVoiceSelection | null): Promise<unknown>
  uploadTtsAsset(model: ProductLocalTtsModelId, file: File): Promise<{ asset: ProductTtsAsset }>
  /** ttsAssetAudioUrl（参考音频地址）：供原生播放器读取音频并定位播放进度。 */
  ttsAssetAudioUrl(model: ProductLocalTtsModelId, asset_id: string): string
  startTtsTranscribe(model: ProductLocalTtsModelId, asset_id: string): Promise<ProductTtsOperation>
  startTtsPreview(model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>, text: string): Promise<ProductTtsOperation>
  startTtsPlay(model: ProductLocalTtsModelId, asset_id: string): Promise<ProductTtsOperation>
  getTtsOperation(id: string): Promise<ProductTtsOperation>
  cancelTtsOperation(id: string): Promise<ProductTtsOperation>
}
