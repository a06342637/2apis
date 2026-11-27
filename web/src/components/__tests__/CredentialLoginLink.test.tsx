import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import type { Credential, CredentialsResponse, Provider, UpstreamAuthPoll } from "../../api/types";
import { copyText } from "../../lib/clipboard";
import type { LoginLinkCancelRegistry } from "../../hooks/useLoginLink";
import { CredentialLoginLink } from "../CredentialLoginLink";
import { AddCredentialDialog } from "../AddCredentialDialog";

vi.mock("../../lib/clipboard", () => ({ copyText: vi.fn() }));
const listing = (credentials: Partial<Credential>[] = []): CredentialsResponse => ({
  credentials: credentials as Credential[], viewer: "root", is_admin: true,
  is_operator: true, tos_risks: {}, expiry_window_seconds: 3600,
  expiry_secondary_window_seconds: 86400, token_expiry_warning_seconds: 3600,
});
function setup(provider: Provider = "codebuddy") {
  const props = { provider, cancelRegistry: {} as LoginLinkCancelRegistry, disabled: false,
    onNotice: vi.fn(), onImported: vi.fn() };
  const view = render(<CredentialLoginLink {...props} />);
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const prefix = `login-link-${provider}`;
  return { ...view, props, user, prefix };
}
async function advance(time: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(time); });
}
const pending = () => ({ status: "pending" as const });

