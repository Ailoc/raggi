import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentPropsWithoutRef, CSSProperties, ReactNode } from "react";
import Box from "@mui/material/Box";
import type { Theme } from "@mui/material/styles";
import type { SxProps } from "@mui/material/styles";
import { alpha, useTheme } from "@mui/material/styles";
import { Glass, GlassSystemProvider, resolveGlassLook } from "open-glass-ui";
import type { GlassLookInput, MaterialPresetName } from "open-glass-ui";

import { useStoreSelect } from "../lib/store";

import "open-glass-ui/styles.css";
import "./glass.css";

/**
 * 液态玻璃材质。
 *
 * 来自 [moekoelueker/open-glass-ui](https://github.com/moekoelueker/open-glass-ui)：
 * CSS 优先的液态玻璃系统，**零运行时依赖**，材质由 CSS 自定义属性
 * （`--ogui-glass-*`）驱动。
 *
 * 为什么不是 WebGPU 那套：liquid-glass 的折射靠 vgpu 渲染 WGSL 位移场，
 * 没有 WebGPU 就一点玻璃都画不出来（而且贴图就绪前玻璃面是
 * `visibility:hidden`，卡片会整个消失）。open-glass-ui 的默认渲染器是
 * **纯 CSS**（`backdrop-filter` + 高光边缘 + 边缘透镜），哪里都能跑，
 * Linux 没有 WebGPU 的机器也照样是玻璃。
 *
 * 材质本身（填充、模糊、高光边缘、边缘透镜、投影）是库算好的内联样式，
 * 本模块只负责三件事：
 *
 * 1. **主题对齐**：玻璃系统有自己的明暗两套 token，跟着应用主题走。
 * 2. **艺术指导**：逐个表面调 `look`（模糊、色调、边缘、光照角度……）。
 * 3. **背景**：玻璃底下得有东西——纯色页面折射不出任何质感。
 */

/**
 * 玻璃系统的主题边界。
 *
 * `renderer="auto"` 解析成纯 CSS，不会偷偷升级到 SVG/WebGL——库的原话
 * 是 "never silently escalates"。真折射（SVG 位移 / WebGL2）是可选的，
 * 但浏览器差异大，且这里都是文字密集的面板，CSS 已经够用。
 */
export function GlassProvider({ children }: { children: ReactNode }): React.ReactElement {
  const theme = useStoreSelect((s) => s.theme);
  const muiTheme = useTheme();
  // 卡片悬停描边用主题主色。必须在这里**下发**成 CSS 变量：样式表里
  // var(--lg-hover-ring, <兜底>) 一旦变量缺失就会用兜底色——深色下是一条
  // 几乎看不见的暗环。
  const vars = {
    "--lg-hover-ring": alpha(
      muiTheme.palette.primary.main,
      theme === "dark" ? 0.34 : 0.38,
    ),
  } as CSSProperties;
  return (
    <GlassSystemProvider
      renderer="auto"
      theme={{
        appearance: theme,
        // 玻璃系统自己的配色：强调色跟 MUI 主色对齐
        theme: { accent: muiTheme.palette.primary.main },
        // 主题边界会渲染一个 div；不接管布局，壳层的高度链不能因此多一层
        style: { height: "100%", ...vars },
      }}
    >
      {children}
    </GlassSystemProvider>
  );
}

