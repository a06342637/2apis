import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as groupsApi from "../../api/credentialGroups";
import { api, ApiError } from "../../api/client";
import { BatchImportDialog, parseBatchText } from "../BatchImportDialog";

describe("parseBatchText", () => {
  it("Miora 多账号 JSONL 保留各账号独立的刷新凭证", () => {
    const { entries, errors } = parseBatchText([
      '{"provider":"miora","nickname":"account-1","credential":{"authToken":"a","refreshToken":"ra"}}',
      '{"provider":"miora","nickname":"account-2","credential":{"authToken":"b","refreshToken":"rb"}}',
    ].join("\n"));
    expect(errors).toEqual([]);
    expect(entries).toMatchObject([
      { provider: "miora", nickname: "account-1", credential: { authToken: "a", refreshToken: "ra" } },
      { provider: "miora", nickname: "account-2", credential: { authToken: "b", refreshToken: "rb" } },
    ]);
  });
  it("空文本不报错", () => {
    expect(parseBatchText("  \n ")).toEqual({ entries: [], errors: [] });
  });

  it("JSON 数组按元素序号校验", () => {
    const { entries, errors } = parseBatchText(JSON.stringify([
      { provider: "codebuddy", credential: { token: "a" }, nickname: "n", tags: ["x"] },
      { provider: "", credential: {} },
      { provider: "qoder", credential: "text" },
      "not-object",
    ]));
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ provider: "codebuddy", nickname: "n", tags: ["x"] });
    expect(errors.map((e) => e.line)).toEqual([2, 3, 4]);
  });

  it("JSONL 逐行解析，坏行只算自己", () => {
    const text = [
      '{"provider":"trae","credential":{"accessToken":"a"}}',
      "{broken",
      "",
      '{"provider":"zen","credential":{"session":"s"}}',
    ].join("\n");
    const { entries, errors } = parseBatchText(text);
    expect(entries.map((e) => e.provider)).toEqual(["trae", "zen"]);
    expect(errors).toEqual([{ line: 2, message: "JSON 解析失败" }]);
  });

  it("顶层不是数组 / JSON 坏了直接报错", () => {
    expect(parseBatchText('{"credential":{}}').errors[0].message).toBe("缺少 provider");
    expect(parseBatchText("[{").errors).toEqual([{ line: 1, message: "JSON 解析失败" }]);
  });
});

