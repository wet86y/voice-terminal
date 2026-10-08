/** userMessageKey（用户气泡标识）：turnId（识别轮次标识）在识别开始时已确定，后续会话归属不换键。 */
export function userMessageKey(turnId: string) {
  return `turn:${turnId}`
}

/** speechMessageKey（播报气泡标识）：conversationId（会话标识）和 speechId（播报标识）贯穿队列、播放结束与正式记录。 */
export function speechMessageKey(conversationId: string, speechId: string) {
  return `speech:${conversationId}:${speechId}`
}
