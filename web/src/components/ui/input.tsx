import * as React from "react"
import { cn } from "cn"

function Input({ className, type, autoComplete = "off", name, ...props }: React.ComponentProps<"input">) {
  const id = React.useId();
  // 普通字段不冒充登录用户名；真正的登录控件显式保留其自动填充语义。
  const loginField = autoComplete === "username" || autoComplete === "current-password";
  const searchField = /搜索|查找|search/i.test(String(props["aria-label"] ?? props.placeholder ?? ""));
  return (
    <input
      type={type ?? (searchField ? "search" : "text")}
      name={name ?? (loginField ? (autoComplete === "username" ? "username" : "password") : `gateway-field-${id}`)}
      autoComplete={autoComplete}
      data-lpignore={loginField ? undefined : "true"}
      data-1p-ignore={loginField ? undefined : "true"}
      data-bwignore={loginField ? undefined : "true"}
      data-slot="input"
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className
      )}
      {...props}
    />
  )
}

export { Input }
