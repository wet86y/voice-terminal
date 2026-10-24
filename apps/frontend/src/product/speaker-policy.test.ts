import { describe, expect, it } from 'vitest'
import { normalizeSettings } from './normalization'
import { SPEAKER_DEFAULTS, SPEAKER_CONTROL_DEFAULTS, SPEAKER_FIELDS, speakerControlsError } from './speaker-policy'

describe('声纹四项设置与旧设备迁移', () => {
  it.each(SPEAKER_FIELDS)('$label 保存值重读且具备中文标签与数值边界', field => {
    const value = SPEAKER_CONTROL_DEFAULTS[field.key] + field.step
    const settings = normalizeSettings({ speaker: { [field.key]: value } })
    expect(settings.speaker[field.key]).toBe(value)
    expect(field.label).toMatch(/[\u4e00-\u9fff]/)
    expect(field.min).toBeLessThanOrEqual(SPEAKER_CONTROL_DEFAULTS[field.key])
    expect(field.max).toBeGreaterThanOrEqual(SPEAKER_CONTROL_DEFAULTS[field.key])
  })
  it('旧声纹设备选择迁移至CAM++且新增参数使用默认值', () => {
    const settings = normalizeSettings({ compute: { devices: { wespeaker: 'cuda' } } })
    expect(settings.compute.devices.campplus).toBe('cuda')
    for (const [key, value] of Object.entries(SPEAKER_DEFAULTS)) expect(settings.speaker[key as keyof typeof SPEAKER_DEFAULTS]).toBe(value)
  })
  it('负派生阈值报错，零阈值与新上限可以保存', () => {
    expect(speakerControlsError({ ...SPEAKER_CONTROL_DEFAULTS, confirm_threshold: .1, intra_turn_relaxation: .2 })).toContain('会话内')
    expect(speakerControlsError({ ...SPEAKER_CONTROL_DEFAULTS, confirm_threshold: .1, continuity_relaxation: .2 })).toContain('会话间')
    expect(speakerControlsError({ confirm_threshold: .3, intra_turn_relaxation: .3, continuity_relaxation: .2, session_weight: .8 })).toBe('')
    expect(speakerControlsError({ confirm_threshold: 0, intra_turn_relaxation: 0, continuity_relaxation: 0, session_weight: 0 })).toBe('')
    expect(speakerControlsError({ ...SPEAKER_CONTROL_DEFAULTS, session_weight: .81 })).toContain('临时声纹权重')
  })
})
