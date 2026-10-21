import { useCallback, useState } from "react";

/**
 * 表格多选：状态口径与半选态只有这一份实现。
 *
 * 这段逻辑原先在文档列表、全库分块、文档工作台里各写一遍（全选、半选、
 * 逐行切换），改一处不改另一处就会对"选了哪些、全选覆盖哪些"给出两个答案。
 *
 * 选择集合是**当前页可见行**的子集：翻页/换过滤后清空（由调用方在
 * 数据变化时 clear），否则会出现"已选 3 篇"而三篇都不在屏幕上的情况。
 */
export interface Picks {
  picked: ReadonlySet<string>;
  has: (key: string) => boolean;
  toggle: (key: string, on?: boolean) => void;
  setAll: (keys: string[], on: boolean) => void;
  clear: () => void;
  all: (keys: string[]) => boolean;
  some: (keys: string[]) => boolean;
  count: (keys: string[]) => number;
}

export function usePicks(): Picks {
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set());

  const has = useCallback((key: string) => picked.has(key), [picked]);

  const toggle = useCallback((key: string, on?: boolean) => {
    setPicked((prev) => {
      const next = new Set(prev);
      const want = on ?? !next.has(key);
      if (want) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  const setAll = useCallback((keys: string[], on: boolean) => {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const k of keys) {
        if (on) next.add(k);
        else next.delete(k);
      }
      return next;
    });
  }, []);

  const clear = useCallback(() => setPicked(new Set()), []);

  const all = useCallback(
    (keys: string[]) => keys.length > 0 && keys.every((k) => picked.has(k)),
    [picked],
  );
  const some = useCallback(
    (keys: string[]) => keys.some((k) => picked.has(k)) && !keys.every((k) => picked.has(k)),
    [picked],
  );
  const count = useCallback(
    (keys: string[]) => keys.filter((k) => picked.has(k)).length,
    [picked],
  );

  return { picked, has, toggle, setAll, clear, all, some, count };
}
