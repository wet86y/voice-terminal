import { inject, provide, type InjectionKey } from "vue";
import type { useProductControls } from "./useProductControls";

/** ProductContext（产品交互上下文）：页面切换只替换呈现层，根层保留表单、会话与设备生命周期。 */
type ProductContext = ReturnType<typeof useProductControls>;
const key: InjectionKey<ProductContext> = Symbol("product-controls");
export function provideProductContext(controls: ProductContext) {
  provide(key, controls);
}
export function useProductContext(): ProductContext {
  const controls = inject(key);
  if (!controls) throw new Error("产品设置面板必须位于应用框架内。");
  return controls;
}
