import { MIORA_OFFICIAL_MEDIA } from "../lib/modelCapabilities";

export function MioraOfficialMediaModels() {
  return (
    <details data-testid="miora-official-media" className="rounded border border-border bg-background/50 text-xs">
      <summary className="cursor-pointer px-2 py-1.5 font-medium text-foreground">
        官网其他创作入口（未接入 API）
      </summary>
      <div className="max-h-56 space-y-2 overflow-y-auto border-t border-border px-2 py-2 text-muted-foreground">
        <p>3D 官网入口参考 · 2026-11-25。图片、视频的可用 API 模型以账号实时目录为准。</p>
        {MIORA_OFFICIAL_MEDIA.map(category => (
          <section key={category.label}>
            <p className="font-medium text-foreground">{category.label}（官网）</p>
            <p>{category.models.join("；")}</p>
          </section>
        ))}
        <p>音频：当前 API 未接入。</p>
        <a href="https://miora.design/" target="_blank" rel="noopener noreferrer"
          className="underline underline-offset-2">查看 Miora 官网</a>
      </div>
    </details>
  );
}

/** 文本对话和媒体生成使用不同端点，生成模型不进入聊天选择器。 */
export function MioraCapabilities({ compact = false }: { compact?: boolean }) {
  return (
    <div data-testid="miora-api-capabilities" className={compact
      ? "space-y-1 text-[11px] leading-relaxed text-muted-foreground"
      : "space-y-1 text-xs leading-relaxed text-muted-foreground"}>
      <p className="font-medium text-foreground">Miora 当前接口用途</p>
      <p>文本问答、代码生成/分析、客户端工具调用（JSON模拟，由客户端执行）。</p>
      <p>生图接口：/v1/images/generations；按当前账号可用模型生成图片。</p>
      <p>视频接口：/v1/videos；提交任务后查询结果。</p>
      <p>3D、音频：官网独立创作入口，当前 API 未接入。</p>
      <p>聊天选择器只列文本模型。生成模型通过独立接口使用，当前支持文字描述生成。</p>
      {!compact && <MioraOfficialMediaModels />}
    </div>
  );
}
