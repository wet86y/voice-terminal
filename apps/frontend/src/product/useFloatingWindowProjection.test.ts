import { describe, expect, it } from 'vitest'
import { effectScope, ref } from 'vue'
import { useVoiceTerminal } from '../composables/useVoiceTerminal'
import type { WorkbenchModel } from '../interface/workbench'
import { playbackCaption, useFloatingWindowProjection, userCaption } from './useFloatingWindowProjection'

describe('悬浮字幕的语音数据投影', () => {
  it('识别追加与改字时定位当前变化片段，不滚回整段中间', () => {
    const first = userCaption('input:1', '今天下午去图书馆', true, null)!
    const append = userCaption('input:1', '今天下午去图书馆读书', true, first)!
    expect(append.text.slice(append.focusStart, append.focusEnd)).toBe('读书')
    const correction = userCaption('input:1', '今天晚上去图书馆读书', true, append)!
    expect(correction.text.slice(correction.focusStart, correction.focusEnd)).toBe('晚上')
    expect(userCaption('input:1', '  ', false, correction)).toBeNull()
    const emoji = userCaption('input:2', '换个表情😊', true, null)!
    const changedEmoji = userCaption('input:2', '换个表情😄', true, emoji)!
    expect(changedEmoji.text.slice(changedEmoji.focusStart, changedEmoji.focusEnd)).toBe('😄')
  })

  it('后端字符下标转换为浏览器编码单元，图片与异常进度不作为字幕', () => {
    expect(playbackCaption('speech:1', { text: '你😊好', start: 1, end: 2, precision: 'word' }))
      .toMatchObject({ text: '你😊好', focusStart: 1, focusEnd: 3, role: 'assistant' })
    for (const invalid of [null, { text: '截图', start: 0, end: 2, precision: 'word', attachments: [] },
      { text: '你好', start: 0, end: 3, precision: 'word' }, { text: '你好', start: 0, end: 1, precision: 'guessed' }]) {
      expect(playbackCaption('speech:1', invalid)).toBeNull()
    }
  })

  it('只接入实际播放事务和用户转写，截图消息和排队文本不弹出', () => {
    const scope = effectScope()
    scope.run(() => {
      const voice = useVoiceTerminal({ webSocketUrl: 'ws://test.invalid/ws' })
      const projection = useFloatingWindowProjection(voice, ref({ state: 'standby', disabled: false } as WorkbenchModel))
      voice.connection.value = 'connected'
      voice.canStart.value = true
      voice.conversationEntries.value = [{ role: 'tool', text: '屏幕截图', attachments: [{ kind: 'screen' }] }] as never
      voice.playbackQueue.value = [{ conversation_id: 'c', speech_id: 's', turn_id: 't', text: '尚未播放的回复', state: 'waiting', output_generation: 1 }]
      expect(projection.value.caption).toBeNull()
      voice.playbackQueue.value = [{ ...voice.playbackQueue.value[0]!, state: 'playing' }]
      expect(projection.value.caption?.role).toBe('assistant')
      voice.speechProgress.value = { conversation_id: 'c', speech_id: 's', output_generation: 1,
        playback_caption: { text: '当前播报位置', start: 2, end: 4, precision: 'word' } }
      expect(projection.value.caption).toMatchObject({ text: '当前播报位置', focusStart: 2, focusEnd: 4 })
      voice.speechProgress.value = { conversation_id: 'c', speech_id: 'other',
        playback_caption: { text: '其他队列回复', start: 0, end: 2, precision: 'word' } }
      expect(projection.value.caption?.text).toBe('当前播报位置')
      voice.speechProgress.value = { conversation_id: 'c', speech_id: 's', output_generation: 0,
        playback_caption: { text: '上一代的回复', start: 0, end: 2, precision: 'word' } }
      expect(projection.value.caption?.text).toBe('当前播报位置')
      voice.listening.value = true
      voice.currentTurnId.value = 'new-input'
      voice.current.partial_text = '用户正在说话'
      expect(projection.value.caption?.role).toBe('user')
      voice.listening.value = false
      expect(projection.value.silenced).toBe(true)
      expect(projection.value.caption?.role).toBe('assistant')
      expect(projection.value.state).toBe('standby')
      voice.dispose()
    })
    scope.stop()
  })

  it('颜色、跳动与波纹数据始终镜像主页面，不根据字幕播放另行推断状态', () => {
    const scope = effectScope()
    scope.run(() => {
      const voice = useVoiceTerminal({ webSocketUrl: 'ws://test.invalid/ws' })
      const workbench = ref({ state: 'listening', sessionActive: true, audioLevels: [.2, .4, .8, .4, .2] })
      const projection = useFloatingWindowProjection(voice, workbench)
      expect(projection.value).toMatchObject({ state: 'listening', sessionActive: true, audioLevels: [.2, .4, .8, .4, .2] })
      voice.playbackQueue.value = [{ conversation_id: 'c', speech_id: 's', turn_id: 't', text: '正在播报', state: 'playing' }]
      expect(projection.value.state).toBe('listening')
      workbench.value = { ...workbench.value, state: 'thinking', audioLevels: [0, 0, 0, 0, 0] }
      expect(projection.value).toMatchObject({ state: 'thinking', audioLevels: [0, 0, 0, 0, 0] })
      voice.dispose()
    })
    scope.stop()
  })
})
