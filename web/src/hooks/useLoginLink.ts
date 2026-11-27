// [CUSTOM] 无弹窗链接登录：每次授权的计时器与server state均由本入口独立持有。
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Provider, UpstreamAuthStart } from "../api/types";
import { copyText } from "../lib/clipboard";
import { errorText } from "../lib/errors";
import { safeHttpUrl } from "../lib/safeUrl";
import { loginSuccessNotice, newLoginCredential } from "../lib/upstreamLogin";

export type LoginLinkCancelRegistry = Partial<Record<Provider, () => Promise<void>>>;
type Session = {
  state: string;
  flow: UpstreamAuthStart["flow"];
  before: Set<string>;
  viewer: string;
  interval: number;
  deadline: number;
  timer?: number;
  checking?: Promise<void>;
  waiting: boolean;
};
type Row = {
  authUrl: string;
  callback: string;
  state: string;
  flow: string;
  loading: boolean;
  completing: boolean;
  waiting: boolean;
  hint: string;
  tone: "ok" | "err" | "info";
};
const initial = (): Row => ({ authUrl: "", callback: "", state: "", flow: "",
  loading: false, completing: false, waiting: false, hint: "", tone: "info" });
export const LOGIN_LINK_TIMEOUT_MS = 20000;

export function useLoginLink(provider: Provider, registry: LoginLinkCancelRegistry,
  callbacks: { onNotice: (message: string) => void; onImported: (message: string) => void },
  disabled: boolean) {
  const [row, setRow] = useState<Row>(initial);
  const rowRef = useRef(row);
  rowRef.current = row;
  const mounted = useRef(false);
  const generation = useRef(0);
  const sessionRef = useRef<Session | null>(null);
  const callbackRef = useRef(callbacks);
  callbackRef.current = callbacks;
  const loadingTimer = useRef<number | null>(null);
  const loadingStarted = useRef(0);
  const [loadingSeconds, setLoadingSeconds] = useState(0);
  const clearLoadingTimer = () => {
    if (loadingTimer.current !== null) window.clearTimeout(loadingTimer.current);
    loadingTimer.current = null;
  };
  useEffect(() => {
    if (!row.loading) return;
    const timer = window.setInterval(() => setLoadingSeconds(
      Math.max(0, Math.floor((Date.now() - loadingStarted.current) / 1000))), 1000);
    return () => window.clearInterval(timer);
  }, [row.loading]);

  const patch = (value: Partial<Row>) => {
    if (mounted.current) {
      rowRef.current = { ...rowRef.current, ...value };
      setRow(rowRef.current);
    }
  };
  const clearTimer = (session: Session | null) => {
    if (session?.timer !== undefined) window.clearTimeout(session.timer);
  };
  const owned = (session: Session) => mounted.current && sessionRef.current === session;
  const cancel = async () => {
    clearLoadingTimer();
    const pending = sessionRef.current || rowRef.current.loading || rowRef.current.completing;
    generation.current += 1;
    const session = sessionRef.current;
    sessionRef.current = null;
    clearTimer(session);
    patch({ ...initial(), hint: pending ? "已取消链接登录。" : "" });
    if (session) await api.upstreamCancel(provider, session.state).catch(() => undefined);
  };

  useEffect(() => {
    mounted.current = true;
    registry[provider] = cancel;
    return () => {
      mounted.current = false;
      delete registry[provider];
      void cancel();
    };
  }, [provider, registry]);

  const succeeded = (session: Session, merged?: boolean) => {
    if (!owned(session)) return;
    generation.current += 1;
    clearTimer(session);
    sessionRef.current = null;
    const message = loginSuccessNotice(merged);
    patch({ state: "", callback: "", waiting: false, completing: false, hint: message, tone: "ok" });
    callbackRef.current.onNotice(message);
    callbackRef.current.onImported(message);
  };

  const schedule = (session: Session) => {
    if (owned(session) && session.waiting) {
      session.timer = window.setTimeout(() => void check(session), session.interval);
    }
  };
  const check = (session: Session): Promise<void> => {
    if (!owned(session)) return Promise.resolve();
    if (session.checking) return session.checking;
    if (Date.now() >= session.deadline) {
      session.waiting = false;
      patch({ waiting: false, hint: "授权超时，请刷新登录链接后重试。", tone: "err" });
      return Promise.resolve();
    }
    const work = (async () => {
      try {
        if (session.flow === "callback") {
          const result = await api.credentials();
          if (owned(session) && newLoginCredential(
            result.credentials, session.before, provider, session.viewer)) succeeded(session);
        } else {
          const result = await api.upstreamPoll(provider, session.state);
          if (owned(session) && result.status === "success") succeeded(session, result.merged);
        }
      } catch (caught) {
        if (owned(session)) {
          session.waiting = false;
          patch({ waiting: false, hint: errorText(caught, "授权状态检测失败，请刷新链接重试。"),
            tone: "err" });
        }
      } finally {
        session.checking = undefined;
        schedule(session);
      }
    })();
    session.checking = work;
    return work;
  };

  const refresh = async (forceLogin = false) => {
    if (disabled || rowRef.current.loading || rowRef.current.completing) return;
    const id = ++generation.current;
    const previous = sessionRef.current;
    sessionRef.current = null;
    clearTimer(previous);
    clearLoadingTimer();
    loadingStarted.current = Date.now();
    setLoadingSeconds(0);
    patch({ ...initial(), loading: true });
    const timer = window.setTimeout(() => {
      if (!mounted.current || generation.current !== id) return;
      generation.current += 1;
      loadingTimer.current = null;
      patch({ loading: false, hint: "获取登录链接超时，请重试；持续失败时请检查服务器到授权服务的网络连接。", tone: "err" });
    }, LOGIN_LINK_TIMEOUT_MS);
    loadingTimer.current = timer;
    try {
      if (previous) await api.upstreamCancel(provider, previous.state).catch(() => undefined);
      if (!mounted.current || generation.current !== id) return;
      const baseline = provider === "trae" ? await api.credentials() : null;
      if (!mounted.current || generation.current !== id) return;
      const started = await api.upstreamStart(provider, forceLogin);
      if (!mounted.current || generation.current !== id) {
        if (typeof started.state === "string" && started.state) {
          await api.upstreamCancel(provider, started.state).catch(() => undefined);
        }
        return;
      }
      const authUrl = safeHttpUrl(started.auth_url);
      if (!authUrl || typeof started.state !== "string" || !started.state
          || !["poll", "callback", "paste"].includes(started.flow)) {
        if (typeof started.state === "string" && started.state) {
          await api.upstreamCancel(provider, started.state).catch(() => undefined);
        }
        if (!mounted.current || generation.current !== id) return;
        patch({ loading: false, hint: "授权地址或登录会话无效，请刷新链接重试。", tone: "err" });
        return;
      }
      const session: Session = { state: started.state, flow: started.flow,
        before: new Set(baseline?.credentials.map(item => item.id) ?? []),
        viewer: baseline?.viewer ?? "", interval: started.flow === "callback" ? 3000
          : Math.max(1, Number.isFinite(started.interval) ? started.interval ?? 5 : 5) * 1000,
        deadline: Date.now() + 5 * 60 * 1000, waiting: started.flow !== "paste" };
      sessionRef.current = session;
      patch({ authUrl, state: session.state, flow: session.flow, loading: false,
        waiting: session.waiting, hint: session.flow === "paste"
          ? "授权后将地址栏中打不开的 127.0.0.1 回调链接粘贴到下方。"
          : "复制链接到浏览器授权，完成后自动检测；轮询渠道回调可以留空。" });
      schedule(session);
    } catch (caught) {
      if (mounted.current && generation.current === id) {
        const networkError = caught instanceof ApiError
          && ["upstream_unavailable", "egress_unavailable", "timeout"].includes(caught.code);
        patch({ loading: false, hint: networkError
          ? "无法连接授权服务，请重试；持续失败时请检查服务器的网络连接或授权服务是否可用。"
          : errorText(caught, "无法获取登录链接，请重试。"), tone: "err" });
      }
    } finally {
      // 旧请求完成时不能清掉新一轮获取链接的超时计时器。
      if (generation.current === id && loadingTimer.current === timer) clearLoadingTimer();
    }
  };

  const copy = async () => {
    const id = generation.current;
    const url = safeHttpUrl(rowRef.current.authUrl);
    if (!url) return;
    const success = await copyText(url);
    if (!mounted.current || generation.current !== id) return;
    const message = success ? "登录链接已复制。" : "复制失败，请手动选择链接复制。";
    patch({ hint: message, tone: success ? "ok" : "err" });
    if (success) callbackRef.current.onNotice(message);
  };

  const complete = async () => {
    const session = sessionRef.current;
    if (!session || disabled || rowRef.current.loading || rowRef.current.completing) return;
    const url = rowRef.current.callback.trim();
    if (!url && session.flow !== "poll") {
      patch({ hint: "请粘贴完整的授权回调链接。", tone: "err" });
      return;
    }
    session.waiting = false;
    clearTimer(session);
    patch({ completing: true, hint: "", waiting: false });
    try {
      await session.checking;
      if (!owned(session)) return;
      const result = url ? await api.upstreamComplete(provider, session.state, url)
        : await api.upstreamPoll(provider, session.state);
      if (!owned(session)) return;
      if (result.status === "success") succeeded(session, result.merged);
      else {
        session.waiting = true;
        patch({ completing: false, waiting: true, hint: "尚未完成授权，正在自动检测。", tone: "info" });
        schedule(session);
      }
    } catch (caught) {
      if (owned(session)) patch({ completing: false,
        hint: errorText(caught, "登录失败，请检查回调链接。"), tone: "err" });
    }
  };

  return { row, loadingSeconds, refresh, copy, complete, cancel,
    setCallback: (callback: string) => patch({ callback }) };
}
