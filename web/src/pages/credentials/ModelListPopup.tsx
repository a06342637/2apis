import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { cn } from "@/lib/utils";

type OpenMode = "closed" | "preview" | "pinned";
const CLOSE_DELAY_MS = 160;
// 关闭浮层会使静止指针命中下方的其它账号触发器。所有实例共用这道门，
// 直到用户真的移动鼠标才重新允许悬浮；不能只保护刚关闭的那个实例。
let hoverBlockedByClose = false;
let mountedPopups = 0;
const resumeHoverOnPointerMove = (event: PointerEvent) => {
  if (event.pointerType !== "touch") hoverBlockedByClose = false;
};

/** 悬浮预览，点击或键盘确认后固定；整层限高并滚动，详情不会顶出视口。 */
export function ModelListPopup({ label, contentLabel, content, children, className }: {
  label: string;
  /** 其它账号明细复用交互与视口约束时，显式声明其内容语义。 */
  contentLabel?: string;
  content: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const [mode, setMode] = useState<OpenMode>("closed");
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const restoreFocus = useRef(false);
  const suppressFocusPreview = useRef(false);

  const cancelClose = () => {
    clearTimeout(closeTimer.current);
    closeTimer.current = undefined;
  };
  const close = () => {
    cancelClose();
    suppressFocusPreview.current = true;
    hoverBlockedByClose = true;
    modeRef.current = "closed";
    setMode("closed");
  };
  const preview = () => {
    cancelClose();
    if (modeRef.current === "closed") {
      restoreFocus.current = false;
      modeRef.current = "preview";
      setMode("preview");
    }
  };
  const pin = () => {
    cancelClose();
    restoreFocus.current = true;
    suppressFocusPreview.current = false;
    hoverBlockedByClose = false;
    modeRef.current = "pinned";
    setMode("pinned");
  };
  const scheduleClose = () => {
    cancelClose();
    if (modeRef.current !== "preview") return;
    closeTimer.current = setTimeout(() => {
      if (modeRef.current === "preview") close();
    }, CLOSE_DELAY_MS);
  };
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => {
    if (mountedPopups++ === 0) document.addEventListener("pointermove", resumeHoverOnPointerMove);
    return () => {
      if (--mountedPopups === 0) {
        document.removeEventListener("pointermove", resumeHoverOnPointerMove);
        hoverBlockedByClose = false;
      }
    };
  }, []);
  useLayoutEffect(() => {
    if (mode === "pinned" && !contentRef.current?.contains(document.activeElement)) {
      contentRef.current?.focus({ preventScroll: true });
    }
  }, [mode]);

  return <Popover.Root open={mode !== "closed"} onOpenChange={(open) => open ? pin() : close()}>
    <Popover.Trigger asChild ref={triggerRef}
      onPointerEnter={(event) => {
        if (event.pointerType !== "touch" && !hoverBlockedByClose) preview();
      }}
      onPointerMove={(event) => {
        // 弹层卸载只会改变鼠标命中的元素；新的真实鼠标移动才恢复悬浮预览。
        if (event.pointerType !== "touch" && hoverBlockedByClose) {
          hoverBlockedByClose = false;
          preview();
        }
      }}
      onPointerLeave={scheduleClose}
      onFocus={() => { if (!suppressFocusPreview.current) preview(); }}
      onBlur={(event) => {
        // 关闭后的卸载/焦点恢复可能产生 relatedTarget=null 的瞬时 blur，
        // 不能把它当成用户主动离开触发器并重新允许预览。
        if (modeRef.current === "closed" && event.relatedTarget
            && !contentRef.current?.contains(event.relatedTarget as Node)) {
          suppressFocusPreview.current = false;
        }
        if (!contentRef.current?.contains(event.relatedTarget as Node | null)) scheduleClose();
      }}
      onClick={(event) => {
        event.preventDefault();
        if (modeRef.current === "pinned") close();
        else pin();
      }}>
      {children}
    </Popover.Trigger>
    <Popover.Portal>
      <Popover.Content ref={contentRef} aria-label={contentLabel ?? `${label} 模型清单`} tabIndex={-1}
        data-slot="model-popup-content" data-surface="panel" data-open-mode={mode}
        side="bottom" align="end" sideOffset={6} collisionPadding={8}
        avoidCollisions sticky="always" hideWhenDetached updatePositionStrategy="always"
        style={{
          maxHeight: "min(calc(100dvh - 16px), var(--radix-popover-content-available-height, calc(100dvh - 16px)))",
          maxWidth: "min(calc(100vw - 16px), var(--radix-popover-content-available-width, calc(100vw - 16px)))",
        }}
        className={cn("z-50 overflow-y-auto overscroll-contain rounded-md bg-panel-raised text-xs text-foreground shadow-lg ring-1 ring-border whitespace-normal break-words text-left outline-none focus-visible:ring-2 focus-visible:ring-ring", className)}
        onPointerEnter={cancelClose} onPointerLeave={scheduleClose}
        onFocusCapture={() => { if (modeRef.current === "preview") pin(); }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          if (modeRef.current === "pinned") contentRef.current?.focus({ preventScroll: true });
        }}
        onCloseAutoFocus={(event) => {
          const active = document.activeElement;
          // 在 Radix 最后恢复焦点前重新设防；点击外部控件则保留外部焦点，
          // 下次用户主动聚焦触发器仍可预览。
          suppressFocusPreview.current = active === document.body || active === triggerRef.current
            || Boolean(active && contentRef.current?.contains(active));
          if (!restoreFocus.current) event.preventDefault();
        }}>
        {content}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>;
}

export const ModelListPopupClose = Popover.Close;
