async (request) => {
  const config = globalThis.__VOICE_APPLICATION__
  const service = !config?.remote_mode && config?.business_ready !== false && config?.voice_api_url
    ? new URL(config.voice_api_url)
    : null
  const safe = service?.protocol === 'http:' && service.hostname === '127.0.0.1'
  const commands = []
  const read = async (url) => {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2500) })
      return response.ok && response.headers.get('content-type')?.includes('application/json')
        ? await response.json()
        : null
    } catch { return null }
  }
  const host = config?.remote_mode ? null : await read('/api/v1/application/operations')
  const schema = safe ? await read(new URL('/openapi.json', service)) : null
  for (const command of schema?.['x-voice-commands'] ?? []) commands.push('WS /ws:' + command)
  const routes = []
  const route = (method, template, origin, definition) => {
    if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return
    if (!template.startsWith('/api/v1/') || template.includes('//') || template.includes('..')) return
    routes.push({ operation: method + ' ' + template, method, template, origin, definition })
  }
  for (const entry of host?.operations ?? []) route(entry.method, entry.path, location.origin, null)
  for (const [path, methods] of Object.entries(schema?.paths ?? {})) {
    for (const [method, definition] of Object.entries(methods)) route(method.toUpperCase(), path, service.origin, definition)
  }
  if (!request) return {
    operations: routes.map(({ operation, definition }) => ({
      operation, description: definition?.summary ?? null, request_body: definition?.requestBody ?? null,
      parameters: definition?.parameters ?? [],
    })),
    realtime_operations: commands,
    core_ready: Boolean(schema) || config?.business_ready === true,
  }
  if (config?.remote_mode) {
    return { error: '当前窗口通过远程产品接口调用已配对设备。' }
  }
  if (commands.includes(request.operation)) {
    const address = new URL('/ws', service)
    address.protocol = 'ws:'
    const requestId = 'agent-' + request.call_id
    return await new Promise((resolve) => {
      const socket = new WebSocket(address)
      let sent = false
      const finish = (data) => {
        clearTimeout(timer)
        socket.close()
        resolve(data)
      }
      const timer = setTimeout(() => finish(sent
        ? { state: 'submitted', request_id: requestId }
        : { error: '实时服务连接超时。' }), 5000)
      socket.onopen = () => {
        socket.send(JSON.stringify({
          type: 'command', request_id: requestId, command: request.operation.slice('WS /ws:'.length),
          payload: request.arguments,
        }))
        sent = true
      }
      socket.onerror = () => finish({ error: '实时服务未返回可信回执。' })
      socket.onmessage = ({data}) => {
        try {
          const event = JSON.parse(data)
          if (event.request_id === requestId || event.payload?.request_id === requestId) {
            finish({ state: 'received', request_id: requestId, event })
          }
        } catch {}
      }
    })
  }
  const entry = routes.find(({ operation }) => operation === request.operation)
  if (!entry) return { error: '当前实例没有可用的对应服务接口。' }
  const args = request.arguments
  const path = entry.template.replace(/\{([^}]+)\}/g, (_, name) => {
    const value = args.path?.[name]
    if (!['string', 'number'].includes(typeof value) || !String(value) || /[\/\\]/.test(String(value)) || ['.','..'].includes(String(value))) {
      throw new Error('接口路径参数无效。')
    }
    return encodeURIComponent(value)
  })
  const url = new URL(path, entry.origin)
  for (const [name, value] of Object.entries(args.query ?? {})) url.searchParams.set(name, String(value))
  const init = { method: entry.method, redirect: 'error', headers: { 'X-Voice-Agent-Call': String(request.call_id) } }
  if (entry.method !== 'GET') {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(args.body ?? {})
  }
  const response = await fetch(url, init)
  const data = response.headers.get('content-type')?.includes('application/json')
    ? await response.json()
    : null
  return response.ok ? { status: response.status, data } : {
    error: typeof data?.detail === 'string' ? data.detail : typeof data?.error === 'string' ? data.error : '正式服务接口返回状态 ' + response.status,
  }
}
