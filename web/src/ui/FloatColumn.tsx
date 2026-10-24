import { useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import Box from "@mui/material/Box";
import { alpha } from "@mui/material/styles";

/**
 * 工作列：内容滚动 + 底部浮动输入台。
 *
 * 检索态与对话态共用这一套几何，否则两处各写一遍就会走样（宽度、
 * 圆角缺口、滚动条补偿三件事很容易只改一处）。约定：
 *
 * - 结果列、结果标题与输入台共用同一列（900px 上限 + 两侧内边距），
 *   窄屏按同一内边距收缩，三者严格同宽同左边界；
 * - 页面底部**只有**一个居中的玻璃输入台，没有全宽底栏或渐变遮罩；
 * - 滚动区止于输入台上沿，并向下多伸出一个圆角半径：条目能进入顶部
 *   两个圆角缺口，绕开玻璃圆角消失，而不是被矩形裁切线提前切掉；
 * - 玻璃下方铺一层同半径的同色圆角衬底，玻璃覆盖区不透内容，圆角
 *   缺口外仍能看到条目。
 */

/** 工作列上限：结果列与输入台共用的最大宽度。 */
export const COLUMN_MAX = 900;

/**
 * 与 SearchComposer 的 COMPOSER_RADIUS 一致。滚动区向下多伸出这段，
 * 让条目能进入顶部两个圆角缺口；玻璃下方另铺同色圆角衬底。
 */
export const COMPOSER_RADIUS = 26;

/** 与输入台外层的内边距一致（xs=16px、md=32px）。 */
const COLUMN_INSET = { xs: 32, md: 64 } as const;

export interface FloatColumnProps {
  /** 滚动区里的内容。 */
  children: ReactNode;
  /** 浮在底部的输入台。 */
  footer: ReactNode;
  /** 滚动容器。父组件持有它，便于发送后复位滚动位置。 */
  scrollRef: RefObject<HTMLDivElement | null>;
  /** 内容列。观察它的高度变化，滚动条出现/消失才能被测到。 */
  contentRef?: RefObject<HTMLDivElement | null>;
  onScroll?: () => void;
  maxWidth?: number;
}

export default function FloatColumn({
  children,
  footer,
  scrollRef,
  contentRef,
  onScroll,
  maxWidth = COLUMN_MAX,
}: FloatColumnProps): React.ReactElement {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);
  // 滚动条占掉的宽度：内容列与输入台都要扣除同一份，才能在任意视口
  // 严格同宽（否则经典滚动条会让内容列凭空窄 15px）。
  const [scrollbarW, setScrollbarW] = useState(0);
  // 从工作台底边到输入台上沿的距离（输入台高度 + 底部外边距）。
  const [composerInset, setComposerInset] = useState(0);

  // 记录滚动条宽度（0 表示 overlay 滚动条/无滚动）。
  // 同时观察滚动区与内容列：内容从骨架切到卡片、或展开一条证据时，
  // 内容高度变化才让滚动条出现，仅观察滚动区本身不会触发尺寸回调。
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const sync = (): void => {
      const w = Math.max(0, el.offsetWidth - el.clientWidth);
      setScrollbarW((prev) => (prev === w ? prev : w));
    };
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    if (contentRef?.current) ro.observe(contentRef.current);
    return () => ro.disconnect();
  }, [scrollRef, contentRef]);

  // 输入台浮在底部，但不与内容重叠：滚动区在它的**上沿**截止，
  // 条目滑到这里就被裁掉，不会穿过玻璃。高度随多行输入/条件 chip
  // 变化；观察输入台与工作台两者：输入台变高、或顶栏高度变化导致
  // 工作台变矮，都要重新计算底边到上沿的距离。
  useLayoutEffect(() => {
    const el = composerRef.current;
    const root = rootRef.current;
    if (!el || !root) return;
    const sync = (): void => {
      const inset = Math.max(0, Math.ceil(
        root.getBoundingClientRect().bottom - el.getBoundingClientRect().top));
      setComposerInset((prev) => (prev === inset ? prev : inset));
    };
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    ro.observe(root);
    return () => ro.disconnect();
  }, []);

  return (
    <Box
      ref={rootRef}
      sx={{
        flex: "1 1 auto",
        minWidth: 0,
        minHeight: 0,
        position: "relative",
        display: "flex",
        flexDirection: "column",
        backgroundImage: (t) => `radial-gradient(1000px 360px at 50% -120px, `
          + `${alpha(t.palette.primary.main, 0.08)}, transparent 70%)`,
        backgroundRepeat: "no-repeat",
      }}
    >
      {/*
        滚动区止于输入台上沿，并向下多伸出一个圆角半径：中间部分被
        输入台的圆角衬底盖住，两个圆角缺口处则继续显示条目——条目是
        绕开玻璃圆角消失的，而不是被一个矩形连同圆角一起裁掉。
      */}
      <Box
        ref={scrollRef}
        onScroll={onScroll}
        sx={{
          height: `calc(100% - ${Math.max(0, composerInset - COMPOSER_RADIUS)}px)`,
          overflowY: "auto",
          // 键盘/scrollIntoView 定位时，为输入台和圆角缺口留出空间。
          scrollPaddingBottom: `${COMPOSER_RADIUS + 24}px`,
        }}
      >
        <Box
          ref={contentRef}
          sx={{
            // 与浮动输入台同宽：两侧内边距 + 900 上限。
            width: {
              xs: `calc(100% - ${COLUMN_INSET.xs}px)`,
              md: `calc(100% - ${COLUMN_INSET.md}px)`,
            },
            maxWidth,
            mx: "auto",
            pt: { xs: 2, md: 3 },
            // 滚动区向下多伸出一个圆角半径；这里再加回它，保证滚到
            // 底时最后一条停在输入台上方，而不是藏在圆角缺口里。
            pb: {
              xs: `${COMPOSER_RADIUS + 24}px`,
              md: `${COMPOSER_RADIUS + 32}px`,
            },
          }}
        >
          {children}
        </Box>
      </Box>

      {/*
        唯一的底部元素：居中的玻璃输入台本身。容器透明、只负责定位，
        不再铺整条背景；左右空白处 pointer-events: none，滚轮与点击
        直接落到下面的结果区。
      */}
      <Box
        sx={{
          position: "absolute",
          left: 0,
          right: `${scrollbarW}px`,
          bottom: 0,
          display: "flex",
          justifyContent: "center",
          pl: { xs: "16px", md: "32px" },
          pr: { xs: "16px", md: "32px" },
          pb: { xs: "calc(10px + env(safe-area-inset-bottom))", md: "12px" },
          pointerEvents: "none",
          zIndex: 2,
        }}
      >
        <Box
          ref={composerRef}
          sx={{
            position: "relative",
            width: "100%",
            maxWidth,
            pointerEvents: "auto",
            // 玻璃投影默认向上扩散约 40px；只保留左/右/下方的投影，
            // 顶部裁到 0，避免阴影压住条目。
            clipPath: "inset(0 -64px -64px -64px)",
          }}
        >
          {/*
            玻璃下方的同色圆角衬底：让滚动内容在玻璃覆盖区不可见，
            但在两个圆角缺口外继续可见（衬底的圆角与玻璃一致）。
          */}
          <Box
            aria-hidden
            sx={{
              position: "absolute",
              inset: 0,
              borderRadius: `${COMPOSER_RADIUS}px`,
              bgcolor: "background.default",
              pointerEvents: "none",
              zIndex: 0,
            }}
          />
          <Box sx={{ position: "relative", zIndex: 1 }}>{footer}</Box>
        </Box>
      </Box>
    </Box>
  );
}