export interface GlassPanelProps extends Omit<ComponentPropsWithoutRef<"div">, "color"> {
  children?: ReactNode;
  /**
   * 材质预设。`regular` 是默认（卡片、面板）；`frosted` 更实（浮层、
   * 盖在杂乱内容上的对话框）；`clear` 更透（图像之上的英雄面）。
   */
  material?: MaterialPresetName;
  /** 艺术指导：预设名（liquid/frosted/clear/smoked/lensed）或逐项系数。 */
  look?: GlassLookInput;
  /** 圆角（CSS 像素）。 */
  radius?: number;
  /** MUI sx 样式（布局、间距等）。 */
  sx?: SxProps<Theme>;
  /** 渲染成哪个元素（默认 div）。取值受 open-glass-ui 限制。 */
  as?: "aside" | "div" | "nav" | "section" | "span";
  /**
   * 关掉这块面的实时模糊（保留填充与高光边缘）。
   *
   * 模糊的成本 ∝ 面积 × 数量，而且底下内容一动就要重算一次。列表卡会
   * 随数据线性增长（证据、对话轮次、知识库），实测检索页 10 张卡时，
   * 去掉卡片模糊后帧间隔 p50 从 ~117ms 降到 ~50ms（本机无 GPU）。
   * 顶栏、输入台、对话框这类"少量且固定"的面继续保留模糊。
   */
  blur?: boolean;
  /**
   * 去掉材质的投影，只保留玻璃本身。
   *
   * 库的默认投影是 `0 16px 48px -8px` 的大范围暗影，堆叠的代码卡片
   * 会互相压到下一张上，看起来像连成一片。代码块、路径条这类"薄片"
   * 用平面玻璃更干净。
   */
  flat?: boolean;
}

/** 应用里的玻璃面种类。 */
export type GlassSurfaceKind = "card" | "composer" | "bar" | "dialog";

/**
 * 浅色下的亮度上限。
 *
 * 材质的浅色基准是 `brightness(1.12)`，而应用的白底是 `#fafafa`
 * （250/250/250）——1.12 乘上去三个通道全部越过 255 被**截断**，玻璃里
 * 透出来的背景变成纯白，卡片于是塌成一张白卡片（实测：卡片内部像素
 * (255,255,255)，而它背后的页面是 (194,210,224)）。必须压到 1 以下。
 *
 * 深色相反：材质把背景压到 0.56 亮度，玻璃里的字才读得出来；那里
 * 放行的是**上限之上**的余量（见 `glassLook`）。
 */
const LIGHT_BRIGHTNESS = 0.78;

/**
 * 艺术指导：按明暗两套给。
 *
 * 深色：材质把背景压到 0.56 亮度，玻璃里的字才读得出来；放行一部分
 * 亮度，底下的光才透得出颜色。
 *
 * 浅色：`brightness` 必须压到 1 以下（见 `LIGHT_BRIGHTNESS` 的说明），
 * 同时底色本身要够饱和——接近白的底色一被提亮就截断成纯白，玻璃里
 * 一点颜色都不剩。
 */
export function glassLook(
  kind: GlassSurfaceKind,
  mode: "dark" | "light",
): GlassLookInput {
  if (mode === "dark") {
    if (kind === "bar") {
      // 顶栏要"薄"：内容从下面滑过去时必须看得见糊掉的形状，
      // 填充太实就退化成一条不透明的色带。边缘也压柔——强 rim/lensing
      // 会在玻璃底边拉出一条高光线，正是顶栏"割裂感"的来源。
      return { blur: 1.9, opacity: 0.16, rim: 0.7, lensing: 0.1, brightness: 1.0 };
    }
    if (kind === "dialog") {
      // 对话框压在遮罩上，底下是被压暗的页面：比卡片更实，才读得清。
      return {
        blur: 1.2, opacity: 1.2, rim: 1.2, lensing: 1.1,
        lightAngle: 320, brightness: 1.25,
      };
    }
    return kind === "composer"
      ? {
        blur: 1.05, opacity: 1.1, rim: 1.25, lensing: 1.45,
        lightAngle: 320, brightness: 1.45,
      }
      : { blur: 0.85, opacity: 0.9, rim: 1.15, lensing: 1.2, brightness: 1.25 };
  }
  if (kind === "bar") {
    return { blur: 1.9, opacity: 0.1, rim: 0.6, lensing: 0.08, brightness: 0.9 };
  }
  if (kind === "dialog") {
    return {
      blur: 1.2, opacity: 1.05, rim: 1.1, lensing: 1.0,
      lightAngle: 320, brightness: LIGHT_BRIGHTNESS,
    };
  }
  return kind === "composer"
    ? {
      blur: 1.05, opacity: 0.85, rim: 1.15, lensing: 1.3,
      lightAngle: 320, brightness: LIGHT_BRIGHTNESS,
    }
    : {
      blur: 0.85, opacity: 0.85, rim: 1.1, lensing: 1.15,
      brightness: LIGHT_BRIGHTNESS,
    };
}

