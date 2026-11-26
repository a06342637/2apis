import type { ProviderModel } from "../../components/ModelPicker";
import { sortProviderModelsByAccess } from "../../lib/modelAccess";
import { MIORA_MODEL_GROUP_LABELS } from "../../lib/modelCapabilities";
import { Badge, Panel, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../ui";

/** 真实账号目录与刊例价目录并列，VIP 条目只读展示，不提供调用或探活按钮。 */
export function MioraAccessCatalog({ models }: { models: ProviderModel[] }) {
  if (!models.length) return null;
  const ordered = sortProviderModelsByAccess(models);
  const sections = [
    { id: "ordinary", label: "普通 / 常用模型", models: ordered.filter(model =>
      !model.access?.vip && model.access?.available !== false) },
    { id: "vip", label: "VIP 模型", models: ordered.filter(model => model.access?.vip) },
    { id: "unavailable", label: "暂不可用", models: ordered.filter(model =>
      !model.access?.vip && model.access?.available === false) },
  ];
  const usable = models.filter(model => model.access?.available === true).length;
  return <Panel title="Miora 账号权限目录" data-testid="miora-access-catalog">
    <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
      {usable} 个已确认可用 / {models.length} 个目录条目。VIP 模型需要有权限的账号；
      每个 API Key 仍按绑定账号的实际权限调用。
    </p>
    <Table cards containerClassName="max-h-80">
      <TableHeader><TableRow>
        <TableHead>模型</TableHead><TableHead>用途</TableHead><TableHead>账号权限</TableHead>
      </TableRow></TableHeader>
      <TableBody>
        {sections.filter(section => section.models.length).map(section => <TableRow key={section.id}>
          <TableCell colSpan={3} className="whitespace-normal" data-testid={`miora-catalog-${section.id}`}>
            <h3 className="mb-2 text-xs font-semibold">{section.label} · {section.models.length}</h3>
            <div className="space-y-2">
              {section.models.map(model => <div key={model.id} data-testid={`miora-access-${model.id}`}
                className="grid gap-1 rounded-md border border-border/60 px-3 py-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.4fr)] md:gap-3">
                <div className="min-w-0">
                  <div className="break-words text-xs font-medium">{model.label}</div>
                  <code className="break-all text-[11px] text-muted-foreground">{model.rawId ?? model.id}</code>
                </div>
                <div className="text-xs text-muted-foreground">
                  {model.gatewayGroup ? MIORA_MODEL_GROUP_LABELS[model.gatewayGroup].split(" · ")[0] : "目录模型"}
                </div>
                <div className="flex flex-wrap items-start gap-1.5 text-xs">
                  <Badge tone={model.access?.vip ? "warn" : model.access?.available === false ? "muted" : "ok"}>
                    {model.access?.label ?? "权限未声明"}
                  </Badge>
                  {model.access?.available === false && model.access.vip && <Badge tone="muted">当前不可用</Badge>}
                  <span className="min-w-0 flex-1 leading-relaxed text-muted-foreground">
                    {model.access?.description ?? "按上游账号目录判断可用性。"}
                  </span>
                </div>
              </div>)}
            </div>
          </TableCell>
        </TableRow>)}
      </TableBody>
    </Table>
  </Panel>;
}
