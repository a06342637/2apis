import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ModelInfo } from "../../api/types";
import { buildSections, displayName, modelValue } from "../../components/ModelPicker";
import { Button as RawButton } from "../../components/ui/button";
import { buildCcSwitchLink, CCSWITCH_TARGETS } from "../../lib/ccSwitch";
import { errorText } from "../../lib/errors";
import { isChatModel } from "../../lib/modelCapabilities";
import { Button, Field, Notice, Select } from "../../ui";

/**
 * 默认模型下拉：使用实际 Key 读取 `/v1/models`，按它的账号范围与白名单过滤，
 * 按渠道分组（多渠道置顶 + 「仅 XX」），值语义与 Playground 一致
 * （多渠道裸 id 走自动路由、单渠道 `id@provider` 强制指定）。
 */
function ModelSelect({
  value,
  onChange,
  testid,
  id,
  apiKey,
}: {
  value: string;
  onChange: (value: string) => void;
  testid: string;
  /** 供外层 `Field` 的 htmlFor 关联（读屏报字段名、点 label 聚焦控件）。 */
  id?: string;
  apiKey: string;
}) {
  // 明文 Key 仅留在弹窗本地 state 与请求头，不写入 React Query 缓存。
  const [result, setResult] = useState<{ key: string; data: ModelInfo[]; error?: string } | null>(null);
  useEffect(() => {
    setResult(null);
    if (!apiKey.trim()) return;
    const controller = new AbortController();
    // 粘贴/输入 Key 时只读取最终值，避免每敲一字就请求上游。
    const timer = setTimeout(() => {
      void api.keyModels(apiKey, controller.signal).then((response) => {
        if (!controller.signal.aborted) setResult({ key: apiKey, data: response.data ?? [] });
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setResult({ key: apiKey, data: [], error: errorText(error, "模型列表加载失败") });
        }
      });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [apiKey]);
  const current = result?.key === apiKey ? result : null;
  const fetched = (current?.data ?? []).filter(item => isChatModel(item));
  const models = fetched.map((item) => ({ ...item, value: modelValue(item) }));
  const sections = buildSections(models);

  return (
    <>
    <Select
      id={id}
      value={value}
      data-testid={testid}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">不写入（留空）</option>
      {sections.map((section) => (
        <optgroup key={section.key} label={section.label}>
          {section.items.map((item) => (
            <option key={item.value} value={item.value}>
              {displayName(item)}
            </option>
          ))}
        </optgroup>
      ))}
    </Select>
    {apiKey.trim() && !current && <p className="mt-1 text-xs text-muted-foreground">正在读取此 Key 可用模型…</p>}
    {current?.error && <Notice tone="warn">{current.error}；可以留空默认模型后导入。</Notice>}
    </>
  );
}

/**
 * CC Switch 导入按钮组：渲染在「明文 Key 存活」的上下文里——创建成功后的对话框，
 * 或列表行的导入对话框（用户自己粘贴 Key）。深链含 Key 明文，故只在用户主动
 * 打开、且已给出 Key 时生成。
 */
export function CcSwitchImportButtons({
  apiKey,
  testidPrefix = "ccswitch",
  warning = "深链含 API Key 明文，勿分享、勿在不可信环境使用。",
}: {
  apiKey: string;
  /** data-testid 前缀：列表行导入对话框用 `ccswitch-row`，避免与创建对话框撞名。 */
  testidPrefix?: string;
  warning?: string;
}) {
  const [selection, setSelection] = useState({ key: apiKey, model: "" });
  useEffect(() => {
    setSelection((previous) => previous.key === apiKey ? previous : { key: apiKey, model: "" });
  }, [apiKey]);
  const model = selection.key === apiKey ? selection.model : "";
  const origin = window.location.origin;
  const ready = apiKey.trim() !== "";

  return (
    <div className="space-y-3" data-testid={`${testidPrefix}-import`}>
      <Field
        label="默认模型（可选）"
        htmlFor={`${testidPrefix}-model-select`}
        hint="留空不写入。Claude Code 无模型名时回落内置列表；非原生模型建议选本服务模型。"
      >
        <ModelSelect
          value={model}
          onChange={(model) => setSelection({ key: apiKey, model })}
          apiKey={apiKey}
          testid={`${testidPrefix}-model`}
          id={`${testidPrefix}-model-select`}
        />
      </Field>
      <div className="flex flex-wrap gap-2">
        {CCSWITCH_TARGETS.map((target) =>
          ready ? (
            <RawButton key={target.app} size="sm" variant="default" asChild>
              <a
                href={buildCcSwitchLink(target, { origin, apiKey, model })}
                data-testid={`${testidPrefix}-${target.app}`}
              >
                导入到 {target.label}
              </a>
            </RawButton>
          ) : (
            <Button
              key={target.app}
              size="sm"
              variant="default"
              disabled
              data-testid={`${testidPrefix}-${target.app}`}
            >
              导入到 {target.label}
            </Button>
          ),
        )}
      </div>
      <Notice tone="warn">{warning}</Notice>
    </div>
  );
}
