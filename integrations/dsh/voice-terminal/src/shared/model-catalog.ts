/** ModelCatalog（模型目录）：直接复用 DSH 正式类型与能力数据，不维护第二份等级定义。 */
import type { ModelCatalog } from '@deepseek-ai/dsh-api-session-controller/types'
export type { ModelCatalog }

/** isCatalogEffortCurrent（保存前校验）：只接受当前 DSH 全局模型仍提供的选项及模型默认值。 */
export function isCatalogEffortCurrent(
  catalog: ModelCatalog,
  expected: { readonly provider: string; readonly id: string } | undefined,
  effort: string | undefined,
): boolean {
  const provider = catalog.default.provider
  const modelId = catalog.default.model
  const model = catalog.groups.find(group => group.id === provider)?.models.find(item => item.id === modelId)
  return model !== undefined && provider === expected?.provider && modelId === expected.id
    && (effort === undefined || model.reasoning?.efforts.some(item => item.id === effort) === true)
}

/** CatalogSnapshot（模型目录读取状态）：能力数据始终由 DSH 正式接口提供。 */
export interface CatalogSnapshot<T> {
  /** catalog（当前宿主模型目录）；新连接确认前为空。 */
  readonly catalog: T | undefined
  /** loading（正在读取新目录），期间不提交旧菜单选项。 */
  readonly loading: boolean
  /** failed（最近一次读取失败）。 */
  readonly failed: boolean
}

/** CatalogInvalidations（目录失效通知）：reset 为真表示宿主连接代次已更换。 */
export type CatalogInvalidations = (listener: (reset: boolean) => void) => () => void

/** ModelCatalogObserver（设置页目录观察）：只管理读取生命周期，不构建模型能力或轮询宿主。 */
export class ModelCatalogObserver<T> {
  private generation = 0 // generation（读取代次）：过期回执不得覆盖新目录。
  private disposed = false // disposed（页面已退出）。
  private snapshot: CatalogSnapshot<T> = { catalog: undefined, loading: true, failed: false }
  private readonly unsubscribe: () => void // unsubscribe（释放正式事件订阅）。

  constructor(
    private readonly read: () => Promise<T>, // read（唯一正式目录查询）。
    subscribe: CatalogInvalidations,
    private readonly publish: (snapshot: CatalogSnapshot<T>) => void,
  ) {
    this.unsubscribe = subscribe(reset => { void this.refresh(reset) })
    void this.refresh()
  }

  /** refresh（读取最新目录）：空值表示失败、页面退出或回执过期，不允许据此保存。 */
  async refresh(reset = false): Promise<T | undefined> {
    if (this.disposed) return undefined
    const generation = ++this.generation
    this.update({ catalog: reset ? undefined : this.snapshot.catalog, loading: true, failed: false })
    try {
      const catalog = await this.read()
      if (this.disposed || generation !== this.generation) return undefined
      this.update({ catalog, loading: false, failed: false })
      return catalog
    } catch {
      if (!this.disposed && generation === this.generation) {
        this.update({ ...this.snapshot, loading: false, failed: true })
      }
      return undefined
    }
  }

  /** dispose（页面退出）：移除监听并使所有在途回执失效。 */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.generation++
    this.unsubscribe()
  }

  private update(snapshot: CatalogSnapshot<T>): void {
    this.snapshot = snapshot
    this.publish(snapshot)
  }
}
