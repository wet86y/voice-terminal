// 派生自 dsh-windows-remote-ssh/tests/unit/observe.spec.ts；保留 MIT（宽松开源许可证）行为边界。
import assert from 'node:assert/strict'
import test from 'node:test'
import { ObservationStore } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/observe.js'

const config = { maxCachedObservations: 2, maxObservationAgeMs: 1000,
  staleCheckTree: true, staleCheckPixels: true, staleCheckPixelsExemptMatchers: [] }
const target = { deviceId: 'B', connectionGeneration: 1, executorGeneration: 1 }
const snapshot = { windowId: 1, processId: 100, executablePath: 'C:\\notepad.exe', title: '记事本',
  className: 'Notepad', rect: { x: 0, y: 0, width: 800, height: 600 }, foreground: true,
  treeHash: 'abc', shotHash: 'def', elementCount: 3 }

test('观察绑定设备和代次，相同窗口不会覆盖其他设备', () => {
  const store = new ObservationStore(config)
  const b = store.record(snapshot, target)
  const c = store.record(snapshot, { ...target, deviceId: 'C' })
  assert.notEqual(b.id, c.id)
  assert.equal(store.verify(b, snapshot, target).ok, true)
  assert.equal(store.verify(b, snapshot, { ...target, deviceId: 'C' }).code, 'STALE_DEVICE')
  assert.equal(store.verify(b, snapshot, { ...target, connectionGeneration: 2 }).code, 'STALE_DEVICE')
  assert.equal(store.verify(b, snapshot, { ...target, executorGeneration: 2 }).code, 'STALE_DEVICE')
})

test('同一状态的再次观察不复活旧编号，缓存淘汰后拒绝旧记录', () => {
  const store = new ObservationStore(config)
  const first = store.record(snapshot, target)
  const second = store.record(snapshot, target)
  assert.notEqual(first.id, second.id)
  store.record({ ...snapshot, windowId: 2 }, target)
  assert.equal(store.verify(first, snapshot, target).code, 'UNKNOWN_OBSERVATION')
  assert.equal(store.get(first.id), undefined)
})

test('变化的身份、界面树、像素和过期时间分别拒绝', () => {
  const store = new ObservationStore(config)
  const record = store.record(snapshot, target)
  assert.equal(store.verify(record, { ...snapshot, processId: 101 }, target).code, 'STALE_IDENTITY')
  assert.equal(store.verify(record, { ...snapshot, treeHash: 'changed' }, target).code, 'STALE_TREE')
  assert.equal(store.verify(record, { ...snapshot, shotHash: 'changed' }, target).code, 'STALE_PIXELS')
  assert.equal(store.verify(record, snapshot, target, record.observedAt + 1001).code, 'EXPIRED')
  assert.equal(store.verify(record, snapshot, target, record.observedAt - 1).code, 'EXPIRED')
  assert.equal(store.verify(record, { ...snapshot, treeHash: 'changed' }, target, record.observedAt, false).ok, true)
})

test('像素例外可重复匹配，关闭检查不影响身份检查', () => {
  const store = new ObservationStore({ ...config, staleCheckPixelsExemptMatchers: [/记事本/g] })
  const record = store.record(snapshot, target)
  assert.equal(store.verify(record, { ...snapshot, shotHash: 'changed' }, target).ok, true)
  assert.equal(store.verify(record, { ...snapshot, shotHash: 'changed' }, target).ok, true)
  assert.equal(store.verify(record, { ...snapshot, processId: 2 }, target).code, 'STALE_IDENTITY')
})

test('观察记录和设备目标不可通过调用方修改', () => {
  const store = new ObservationStore(config)
  const mutableTarget = { ...target }
  const mutableSnapshot = { ...snapshot, rect: { ...snapshot.rect } }
  const record = store.record(mutableSnapshot, mutableTarget)
  mutableTarget.deviceId = 'C'
  mutableSnapshot.rect.x = 100
  assert.equal(record.target.deviceId, 'B')
  assert.equal(record.rect.x, 0)
  assert.throws(() => { record.target.deviceId = 'C' }, TypeError)
  assert.throws(() => { record.rect.x = 100 }, TypeError)
})
