import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { cn } from "@/lib/utils";

type OpenMode = "closed" | "preview" | "pinned";
const CLOSE_DELAY_MS = 160;

/** 悬浮预览，点击或键盘确认后固定；整层限高并滚动，详情不会顶出视口。 */
export function ModelListPopup({ label, content, children, className }: {
  label: string;
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
    setMode("closed");
  };
  const preview = () => {
    cancelClose();
    if (modeRef.current === "closed") {
      restoreFocus.current = false;
      setMode("preview");
    }
  };
  const pin = () => {
    cancelClose();
    restoreFocus.current = true;
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
  useLayoutEffect(() => {
    if (mode === "pinned" && !contentRef.current?.contains(document.activeElement)) {
      contentRef.current?.focus({ preventScroll: true });
    }
  }, [mode]);

  return <Popover.Root open={mode !== "closed"} onOpenChange={(open) => open ? pin() : close()}>
    <Popover.Trigger asChild ref={triggerRef}
      onPointerEnter={(event) => { if (event.pointerType !== "touch") preview(); }}
      onPointerLeave={scheduleClose}
      onFocus={() => { if (!suppressFocusPreview.current) preview(); }}
      onBlur={(event) => {
        suppressFocusPreview.current = false;
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
      <Popover.Content ref={contentRef} aria-label={`${label} 模型清单`} tabIndex={-1}
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
        onCloseAutoFocus={(event) => { if (!restoreFocus.current) event.preventDefault(); }}>
        {content}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>;
}

export const ModelListPopupClose = Popover.Close;
