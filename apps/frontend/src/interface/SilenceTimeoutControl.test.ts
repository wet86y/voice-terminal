// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { h, nextTick, ref } from "vue";
import SilenceTimeoutControl from "./SilenceTimeoutControl.vue";

const mounted: VueWrapper[] = [];
function renderDuration(initialSeconds: number) {
  const seconds = ref(initialSeconds); // seconds（父组件保存的整数秒数）。
  const wrapper = mount({
    setup: () => () => h(SilenceTimeoutControl, {
      modelValue: seconds.value,
      "onUpdate:modelValue": (value: number) => { seconds.value = value; },
    }),
  });
  mounted.push(wrapper);
  return { wrapper, seconds };
}
afterEach(() => mounted.splice(0).forEach(wrapper => wrapper.unmount()));

async function scrollControl(control: { element: Element }, deltaY: number) {
  // scrollControl（发送可取消的滚轮事件），同时验证阻止浏览器额外步进。
  const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true });
  control.element.dispatchEvent(event);
  await nextTick();
  return event;
}

describe("静默时限的秒与分钟交互", () => {
  it("前半段到 180 秒，后半段切换为分钟并可到 30 分钟", async () => {
    const { wrapper, seconds } = renderDuration(90);
    const slider = wrapper.get('input[type="range"]');
    const number = () => wrapper.get('input[type="number"]').element as HTMLInputElement;
    await slider.setValue(0);
    expect(seconds.value).toBe(5);
    expect(number().value).toBe("5");
    await slider.setValue(50);
    expect(seconds.value).toBe(180);
    expect(wrapper.get(".silence-timeout-number").text()).toBe("秒");
    await slider.setValue(50.01);
    expect(seconds.value).toBe(240);
    expect(number().value).toBe("4");
    expect(wrapper.get(".silence-timeout-number").text()).toBe("分钟");
    await slider.setValue(100);
    expect(seconds.value).toBe(1800);
    expect(number().value).toBe("30");
    expect(slider.attributes("aria-valuetext")).toBe("30 分钟");
  });

  it("手动输入分钟时保持单位，连续输入多位数字后滑条同步", async () => {
    const { wrapper, seconds } = renderDuration(1200);
    const number = wrapper.get('input[type="number"]');
    await number.trigger("focus");
    await number.setValue("1");
    expect(seconds.value).toBe(60);
    expect(wrapper.get(".silence-timeout-number").text()).toBe("分钟");
    expect((number.element as HTMLInputElement).value).toBe("1");
    await number.setValue("12");
    expect(seconds.value).toBe(720);
    expect(Number((wrapper.get('input[type="range"]').element as HTMLInputElement).value)).toBeCloseTo(66.6667, 3);
    await number.trigger("blur");
    expect((number.element as HTMLInputElement).value).toBe("12");
  });

  it("输入 3 分钟后回到秒数区间，清空输入后恢复当前值", async () => {
    const { wrapper, seconds } = renderDuration(600);
    const number = wrapper.get('input[type="number"]');
    await number.trigger("focus");
    await number.setValue("3");
    expect(seconds.value).toBe(180);
    await number.trigger("blur");
    expect(wrapper.get(".silence-timeout-number").text()).toBe("秒");
    expect((number.element as HTMLInputElement).value).toBe("180");
    await number.trigger("focus");
    await number.setValue("");
    expect(seconds.value).toBe(180);
    await number.trigger("blur");
    expect((number.element as HTMLInputElement).value).toBe("180");
  });

  it("超出范围的输入被限制为 5 秒到 30 分钟，始终保存整数秒", async () => {
    const { wrapper, seconds } = renderDuration(600);
    const number = wrapper.get('input[type="number"]');
    await number.trigger("focus");
    await number.setValue("35");
    expect(seconds.value).toBe(1800);
    await number.trigger("blur");
    expect((number.element as HTMLInputElement).value).toBe("30");
    await number.trigger("focus");
    await number.setValue("0");
    expect(seconds.value).toBe(5);
    await number.trigger("blur");
    expect((number.element as HTMLInputElement).value).toBe("5");
    expect(wrapper.get(".silence-timeout-number").text()).toBe("秒");
  });

  it.each(["number", "range"])("%s 控件的滚轮能双向跨过 180 秒，并同步数值和单位", async (type) => {
    const { wrapper, seconds } = renderDuration(179);
    const control = wrapper.get(`input[type="${type}"]`);
    const number = wrapper.get('input[type="number"]');
    await number.trigger("focus");
    for (const expected of [180, 240, 300]) {
      expect((await scrollControl(control, -100)).defaultPrevented).toBe(true);
      expect(seconds.value).toBe(expected);
      expect((number.element as HTMLInputElement).value).toBe(String(expected <= 180 ? expected : expected / 60));
      expect(wrapper.get(".silence-timeout-number").text()).toBe(expected <= 180 ? "秒" : "分钟");
    }
    for (const expected of [240, 180, 179]) {
      expect((await scrollControl(control, 100)).defaultPrevented).toBe(true);
      expect(seconds.value).toBe(expected);
      expect(wrapper.get('input[type="range"]').attributes("aria-valuetext")).toBe(`${expected <= 180 ? expected : expected / 60} ${expected <= 180 ? "秒" : "分钟"}`);
    }
  });

  it("分钟区按整分钟滚动，键盘与滚轮使用相同的步进", async () => {
    const { wrapper, seconds } = renderDuration(480);
    const number = wrapper.get('input[type="number"]');
    const slider = wrapper.get('input[type="range"]');
    await number.trigger("focus");
    expect(number.attributes("step")).toBe("1");
    await scrollControl(number, -1);
    expect(seconds.value).toBe(540);
    expect((number.element as HTMLInputElement).value).toBe("9");
    await number.trigger("keydown", { key: "ArrowDown" });
    expect(seconds.value).toBe(480);
    await slider.trigger("keydown", { key: "ArrowRight" });
    expect(seconds.value).toBe(540);
    await slider.trigger("keydown", { key: "ArrowLeft" });
    expect(seconds.value).toBe(480);
    await slider.trigger("keydown", { key: "ArrowUp" });
    expect(seconds.value).toBe(540);
    await number.trigger("keydown", { key: "ArrowDown" });
    expect(seconds.value).toBe(480);
  });

  it.each([5, 1800])("滚轮在 %s 秒边界停止，零位移滚轮不改变值", async (initial) => {
    const { wrapper, seconds } = renderDuration(initial);
    const number = wrapper.get('input[type="number"]');
    await scrollControl(number, initial === 5 ? 100 : -100);
    expect(seconds.value).toBe(initial);
    await scrollControl(number, 0);
    expect(seconds.value).toBe(initial);
  });

  it("编辑数值框时操作滑条，也会立即同步新的数值和单位", async () => {
    const { wrapper, seconds } = renderDuration(90);
    const number = wrapper.get('input[type="number"]');
    await number.trigger("focus");
    await wrapper.get('input[type="range"]').setValue(100);
    expect(seconds.value).toBe(1800);
    expect((number.element as HTMLInputElement).value).toBe("30");
    expect(wrapper.get(".silence-timeout-number").text()).toBe("分钟");
  });
});
