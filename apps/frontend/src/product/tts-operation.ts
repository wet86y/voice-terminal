import { productApi } from './api'
import type { ProductTtsOperation } from './types'

/** TtsOperationClient（语音任务接口）：继续使用正式启动、查询和取消入口。 */
type TtsOperationClient = Pick<typeof productApi, 'getTtsOperation' | 'cancelTtsOperation'>

export function isTtsOperationAborted(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

/** ttsOperationTarget（任务所属目标）：连接切换后不能把旧任务编号发给新核心。 */
function ttsOperationTarget(): string {
  const value = typeof window === 'undefined' ? undefined : window.__VOICE_APPLICATION__
  return JSON.stringify([value?.remote_mode === true, value?.host_id, value?.endpoint_revision, value?.voice_api_url])
}

/** createTtsOperationScope（页面私有任务作用域）：接管迟到回执、定时等待与一次取消。 */
export function createTtsOperationScope(client: TtsOperationClient = productApi) {
  let disposed = false
  const controllers = new Set<AbortController>()
  return {
    dispose() {
      disposed = true
      for (const controller of controllers) controller.abort()
    },
    async run(
      start: (assertActive: () => void) => Promise<ProductTtsOperation>,
      intervalMs = 400,
    ): Promise<ProductTtsOperation> {
      const controller = new AbortController()
      const target = ttsOperationTarget()
      let operation: ProductTtsOperation | undefined
      let cancellationSent = false
      const current = () => target === ttsOperationTarget()
      const assertActive = () => {
        if (disposed || controller.signal.aborted || !current()) {
          throw new DOMException('语音任务所属页面或连接已结束。', 'AbortError')
        }
      }
      const cancel = () => {
        if (operation?.state !== 'running' || cancellationSent || !current()) return
        cancellationSent = true
        void client.cancelTtsOperation(operation.operation_id).catch(() => {})
      }
      controllers.add(controller)
      controller.signal.addEventListener('abort', cancel)
      try {
        assertActive()
        operation = await start(assertActive)
        assertActive() // 启动回执迟到时，finally（收尾）仍会取消刚取得编号的旧任务。
        while (operation.state === 'running') {
          await new Promise<void>((resolve, reject) => {
            const abort = () => {
              clearTimeout(timer)
              reject(new DOMException('语音任务等待已取消。', 'AbortError'))
            }
            const timer = setTimeout(() => {
              controller.signal.removeEventListener('abort', abort)
              resolve()
            }, intervalMs)
            controller.signal.addEventListener('abort', abort, { once: true })
          })
          assertActive()
          operation = await client.getTtsOperation(operation.operation_id)
          assertActive()
        }
        return operation
      } catch (error) {
        assertActive()
        throw error
      } finally {
        if (disposed || controller.signal.aborted || !current()) cancel()
        controller.signal.removeEventListener('abort', cancel)
        controllers.delete(controller)
      }
    },
  }
}
