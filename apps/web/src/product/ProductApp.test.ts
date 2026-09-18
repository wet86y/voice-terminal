import { describe, expect, it } from 'vitest'

import productAppSource from './ProductApp.vue?raw'
import productAdapterSource from './useProductAdapter.ts?raw'
import { readFileSync } from 'node:fs'

// Vitest returns an empty string for every CSS text form (``?raw``, ``?inline``
// and ``import.meta.glob``), so the stylesheet is read from disk.  The Shadow
// panel's three-region layout only exists in CSS and it regressed once already,
// so it needs a direct source-level guard.
const productCssSource = readFileSync(new URL('../ui-demo/ui-demo.css', import.meta.url), 'utf8')

describe('fixed Shadow reasoning policy', () => {
  it('renders a read-only High indicator without the reasoning slider', () => {
    expect(productAppSource).toContain('High · 固定')
    expect(productAppSource).not.toContain('class="thinking-slider"')
    expect(productAppSource).not.toContain('Shadow 思考等级设置')
  })

  it('does not submit a mutable Shadow reasoning field', () => {
    expect(productAdapterSource).not.toContain('shadow_reasoning_effort:')
    expect(productAdapterSource).not.toContain('setShadowThinking')
  })

  it('renders the conversation rail from the server-ordered log', () => {
    // Ordering is the server's ``seq``: the rail renders one ordered
    // projection plus the transactions that have not settled yet.  The client
    // no longer merges recent inputs with the episode output projections, so
    // nothing can re-derive the chronology (or pin the KWS wake
    // acknowledgement to the bottom) on its own.
    expect(productAdapterSource).toContain('const conversationRows = computed')
    expect(productAdapterSource).toContain('voice.conversationEntries.value')
    expect(productAdapterSource).toContain('const pendingSpeechRows = computed')
    expect(productAdapterSource).not.toContain('mergeConversationTimeline')
    expect(productAdapterSource).not.toContain('createConversationOrder')
    expect(productAdapterSource).not.toContain('ordinalOf')
    expect(productAppSource).toContain('v-for="entry in conversationEntries"')
    expect(productAppSource).toContain('v-for="entry in liveSpeechEntries"')
    expect(productAppSource).not.toContain('orderedConversation')
    expect(productAppSource).not.toContain('speechOutputsForTurn')
    expect(productAppSource).not.toContain('hasSpeechTrackForTurn')
    expect(productAppSource).toContain('ref="conversationScrollElement"')
    expect(productAppSource).toContain('@scroll="captureConversationScrollPosition"')
    expect(productAppSource).toContain('class="conversation-stack"')
  })

  it('keeps a short conversation bottom-anchored without unreachable overflow', () => {
    expect(productAppSource).toContain("'is-tail-anchored': !conversationOverflow")
    expect(productAppSource).toContain('measureConversationOverflow')
  })
})

describe('Shadow panel display contract', () => {
  it('hides the focus line when it only repeats the summary', () => {
    // The backend projection writes the same text into ``focus`` and
    // ``summary``; rendering both duplicated the paragraph verbatim.
    expect(productAppSource).toContain('const shadowFocusText = computed')
    expect(productAppSource).toContain('if (!focus || focus === summary) return \'\'')
    expect(productAppSource).toContain('v-if="shadowFocusText" class="shadow-focus-line"')
  })

  it('renders the run trace and event output as fixed display boxes', () => {
    expect(productAppSource).toContain('class="shadow-trace-box"')
    expect(productAppSource).toContain('class="shadow-event-box"')
    expect(productAppSource).toContain('v-for="activity in shadowRunningTrace"')
    expect(productAppSource).toContain('shadowLatestEventActivity')
    // The old unbounded per-section lists must not come back.
    expect(productAppSource).not.toContain('shadowThinkingActivities')
    expect(productAppSource).not.toContain('shadowToolActivities')
    expect(productAppSource).not.toContain('v-for="activity in shadowEventActivities"')
  })

  it('keeps the whole trajectory and follows its tail by default', () => {
    // The box scrolls internally, so capping the rendered history would make
    // that scrolling pointless.
    expect(productAppSource).not.toContain('SHADOW_TRACE_LIMIT')
    expect(productAppSource).toContain('ref="shadowTraceElement"')
    expect(productAppSource).toContain('@scroll="captureShadowTraceScroll"')
    expect(productAppSource).toContain('const shadowTraceElement = ref<HTMLElement | null>(null)')
    expect(productAppSource).toContain('element.scrollTop = element.scrollHeight')
    // The retired whole-column follower must not come back.
    expect(productAppSource).not.toContain('shadowScrollElement')
    expect(productAppSource).not.toContain('captureShadowScrollPosition')
  })
})