describe("BatchImportDialog", () => {
  beforeEach(() => vi.spyOn(groupsApi, "listCredentialGroups").mockResolvedValue({ groups: [], is_operator: true }));
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const entry = { provider: "codebuddy", credential: { token: "a" }, nickname: "", tags: [] };

  function renderDialog(overrides: Partial<Parameters<typeof BatchImportDialog>[0]> = {}) {
    const onClose = vi.fn();
    const onImported = vi.fn();
    const onNotice = vi.fn();
    render(
      <BatchImportDialog
        onClose={onClose}
        onImported={onImported}
        onNotice={onNotice}
        {...overrides}
      />,
    );
    return { onClose, onImported, onNotice };
  }

  it("提交成功：汇总并回调刷新", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "cred_1", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [],
      failed: [],
    });
    const { onImported } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(onImported).toHaveBeenCalledWith("批量导入完成：成功 1 条"));
    expect(screen.getByTestId("batch-result")).toHaveTextContent("成功 1 条");
    spy.mockRestore();
  });

  it("失败行回填成 JSONL，方便直接重试", async () => {
    vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [],
      skipped: [],
      failed: [{ index: 0, reason: "bad credential" }],
    });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("batch-text")).toHaveValue(JSON.stringify(entry)));
    expect(screen.getByTestId("batch-result")).toHaveTextContent("失败 1 条");
  });

  it("全部重复时提示且不报成功", async () => {
    vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [],
      skipped: [{ index: 0, reason: "duplicate" }],
      failed: [],
    });
    const { onImported, onNotice } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("全部为重复凭证（1 条），未新增"));
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.getByTestId("batch-result")).toHaveTextContent("跳过原因：账号或凭证已存在");
    expect(screen.queryByTestId("batch-preview")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-text")).toHaveValue("");
  });

  it("请求失败显示后端文案", async () => {
    vi.spyOn(api, "batchImportCredentials").mockRejectedValue(
      new ApiError(400, "invalid_request", "items must be a non-empty array"),
    );
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(screen.getByTestId("batch-error"))
      .toHaveTextContent("items must be a non-empty array"));
  });

  it("没有可导入条目时提交被禁用", () => {
    renderDialog();
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
  });

  it("解析错误提示行号", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: "{broken" } });
    expect(screen.getByTestId("batch-parse-errors")).toHaveTextContent("第 1 行");
  });

  it("关闭按钮回调", async () => {
    const { onClose } = renderDialog();
    await userEvent.click(screen.getByTestId("batch-cancel"));
    expect(onClose).toHaveBeenCalled();
  });

  it("混合粘贴分别预览邮箱并提交每个账号自己的刷新票", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "new", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [], failed: [],
    });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: [
      "one@example.com", JSON.stringify({ authToken: "synthetic-one", refreshToken: "r1" }, null, 2),
      "two@example.com Cookie: authToken=synthetic-two; refreshToken=r2",
      "synthetic-third-account",
    ].join("\r\n") } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    expect(spy).toHaveBeenCalledWith([
      expect.objectContaining({ provider: "miora", nickname: "one@example.com",
        credential: expect.objectContaining({ authToken: "synthetic-one", refreshToken: "r1" }) }),
      expect.objectContaining({ provider: "miora", nickname: "two@example.com",
        credential: expect.objectContaining({ authToken: "synthetic-two", refreshToken: "r2" }) }),
      expect.objectContaining({ provider: "miora", nickname: "",
        credential: expect.objectContaining({ authToken: "synthetic-third-account" }) }),
    ]);
  });

  it("邮箱紧贴Cookie或JSON作为备注，重复由服务端跳过且坏行保留原邮箱", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "new", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [{ index: 1, reason: "duplicate", duplicate_type: "batch" }], failed: [],
    });
    const { onImported } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: [
      "first@example.com{authToken=synthetic-account; refreshToken=account-refresh}",
      'duplicate@example.com{"authToken":"synthetic-account","refreshToken":"account-refresh"}',
      "bad@example.com{refreshToken=missing-own-auth}",
    ].join("\n") } });
    expect(screen.getByTestId("batch-count")).toHaveTextContent("识别 2 条，1 行有问题");
    await userEvent.click(screen.getByTestId("batch-submit"));
    expect(spy).toHaveBeenCalledExactlyOnceWith([
      expect.objectContaining({ provider: "miora", nickname: "first@example.com",
        credential: expect.objectContaining({ authToken: "synthetic-account", refreshToken: "account-refresh" }) }),
      expect.objectContaining({ provider: "miora", nickname: "duplicate@example.com",
        credential: expect.objectContaining({ authToken: "synthetic-account", refreshToken: "account-refresh" }) }),
    ]);
    await waitFor(() => expect(screen.getByTestId("batch-result")).toHaveTextContent("成功 1 条，跳过 1 条"));
    expect(onImported).toHaveBeenCalledWith("批量导入完成：成功 1 条");
    expect(screen.getByTestId("batch-result")).toHaveTextContent("跳过原因：本批次重复");
    const retry = (screen.getByTestId("batch-text") as HTMLTextAreaElement).value;
    expect(retry).toContain("bad@example.com");
    expect(retry).not.toContain("first@example.com");
    expect(parseBatchText(retry).entries).toEqual([]);
    expect(parseBatchText(retry).errors).toHaveLength(1);
  });

  it("服务器失败项和本地坏记录一起保留，成功项不会重新提交", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "new", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [], failed: [{ index: 1, reason: "synthetic upstream failure" }],
    });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: [
      "synthetic-success", "retry@example.com synthetic-retry-account", '{"refreshToken":"refresh-only"}',
    ].join("\n") } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    await waitFor(() => expect(screen.getByTestId("batch-text")).toHaveValue(
      JSON.stringify(spy.mock.calls[0][0][1]) + '\n{"refreshToken":"refresh-only"}',
    ));
    expect(screen.getByTestId("batch-count")).toHaveTextContent("识别 1 条，1 行有问题");
    spy.mockResolvedValue({ created: [], skipped: [{ index: 0, reason: "duplicate" }], failed: [] });
    await userEvent.click(screen.getByTestId("batch-submit"));
    expect(spy.mock.calls[1][0]).toEqual([spy.mock.calls[0][0][1]]);
  });

  it("可切换原始票据默认渠道，明确JSON渠道仍优先", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value:
      'synthetic-opaque\n{"authToken":"synthetic-miora"}' } });
    fireEvent.change(screen.getByTestId("batch-provider"), { target: { value: "codebuddy" } });
    expect(parseBatchText((screen.getByTestId("batch-text") as HTMLTextAreaElement).value,
      "codebuddy").entries.map((row) => row.provider)).toEqual(["codebuddy", "miora"]);
    expect(screen.getByTestId("batch-count")).toHaveTextContent("识别 2 条");
  });

  it("超过100条禁用提交，回到100条可提交", () => {
    renderDialog();
    const text = Array.from({ length: 101 }, (_, index) => "synthetic-account-" + index);
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: text.join("\n") } });
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
    expect(screen.getByText("一次最多导入 100 条，请分批粘贴。")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: text.slice(0, 100).join("\n") } });
    expect(screen.getByTestId("batch-submit")).not.toBeDisabled();
  });

  it("提交期间禁用编辑和文件入口，完成后恢复", async () => {
    let finish!: (result: { created: []; skipped: []; failed: [] }) => void;
    vi.spyOn(api, "batchImportCredentials").mockImplementation(() => new Promise((resolve) => {
      finish = resolve;
    }));
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: "synthetic-account" } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    for (const id of ["batch-text", "batch-provider", "batch-pick-file", "batch-cancel"]) {
      expect(screen.getByTestId(id)).toBeDisabled();
    }
    finish({ created: [], skipped: [], failed: [] });
    await waitFor(() => expect(screen.getByTestId("batch-text")).not.toBeDisabled());
  });

  it("读取文件期间不能提交旧文本，后选文件优先于先选文件的迟到结果", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({ created: [], skipped: [], failed: [] });
    let finishFirst!: (text: string) => void;
    let finishSecond!: (text: string) => void;
    const first = new File([""], "first.json");
    const second = new File([""], "second.json");
    Object.defineProperty(first, "text", { value: () => new Promise<string>((resolve) => { finishFirst = resolve; }) });
    Object.defineProperty(second, "text", { value: () => new Promise<string>((resolve) => { finishSecond = resolve; }) });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: JSON.stringify([entry]) } });
    const submit = screen.getByTestId("batch-submit");
    act(() => {
      fireEvent.change(screen.getByTestId("batch-file"), { target: { files: [first] } });
      submit.click();
    });
    expect(spy).not.toHaveBeenCalled();
    expect(submit).toBeDisabled();
    expect(submit).toHaveTextContent("读取文件中");
    expect(screen.getByTestId("batch-text")).toBeDisabled();
    fireEvent.change(screen.getByTestId("batch-file"), { target: { files: [second] } });
    const latest = { ...entry, nickname: "second", credential: { token: "second-token" } };
    await act(async () => { finishSecond(JSON.stringify([latest])); });
    expect(screen.getByTestId("batch-text")).toHaveValue(JSON.stringify([latest]));
    expect(submit).not.toBeDisabled();
    await act(async () => { finishFirst(JSON.stringify([entry])); });
    expect(screen.getByTestId("batch-text")).toHaveValue(JSON.stringify([latest]));
    await userEvent.click(submit);
    expect(spy).toHaveBeenCalledExactlyOnceWith([latest]);
  });

  it("过期文件读取失败不覆盖新文件，当前文件读取失败后可以继续编辑", async () => {
    let rejectFirst!: (error: Error) => void;
    const first = new File([""], "first.json");
    const second = new File([""], "second.json");
    Object.defineProperty(first, "text", { value: () => new Promise<string>((_resolve, reject) => { rejectFirst = reject; }) });
    Object.defineProperty(second, "text", { value: async () => JSON.stringify([entry]) });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-file"), { target: { files: [first] } });
    await act(async () => {
      fireEvent.change(screen.getByTestId("batch-file"), { target: { files: [second] } });
    });
    await act(async () => { rejectFirst(new Error("old read failed")); });
    expect(screen.queryByTestId("batch-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-text")).toHaveValue(JSON.stringify([entry]));
    const broken = new File([""], "broken.json");
    Object.defineProperty(broken, "text", { value: async () => { throw new Error("read failed"); } });
    await act(async () => { fireEvent.change(screen.getByTestId("batch-file"), { target: { files: [broken] } }); });
    expect(screen.getByTestId("batch-error")).toHaveTextContent("读取文件失败");
    expect(screen.getByTestId("batch-text")).not.toBeDisabled();
  });

  it("导入锁覆盖同一帧重复点击和 Escape 关闭，完成后恢复关闭", async () => {
    let finish!: (result: { created: []; skipped: []; failed: [] }) => void;
    const spy = vi.spyOn(api, "batchImportCredentials").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const { onClose } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: JSON.stringify([entry]) } });
    act(() => {
      const button = screen.getByTestId("batch-submit");
      button.click();
      button.click();
    });
    expect(spy).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByTestId("batch-import-dialog"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId("batch-file")).toBeDisabled();
    await act(async () => { finish({ created: [], skipped: [], failed: [] }); });
    fireEvent.keyDown(screen.getByTestId("batch-import-dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
