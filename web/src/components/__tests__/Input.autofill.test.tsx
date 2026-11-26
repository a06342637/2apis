import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Input } from "../ui/input";
import { Textarea } from "../ui/textarea";
import { AddCredentialDialog } from "../AddCredentialDialog";

describe("普通输入不参与登录凭证自动填充", () => {
  it("普通字段禁用自动填充并拥有互不冲突的稳定字段名", () => {
    const { rerender } = render(<><Input aria-label="昵称" /><Input aria-label="模型" /></>);
    const first = screen.getByLabelText("昵称") as HTMLInputElement;
    const second = screen.getByLabelText("模型") as HTMLInputElement;
    const name = first.name;
    expect(name).toMatch(/^gateway-field-/);
    expect(first.name).not.toBe(second.name);
    for (const field of [first, second]) {
      expect(field).toHaveAttribute("autocomplete", "off");
      expect(field).toHaveAttribute("data-lpignore", "true");
      expect(field).toHaveAttribute("data-1p-ignore", "true");
      expect(field).toHaveAttribute("data-bwignore", "true");
    }
    rerender(<><Input aria-label="昵称" /><Input aria-label="模型" /></>);
    expect(first.name).toBe(name);
  });

  it("搜索使用搜索字段，用户仍能主动搜索 admin", async () => {
    function Search() {
      const [value, setValue] = useState("");
      return <Input aria-label="搜索凭证" value={value} onChange={event => setValue(event.target.value)} />;
    }
    render(<Search />);
    const search = screen.getByRole("searchbox", { name: "搜索凭证" });
    expect(search).toHaveAttribute("autocomplete", "off");
    await userEvent.type(search, "admin");
    expect(search).toHaveValue("admin");
  });

  it("明确的登录字段继续使用用户名和当前密码语义", () => {
    render(<><Input aria-label="登录用户名" autoComplete="username" />
      <Input aria-label="登录密码" type="password" autoComplete="current-password" /></>);
    expect(screen.getByLabelText("登录用户名")).toHaveAttribute("name", "username");
    expect(screen.getByLabelText("登录密码")).toHaveAttribute("name", "password");
    expect(screen.getByLabelText("登录密码")).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByLabelText("登录用户名")).not.toHaveAttribute("data-lpignore");
  });

  it("JSON 和配置文本也禁用自动填充，保留调用者的字段名", () => {
    render(<Textarea name="credential-json" aria-label="凭证 JSON" defaultValue='{"token":"test"}' />);
    const field = screen.getByLabelText("凭证 JSON");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field).toHaveAttribute("name", "credential-json");
    expect(field).toHaveAttribute("data-1p-ignore", "true");
    expect(field).toHaveValue('{"token":"test"}');
  });

  it("添加凭证的登录字段归属独立表单，不关联背景搜索框", () => {
    render(<><Input aria-label="搜索凭证" /><AddCredentialDialog open onClose={vi.fn()}
      credentialCount={0} hasZen={false} hasKilo={false} onImported={vi.fn()}
      onNotice={vi.fn()} onError={vi.fn()} /></>);
    const search = screen.getByLabelText("搜索凭证") as HTMLInputElement;
    const token = screen.getByTestId("zen-api-key") as HTMLInputElement;
    expect(search.form).toBeNull();
    expect(token.form).toHaveAttribute("name", "upstream-credential-entry");
    expect(token.form).toHaveAttribute("autocomplete", "off");
  });
});