/** 按当前主题取艺术指导。 */
export function useGlassLook(kind: GlassSurfaceKind): GlassLookInput {
  // 只订阅 theme：每块玻璃都跟着 busy/kbs/crumbs 重渲染是一笔纯粹的浪费。
  const theme = useStoreSelect((s) => s.theme);
  return useMemo(() => glassLook(kind, theme), [kind, theme]);
}

/**
 * 输入台上的浮层（知识库多选、召回模式）共用的材质。
 *
 * 与输入台同源（regular + composer look），只比输入台多 15% 填充、
 * 10% 模糊：多行文字或选项列表比单行输入需要一点体量。边缘 rim/lensing
 * 保持与输入台一致，视觉上才是同一块玻璃，而不是白色浮层。
 *
 * `dense` 用于叠在条件面板里的二级浮层：底下已经有表单文字，需要再
 * 多一档填充，否则清单会和背后的字段糊在一起。
 */
export function usePopoverGlassLook(dense = false): GlassLookInput {
  const base = useGlassLook("composer");
  return useMemo(() => {
    const resolved = resolveGlassLook(base);
    return {
      ...resolved,
      opacity: (resolved.opacity ?? 1) + (dense ? 0.55 : 0.15),
      blur: (resolved.blur ?? 1) * (dense ? 1.35 : 1.1),
    };
  }, [base, dense]);
}

/**
 * 玻璃面板。
 *
 * 材质（填充、模糊、高光边缘、边缘透镜）是库算好的内联样式，必须由
 * `Glass` 自己渲染。把 `Glass` 交给 `<Box component={Glass}>` 或
 * `styled(Glass)` 都会让 emotion 把 `as` 吃掉，最终渲染成普通 div——
 * 玻璃材质整个消失（实测 DOM 里没有 `data-ogui-glass`）。
 *
 * `sx` 因此只作用于**内容层**（包一层 Box）：内边距、flex 排列这类
 * 都正确；需要让玻璃面本身定位的（sticky、gridColumn）请走 `style`。
 */
export const GlassPanel = forwardRef<HTMLElement, GlassPanelProps>(
  function GlassPanel({
    children,
    material = "regular",
    look,
    radius = 20,
    className,
    style,
    sx,
    as = "div",
    blur = true,
    flat = false,
    ...rest
  }, ref): React.ReactElement {
    return (
      <Glass
        ref={ref}
        as={as}
        material={material}
        look={look}
        className={[
          "lg-glass-panel",
          flat ? "lg-glass-flat" : "",
          blur ? "" : "lg-glass-flat-blur",
          className ?? "",
        ].filter(Boolean).join(" ")}
        style={{ borderRadius: radius, ...style }}
        {...rest}
      >
        {sx ? <Box sx={sx}>{children}</Box> : children}
      </Glass>
    );
  },
);

/**
 * MUI Dialog 的纸面：真正的液态玻璃，而不是一层 CSS 毛玻璃。
 *
 * 通过 `MuiDialog.defaultProps.PaperComponent` 全站生效。MUI 会把
 * `MuiDialog-paper` 的布局类名（margin / maxHeight / flex 列）挂到这块
 * 玻璃上，所以对话框的定位与滚动行为不变，只是材质换成了玻璃。
 */
export const GlassDialogPaper = forwardRef<
  HTMLElement,
  ComponentPropsWithoutRef<"div">
>(function GlassDialogPaper({ children, className, ...rest }, ref) {
  const look = useGlassLook("dialog");
  return (
    <GlassPanel
      ref={ref}
      flat
      material="frosted"
      radius={20}
      look={look}
      className={`lg-dialog-paper${className ? ` ${className}` : ""}`}
      {...rest}
    >
      {children}
    </GlassPanel>
  );
});

