/** RuntimeOperation（运行操作）：显示层只依赖阶段契约，不枚举业务事件。 */
export interface RuntimeOperation {
  id: string // id（操作唯一标识）。
  revision: number // revision（操作修订号，过滤迟到回包）。
  target: string // target（组件或能力标识）。
  label: string // label（中文显示名称）。
  state: 'running' | 'succeeded' | 'failed' | 'cancelled' // state（进行中、成功、失败、取消）。
  phase: string // phase（可扩展阶段编号）。
  detail: string // detail（当前阶段中文说明或错误）。
}

export const operationPhaseLabel = (value: RuntimeOperation) => value.detail || ({
  queued: '等待切换', pausing: '正在暂停监听', checking: '检查模型资源与运行环境', waiting_output: '等待当前回答与播报结束',
  switching: '切换中', restarting: '重启中', loading: '加载与预热中', verifying: '核验中',
  rolling_back: '正在恢复原设置', resuming: '正在恢复监听', processing: '处理中',
  recording: '录音中', ready: '已就绪', failed: '操作失败',
}[value.phase] || '处理中')
