import { useCallback, useEffect, useId, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'
// DSH Remote API's SettingsPathOpView（设置接口提供的路径操作类型）.
import type { SettingsPathOpView } from '@deepseek-ai/dsh-api-remotes/client'
import type { ConfigForm } from '@deepseek-ai/dsh-client-ui-settings/client'
import type { IWorkspaces, WorkspaceSnapshot } from '@deepseek-ai/dsh-api-workspace-controller/client'
import { Button, IconChevronDownOutlineRegular, IconFolderCloseRegular, Menu } from '@deepseek-ai/dsh-client-ui-primitives'
import type { MenuEntry } from '@deepseek-ai/dsh-client-ui-primitives'
import type { PropsLocale, PropsRuntime, InjectFace } from '@deepseek-ai/dsh-client-ui-slots'
import type { VoiceTerminalSettings } from '../settings-contract.js'
import css from './VoiceTerminalSettingsPage.module.css'

interface ModelChoice {
  /** Stable model identifier（稳定模型标识）. */
  readonly id: string
  /** Display name supplied by DSH（DSH 提供的显示名称）. */
  readonly name: string
  /** Reasoning options advertised by this model（该模型公布的推理选项）. */
  readonly reasoning?: {
    /** Supported reasoning levels（支持的推理强度）. */
    readonly efforts: readonly { readonly id: string; readonly name: string }[]
    /** Provider default reasoning level（供应方默认推理强度）. */
    readonly defaultEffort?: string
  }
}

interface ModelCatalog {
  /** DSH global model selection（DSH 全局模型选择）. */
  readonly default: { readonly provider: string; readonly model: string; readonly reasoningEffort?: string }
  /** Models grouped by provider（按供应方分组的模型）. */
  readonly groups: readonly {
    /** Stable provider identifier（稳定供应方标识）. */
    readonly id: string
    /** Display name supplied by DSH（DSH 提供的显示名称）. */
    readonly name: string
    /** Routable models for this provider（该供应方当前可用模型）. */
    readonly models: readonly ModelChoice[]
  }[]
}

/** DSH services captured by this settings page（设置页面）. */
export interface VoiceTerminalSettingsInjected {
  /** Persistent plugin preferences（插件专属持久设置）. */
  readonly settings: ConfigForm<VoiceTerminalSettings>
  /** DSH's durable workspace projection（DSH 持久工作区列表）. */
  readonly workspaces: IWorkspaces
  /** Current routable DSH models（DSH 当前可用模型目录）. */
  readonly modelCatalog: () => Promise<ModelCatalog>
}

export type VoiceTerminalSettingsPageProps =
  & PropsRuntime<'settings.section'>
  & PropsLocale<'settings.voiceTerminal'>
  & InjectFace<VoiceTerminalSettingsInjected>

interface MenuFieldProps {
  /** Visible field title（字段标题）. */
  readonly label: string
  /** Optional explanation below the control（控件下方说明）. */
  readonly description?: string | undefined
  /** Current choice shown by the DSH button（DSH 按钮上显示的当前选项）. */
  readonly value: ReactNode
  /** DSH menu rows（DSH 菜单选项）. */
  readonly items: readonly MenuEntry[]
  /** Selected menu row id（当前选项标识）. */
  readonly selectedId?: string | undefined
  /** Whether the field can be changed（字段是否可更改）. */
  readonly disabled: boolean
  /** Persist the selected row id（保存所选标识）. */
  readonly onSelect: (id: string) => void
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

const FOLLOW_GLOBAL_ID = 'follow-global'
const MODEL_DEFAULT_ID = 'model-default'

function modelKey(provider: string, model: string): string {
  return provider + '\u0000' + model
}

function parseModelKey(key: string): { provider: string; model: string } | undefined {
  const separator = key.indexOf('\u0000')
  if (separator < 1) return undefined
  return { provider: key.slice(0, separator), model: key.slice(separator + 1) }
}

function ChoiceContent({ primary, detail }: { readonly primary: string; readonly detail?: string }) {
  return (
    <span className={css.choiceContent}>
      <span className={css.choicePrimary}>{primary}</span>
      {detail ? <span className={css.choiceDetail}>{detail}</span> : null}
    </span>
  )
}

/** DSH menu-backed setting control（使用 DSH 菜单的设置控件）. */
function MenuField({ label, description, value, items, selectedId, disabled, onSelect }: MenuFieldProps) {
  const [open, setOpen] = useState(false)
  const descriptionId = useId()
  const close = (): void => { setOpen(false) }

  return (
    <div className={css.field}>
      <span className={css.fieldLabel}>{label}</span>
      <Menu
        open={open}
        anchor={(
          <Button
            variant="outline"
            className={css.trigger}
            aria-label={label}
            aria-describedby={description ? descriptionId : undefined}
            aria-expanded={open}
            disabled={disabled}
            onClick={() => { setOpen(current => !current) }}
          >
            <span className={css.triggerValue}>{value}</span>
            <IconChevronDownOutlineRegular className={css.chevron} />
          </Button>
        )}
        items={items}
        selectedId={selectedId}
        onSelect={id => {
          close()
          onSelect(id)
        }}
        onClose={close}
        portal
        autoFocus
        dense
        className={css.menuAnchor}
      />
      {description ? <small id={descriptionId} className={css.help}>{description}</small> : null}
    </div>
  )
}

/** Render Voice Terminal plugin settings as an independent DSH settings page（独立的 DSH 设置页面）. */
export function VoiceTerminalSettingsPage({ settings, workspaces, modelCatalog, t }: VoiceTerminalSettingsPageProps): ReactNode {
  const subscribePreference = useCallback((listener: () => void) => settings.subscribe(listener), [settings])
  const getPreferenceSnapshot = useCallback(() => settings.getSnapshot(), [settings])
  const preference = useSyncExternalStore(subscribePreference, getPreferenceSnapshot, getPreferenceSnapshot)
  const [workspaceSnapshot, setWorkspaceSnapshot] = useState<WorkspaceSnapshot>(() => workspaces.list.getSnapshot())
  const [catalog, setCatalog] = useState<ModelCatalog>()
  const [catalogFailed, setCatalogFailed] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const value = preference.value

  useEffect(() => {
    setWorkspaceSnapshot(workspaces.list.getSnapshot())
    return workspaces.list.subscribe(() => { setWorkspaceSnapshot(workspaces.list.getSnapshot()) })
  }, [workspaces])

  useEffect(() => {
    let current = true
    void modelCatalog().then(
      result => { if (current) { setCatalog(result); setCatalogFailed(false) } },
      () => { if (current) setCatalogFailed(true) },
    )
    return () => { current = false }
  }, [modelCatalog])

  const models = useMemo(() => catalog?.groups.flatMap(group =>
    group.models.map(model => ({ ...model, provider: group.id, providerName: group.name }))) ?? [], [catalog])
  const configuredModelKey = value?.provider && value.model ? modelKey(value.provider, value.model) : ''
  const configuredModel = models.find(model => modelKey(model.provider, model.id) === configuredModelKey)
  const modelMissing = catalog !== undefined && configuredModelKey !== '' && configuredModel === undefined
  const globalModel = models.find(model =>
    model.provider === catalog?.default.provider && model.id === catalog.default.model)
  const effectiveModel = configuredModel ?? globalModel
  const defaultWorkspaceId = value?.defaultWorkspaceId ?? ''
  const workspaceId = value?.workspaceId ?? defaultWorkspaceId
  const workspace = workspaceSnapshot.items.find(item => item.workspaceId === workspaceId)
  const invalidWorkspace = workspaceSnapshot.phase === 'ready' && workspaceId !== '' && workspace === undefined
  const effectiveWorkspace = workspace
    ?? workspaceSnapshot.items.find(item => item.workspaceId === defaultWorkspaceId)
    ?? workspaceSnapshot.items[0]
  const efforts = effectiveModel?.reasoning?.efforts ?? []
  const configuredEffort = value?.reasoningEffort ?? ''
  const effortMissing = configuredEffort !== '' && !efforts.some(effort => effort.id === configuredEffort)
  const effectiveEffort = effortMissing ? effectiveModel?.reasoning?.defaultEffort ?? ''
    : configuredEffort || effectiveModel?.reasoning?.defaultEffort || ''

  const save = async (ops: readonly SettingsPathOpView[]): Promise<void> => {
    setSaveState('saving')
    try {
      await settings.mutate(ops)
      setSaveState('saved')
    } catch (_error) {
      setSaveState('error')
    }
  }
  const setWorkspace = (next: string): void => {
    if (!next) return
    void save([
      { op: 'set', path: ['workspaceId'], value: next },
      { op: 'unset', path: ['fallbackNotice'] },
    ])
  }
  const setModel = (next: string): void => {
    if (next === FOLLOW_GLOBAL_ID) {
      void save([
        { op: 'unset', path: ['provider'] },
        { op: 'unset', path: ['model'] },
        { op: 'unset', path: ['reasoningEffort'] },
        { op: 'unset', path: ['fallbackNotice'] },
      ])
      return
    }
    const selected = parseModelKey(next)
    if (selected === undefined) return
    void save([
      { op: 'set', path: ['provider'], value: selected.provider },
      { op: 'set', path: ['model'], value: selected.model },
      { op: 'unset', path: ['reasoningEffort'] },
      { op: 'unset', path: ['fallbackNotice'] },
    ])
  }
  const setEffort = (next: string): void => {
    void save([
      next === MODEL_DEFAULT_ID
        ? { op: 'unset', path: ['reasoningEffort'] }
        : { op: 'set', path: ['reasoningEffort'], value: next },
      { op: 'unset', path: ['fallbackNotice'] },
    ])
  }

  const workspaceItems = useMemo<readonly MenuEntry[]>(() => workspaceSnapshot.items.map(item => ({
    id: item.workspaceId,
    icon: <IconFolderCloseRegular size={16} />,
    label: <ChoiceContent primary={item.title} detail={item.path} />,
  })), [workspaceSnapshot.items])
  const workspaceValue = effectiveWorkspace
    ? <ChoiceContent primary={effectiveWorkspace.title} detail={effectiveWorkspace.path} />
    : <ChoiceContent primary={t('noWorkspace')} />
  const modelItems = useMemo<readonly MenuEntry[]>(() => catalog === undefined ? [] : [
    {
      id: FOLLOW_GLOBAL_ID,
      label: (
        <ChoiceContent
          primary={t('followGlobal')}
          detail={catalog.default.provider + '/' + catalog.default.model}
        />
      ),
    },
    { type: 'separator' as const, id: 'provider-separator' },
    ...catalog.groups.flatMap(group => [
      { type: 'label' as const, id: 'provider:' + group.id, text: group.name },
      ...group.models.map(model => ({
        id: modelKey(group.id, model.id),
        label: <ChoiceContent primary={model.name} detail={group.id + '/' + model.id} />,
      })),
    ]),
  ], [catalog, t])
  const modelSelectedId = configuredModel === undefined ? FOLLOW_GLOBAL_ID : configuredModelKey
  const modelValue = configuredModel
    ? <ChoiceContent primary={configuredModel.name} detail={configuredModel.providerName + '/' + configuredModel.id} />
    : catalog
      ? <ChoiceContent
        primary={globalModel?.name ?? catalog.default.model}
        detail={catalog.default.provider + '/' + catalog.default.model}
      />
      : <ChoiceContent primary={t('followGlobal')} />
  const effortItems = useMemo<readonly MenuEntry[]>(() => [
    { id: MODEL_DEFAULT_ID, label: t('modelDefault') },
    ...efforts.map(effort => ({ id: effort.id, label: effort.name })),
  ], [efforts, t])
  const effectiveEffortName = efforts.find(effort => effort.id === effectiveEffort)?.name
  const effortValue = effectiveEffortName
    ? <ChoiceContent primary={effectiveEffortName} detail={effectiveEffort} />
    : <ChoiceContent primary={t('modelDefault')} />
  const warningText = value?.fallbackNotice === 'workspace-unavailable' ? t('workspaceUnavailable')
    : value?.fallbackNotice === 'model-unavailable' ? t('modelUnavailable')
      : value?.fallbackNotice === 'reasoning-unavailable' ? t('reasoningUnavailable')
        : undefined

  return (
    <section
      aria-busy={preference.status === 'loading' || workspaceSnapshot.state === 'loading'}
      className={css.page}
    >
      <h2 className={css.heading}>Voice Terminal</h2>
      <p className={css.intro}>{t('intro')}</p>
      {preference.status === 'loading' || workspaceSnapshot.phase !== 'ready'
        ? <p role="status" className={css.status}>{t('loading')}</p>
        : null}
      {workspaceSnapshot.state === 'error' ? <p role="alert" className={css.warning}>{t('workspaceError')}</p> : null}
      {catalogFailed ? <p role="alert" className={css.warning}>{t('catalogError')}</p> : null}
      {warningText !== undefined ? <p role="status" className={css.warning}>{warningText}</p> : null}
      {invalidWorkspace ? <p role="status" className={css.warning}>{t('workspaceMissing')}</p> : null}
      {modelMissing ? <p role="status" className={css.warning}>{t('modelMissing')}</p> : null}
      {effortMissing ? <p role="status" className={css.warning}>{t('reasoningUnavailable')}</p> : null}

      <MenuField
        label={t('workspace')}
        description={t('workspaceHelp')}
        value={workspaceValue}
        items={workspaceItems}
        selectedId={effectiveWorkspace?.workspaceId}
        disabled={workspaceSnapshot.phase !== 'ready' || workspaceSnapshot.items.length === 0 || !preference.writable}
        onSelect={setWorkspace}
      />

      <MenuField
        label={t('model')}
        description={t('modelHelp')}
        value={modelValue}
        items={modelItems}
        selectedId={modelSelectedId}
        disabled={catalog === undefined || models.length === 0 || !preference.writable}
        onSelect={setModel}
      />

      <MenuField
        label={t('effort')}
        value={effortValue}
        items={effortItems}
        selectedId={effectiveEffort || MODEL_DEFAULT_ID}
        disabled={catalog === undefined || efforts.length === 0 || !preference.writable}
        onSelect={setEffort}
      />

      <p aria-live="polite" className={css.saveState}>
        {saveState === 'saving' ? t('saving') : saveState === 'saved' ? t('saved') : saveState === 'error' ? t('saveError') : ''}
      </p>
    </section>
  )
}