/**
 * 检索条件面板的纸面。
 *
 * 这是一个带滚动内容与固定底栏的大面板，材质沿用输入台/浮层的
 * regular + composer look（略厚一档），而不是对话框的 frosted：
 * frosted 在浅色下会把 73% 的白铺满整块面板，17 个字段看起来像一张
 * 白纸。定位、max-height 和动画仍由 MuiDialog-paper 提供。
 */
export const GlassFilterPaper = forwardRef<
  HTMLElement,
  ComponentPropsWithoutRef<"div">
>(function GlassFilterPaper({ children, className, ...rest }, ref) {
  const look = usePopoverGlassLook();
  return (
    <GlassPanel
      ref={ref}
      material="regular"
      radius={24}
      look={look}
      className={`lg-dialog-paper lg-filter-panel${className ? ` ${className}` : ""}`}
      {...rest}
    >
      {children}
    </GlassPanel>
  );
});

/**
 * 玻璃背后的极光背景。
 *
 * 玻璃要读得出"玻璃"，**底下必须有光**：
 * - 模糊一张近乎纯色的背景，得到的还是近乎纯色的面；
 * - 光斑必须比玻璃面**小**（几百像素级），这样一块玻璃横跨一道明显
 *   的明暗渐变，边缘的高光才有东西可折射。
 *
 * 两种渐变分开用，各有各的毛病：
 * - **径向**光斑能做出"一团光"，但光斑亮核一旦贴近容器边缘，就会被
 *   圆角切出一条硬边；
 * - **线性**渐变跨过整个容器，不会被切，但它只能做方向性的明暗。
 * 所以页面底色用「线性大wash + 几个小径向光斑」，而矮胖的输入台
 * （780×115）只用线性渐变——径向光斑在这么矮的盒子里必然被切。
 *
 * 两种形态：
 * - `shell`：整个应用共用的一片环境底色，**固定在视口最底层**。顶栏和
 *   内容区共用同一片光，顶栏的玻璃才不会和下面的页面断层。
 * - `panel`：一小块玻璃自己的局部光。
 */
export type BackdropVariant = "shell" | "panel";

export interface GlassBackdropProps {
  variant?: BackdropVariant;
  /**
   * 背景层的圆角，必须和玻璃面一致——背景层是玻璃面的**兄弟节点**
   * （不是子节点），玻璃面的圆角裁不到它，不给同样的圆角就会在四个角
   * 上漏出方形的色块。
   */
  radius?: number;
}

/**
 * 滚动期间暂停极光。
 *
 * 底下的光一直在动时，压在上面的每一块玻璃每帧都要重新模糊一次；
 * 光静止下来，合成器才能把模糊结果缓存住。停止滚动约 220ms 后恢复，
 * 用户看到的是"滚动时背景静下来"，而不是动画被打断。
 */
function useAuroraPause(enabled: boolean): boolean {
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const resume = (): void => {
      if (!pausedRef.current) return;
      pausedRef.current = false;
      setPaused(false);
    };
    // 滚动发生在各自的滚动容器里（main / 面板），必须用捕获阶段才收得到
    const onScroll = (): void => {
      if (!pausedRef.current) {
        pausedRef.current = true;
        setPaused(true);
      }
      clearTimeout(timer);
      timer = setTimeout(resume, 220);
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "hidden") {
        pausedRef.current = true;
        setPaused(true);
      } else {
        onScroll();
      }
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("scroll", onScroll, { capture: true });
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(timer);
    };
  }, [enabled]);
  return paused;
}

