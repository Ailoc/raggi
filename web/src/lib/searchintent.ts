/**
 * 命令面板发起检索时的“意图交接”。
 *
 * 检索词不再写进 URL，因此面板不能靠地址把查询交给检索页：
 * - 目标页已挂载时用 `raggi:search` 事件即时送达；
 * - 目标页尚未挂载时先存这里，检索页初始化时取走。
 *
 * 两者只会命中一个；取走即清空，避免下次进入又被旧意图触发。
 */
let pending = "";

export function setPendingSearch(q: string): void {
  pending = q;
}

export function takePendingSearch(): string {
  const q = pending;
  pending = "";
  return q;
}