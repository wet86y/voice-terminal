/** Shared transport（共享通信）：本机请求、鉴权头和调用者取消由一个实现维护。 */
import { CALLBACK_ORIGIN, TOKEN } from './environment.js'

/** Request options（请求选项）：业务模块负责正文和回执语义。 */
export interface VoiceRequestOptions {
  /** HTTP method（请求方法）。 */
  readonly method: string
  /** JSON body（结构化请求正文）。 */
  readonly body?: unknown
  /** Execution cancellation（执行取消信号）；后台投递由服务生命周期提供。 */
  readonly signal?: AbortSignal | undefined
  /** Timeout（请求超时毫秒数）；省略时只遵守调用者取消。 */
  readonly timeoutMs?: number
  /** Accept header（声明接收结构化回执）。 */
  readonly acceptJson?: boolean
}

/**
 * Send one authenticated loopback request（发送一次已鉴权本机请求）。
 * @param path - Voice route（语音端路由）。
 * @param options - Method, body and cancellation（方法、正文与取消选项）。
 * @returns Unconsumed response（尚未读取正文的响应）。
 */
export function voiceFetch(path: string, options: VoiceRequestOptions): Promise<Response> {
  const timeout = options.timeoutMs === undefined ? undefined : AbortSignal.timeout(options.timeoutMs)
  const signal = options.signal && timeout ? AbortSignal.any([options.signal, timeout]) : options.signal ?? timeout
  return fetch(`${CALLBACK_ORIGIN}${path}`, {
    method: options.method,
    ...(signal ? { signal } : {}),
    headers: {
      authorization: `Bearer ${TOKEN}`,
      ...(options.acceptJson ? { accept: 'application/json' } : {}),
      ...(options.body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
  })
}