export function GlassBackdrop({
  variant = "shell",
  radius = 0,
}: GlassBackdropProps = {}): React.ReactElement {
  const paused = useAuroraPause(variant === "shell");
  /**
   * 动态光斑的配色：清新路线。
   *
   * 知识库是长时间阅读的界面，背景要"透气"而不是浓烈：薄荷、天蓝、
   * 水绿三团冷色 + 一团很淡的暖光，避免整屏偏紫发闷。深色模式换成
   * 深青底上的低饱和翡翠/湖蓝，亮度压住，不刺眼。
   *
   * 四团光各自以不同周期漂移（CSS `transform`，只走合成层），周期之间
   * 互质（28/34/41/37s），叠加后的花纹很久不会重复。
   */
  const blob = (
    lightColor: string,
    darkColor: string,
    lightAlpha: number,
    darkAlpha: number,
  ) => (t: Theme) => {
    const color = t.palette.mode === "dark" ? darkColor : lightColor;
    const a = t.palette.mode === "dark" ? darkAlpha : lightAlpha;
    // 中心保持一小块较实的色，边缘再散开：纯线性衰减的大光斑位移时
    // 像素变化太小，肉眼几乎看不出在动。
    return {
      backgroundImage: `radial-gradient(ellipse 58% 50% at 40% 45%, ${
        alpha(color, a)
      } 0%, ${alpha(color, a * 0.72)} 38%, transparent 72%)`,
    };
  };

  return (
    <Box
      aria-hidden
      className={`lg-aurora lg-aurora--${variant}${paused ? " lg-aurora--paused" : ""}`}
      sx={(t: Theme) => {
        const dark = t.palette.mode === "dark";
        const p = t.palette.primary.main;
        const s = t.palette.secondary.main;
        // 点阵：给折射一点可弯的细节
        const dots = {
          content: '""',
          position: "absolute",
          inset: 0,
          backgroundImage: "radial-gradient(rgba(255,255,255,.7) 1px, transparent 1px)",
          backgroundSize: "26px 26px",
          opacity: dark ? 0.04 : 0.07,
        };
        if (variant === "panel") {
          return {
            position: "absolute",
            inset: 0,
            overflow: "hidden",
            pointerEvents: "none",
            borderRadius: radius,
            // 两道方向相反的线性光：左上主光、右下次光。
            // 线性渐变跨过整个盒子，不会被圆角切出硬边。
            backgroundImage: [
              `linear-gradient(118deg, ${alpha(p, dark ? 0.85 : 0.5)} 0%, ${alpha(p, dark ? 0.26 : 0.18)} 42%, transparent 74%)`,
              `linear-gradient(242deg, ${alpha(s, dark ? 0.7 : 0.4)} 0%, ${alpha(s, dark ? 0.18 : 0.14)} 38%, transparent 66%)`,
            ].join(", "),
            "&::after": dots,
          };
        }
        return {
          // 固定在视口最底层：顶栏与内容区共用同一片光。
          // z-index 必须是 0 而不是 -1：body 有不透明底色，负层级的
          // 固定层会**被 body 的背景盖住**（body 的背景属于常规流内容，
          // 画在负层级之后）。0 + main 抬到 1 才是正确顺序。
          position: "fixed",
          inset: 0,
          zIndex: 0,
          overflow: "hidden",
          pointerEvents: "none",
          // 底色只留一层很轻的冷白 / 深青渐变。原本那些固定光斑全部
          // 撤掉——静态光斑会压过会动的光，背景看起来就"没动"。
          backgroundImage: dark
            ? "linear-gradient(158deg, #0d1b1e 0%, #0a1416 52%, #0b1714 100%)"
            : "linear-gradient(158deg, #f2fbf8 0%, #eef7fb 48%, #f7fbf3 100%)",
          "&::after": dots,
        };
      }}
    >
      {variant === "shell" && (
        <>
          <Box
            className="lg-aurora-blob lg-aurora-blob--a"
            sx={blob("#7edcc4", "#1e9e86", 0.82, 0.5)}
          />
          <Box
            className="lg-aurora-blob lg-aurora-blob--b"
            sx={blob("#8fcdf0", "#287fa6", 0.72, 0.44)}
          />
          <Box
            className="lg-aurora-blob lg-aurora-blob--c"
            sx={blob("#a8e6d4", "#2a8f68", 0.62, 0.38)}
          />
          <Box
            className="lg-aurora-blob lg-aurora-blob--d"
            sx={blob("#f6e3bd", "#8a6a3c", 0.42, 0.22)}
          />
        </>
      )}
    </Box>
  );
}
