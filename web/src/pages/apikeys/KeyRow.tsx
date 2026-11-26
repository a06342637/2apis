import { Gauge, Pencil, Share2, Trash2 } from "lucide-react";
import { type ApiKeyWindow, USAGE_WINDOW_LABELS } from "../../api/apiKeys";
import { formatTime } from "../../api/display";
import {
  quotaOf,
  quotaSummary,
  usageLabel,
  usageOf,
} from "../../api/keyQuota";
import type { ApiKey, CredentialGroup } from "../../api/types";
import { Button, TableCell, TableRow } from "../../ui";
import { providerBindingLabel } from "./meta";
import { RevealKey } from "./RevealKey";

/**
 * 列表行：纯展示 + 行内动作回调。
 *
 * 行内状态（删除二次确认、正在编辑的配额/策略、导入对话框）仍由页面壳持有——
 * 这里只把「点了哪一行」传回去，不复制一份状态。
 * 验证后查看的密码/明文由 RevealKey 临时持有，不进入页面的查询缓存。
 */
export function KeyRow({
  item,
  accountGroups,
  window,
  confirming,
  onRemove,
  onCancelDelete,
  onEdit,
  onEditQuota,
  onImport,
  onConfirmDelete,
}: {
  item: ApiKey;
  accountGroups?: CredentialGroup[];
  /** 用量窗口（today / 7d / 30d）：仅影响「用量」列文案。 */
  window: ApiKeyWindow;
  confirming: boolean;
  onRemove: (id: string) => void;
  onCancelDelete: () => void;
  onEdit: (key: ApiKey) => void;
  onEditQuota: (key: ApiKey) => void;
  onImport: (key: ApiKey) => void;
  onConfirmDelete: (id: string) => void;
}) {
  return (
    <TableRow key={item.id} className="[&>td]:align-top [&>td]:whitespace-normal">
      <TableCell label="Key">
        <div className="min-w-0 space-y-1.5">
          <div className="break-words font-medium" data-testid={`key-name-${item.id}`}>
            {item.name || "未命名 Key"}
          </div>
          <div className="font-mono text-xs text-muted-foreground">
            <RevealKey key={item.id} id={item.id} preview={item.preview} canReveal={item.can_reveal} />
          </div>
        </div>
      </TableCell>
      <TableCell label="渠道与范围" className="text-xs">
        <div className="min-w-0 space-y-2">
          <span className="inline-flex rounded-md border border-border bg-muted/40 px-2 py-0.5 font-medium"
                data-testid={`key-binding-${item.id}`}>
            {providerBindingLabel(item.provider_binding)}
          </span>
          <div className="break-words leading-relaxed text-muted-foreground"
               data-testid={`key-credentials-${item.id}`}>
            {item.group_ids?.length ? `按 ${item.group_ids.length} 个分组：${item.group_ids.map((id) =>
              accountGroups?.find((group) => group.id === id)?.name ?? id).join("、")}`
              : item.credential_ids?.length ? `指定 ${item.credential_ids.length} 个账号` : "全部账号"}
          </div>
        </div>
      </TableCell>
      <TableCell label="权限与配额" className="text-xs">
        <dl className="grid min-w-0 grid-cols-[2rem_minmax(0,1fr)] gap-x-2 gap-y-1.5 leading-relaxed">
          <dt className="text-muted-foreground" title="来源 IP 白名单">IP</dt>
          <dd className="break-all font-mono" data-testid={`key-ips-${item.id}`}>
            {item.allowed_ips || "不限制"}
          </dd>
          <dt className="text-muted-foreground" title="模型白名单">模型</dt>
          <dd className="break-all font-mono" data-testid={`key-models-${item.id}`}>
            {item.allowed_models || "不限制"}
          </dd>
          <dt className="text-muted-foreground">配额</dt>
          <dd className="break-words" data-testid={`key-quota-${item.id}`}>
            {quotaSummary(quotaOf(item))}
          </dd>
        </dl>
      </TableCell>
      <TableCell label={`用量 · ${USAGE_WINDOW_LABELS[window]}`} className="text-xs">
        <div className="min-w-0 space-y-2">
          <div className="break-words font-medium leading-relaxed" data-testid={`key-usage-${item.id}`}>
            {usageLabel(usageOf(item), window)}
          </div>
          <dl className="grid grid-cols-[2rem_minmax(0,1fr)] gap-x-2 gap-y-1 leading-relaxed text-muted-foreground">
            <dt>到期</dt>
            <dd className="tabular-nums" data-testid={`key-expires-${item.id}`}>
              {item.expires_at ? formatTime(item.expires_at) : "永不过期"}
            </dd>
            <dt>使用</dt>
            <dd className="tabular-nums" data-testid={`key-last-used-${item.id}`}>
              {item.last_used_at ? formatTime(item.last_used_at) : "从未使用"}
            </dd>
            <dt>创建</dt>
            <dd className="tabular-nums" data-testid={`key-created-${item.id}`}>
              {formatTime(item.created_at)}
            </dd>
          </dl>
        </div>
      </TableCell>
      <TableCell label="操作" className="text-right">
        {confirming ? (
          <span className="inline-flex gap-1">
            <Button size="sm" variant="danger" onClick={() => onRemove(item.id)}>
              确认删除
            </Button>
            <Button size="sm" variant="ghost" onClick={onCancelDelete}>
              取消
            </Button>
          </span>
        ) : (
          <span className="inline-flex gap-0.5 rounded-lg border border-border/70 bg-muted/20 p-0.5"
                role="group" aria-label="Key 操作">
            <Button
              size="icon"
              variant="ghost"
              aria-label="编辑"
              title="编辑 Key"
              data-testid={`edit-key-${item.id}`}
              onClick={() => onEdit(item)}
            >
              <Pencil className="size-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              aria-label="配额"
              title="修改配额"
              data-testid={`edit-quota-${item.id}`}
              onClick={() => onEditQuota(item)}
            >
              <Gauge className="size-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              aria-label="导入 CC Switch"
              title="导入 CC Switch"
              data-testid={`import-ccswitch-${item.id}`}
              onClick={() => onImport(item)}
            >
              <Share2 className="size-3.5" />
            </Button>
            <Button size="icon" variant="ghost" aria-label="删除" title="删除 Key"
                    className="hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => onConfirmDelete(item.id)}>
              <Trash2 className="size-3.5" />
            </Button>
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}
