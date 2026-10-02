// Managed Node.js shutdown（受管 JavaScript 运行环境退出）：由语音服务的命名事件转发。
// 私有 stdin（标准输入）管道触发 DSH（代理执行宿主）原有的正常资源清理流程。
if (process.env.VOICE_APP_ORIGIN) {
  let pending = ''
  let stopping = false
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', chunk => {
    pending = (pending + chunk).slice(-128)
    if (stopping || !pending.includes('voice-terminal:shutdown\n')) return
    stopping = true
    process.stdin.destroy()
    const deadline = Date.now() + 10000
    const timer = setInterval(() => {
      if (process.listenerCount('SIGTERM') > 0) {
        clearInterval(timer)
        process.emit('SIGTERM') // SIGTERM（正常停止信号）交给现有 CLI（命令行程序）处理。
      } else if (Date.now() > deadline) {
        clearInterval(timer) // 外层 Job Object（作业对象）执行超时回收。
      }
    }, 50)
  })
}
