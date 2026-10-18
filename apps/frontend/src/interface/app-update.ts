/** AppUpdateStatus（应用更新状态）：正式界面与隔离样例共用展示契约。 */
export interface AppUpdateStatus {
  /** state（更新阶段）。 */
  state: 'idle' | 'checking' | 'current' | 'available' | 'downloading' | 'handing_off' | 'cancelled' | 'failed'
  /** current_version（当前程序版本）。 */
  current_version: string
  /** target_version（目标程序版本）。 */
  target_version?: string | null
  /** release_notes（发行说明）。 */
  release_notes?: string | null
  /** completed_bytes（已下载字节数）。 */
  completed_bytes?: number | null
  /** total_bytes（安装器总字节数）。 */
  total_bytes?: number | null
  /** phase（当前处理阶段）。 */
  phase?: string | null
  /** error（更新失败原因）。 */
  error?: string | null
  /** can_cancel（当前是否允许取消）。 */
  can_cancel?: boolean
  /** fields（状态字段的中文说明）。 */
  fields: Record<string, string>
}
