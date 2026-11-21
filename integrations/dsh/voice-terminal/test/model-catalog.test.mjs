import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { ModelCatalogObserver, isCatalogEffortCurrent } from '../../../../.local/build/dsh/bundles/voice-terminal-test/lib/shared/model-catalog.js'

const catalog = model => ({ default: { provider: 'minimax', model }, groups: [{ id: 'minimax', name: 'MiniMax', models: [
  { id: 'flash', name: 'Flash', reasoning: { efforts: [{ id: 'high', name: '高' }, { id: 'max', name: '最高' }] } },
  { id: 'standard', name: 'Standard', reasoning: { efforts: [{ id: 'high', name: '高' }] } },
] }] })

function deferred() { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }

test('挂载时先订阅，宿主目录变更后更新全局模型和推理能力，无需重载页面', async () => {
  let invalidate; let current = catalog('flash'); let subscribed = false; const snapshots = []
  const observer = new ModelCatalogObserver(async () => { assert.ok(subscribed); return current }, listener => {
    subscribed = true; invalidate = listener; return () => { subscribed = false }
  }, value => snapshots.push(value))
  await setImmediate()
  assert.equal(snapshots.at(-1).catalog.default.model, 'flash')
  current = catalog('standard'); invalidate(false); await setImmediate()
  const latest = snapshots.at(-1)
  assert.equal(latest.catalog.default.model, 'standard')
  assert.equal(latest.loading, false)
  assert.equal(isCatalogEffortCurrent(latest.catalog, { provider: 'minimax', id: 'standard' }, 'max'), false)
  observer.dispose(); assert.equal(subscribed, false)
})

test('重连清空旧目录，旧连接晚到的读取不能覆盖新宿主目录', async () => {
  const old = deferred(); const next = deferred(); let reads = 0; let invalidate; const snapshots = []
  const observer = new ModelCatalogObserver(() => ++reads === 1 ? old.promise : next.promise,
    listener => { invalidate = listener; return () => {} }, value => snapshots.push(value))
  invalidate(true)
  assert.equal(snapshots.at(-1).catalog, undefined)
  next.resolve(catalog('standard')); await setImmediate()
  old.resolve(catalog('flash')); await setImmediate()
  assert.equal(snapshots.at(-1).catalog.default.model, 'standard')
  observer.dispose()
})

test('退出页面移除监听且拒绝晚到目录；读取失败不返回可用于保存的旧快照', async () => {
  const pending = deferred(); let off = 0; const snapshots = []
  const observer = new ModelCatalogObserver(() => pending.promise, () => () => { off++ }, value => snapshots.push(value))
  observer.dispose(); observer.dispose(); const count = snapshots.length
  pending.resolve(catalog('flash')); await setImmediate()
  assert.equal(off, 1); assert.equal(snapshots.length, count); assert.equal(await observer.refresh(), undefined)
  const failed = []
  const unavailable = new ModelCatalogObserver(async () => { throw new Error('目录暂不可用') }, () => () => {}, value => failed.push(value))
  await setImmediate(); assert.equal(failed.at(-1).failed, true)
  assert.equal(await unavailable.refresh(), undefined); unavailable.dispose()
})

test('旧菜单不能把 max（最高推理等级）保存给已切换的 DSH 全局模型', () => {
  const latest = catalog('standard')
  const flash = { provider: 'minimax', id: 'flash' }; const standard = { provider: 'minimax', id: 'standard' }
  assert.equal(isCatalogEffortCurrent(latest, flash, 'max'), false)
  assert.equal(isCatalogEffortCurrent(latest, standard, 'max'), false)
  assert.equal(isCatalogEffortCurrent(latest, standard, 'high'), true)
  assert.equal(isCatalogEffortCurrent(latest, standard, undefined), true)
  assert.equal(isCatalogEffortCurrent(catalog('flash'), flash, 'max'), true)
})