describe("CredentialLoginLink", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(api, "upstreamStart").mockResolvedValue({ flow: "poll", state: "first",
      auth_url: "https://login.example/?state=first", interval: 1 });
    vi.spyOn(api, "upstreamCancel").mockResolvedValue({ cancelled: true });
    vi.spyOn(api, "upstreamPoll").mockResolvedValue(pending());
    vi.spyOn(api, "upstreamComplete").mockResolvedValue({ status: "success", credential_id: "new" });
    vi.spyOn(api, "credentials").mockResolvedValue(listing());
    vi.mocked(copyText).mockResolvedValue(true);
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("获取国际版链接时显示计时和取消，取消后清理迟到state且可重新获取", async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.upstreamStart>>) => void;
    vi.mocked(api.upstreamStart).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { props, user, prefix } = setup("codebuddy_intl");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(3000);
    expect(screen.getByTestId(`${prefix}-refresh`)).toHaveTextContent("3 秒");
    expect(screen.getByTestId(`${prefix}-loading`)).toHaveTextContent("可随时取消");
    await user.click(screen.getByTestId(`${prefix}-cancel`));
    expect(screen.getByTestId(`${prefix}-refresh`)).toBeEnabled();
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("https://login.example/?state=first");
    await act(async () => finish({ flow: "poll", state: "late", auth_url: "https://login.example/late", interval: 1 }));
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy_intl", "late");
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("https://login.example/?state=first");
    expect(props.onImported).not.toHaveBeenCalled();
  });

  it("超过20秒结束获取中并允许重试，晚到state被取消且不能覆盖新计时器", async () => {
    let first!: (value: Awaited<ReturnType<typeof api.upstreamStart>>) => void;
    const finishes: typeof first[] = [];
    vi.mocked(api.upstreamStart).mockImplementation(() => new Promise(resolve => finishes.push(resolve)));
    const { user, prefix } = setup("codebuddy_intl");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    first = finishes[0];
    await advance(20001);
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("获取登录链接超时");
    expect(screen.getByTestId(`${prefix}-refresh`)).toBeEnabled();
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await act(async () => first({ flow: "poll", state: "old", auth_url: "https://login.example/old", interval: 1 }));
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy_intl", "old");
    await advance(20001);
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("获取登录链接超时");
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("");
  });

  it("授权服务网络失败显示中文可重试说明而非停在获取中", async () => {
    vi.mocked(api.upstreamStart).mockRejectedValue(new ApiError(502, "upstream_unavailable", "upstream transport failed"));
    const { user, prefix } = setup("codebuddy_intl");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("无法连接授权服务");
    expect(screen.getByTestId(`${prefix}-refresh`)).toBeEnabled();
    expect(screen.queryByTestId(`${prefix}-loading`)).not.toBeInTheDocument();
  });

  it("默认折叠，只获取/复制安全链接，不调用window.open", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { props, user, prefix } = setup();
    expect(screen.getByTestId(prefix)).not.toHaveAttribute("open");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("https://login.example/?state=first");
    await user.click(screen.getByTestId(`${prefix}-copy`));
    expect(copyText).toHaveBeenCalledWith("https://login.example/?state=first");
    expect(props.onNotice).toHaveBeenCalledWith("登录链接已复制。");
    expect(props.onImported).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
  });

  it.each(["codebuddy", "codebuddy_intl", "qoder", "officeace"] as Provider[])(
    "%s 自动轮询成功，局部与顶部绿色提示且停止定时器", async provider => {
      vi.mocked(api.upstreamPoll).mockResolvedValue({ status: "success", credential_id: "existing", merged: true });
      const { props, user, prefix } = setup(provider);
      await user.click(screen.getByTestId(`${prefix}-summary`));
      await user.click(screen.getByTestId(`${prefix}-refresh`));
      await advance(1000);
      expect(api.upstreamPoll).toHaveBeenCalledWith(provider, "first");
      expect(props.onNotice).toHaveBeenCalledWith("登录成功，已更新该账号的凭证。");
      expect(props.onImported).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId(`${prefix}-hint`)).toHaveClass("text-green-600");
      await advance(20_000);
      expect(api.upstreamPoll).toHaveBeenCalledTimes(1);
    },
  );

  it("CodeArts只能粘贴回调完成，不做轮询", async () => {
    vi.mocked(api.upstreamStart).mockResolvedValue({ flow: "paste", state: "local-state",
      auth_url: "https://codearts.example/authorize", interval: null });
    const { props, user, prefix } = setup("codearts");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(10_000);
    expect(api.upstreamPoll).not.toHaveBeenCalled();
    expect(screen.getByTestId(`${prefix}-complete`)).toBeDisabled();
    const callback = "http://127.0.0.1:12800/oauth/callback?code=sample&state=local-state";
    await user.type(screen.getByTestId(`${prefix}-callback`), callback);
    await user.click(screen.getByTestId(`${prefix}-complete`));
    expect(api.upstreamComplete).toHaveBeenCalledWith("codearts", "local-state", callback);
    expect(props.onImported).toHaveBeenCalledWith("登录成功，凭证已保存。");
  });

  it("启动或检测失败只更新局部红字", async () => {
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    vi.mocked(api.upstreamStart).mockRejectedValueOnce(new ApiError(502, "upstream_error", "上游不可用"));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("上游不可用");
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveClass("text-red-600");
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    vi.mocked(api.upstreamPoll).mockRejectedValueOnce(new ApiError(400, "upstream_error", "账号尚未开通"));
    await advance(1000);
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("账号尚未开通");
    expect(props.onNotice).not.toHaveBeenCalled();
    expect(props.onImported).not.toHaveBeenCalled();
  });

  it("拒绝脚本授权URL并取消已分配会话", async () => {
    vi.mocked(api.upstreamStart).mockResolvedValue({ flow: "poll", state: "unsafe",
      auth_url: "javascript:alert(1)", interval: 1 });
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy", "unsafe");
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("");
    expect(screen.getByTestId(`${prefix}-copy`)).toBeDisabled();
    expect(props.onNotice).not.toHaveBeenCalled();
  });

  it("复制失败只显示局部错误", async () => {
    vi.mocked(copyText).mockResolvedValue(false);
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await user.click(screen.getByTestId(`${prefix}-copy`));
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("手动选择链接复制");
    expect(props.onNotice).not.toHaveBeenCalled();
  });

  it("OfficeAce换账号刷新先取消旧state，后传forceLogin=true", async () => {
    const { user, prefix } = setup("officeace");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    vi.mocked(api.upstreamStart).mockResolvedValueOnce({ flow: "poll", state: "second",
      auth_url: "https://login.example/?state=second", interval: 1 });
    await user.click(screen.getByTestId(`${prefix}-switch`));
    expect(api.upstreamCancel).toHaveBeenCalledWith("officeace", "first");
    expect(api.upstreamStart).toHaveBeenLastCalledWith("officeace", true);
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("https://login.example/?state=second");
  });

  it("TRAE自动检测只看本渠道且当前发起人新凭证", async () => {
    vi.mocked(api.upstreamStart).mockResolvedValue({ flow: "callback", state: "trace",
      auth_url: "https://trae.example/authorize", interval: null });
    vi.mocked(api.credentials).mockResolvedValueOnce(listing([{ id: "old", provider: "trae", added_by: "root" }]))
      .mockResolvedValueOnce(listing([{ id: "miora", provider: "miora", added_by: "root" }]))
      .mockResolvedValueOnce(listing([{ id: "other", provider: "trae", added_by: "other-admin" }]))
      .mockResolvedValue(listing([{ id: "new", provider: "trae", added_by: "root" }]));
    const { props, user, prefix } = setup("trae");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(6000);
    expect(props.onImported).not.toHaveBeenCalled();
    await advance(3000);
    expect(props.onImported).toHaveBeenCalledWith("登录成功，凭证已保存。");
    expect(api.upstreamPoll).not.toHaveBeenCalled();
  });

  it("poll可留空回调，完成按钮先立即检测后继续自动检测", async () => {
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await user.click(screen.getByTestId(`${prefix}-complete`));
    expect(api.upstreamPoll).toHaveBeenCalledTimes(1);
    expect(api.upstreamComplete).not.toHaveBeenCalled();
    vi.mocked(api.upstreamPoll).mockResolvedValue({ status: "success" });
    await advance(1000);
    expect(props.onImported).toHaveBeenCalledTimes(1);
  });

  it("TRAE手动粘贴回调时先等在途检查结束，不并发消费登录state", async () => {
    vi.mocked(api.upstreamStart).mockResolvedValue({ flow: "callback", state: "trace",
      auth_url: "https://trae.example/authorize", interval: null });
    let resolveCheck!: (result: CredentialsResponse) => void;
    vi.mocked(api.credentials).mockResolvedValueOnce(listing()).mockImplementationOnce(
      () => new Promise(resolve => { resolveCheck = resolve; }));
    const { props, user, prefix } = setup("trae");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(3000);
    const callback = "/authorize?refreshToken=sample&loginTraceID=trace";
    await user.type(screen.getByTestId(`${prefix}-callback`), callback);
    await user.click(screen.getByTestId(`${prefix}-complete`));
    expect(api.upstreamComplete).not.toHaveBeenCalled();
    await act(async () => { resolveCheck(listing()); });
    expect(api.upstreamComplete).toHaveBeenCalledWith("trae", "trace", callback);
    expect(props.onImported).toHaveBeenCalledWith("登录成功，凭证已保存。");
    await advance(10_000);
    expect(api.credentials).toHaveBeenCalledTimes(2);
  });

  it("回调失败仅局部红字，可在同一会话中重试", async () => {
    vi.mocked(api.upstreamStart).mockResolvedValue({ flow: "paste", state: "local",
      auth_url: "https://codearts.example/authorize", interval: null });
    const { props, user, prefix } = setup("codearts");
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await user.type(screen.getByTestId(`${prefix}-callback`), "http://127.0.0.1/callback?code=a");
    vi.mocked(api.upstreamComplete).mockRejectedValueOnce(new ApiError(400, "invalid_request", "回调不匹配"));
    await user.click(screen.getByTestId(`${prefix}-complete`));
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("回调不匹配");
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveClass("text-red-600");
    expect(props.onNotice).not.toHaveBeenCalled();
    expect(props.onImported).not.toHaveBeenCalled();
    await user.click(screen.getByTestId(`${prefix}-complete`));
    expect(props.onImported).toHaveBeenCalledTimes(1);
  });

  it("取消后清空敏感回调，旧会话不再轮询", async () => {
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await user.type(screen.getByTestId(`${prefix}-callback`), "http://localhost/?code=sensitive");
    await user.click(screen.getByTestId(`${prefix}-cancel`));
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy", "first");
    expect(screen.getByTestId(`${prefix}-callback`)).toHaveValue("");
    expect(screen.getByTestId(`${prefix}-url`)).toHaveValue("");
    await advance(10_000);
    expect(api.upstreamPoll).not.toHaveBeenCalled();
    expect(props.onImported).not.toHaveBeenCalled();
  });

  it("卸载取消轮询，忽略晚到的成功结果", async () => {
    let finish!: (value: UpstreamAuthPoll) => void;
    vi.mocked(api.upstreamPoll).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { props, user, prefix, unmount } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(1000);
    unmount();
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy", "first");
    await act(async () => { finish({ status: "success" }); });
    await advance(10_000);
    expect(props.onImported).not.toHaveBeenCalled();
    expect(props.onNotice).not.toHaveBeenCalled();
    expect(api.upstreamPoll).toHaveBeenCalledTimes(1);
  });

  it("卸载时尚未返回的start结果也会取消其server state", async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.upstreamStart>>) => void;
    vi.mocked(api.upstreamStart).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { props, user, prefix, unmount } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    unmount();
    await act(async () => { finish({ flow: "poll", state: "late", auth_url: "https://login.example/", interval: 1 }); });
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy", "late");
    expect(props.onImported).not.toHaveBeenCalled();
  });

  it("慢poll不重叠，五分钟超时后停止自动检测", async () => {
    let finish!: (value: UpstreamAuthPoll) => void;
    vi.mocked(api.upstreamPoll).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { props, user, prefix } = setup();
    await user.click(screen.getByTestId(`${prefix}-summary`));
    await user.click(screen.getByTestId(`${prefix}-refresh`));
    await advance(10_000);
    expect(api.upstreamPoll).toHaveBeenCalledTimes(1);
    await act(async () => { finish({ status: "pending" }); });
    vi.setSystemTime(Date.now() + 301_000);
    await advance(1000);
    expect(screen.getByTestId(`${prefix}-hint`)).toHaveTextContent("授权超时");
    expect(props.onNotice).not.toHaveBeenCalled();
    expect(api.upstreamPoll).toHaveBeenCalledTimes(1);
  });

  it("保留顶部绿色按钮，切回弹窗前同步取消链接会话；手工渠道默认折叠", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    render(<AddCredentialDialog open onClose={vi.fn()} credentialCount={0} hasZen={false} hasKilo={false}
      onImported={vi.fn()} onNotice={vi.fn()} onError={vi.fn()} />);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    for (const id of ["manual-raccoon", "manual-lingxi", "manual-free"]) {
      expect(screen.getByTestId(id)).not.toHaveAttribute("open");
    }
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();
    await user.click(screen.getByTestId("login-link-codebuddy-summary"));
    await user.click(screen.getByTestId("login-link-codebuddy-refresh"));
    await user.click(screen.getByTestId("start-login-codebuddy"));
    expect(api.upstreamCancel).toHaveBeenCalledWith("codebuddy", "first");
    expect(api.upstreamStart).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("miora-import-help")).toHaveTextContent("refreshToken");
  });

  it("默认折叠时键盘焦点在可见summary和关闭按钮间循环", async () => {
    render(<AddCredentialDialog open onClose={vi.fn()} credentialCount={0} hasZen={false} hasKilo={false}
      onImported={vi.fn()} onNotice={vi.fn()} onError={vi.fn()} />);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const close = screen.getByTestId("add-credential-close");
    close.focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByTestId("manual-free-summary")).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(close).toHaveFocus();
  });
});
