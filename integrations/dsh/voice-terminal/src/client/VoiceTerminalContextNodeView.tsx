import type { PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import { JsonBlock } from '@deepseek-ai/dsh-client-ui-primitives'
import type { VoiceTerminalContextNodeData } from './voice-terminal-context.js'
import css from './VoiceTerminalContextNodeView.module.css'

type Props = PropsRuntime<'conversation.chat.node', 'voice-terminal-context'>
  & PropsLocale<'settings.voiceTerminal'>

function isTextBlock(value: unknown): value is { type: 'text'; text: string } {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const block = value as Record<string, unknown>
  return block.type === 'text' && typeof block.text === 'string'
}

function sourceKind(source: unknown): string {
  if (typeof source !== 'object' || source === null || Array.isArray(source)) return ''
  const kind = (source as Record<string, unknown>).kind
  return typeof kind === 'string' ? kind : ''
}

function messageTime(time: number): { label: string; iso: string | undefined } {
  const date = new Date(time)
  if (!Number.isFinite(date.getTime())) return { label: String(time), iso: undefined }
  return { label: date.toLocaleString(), iso: date.toISOString() }
}

/** Show the exact bridge context recorded in the session（显示会话中记录的完整桥接上下文）. */
export function VoiceTerminalContextNodeView({ node, t }: Props) {
  const data: VoiceTerminalContextNodeData = node.data
  const time = messageTime(data.time)
  return (
    <section className={css.root} aria-label={t('chatContextTitle')} data-voice-terminal-context>
      <header className={css.header}>
        <strong>{t('chatContextTitle')}</strong>
        <time dateTime={time.iso}>{time.label}</time>
      </header>
      <div className={css.content}>
        {data.content.map((block, index) => isTextBlock(block)
          ? <pre className={css.text} key={`text:${index}`}>{block.text}</pre>
          : (
            <JsonBlock
              key={`block:${index}`}
              label={t('chatContextBlock', { index: index + 1 })}
              payload={block}
              defaultOpen
              truncatedLabel={total => t('chatContextTruncated', { total })}
            />
          ))}
      </div>
      <footer className={css.meta}>
        <span>{t('chatContextSequence', { seq: data.seq })}</span>
        <span>{t('chatContextSource', { source: sourceKind(data.source) || t('chatContextUnknownSource') })}</span>
      </footer>
    </section>
  )
}
