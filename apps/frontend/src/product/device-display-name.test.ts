import { describe, expect, it } from 'vitest'
import { connectionDisplayNames, deviceDisplayName } from './device-display-name'

describe('设备显示名称与真实身份分离', () => {
  it('旧设备编号转换为稳定的基础随机名称，保留中文自定义名称', () => {
    expect(deviceDisplayName('', 'device-123456')).toBe('Voice Terminal 123456')
    expect(deviceDisplayName('device-123456', 'device-123456')).toBe('Voice Terminal 123456')
    expect(deviceDisplayName('书房服务端', 'device-123456')).toBe('书房服务端')
  })

  it('重名按稳定身份编号派生后缀，刷新顺序变化不会改号或修改原数据', () => {
    const entries = [{ id: 'b', name: '书房' }, { id: 'a', name: '书房' }, { id: 'c', name: '书房' }]
    const original = structuredClone(entries)
    const labels = connectionDisplayNames(entries)
    expect([...labels]).toEqual([['a', '书房 1'], ['b', '书房 2'], ['c', '书房 3']])
    expect(connectionDisplayNames([...entries].reverse())).toEqual(labels)
    expect(entries).toEqual(original)
  })

  it('不会把派生后缀与已有名称混为同一个连接条目', () => {
    const labels = connectionDisplayNames([{ id: 'a', name: '书房' }, { id: 'b', name: '书房' }, { id: 'c', name: '书房 1' }])
    expect(new Set(labels.values()).size).toBe(3)
    expect(labels.get('c')).toBe('书房 1')
  })
})
