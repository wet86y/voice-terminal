import { describe, expect, it } from 'vitest'
import { displayClockStatus, parseFloatingDisplay, setDisplayFocus, startDisplayClock } from './floating-display'

describe('固定图文展示', () => {
  const resolveImage = (reference: string) => reference

  it('保留标题、强调与图片顺序，渲染正文和图注共用字符额度', () => {
    const document = parseFloatingDisplay('# 示例\n![图注](fixture:icon)\n- **重点**', resolveImage)
    expect(document.lineLimit).toBe(3)
    expect(document.blocks).toEqual([
      { kind: 'title', runs: [{ text: '示例', bold: false }] },
      { kind: 'image', src: 'fixture:icon', description: '图注' },
      { kind: 'bullet', runs: [{ text: '重点', bold: true }] },
    ])
    const limit = '![' + '图'.repeat(497) + '](fixture:icon)\n**正文好**'
    expect(() => parseFloatingDisplay(limit, resolveImage)).not.toThrow()
    expect(() => parseFloatingDisplay(limit + '。', resolveImage)).toThrow(/500 字符/)
  })

  it('每项最多一张图片并要求把图放在独立行，文字需要符合模板行数', () => {
    expect(() => parseFloatingDisplay('![](one)\n![](two)', resolveImage)).toThrow(/一张图片/)
    expect(() => parseFloatingDisplay('段中 ![](one)', resolveImage)).toThrow(/单独一行/)
    expect(() => parseFloatingDisplay('![](one)\n甲\n乙\n丙\n丁', resolveImage)).toThrow(/行数/)
    expect(() => parseFloatingDisplay('甲\n乙\n丙\n丁\n戊\n己\n庚', resolveImage)).toThrow(/行数/)
  })
})

describe('悬浮展示计时', () => {
  it('悬停耗尽仍保留，离开恰好续五秒', () => {
    const active = startDisplayClock(1000, 5, false)
    const focused = setDisplayFocus(active, true, 5000)
    expect(displayClockStatus(focused, 9000)).toEqual({ ratio: 0, expired: false })
    const left = setDisplayFocus(focused, false, 9000)
    expect(displayClockStatus(left, 13999).expired).toBe(false)
    expect(displayClockStatus(left, 14000).expired).toBe(true)
  })

  it('未耗尽时移出，在实际剩余时间上加五秒，连续离开事件只结算一次', () => {
    const clock = startDisplayClock(0, 5, true)
    const left = setDisplayFocus(clock, false, 3000)
    expect(left.deadline).toBe(10000)
    expect(setDisplayFocus(left, false, 3100)).toBe(left)
  })
})
