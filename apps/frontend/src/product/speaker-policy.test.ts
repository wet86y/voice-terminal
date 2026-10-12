import { describe, expect, it } from 'vitest'
import { normalizeSettings } from './normalization'
import { SPEAKER_DEFAULTS, SPEAKER_FIELDS } from './speaker-policy'

describe('声纹高级设置与旧设备迁移', () => {
  it.each(SPEAKER_FIELDS)('$label 保存值重读且具备中文标签与数值边界', field => {
    const value = SPEAKER_DEFAULTS[field.key] + field.step
    const settings = normalizeSettings({ speaker: { [field.key]: value } })
    expect(settings.speaker[field.key]).toBe(value)
    expect(field.label).toMatch(/[\u4e00-\u9fff]/)
    expect(field.min).toBeLessThanOrEqual(SPEAKER_DEFAULTS[field.key])
    expect(field.max).toBeGreaterThanOrEqual(SPEAKER_DEFAULTS[field.key])
  })
  it('旧声纹设备选择迁移至CAM++且新增参数使用默认值', () => {
    const settings = normalizeSettings({ compute: { devices: { wespeaker: 'cuda' } } })
    expect(settings.compute.devices.campplus).toBe('cuda')
    for (const field of SPEAKER_FIELDS) expect(settings.speaker[field.key]).toBe(SPEAKER_DEFAULTS[field.key])
  })
})
