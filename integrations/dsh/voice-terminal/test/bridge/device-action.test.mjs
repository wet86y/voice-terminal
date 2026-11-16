import assert from 'node:assert/strict'
import test from 'node:test'
import { ObservationStore } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/observe.js'
import { DeviceActionExecutor } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/executor.js'

function fixture() {
  const target = { deviceId: 'B', connectionGeneration: 1, executorGeneration: 1 }
  const snapshot = { windowId: 1, processId: 10, executablePath: 'C:\\test.exe', title: '窗口',
    className: 'Window', rect: { x: 0, y: 0, width: 100, height: 100 }, foreground: true,
    treeHash: 'tree', shotHash: 'pixels', elementCount: 1 }
  const store = new ObservationStore({ maxCachedObservations: 8, maxObservationAgeMs: 10000,
    staleCheckTree: true, staleCheckPixels: true, staleCheckPixelsExemptMatchers: [] })
  const record = store.record(snapshot, target)
  const state = { target, fresh: snapshot, calls: 0, observations: 0, approved: true, afterApproval: undefined }
  const backend = {
    currentTarget: () => state.target,
    async observe() { state.observations++; return state.fresh },
    async execute() { state.calls++; return { ok: true } },
  }
  const approval = { async approve() { state.afterApproval?.(); return state.approved } }
  const executor = new DeviceActionExecutor(store, backend, approval)
  const action = { requestId: 'r', basedOn: record.id, deviceId: 'B', name: 'click', arguments: { x: 3 }, checkTree: true }
  return { executor, backend, state, action, target, snapshot }
}

test('授权前后复核并只执行一次，执行后允许窗口内容变化', async () => {
  const f = fixture()
  const backendSignal = new AbortController().signal
  // 只有授权后才发送动作；第三次读取用于确认进程身份。
  f.backend.execute = async () => { f.state.calls++; f.state.fresh = { ...f.snapshot, treeHash: 'after', shotHash: 'after' }; return { ok: true } }
  assert.deepEqual(await f.executor.execute(f.action, backendSignal), { ok: true })
  assert.equal(f.state.calls, 1)
  assert.equal(f.state.observations, 3)
})

test('跨设备观察、拒绝审批和审批期间窗口变化都不执行', async () => {
  for (const scenario of ['device', 'denied', 'stale', 'connection']) {
    const f = fixture()
    if (scenario === 'device') f.action.deviceId = 'C'
    if (scenario === 'denied') f.state.approved = false
    if (scenario === 'stale') f.state.afterApproval = () => { f.state.fresh = { ...f.snapshot, treeHash: 'changed' } }
    if (scenario === 'connection') f.state.afterApproval = () => { f.state.target = { ...f.target, executorGeneration: 2 } }
    await assert.rejects(f.executor.execute(f.action, new AbortController().signal), error =>
      ['STALE_DEVICE', 'DENIED', 'STALE_TREE'].includes(error.code))
    assert.equal(f.state.calls, 0)
  }
})

test('执行后进程变化返回未知，不重发动作', async () => {
  const f = fixture()
  let reads = 0
  // 在第二次观察完成后模拟进程替换。
  f.state.afterApproval = () => {
    Object.defineProperty(f.state, 'fresh', { get() { reads++; return reads > 1 ? { ...f.snapshot, processId: 11 } : f.snapshot } })
  }
  await assert.rejects(f.executor.execute(f.action, new AbortController().signal), error => error.code === 'OUTCOME_UNKNOWN')
  assert.equal(f.state.calls, 1)
})