describe('Shadow panel three-region layout', () => {
  it('never scrolls the whole panel column', () => {
    // A scrolling column let a long summary push the trace and output regions
    // out of view, which is what made the three regions look like one feed.
    const columnRules = productCssSource
      .split('\n')
      .filter(line => line.includes('.shadow-scroll {'))
    expect(columnRules.length).toBeGreaterThan(0)
    expect(columnRules.some(line => line.includes('overflow: hidden'))).toBe(true)
    expect(columnRules.some(line => line.includes('overflow-y: auto'))).toBe(false)
    expect(columnRules.some(line => line.includes('scrollbar-gutter'))).toBe(false)
  })

  it('bounds the summary and lets only the trace region flex and scroll', () => {
    const summaryRule = productCssSource
      .split('\n')
      .find(line => line.includes('.shadow-summary-card {') && line.includes('margin-top: 20px'))
    expect(summaryRule).toContain('flex: 0 1 auto')
    expect(summaryRule).toContain('max-height: 180px')
    expect(summaryRule).toContain('overflow-y: auto')
    // The run card absorbs the leftover height and owns both lower regions.
    const runtimeRule = productCssSource
      .split('\n')
      .find(line => line.includes('.shadow-runtime-card {') && line.includes('margin-top: 13px'))
    expect(runtimeRule).toContain('flex: 1 1 419px')
    expect(runtimeRule).toContain('min-height: 419px')
    expect(runtimeRule).toContain('display: flex')
    // Region 2 takes its height from the flex parent and scrolls; region 3 is
    // content-sized so a single result never reserves the trace's height.
    expect(productCssSource).toContain('.shadow-activity-section.thinking-section { flex: 1 1 0; min-height: 143px;')
    expect(productCssSource).toContain('.shadow-activity-section.event-section { flex: 0 0 auto; }')
    expect(productCssSource).toContain('.shadow-trace-box { flex: 1 1 auto; min-height: 80px; }')
    expect(productCssSource).toContain('.shadow-event-box { flex: 0 0 auto; min-height: 143px; max-height: 252px; }')
  })

  it('compresses the summary text so the box can stay short', () => {
    const headingRule = productCssSource
      .split('\n')
      .find(line => line.includes('.shadow-summary-heading h3 {'))
    expect(headingRule).toContain('font-size: 13px')
    expect(headingRule).toContain('line-height: 1.5')
  })

  it('gives the run card a single merged heading row', () => {
    // The card used to render its title twice: the card header ("AGENT 运行轨迹")
    // directly above the section header ("运行轨迹"), with the status chip on a
    // row of its own.  Now one row carries kicker, title, chip and count.
    expect(productAppSource).not.toContain('shadow-runtime-heading')
    expect(productAppSource).not.toContain('shadow-runtime-sections')
    expect(productCssSource).not.toContain('.shadow-runtime-heading')
    expect(productCssSource).not.toContain('.shadow-runtime-sections')

    const sectionStart = productAppSource.indexOf('thinking-section')
    const sectionHeaderEnd = productAppSource.indexOf('</header>', sectionStart)
    const sectionHeader = productAppSource.slice(sectionStart, sectionHeaderEnd)
    for (const token of ['section-label', '<strong>运行轨迹</strong>', 'shadow-run-indicator', '<em>']) {
      expect(sectionHeader).toContain(token)
    }
    // The title must not be rendered twice: the kicker is the only other place
    // the words appear, and each element occurs exactly once.
    const cardStart = productAppSource.indexOf('shadow-runtime-card')
    const cardEnd = productAppSource.indexOf('shadow-details', cardStart)
    const cardBlock = productAppSource.slice(cardStart, cardEnd)
    expect(cardBlock.match(/<strong>运行轨迹<\/strong>/g)?.length).toBe(1)
    expect(cardBlock.match(/Agent 运行轨迹/g)?.length).toBe(1)
    expect(cardBlock).not.toContain('shadow-runtime-heading')
  })

  it('sizes every region floor so no region can overflow into the next', () => {
    // Regression: the trace box once carried a floor larger than its own
    // section, so it spilled over the output region instead of scrolling.
    // The boxes themselves must stay shrinkable; the floors live on the
    // sections and on the run card that contains them.  The card floor is
    // asserted against the CSS values rather than hard-coded numbers so this
    // stays honest when a floor is retuned.
    const cssLine = (needle: string) => productCssSource.split('\n').find(l => l.includes(needle)) ?? ''
    const numberIn = (line: string, property: string) =>
      Number(new RegExp(`${property}:\\s*(\\d+)px`).exec(line)?.[1])

    const traceFloor = numberIn(cssLine('.shadow-trace-box { flex: 1 1 auto'), 'min-height')
    const eventFloor = numberIn(cssLine('.shadow-event-box { flex: 0 0 auto'), 'min-height')
    const thinkingFloor = numberIn(cssLine('.shadow-activity-section.thinking-section'), 'min-height')
    const runtimeLine = cssLine('.product-shell .shadow-runtime-card { margin-top: 13px')
    const runtimeFloor = numberIn(runtimeLine, 'min-height')

    expect(traceFloor).toBe(80)
    expect(eventFloor).toBe(143)
    // A section's floor is its padding (15 top + 2 bottom) plus its activity
    // header (27px) plus the floor of the box it holds.
    const sectionFloor = (boxFloor: number) => 15 + 2 + 27 + boxFloor
    expect(thinkingFloor).toBeGreaterThanOrEqual(sectionFloor(traceFloor))
    // The card floor adds its own padding (14 top + 15 bottom); the heading row
    // is the thinking section's own header, so it is already inside that floor.
    expect(runtimeFloor).toBeGreaterThanOrEqual(
      14 + 15 + sectionFloor(traceFloor) + sectionFloor(eventFloor),
    )
    expect(productCssSource).toContain('.product-shell .shadow-runtime-card { margin-top: 13px; padding: 14px 14px 15px; flex: 1 1 419px; min-height: 419px;')
  })

  it('scales the page to a 80% baseline with matching viewport units', () => {
    // ``zoom`` scales layout and viewport units together, so every shell
    // height must divide by the same factor or the page clips at the bottom.
    const shellRule = productCssSource
      .split('\n')
      .find(line => line.includes('.product-shell {') && line.includes('zoom:'))
    expect(shellRule).toContain('zoom: 0.8')
    expect(shellRule).toContain('height: calc(100vh / 0.8)')
    expect(shellRule).toContain('min-height: calc(100vh / 0.8)')
    // No shell-applied height rule may still use an unscaled 100vh.
    const unscaled = productCssSource
      .split('\n')
      .filter(line => line.includes('.product-shell') && /height:\s*calc\(100vh\s*-/.test(line))
    expect(unscaled).toEqual([])
    for (const needle of [
      '.product-shell .workbench { height: calc(100vh / 0.8 - 136px)',
      '.product-shell .settings-page { height: calc(100vh / 0.8 - 155px)',
    ]) {
      expect(productCssSource).toContain(needle)
    }
    expect(productCssSource).toContain('.product-shell .workbench { height: calc(100vh / 0.8 - 118px)')
    expect(productCssSource).toContain('.product-shell .settings-page { height: calc(100vh / 0.8 - 118px)')
  })

  it('keeps the trace scrolling through the shared flex rules at every width', () => {
    // Below 1180px the panel becomes a full-width band whose grid row is much
    // shorter than the three regions want.  It must keep using the same flex
    // distribution rather than a fixed height that would fight the summary.
    const narrowBlock = productCssSource
      .split('@media (max-width: 1180px) {')[1]
      ?.split('\n}')[0] ?? ''
    expect(narrowBlock).toContain('.shadow-column')
    expect(narrowBlock).not.toContain('.shadow-trace-box')
    expect(narrowBlock).not.toContain('.shadow-scroll')
  })
})
