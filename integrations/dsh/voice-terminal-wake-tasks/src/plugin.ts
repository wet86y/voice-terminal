import z from '@deepseek-ai/schemastery'
import { apply as applyWakeTasks, inject as wakeTaskInject, name as wakeTaskName } from './index.js'

export const name = wakeTaskName
export const inject = wakeTaskInject
export const Config = z.object({})

/** Register project-level wake tools on each DSH root Agent. */
export function apply(ctx: any, _config: unknown): void {
  applyWakeTasks(ctx)
}
