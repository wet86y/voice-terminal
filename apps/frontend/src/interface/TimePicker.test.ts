// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { h, nextTick, ref } from "vue";
import TimePicker from "./TimePicker.vue";

const mounted: VueWrapper[] = [];
function renderTime(initial = "23:00") {
  const value = ref(initial); // value（父组件保存的时间值）。
  const disabled = ref(false); // disabled（表单保存期间的禁用状态）。
  const wrapper = mount({
    setup: () => () => h(TimePicker, {
      modelValue: value.value,
      label: "开始时间",
      disabled: disabled.value,
      "onUpdate:modelValue": (time: string) => { value.value = time; },
    }),
  });
  mounted.push(wrapper);
  return { wrapper, value, disabled };
}
afterEach(() => mounted.splice(0).forEach(wrapper => wrapper.unmount()));

describe("每日静默时间的输入和步进", () => {
  it("直接输入短时间格式，离开输入框后补齐为原有的时分格式", async () => {
    const { wrapper, value } = renderTime();
    const input = wrapper.get("input");
    await input.trigger("focus");
    await input.setValue("7:5");
    expect(value.value).toBe("23:00");
    await input.trigger("blur");
    expect(value.value).toBe("07:05");
    expect((input.element as HTMLInputElement).value).toBe("07:05");
  });

  it.each(["25:00", "23:60", "", "-1:00"])("无效输入 %s 不会写入静默设置", async text => {
    const { wrapper, value } = renderTime();
    const input = wrapper.get("input");
    await input.trigger("focus");
    await input.setValue(text);
    await input.trigger("blur");
    expect(value.value).toBe("23:00");
    expect((input.element as HTMLInputElement).value).toBe("23:00");
  });

  it("按分钟上下调整时可双向跨过午夜，并继续保留时分字符串", async () => {
    const { wrapper, value } = renderTime("23:59");
    const input = wrapper.get("input"), element = input.element as HTMLInputElement;
    await input.trigger("focus");
    element.setSelectionRange(3, 5);
    await input.trigger("keydown", { key: "ArrowUp" });
    expect(value.value).toBe("00:00");
    expect(element.value).toBe("00:00");
    await input.trigger("keydown", { key: "ArrowDown" });
    expect(value.value).toBe("23:59");
  });

  it("按小时调整保留分钟，外部设置更新会同步回输入框", async () => {
    const { wrapper, value } = renderTime("23:15");
    const input = wrapper.get("input"), element = input.element as HTMLInputElement;
    await input.trigger("focus");
    element.setSelectionRange(0, 2);
    await input.trigger("keydown", { key: "ArrowUp" });
    expect(value.value).toBe("00:15");
    await input.trigger("blur");
    value.value = "08:30";
    await nextTick();
    expect(element.value).toBe("08:30");
  });

  it("保存期间禁用输入框和时间选项，键盘不会改变值", async () => {
    const { wrapper, value, disabled } = renderTime();
    disabled.value = true;
    await nextTick();
    expect((wrapper.get("input").element as HTMLInputElement).disabled).toBe(true);
    expect((wrapper.get(".time-picker-trigger").element as HTMLButtonElement).disabled).toBe(true);
    await wrapper.get("input").trigger("keydown", { key: "ArrowUp" });
    expect(value.value).toBe("23:00");
  });
});
