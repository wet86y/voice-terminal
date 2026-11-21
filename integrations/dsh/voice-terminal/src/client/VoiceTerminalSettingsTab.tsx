import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
// DSH Remote API's SettingsPathOpView（设置接口提供的路径操作类型）.
import type { SettingsPathOpView } from '@deepseek-ai/dsh-api-remotes/client'
import type { ConfigForm } from '@deepseek-ai/dsh-client-ui-settings/client'
import type { IWorkspaces } from '@deepseek-ai/dsh-api-workspace-controller/client'
import { Button, IconChevronDownOutlineRegular, IconFolderCloseRegular, Menu } from '@deepseek-ai/dsh-client-ui-primitives'
import type { MenuEntry } from '@deepseek-ai/dsh-client-ui-primitives'
import type { PropsLocale, PropsRuntime, InjectFace } from '@deepseek-ai/dsh-client-ui-slots'
import type { VoiceTerminalSettings } from '../shared/settings-contract.js'
import { ModelCatalogObserver, isCatalogEffortCurrent, type ModelCatalog, type CatalogInvalidations, type CatalogSnapshot } from '../shared/model-catalog.js'
import css from './VoiceTerminalSettingsPage.module.css'

/** DSH services captured by this settings page（设置页面）. */
export interface VoiceTerminalSettingsInjected {
  /** Persistent plugin preferences（插件专属持久设置）. */
  readonly hooks: {
    /** Preferences（持久设置快照）：由官方 slots（界面插槽）生成订阅钩子。 */
    readonly preferences: ConfigForm<VoiceTerminalSettings>
    /** Global model（全局模型）：与 DSH 正式选择器共用一个设置条目。 */
    readonly globalModel: ConfigForm<ModelCatalog['default']>
    /** Workspaces（工作区快照）：由宿主同步。 */
    readonly workspaces: IWorkspaces['list']
  }
  /** mutateSettings（修改设置）：普通操作回调，不携带服务实例。 */
  readonly mutateSettings: (ops: readonly SettingsPathOpView[]) => Promise<boolean>
  /** mutateGlobalModel（修改宿主全局模型）：使用官方设置校验及版本保护。 */
  readonly mutateGlobalModel: (ops: readonly SettingsPathOpView[], expectedRevision: number | undefined) => Promise<boolean>
  /** Current routable DSH models（DSH 当前可用模型目录）. */
  readonly modelCatalog: () => Promise<ModelCatalog>
  /** subscribeModelCatalog（目录变更订阅）：使用公开宿主事件，随页面释放。 */
  readonly subscribeModelCatalog: CatalogInvalidations
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

type SaveState = 'idle' | 'saving' | 'saved' | 'error' | 'stale'

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
export function VoiceTerminalSettingsPage({ usePreferences, useGlobalModel, useWorkspaces, mutateSettings, mutateGlobalModel, modelCatalog, subscribeModelCatalog, t }: VoiceTerminalSettingsPageProps): ReactNode {
  const preference = usePreferences(value => value)
  const globalModelPreference = useGlobalModel(value => value)
  const workspaceSnapshot = useWorkspaces(value => value)
  const [catalogState, setCatalogState] = useState<CatalogSnapshot<ModelCatalog>>({ catalog: undefined, loading: true, failed: false })
  const catalogObserver = useRef<ModelCatalogObserver<ModelCatalog> | undefined>(undefined)
  const { catalog, loading: catalogLoading, failed: catalogFailed } = catalogState
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const value = preference.value

  useEffect(() => {
    const observer = new ModelCatalogObserver(modelCatalog, subscribeModelCatalog, setCatalogState)
    catalogObserver.current = observer
    return () => { catalogObserver.current = undefined; observer.dispose() }
  }, [modelCatalog, subscribeModelCatalog])

  const models = useMemo(() => catalog?.groups.flatMap(group =>
    group.models.map(model => ({ ...model, provider: group.id, providerName: group.name }))) ?? [], [catalog])
  const globalModel = models.find(model =>
    model.provider === catalog?.default.provider && model.id === catalog.default.model)
  const defaultWorkspaceId = value?.defaultWorkspaceId ?? ''
  const workspaceId = value?.workspaceId ?? defaultWorkspaceId
  const workspace = workspaceSnapshot.items.find(item => item.workspaceId === workspaceId)
  const invalidWorkspace = workspaceSnapshot.phase === 'ready' && workspaceId !== '' && workspace === undefined
  const effectiveWorkspace = workspace
    ?? workspaceSnapshot.items.find(item => item.workspaceId === defaultWorkspaceId)
    ?? workspaceSnapshot.items[0]
  const efforts = globalModel?.reasoning?.efforts ?? []
  const effectiveEffort = catalog?.default.reasoningEffort ?? globalModel?.reasoning?.defaultEffort ?? ''

  const save = async (ops: readonly SettingsPathOpView[], mutate = mutateSettings): Promise<void> => {
    setSaveState('saving')
    try {
      if (!await mutate(ops)) { setSaveState('stale'); return }
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
  const setModel = async (next: string): Promise<void> => {
    const selected = parseModelKey(next)
    if (selected === undefined) return
    setSaveState('saving')
    const latest = await catalogObserver.current?.refresh()
    if (!latest) { setSaveState('error'); return }
    const model = latest.groups.find(group => group.id === selected.provider)?.models.find(model => model.id === selected.model)
    if (!model) {
      setSaveState('stale'); return
    }
    await save([
      { op: 'set', path: ['provider'], value: selected.provider },
      { op: 'set', path: ['model'], value: selected.model },
      model.reasoning?.defaultEffort === undefined
        ? { op: 'unset', path: ['reasoningEffort'] }
        : { op: 'set', path: ['reasoningEffort'], value: model.reasoning.defaultEffort },
    ], ops => mutateGlobalModel(ops, globalModelPreference.revision))
  }
  const setEffort = async (next: string): Promise<void> => {
    setSaveState('saving')
    const latest = await catalogObserver.current?.refresh()
    if (!latest) { setSaveState('error'); return }
    if (!isCatalogEffortCurrent(latest, globalModel,
      next === MODEL_DEFAULT_ID ? undefined : next)) {
      setSaveState('stale'); return
    }
    await save([
      next === MODEL_DEFAULT_ID
        ? globalModel?.reasoning?.defaultEffort === undefined
          ? { op: 'unset', path: ['reasoningEffort'] }
          : { op: 'set', path: ['reasoningEffort'], value: globalModel.reasoning.defaultEffort }
        : { op: 'set', path: ['reasoningEffort'], value: next },
    ], ops => mutateGlobalModel(ops, globalModelPreference.revision))
  }

  const workspaceItems = useMemo<readonly MenuEntry[]>(() => workspaceSnapshot.items.map(item => ({
    id: item.workspaceId,
    icon: <IconFolderCloseRegular size={16} />,
    label: <ChoiceContent primary={item.title} detail={item.path} />,
  })), [workspaceSnapshot.items])
  const workspaceValue = effectiveWorkspace
    ? <ChoiceContent primary={effectiveWorkspace.title} detail={effectiveWorkspace.path} />
    : <ChoiceContent primary={t('noWorkspace')} />
  const modelItems = useMemo<readonly MenuEntry[]>(() => catalog?.groups.flatMap(group => [
      { type: 'label' as const, id: 'provider:' + group.id, text: group.name },
      ...group.models.map(model => ({
        id: modelKey(group.id, model.id),
        label: <ChoiceContent primary={model.name} detail={group.id + '/' + model.id} />,
      })),
    ]) ?? [], [catalog])
  const modelSelectedId = catalog ? modelKey(catalog.default.provider, catalog.default.model) : undefined
  const modelValue = catalog
      ? <ChoiceContent
        primary={globalModel?.name ?? catalog.default.model}
        detail={catalog.default.provider + '/' + catalog.default.model}
      />
      : <ChoiceContent primary={t('loading')} />
  const effortItems = useMemo<readonly MenuEntry[]>(() => [
    { id: MODEL_DEFAULT_ID, label: t('modelDefault') },
    ...efforts.map(effort => ({ id: effort.id, label: effort.name })),
  ], [efforts, t])
  const effectiveEffortName = efforts.find(effort => effort.id === effectiveEffort)?.name
  const effortValue = effectiveEffortName
    ? <ChoiceContent primary={effectiveEffortName} detail={effectiveEffort} />
    : <ChoiceContent primary={t('modelDefault')} />
  const warningText = value?.fallbackNotice === 'workspace-unavailable' ? t('workspaceUnavailable') : undefined

  return (
    <section
      aria-busy={preference.status === 'loading' || globalModelPreference.status === 'loading' || workspaceSnapshot.state === 'loading' || catalogLoading}
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
        disabled={catalog === undefined || catalogLoading || catalogFailed || saveState === 'saving' || models.length === 0 || globalModelPreference.status !== 'ready' || !globalModelPreference.writable}
        onSelect={setModel}
      />

      <MenuField
        label={t('effort')}
        description={t('effortHelp')}
        value={effortValue}
        items={effortItems}
        selectedId={effectiveEffort || MODEL_DEFAULT_ID}
        disabled={catalog === undefined || catalogLoading || catalogFailed || saveState === 'saving' || efforts.length === 0 || globalModelPreference.status !== 'ready' || !globalModelPreference.writable}
        onSelect={setEffort}
      />

      <p aria-live="polite" className={css.saveState}>
        {saveState === 'saving' ? t('saving') : saveState === 'saved' ? t('saved') : saveState === 'error' ? t('saveError') : saveState === 'stale' ? t('catalogChanged') : ''}
      </p>
    </section>
  )
}
