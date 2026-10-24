import Box from "@mui/material/Box";
import { useTheme } from "@mui/material/styles";
import type { SxProps, Theme } from "@mui/material/styles";
import { useId } from "react";
import type { ReactElement } from "react";

/**
 * 品牌字标：RAGGI，几何单线字形，内联 SVG。
 *
 * 为什么是矢量而不是图片：字标要在顶栏 17px（小屏）和 26px 两档显示，
 * 还要跟深浅两套主题走同一条造型线。位图要么锯齿、要么糊，要么得为每个
 * 尺寸/主题各出一份；内联 SVG 一次成型，缩放不失真，配色跟着主题切。
 *
 * 造型：等宽笔画（round cap + round join），cap height 28、笔画 4.8，
 * 全部落在 120×40 的画布里。斜笔（R 的腿、A 的右斜）走品牌渐变——
 * 这是老字标就有的识别点，深绿墨色配祖母绿渐变，和玻璃层的环境光同一色族。
 */
const BRAND = {
  light: { ink: "#12312A", accentFrom: "#0C7F63", accentTo: "#1BA183" },
  dark: { ink: "#B9F0E1", accentFrom: "#9CF2DC", accentTo: "#45CDA9" },
} as const;

export interface WordmarkProps {
  /** 尺寸/排版覆盖；默认按顶栏实际用法给一档。 */
  sx?: SxProps<Theme>;
}

export function Wordmark({ sx }: WordmarkProps): ReactElement {
  const mode = useTheme().palette.mode;
  const brand = BRAND[mode];
  // useId 的串带冒号，CSS url(#…) 引用不稳；去掉冒号再当渐变 id 用。
  const gid = `wm-grad-${useId().replace(/:/g, "")}`;

  return (
    <Box
      component="svg"
      viewBox="0 0 120 40"
      role="img"
      aria-label="Raggi"
      sx={{
        display: "block",
        height: 26,
        width: "auto",
        color: brand.ink,
        ...sx,
      }}
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={brand.accentFrom} />
          <stop offset="1" stopColor={brand.accentTo} />
        </linearGradient>
      </defs>
      <g
        fill="none"
        strokeWidth={4.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        stroke="currentColor"
      >
        {/* R */}
        <path d="M3 34 V6" />
        <path d="M3 6 H12.5 A8 8 0 0 1 12.5 22 H3" />
        {/* A */}
        <path d="M27 34 L38 6" />
        <path d="M31.3 23 H44.7" />
        {/* G */}
        <path d="M76.5 10 A13 13 0 1 0 82 20" />
        <path d="M82 20 H69" />
        <path d="M104.5 10 A13 13 0 1 0 110 20" />
        <path d="M110 20 H97" />
        {/* I */}
        <path d="M115 6 V34" />
      </g>
      {/* 斜笔走品牌渐变 */}
      <g
        fill="none"
        strokeWidth={4.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        stroke={`url(#${gid})`}
      >
        <path d="M12.5 22 L21.5 34" />
        <path d="M38 6 L49 34" />
      </g>
    </Box>
  );
}
