import assert from 'node:assert/strict'
import { cpSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'

const repo = resolve(fileURLToPath(new URL('../../../../../', import.meta.url)))
const dshSource = join(repo, '.local/build/dsh/source')
const scheduleDir = join(dshSource, 'packages/schedule/schedule')
const scheduleToolsDir = join(dshSource, 'packages/schedule/tool-schedule')
const scheduleRequire = createRequire(join(scheduleDir, 'package.json'))
const toolsRequire = createRequire(join(dshSource, 'packages/core/tools/package.json'))

function packageRoot(modulePath) {
  return dirname(dirname(modulePath))
}

function linkDirectory(target, link) {
  symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir')
}

function writeNativeFixture(path, rows) {
  // schedule.json（官方会话任务存储）使用隔离内存 Domain（存储域）的快照，仅供回归检测项目插件是否误导入。
  writeFileSync(path, JSON.stringify({ tables: { tasks: Object.fromEntries(rows) } }), 'utf8')
}

async function callTool(ctx, agent, name, args = {}) {
  const result = await ctx.tools.execute({
    callId: `coexistence-${name}-${Math.random().toString(16).slice(2)}`,
    signal: new AbortController().signal,
    name,
    arguments: args,
    agent,
  })
  assert.equal(result.isError, false, `${name}（工具名）应成功执行：${JSON.stringify(result.content)}`)
  return result.value
}

test('官方 schedule_*（会话任务工具）与项目 wake_task_*（语音链路任务工具）在同一 root Agent（根智能体）独立共存', async () => {
  const temporary = mkdtempSync(join(tmpdir(), 'voice-dsh-coexistence-'))
  const bundleDir = join(temporary, 'bundle')
  const bundleLib = join(bundleDir, 'lib')
  const taskStore = join(temporary, 'project-tasks.json')
  const nativeStore = join(temporary, 'native-schedule.json')
  const legacyStore = join(temporary, 'legacy-wake.json')
  const envKeys = [
    'VOICE_DSH_TASKS_ACTIVE', 'VOICE_DSH_TASKS_PATH',
    'VOICE_DSH_NATIVE_SCHEDULE_PATH', 'VOICE_DSH_LEGACY_WAKE_PATH',
    'VOICE_DSH_CALLBACK_ORIGIN', 'VOICE_DSH_BRIDGE_TOKEN',
  ]
  const previousEnv = Object.fromEntries(envKeys.map(key => [key, process.env[key]]))
  const originalFetch = globalThis.fetch
  const nativeRows = new Map()
  const harnesses = []

  try {
    mkdirSync(bundleLib, { recursive: true })
    const builtBundle = join(repo, '.local/build/dsh/bundles/voice-terminal-test')
    for (const file of ['package.json', 'lib']) cpSync(join(builtBundle, file), join(bundleDir, file), { recursive: true })
    const scopedModules = join(bundleDir, 'node_modules/@deepseek-ai')
    mkdirSync(scopedModules, { recursive: true })
    linkDirectory(packageRoot(toolsRequire.resolve('@deepseek-ai/dsh-tools')), join(scopedModules, 'dsh-tools'))
    linkDirectory(scheduleDir, join(scopedModules, 'dsh-schedule'))
    linkDirectory(packageRoot(scheduleRequire.resolve('@deepseek-ai/schemastery')), join(scopedModules, 'schemastery'))

    process.env.VOICE_DSH_TASKS_ACTIVE = '1'
    process.env.VOICE_DSH_TASKS_PATH = taskStore
    process.env.VOICE_DSH_NATIVE_SCHEDULE_PATH = nativeStore
    process.env.VOICE_DSH_LEGACY_WAKE_PATH = legacyStore
    process.env.VOICE_DSH_CALLBACK_ORIGIN = 'http://127.0.0.1:18765'
    process.env.VOICE_DSH_BRIDGE_TOKEN = 'coexistence-test-only'
    globalThis.fetch = async () => { throw new Error('coexistence test must not call an external callback') }

    const cordis = await import(pathToFileURL(scheduleRequire.resolve('@deepseek-ai/cordis')).href)
    const systemPromptModule = await import(pathToFileURL(toolsRequire.resolve('@deepseek-ai/dsh-system-prompt')).href)
    const toolRuntimeModule = await import(pathToFileURL(toolsRequire.resolve('@deepseek-ai/dsh-tools')).href)
    const scopeModule = await import(pathToFileURL(toolsRequire.resolve('@deepseek-ai/dsh-scope')).href)
    const scheduleModule = await import(pathToFileURL(join(scheduleDir, 'lib/index.js')).href)
    const scheduleToolsModule = await import(pathToFileURL(join(scheduleToolsDir, 'lib/index.js')).href)
    const builtPlugin = await import(pathToFileURL(join(bundleDir, 'lib/wake-tasks/plugin.js')).href)

    const { Context } = cordis
    const SystemPrompt = systemPromptModule.default
    const ToolRuntime = toolRuntimeModule.default
    const { createScope } = scopeModule
    const { ScheduleService } = scheduleModule
    const ToolSchedule = scheduleToolsModule

    async function mountHarness() {
      const ctx = new Context()
      let rootAgents = []
      const agents = {
        roots: () => rootAgents,
        list: () => rootAgents,
        get: id => rootAgents.find(agent => agent.session.id === id),
        withoutInitiator: operation => operation(),
      }
      const table = {
        entries: () => nativeRows.entries(),
        get: id => nativeRows.get(id),
        async put(id, row) { nativeRows.set(id, row) },
        async delete(id) { nativeRows.delete(id) },
      }
      const domain = { table: name => {
        assert.equal(name, 'tasks', '官方 Schedule（调度）服务只应访问自己的 tasks（任务）表')
        return table
      }, async close() {} }
      const webServer = {
        routes: new Set(),
        register(route) {
          this.routes.add(route)
          return () => this.routes.delete(route)
        },
      }

      ctx.provide('agents', agents)
      ctx.provide('sessions', { async flush() { return true } })
      ctx.provide('sessionController', { async resolveAgent() { return undefined } })
      ctx.provide('sessionPersistence', {})
      ctx.provide('storageDomain', { async open() { return domain } })
      ctx.provide('webServer', webServer)
      await ctx.plugin(SystemPrompt, {})
      await ctx.plugin(ToolRuntime)

      const agent = {
        id: 'root-agent',
        session: { id: 'session-coexistence', header: { delegationDepth: 0 }, snapshotEvents: () => [] },
        options: {},
      }
      let scope
      const scopePlugin = Object.assign(function coexistenceRootAgentScope(inner) {
        scope = createScope(inner, agent)
        agent.ctx = scope.ctx
      }, { inject: ['tools', 'systemPrompt'] })
      const scopeFiber = await ctx.plugin(scopePlugin)
      const scheduleFiber = await ctx.plugin(ScheduleService)
      rootAgents = [agent]
      const nativeToolsFiber = await agent.ctx.plugin(ToolSchedule)

      const harness = {
        ctx, agent, webServer, scheduleFiber, scopeFiber, nativeToolsFiber,
        projectFiber: undefined,
        async mountProjectPlugin() {
          const projectPlugin = Object.assign(function builtWakeTasksPlugin(inner) {
            return builtPlugin.apply(inner, {})
          }, {
            inject: builtPlugin.inject,
          })
          this.projectFiber = await ctx.plugin(projectPlugin)
          return this.projectFiber
        },
        async disposeProjectPlugin() {
          if (!this.projectFiber) return
          await this.projectFiber.dispose()
          this.projectFiber = undefined
        },
        async dispose() {
          await this.disposeProjectPlugin()
          await nativeToolsFiber.dispose()
          await ctx.fiber.dispose()
        },
      }
      harnesses.push(harness)
      return harness
    }

    const first = await mountHarness()
    const tools = () => first.ctx.tools.schemas(first.agent).map(item => item.name).sort()
    assert.deepEqual(tools(), [
      'schedule_create', 'schedule_delete', 'schedule_list', 'schedule_update',
    ], '先注册官方工具，供对照项目插件挂载前的 root Agent（根智能体）作用域')

    const nativeBeforeMount = await callTool(first.ctx, first.agent, 'schedule_create', {
      title: 'Native reminder', prompt: 'Synthetic native session task', after_seconds: 3600,
    })
    const nativeId = nativeBeforeMount.id
    assert.ok(nativeRows.has(nativeId), '官方 schedule_create（创建会话任务）应写入官方 Domain（存储域）')
    writeNativeFixture(nativeStore, nativeRows)

    await first.mountProjectPlugin()
    assert.deepEqual(tools(), [
      'schedule_create', 'schedule_delete', 'schedule_list', 'schedule_update',
      'wake_task_create', 'wake_task_delete', 'wake_task_list', 'wake_task_update',
      'schedule_wake', 'cancel_wake', 'read_wake_reference', 'publish_voice_event',
    ].sort(), '官方和项目工具应同时注册到同一个 root Agent（根智能体）工具注册器')
    assert.equal(first.webServer.routes.size, 1, '项目插件应挂载一个隔离到测试 Context（上下文）的 HTTP 路由')
    assert.deepEqual(await callTool(first.ctx, first.agent, 'wake_task_list'), [],
      '项目启动不能把 native schedule（官方会话任务）导入链路任务存储')

    const createToolSchema = first.ctx.tools.schemas(first.agent)
      .find(schema => schema.name === 'wake_task_create')
    assert.ok(createToolSchema, 'wake_task_create（创建任务）应暴露给 root Agent（根智能体）')
    const parameters = createToolSchema.parameters.properties
    assert.deepEqual(createToolSchema.parameters.required, ['title', 'prompt'],
      'title（任务名称）和 prompt（任务内容）必须作为 DSH 工具必填参数暴露')
    assert.equal(parameters.at.oneOf[0].type, 'string', 'at（单次时间）应允许带时区偏移的日期时间字符串')
    assert.deepEqual(Object.keys(parameters.at.oneOf[1].properties).sort(), ['date', 'time', 'time_zone'])
    assert.deepEqual(parameters.at.oneOf[1].required, ['date', 'time', 'time_zone'])
    assert.equal(parameters.at.oneOf[1].additionalProperties, false,
      'at（单次时间）的本地日期对象应只接受受支持字段')
    assert.deepEqual(parameters.daily.required, ['time', 'time_zone'])
    assert.equal(parameters.daily.additionalProperties, false,
      'daily（每日规则）应完整暴露必填字段并拒绝其他字段')
    assert.deepEqual(parameters.weekly.required, ['time', 'time_zone', 'weekdays'])
    assert.equal(parameters.weekly.properties.weekdays.items.type, 'integer')
    assert.equal(parameters.weekly.additionalProperties, false,
      'weekly（每周规则）应完整暴露必填字段并拒绝其他字段')
    assert.equal(parameters.every_seconds.type, 'integer')
    for (const field of ['at', 'daily', 'weekly', 'every_seconds']) {
      assert.match(parameters[field].description, /[\u4e00-\u9fff]/u,
        `${field}（规则字段）应携带中文参数说明`)
    }

    const projectCron = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Workday check', prompt: 'Check the task',
      cron: { expression: '0 9 * * 1-5', time_zone: 'Asia/Hong_Kong' },
    })
    assert.deepEqual(projectCron.cron_input, { expression: '0 9 * * 1-5', time_zone: 'Asia/Hong_Kong' },
      'wake_task_create（创建任务）应接受并返回与 DSH 官方调度器相同的 Cron 输入格式')

    const singleAt = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Single absolute time', prompt: 'Synthetic single absolute-time task',
      at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    })
    const dailyTask = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Daily task', prompt: 'Synthetic daily task',
      daily: { time: '23:59:59', time_zone: 'Asia/Hong_Kong' },
    })
    const weeklyTask = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Weekly task', prompt: 'Synthetic weekly task',
      weekly: { time: '23:59:59', time_zone: 'Asia/Hong_Kong', weekdays: [1, 3] },
    })
    assert.deepEqual([singleAt.rule_kind, dailyTask.rule_kind, weeklyTask.rule_kind], ['at', 'daily', 'weekly'],
      'wake_task_create（创建任务）应通过 DSH 参数入口创建单次、每日与每周规则')

    const incompatibleCronShape = await first.ctx.tools.execute({
      callId: 'coexistence-wake-task-create-cron-wrong-time-zone-field',
      signal: new AbortController().signal,
      name: 'wake_task_create',
      arguments: { title: '错误格式', prompt: '应拒绝不兼容字段',
        cron: { expression: '0 9 * * 1-5', timeZone: 'Asia/Hong_Kong' } },
      agent: first.agent,
    })
    assert.equal(incompatibleCronShape.isError, true,
      'wake_task_create（创建任务）必须拒绝 native record（原生记录）格式的 timeZone 字段，要求输入格式 time_zone')
    const incompatibleCronExpression = await first.ctx.tools.execute({
      callId: 'coexistence-wake-task-create-cron-hhmm',
      signal: new AbortController().signal,
      name: 'wake_task_create',
      arguments: { title: '错误表达式', prompt: '不能使用时钟时间',
        cron: { expression: '02:55', time_zone: 'Asia/Hong_Kong' } },
      agent: first.agent,
    })
    assert.equal(incompatibleCronExpression.isError, true,
      'wake_task_create（创建任务）必须拒绝 HH:mm（时:分），只接受官方五字段 Cron 表达式')

    const nativeListed = await callTool(first.ctx, first.agent, 'schedule_list')
    assert.deepEqual(nativeListed.map(row => row.id), [nativeId], '官方 schedule_list（列出会话任务）只读官方存储')
    const projectOne = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Project task one', prompt: 'Synthetic voice mainline task one', after_seconds: 3600,
    })
    const projectTwo = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Project task two', prompt: 'Synthetic voice mainline task two', after_seconds: 3600,
    })
    const finiteInterval = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Finite interval', prompt: 'Synthetic finite interval', every_seconds: 60,
      loop: false, trigger_count: 2,
    })
    assert.equal(finiteInterval.rule_kind, 'every',
      'wake_task_create（创建任务）应通过 DSH 参数入口创建间隔规则')
    assert.equal(finiteInterval.loop, false)
    assert.equal(finiteInterval.trigger_count, 2)
    const finiteUpdate = await callTool(first.ctx, first.agent, 'wake_task_update', {
      id: finiteInterval.id, trigger_count: 3,
    })
    assert.equal(finiteUpdate.trigger_count, 3, 'wake_task_update（修改任务）应更新有限触发次数')
    const defaultLoop = await callTool(first.ctx, first.agent, 'wake_task_create', {
      title: 'Default interval', prompt: 'Synthetic default interval', every_seconds: 60,
    })
    assert.equal(defaultLoop.loop, true, '省略 loop（是否循环）时应持续循环')
    assert.equal(defaultLoop.trigger_count, null)
    const shortcutInterval = await callTool(first.ctx, first.agent, 'schedule_wake', {
      target: 'shadow_agent', when: '1h', repeat: '30m', loop: false,
      trigger_count: 2, text: 'Synthetic shortcut interval',
    })
    assert.equal(shortcutInterval.loop, false)
    assert.equal(shortcutInterval.trigger_count, 2, 'schedule_wake（快捷创建任务）应使用相同的间隔次数设置')
    const projectUpdated = await callTool(first.ctx, first.agent, 'wake_task_update', {
      id: projectOne.id, title: 'Project task one updated', prompt: 'Updated synthetic project content',
    })
    assert.equal(projectUpdated.title, 'Project task one updated')
    assert.equal(projectUpdated.text, 'Updated synthetic project content')
    const projectDeleted = await callTool(first.ctx, first.agent, 'wake_task_delete', { id: projectTwo.id })
    assert.equal(projectDeleted.status, 'cancelled', 'wake_task_delete（删除链路任务）按契约取消并保留历史记录')
    const projectListed = await callTool(first.ctx, first.agent, 'wake_task_list')
    assert.deepEqual(new Set(projectListed.map(row => row.id)), new Set([
      projectOne.id, projectTwo.id, projectCron.id, singleAt.id, dailyTask.id, weeklyTask.id,
      finiteInterval.id, defaultLoop.id, shortcutInterval.id,
    ]))
    assert.equal(projectListed.find(row => row.id === finiteInterval.id).trigger_count, 3,
      'wake_task_list（查询任务）应返回更新后的触发次数')
    assert.deepEqual((await callTool(first.ctx, first.agent, 'schedule_list')).map(row => row.id), [nativeId],
      '项目 CRUD（创建／读取／更新／删除）不能改写官方会话任务')

    const nativeUpdated = await callTool(first.ctx, first.agent, 'schedule_update', {
      id: nativeId, title: 'Native reminder updated', prompt: 'Updated synthetic native content',
    })
    assert.equal(nativeUpdated.title, 'Native reminder updated')
    assert.equal(nativeRows.get(nativeId).record.prompt, 'Updated synthetic native content',
      'schedule_update（更新会话任务）应只更新官方 Domain（存储域）')
    const nativeDeleted = await callTool(first.ctx, first.agent, 'schedule_delete', { id: nativeId })
    assert.equal(nativeDeleted.deleted, true)
    assert.equal(nativeRows.has(nativeId), false)
    assert.deepEqual(await callTool(first.ctx, first.agent, 'schedule_list'), [])
    assert.deepEqual(new Set((await callTool(first.ctx, first.agent, 'wake_task_list')).map(row => row.id)),
      new Set([projectOne.id, projectTwo.id, projectCron.id, singleAt.id, dailyTask.id, weeklyTask.id,
        finiteInterval.id, defaultLoop.id, shortcutInterval.id]),
      '官方 CRUD（创建／读取／更新／删除）不能改写项目任务')

    const nativeAcrossRestart = await callTool(first.ctx, first.agent, 'schedule_create', {
      title: 'Native restart reminder', prompt: 'Synthetic native task retained across restart', after_seconds: 3600,
    })
    writeNativeFixture(nativeStore, nativeRows)
    const nativeFileBeforeUnmount = readFileSync(nativeStore, 'utf8')
    const projectFileBeforeUnmount = readFileSync(taskStore, 'utf8')
    await first.disposeProjectPlugin()
    assert.ok(tools().includes('schedule_list'), '卸载项目插件后官方工具仍应注册')
    assert.ok(!tools().some(name => name.startsWith('wake_task_')), '卸载项目插件应撤销全部 wake_task_*（链路任务工具）')
    assert.equal(first.webServer.routes.size, 0, '卸载项目插件应释放其 HTTP 路由')
    assert.equal(readFileSync(nativeStore, 'utf8'), nativeFileBeforeUnmount, '项目插件不能改写官方任务文件')
    assert.equal(readFileSync(taskStore, 'utf8'), projectFileBeforeUnmount, '仅卸载不应丢失项目任务数据')

    await first.dispose()
    const second = await mountHarness()
    assert.deepEqual((await callTool(second.ctx, second.agent, 'schedule_list')).map(row => row.id),
      [nativeAcrossRestart.id], '重启后的官方工具应从官方存储恢复会话任务')
    await second.mountProjectPlugin()
    const afterRestart = await callTool(second.ctx, second.agent, 'wake_task_list')
    assert.deepEqual(new Set(afterRestart.map(row => row.id)), new Set([
      projectOne.id, projectTwo.id, projectCron.id, singleAt.id, dailyTask.id, weeklyTask.id,
      finiteInterval.id, defaultLoop.id, shortcutInterval.id,
    ]),
      '项目重启只恢复项目 tasks store（任务存储），不导入新增或既有官方任务')
    assert.ok(!afterRestart.some(row => row.id === nativeAcrossRestart.id))
    assert.equal((await callTool(second.ctx, second.agent, 'schedule_list')).some(row => row.id === nativeAcrossRestart.id), true)
    const persisted = JSON.parse(readFileSync(taskStore, 'utf8'))
    assert.deepEqual(new Set(persisted.tasks.map(row => row.task_id)),
      new Set([projectOne.id, projectTwo.id, projectCron.id, singleAt.id, dailyTask.id, weeklyTask.id,
        finiteInterval.id, defaultLoop.id, shortcutInterval.id]))
    assert.deepEqual(JSON.parse(readFileSync(nativeStore, 'utf8')).tables.tasks[nativeAcrossRestart.id].sessionId,
      'session-coexistence', '官方 fixture（测试样本）应保留原会话绑定')
  } finally {
    for (const harness of harnesses.reverse()) {
      try { await harness.dispose() } catch {}
    }
    globalThis.fetch = originalFetch
    for (const key of envKeys) {
      if (previousEnv[key] === undefined) delete process.env[key]
      else process.env[key] = previousEnv[key]
    }
    rmSync(temporary, { recursive: true, force: true })
  }
})
