/** ComponentRecovery（组件恢复结果）：纯展示协议，供正式页面与界面演示共用。 */
export interface ComponentRecovery {
  /** event（故障类别）、state（恢复状态）和 message（用户处理说明）。 */
  event: string
  state: string
  message: string
  /** next_action（下一步）；完整性校验始终使用现有全面检查。 */
  next_action: 'none' | 'retry' | 'restart' | 'check_integrity'
}
