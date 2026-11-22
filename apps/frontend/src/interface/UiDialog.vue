<script lang="ts">
// dialogStack（弹窗顺序）：未连接文档的渲染环境也只允许最上层处理键盘。
const dialogStack: symbol[] = [];
</script>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
const props = defineProps<{
  open: boolean; // open（是否打开）。
  title: string; // title（弹窗标题）。
  busy?: boolean; // busy（处理中）：阻止关闭，操作按钮由调用方按任务状态禁用。
  kicker?: string; // kicker（可选的标题上方说明）。
  wide?: boolean; // wide（宽弹窗）：用于编辑器与长内容。
  titleId?: string; // titleId（标题标识）：未指定时生成独立标识。
}>();
const emit = defineEmits<{ close: [] }>(); // close（请求关闭）：保留调用方的业务收尾。
const instanceId = useId(); // instanceId（组件实例标识）。
const headingId = computed(() => props.titleId ?? `dialog-title-${instanceId}`);
const modal = ref<HTMLDialogElement | null>(null); // modal（浏览器模态层）：隔离背景交互。
const panel = ref<HTMLElement | null>(null); // panel（内容容器）：初始阅读焦点。
const token = Symbol("dialog"); // token（弹窗顺序标识）。
let previous: HTMLElement | null = null; // previous（打开前的焦点）。
let nativeModal = false; // nativeModal（浏览器已接管模态交互）。
let generation = 0; // generation（开关代次）：防止迟到的聚焦。
let backdropPressed = false; // backdropPressed（按下起点位于遮罩）。
let backdropReleased = false; // backdropReleased（释放终点位于遮罩）。

function topmost() { return dialogStack.at(-1) === token; }
function requestClose() {
  if (props.open && !props.busy && topmost()) emit("close");
}
function beginBackdrop(event: PointerEvent) {
  backdropPressed = event.target === event.currentTarget;
  backdropReleased = false;
}
function endBackdrop(event: PointerEvent) { backdropReleased = event.target === event.currentTarget; }
function cancelBackdrop() { backdropPressed = false; backdropReleased = false; }
function clickBackdrop() {
  const close = backdropPressed && backdropReleased;
  cancelBackdrop();
  if (close) requestClose();
}
function key(event: KeyboardEvent) {
  // 原生模态层隔离背景并处理退出键；显式循环防止 Tab（制表键）离开末尾操作。
  if (event.defaultPrevented || !topmost() || !panel.value) return;
  if (event.key === "Escape") {
    if (!nativeModal) { event.preventDefault(); requestClose(); }
    return;
  }
  if (event.key !== "Tab") return;
  // 日期、时间等已打开的选择面板继续使用自身键盘导航。
  if (nativeModal && modal.value?.querySelector(":popover-open")) return;
  const focus = [...panel.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), a[href], summary, input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
  )].filter(item => item.tabIndex >= 0 && !item.closest('[hidden], [inert], [aria-hidden="true"]')
    && item.getAttribute("aria-disabled") !== "true"
    && getComputedStyle(item).display !== "none" && getComputedStyle(item).visibility !== "hidden"
    && (!nativeModal || item.getClientRects().length > 0));
  const index = focus.indexOf(document.activeElement as HTMLElement);
  if (!focus.length) { event.preventDefault(); panel.value.focus(); }
  else if (event.shiftKey && index <= 0) { event.preventDefault(); focus.at(-1)?.focus(); }
  else if (!event.shiftKey && (index === -1 || index === focus.length - 1)) {
    event.preventDefault(); focus[0]?.focus();
  }
}
function release() {
  const wasTopmost = topmost();
  const index = dialogStack.indexOf(token);
  if (index !== -1) dialogStack.splice(index, 1);
  document.removeEventListener("keydown", key);
  if (nativeModal) modal.value?.close();
  else modal.value?.removeAttribute("open");
  nativeModal = false;
  return wasTopmost;
}
function restoreFocus() {
  if (previous?.isConnected && !previous.matches(":disabled") && !previous.closest("[inert]")) previous.focus();
}
watch(() => props.open, async open => {
  const current = ++generation;
  if (open) {
    cancelBackdrop();
    previous = document.activeElement as HTMLElement;
    dialogStack.push(token);
    document.addEventListener("keydown", key);
    await nextTick();
    if (current !== generation || !props.open || !modal.value) return;
    if (modal.value.isConnected && typeof modal.value.showModal === "function") {
      modal.value.showModal();
      nativeModal = true;
    } else {
      // 未连接文档的组件渲染仍保留语义、焦点循环和可观察的打开状态。
      modal.value.setAttribute("open", "");
    }
    if (topmost()) panel.value?.focus();
  } else {
    const wasTopmost = release();
    await nextTick();
    if (current === generation && wasTopmost && !props.open) restoreFocus();
  }
}, { immediate: true });
onBeforeUnmount(() => {
  generation++;
  if (release()) restoreFocus();
});
</script>
<template>
  <dialog
    v-if="open"
    ref="modal"
    class="modal-backdrop"
    role="dialog"
    aria-modal="true"
    :aria-labelledby="headingId"
    :aria-busy="busy || undefined"
    @cancel.prevent="requestClose"
    @pointerdown="beginBackdrop"
    @pointerup="endBackdrop"
    @pointercancel="cancelBackdrop"
    @click.self="clickBackdrop"
  >
    <section ref="panel" class="apply-dialog" :class="{ 'apply-dialog-wide': wide }" tabindex="-1">
      <header class="dialog-header">
        <div class="dialog-heading">
          <p v-if="kicker" class="panel-kicker">{{ kicker }}</p>
          <h2 :id="headingId">{{ title }}</h2>
        </div>
        <div v-if="$slots['header-actions']" class="dialog-header-actions"><slot name="header-actions" /></div>
      </header>
      <div class="dialog-body"><slot /></div>
      <footer v-if="$slots.actions" class="dialog-actions"><slot name="actions" /></footer>
    </section>
  </dialog>
</template>
