import type { Credential } from "../../api/types";
import { formatDuration } from "../../api/display";
import { Badge } from "../../ui";
import { mioraAccountPlan, mioraPlanExpiryTime } from "./mioraAccountPlan";
import { ModelListPopup, ModelListPopupClose } from "./ModelListPopup";

/** 会员徽章附当前剩余时间，完整到期日期收进与额度包相同的明细浮层。 */
export function MioraPlanBadge({ credential, now }: { credential: Credential; now: number }) {
  const plan = mioraAccountPlan(credential, now);
  if (!plan) return null;
  const remaining = plan.remainingSeconds === null ? null
    : plan.remainingSeconds === 0 ? "已到期" : `剩余 ${formatDuration(plan.remainingSeconds)}`;
  return <span data-testid={`miora-plan-${credential.id}`} className="shrink-0" title={plan.description}>
    <ModelListPopup label="Miora" contentLabel="Miora 会员明细" className="w-96"
      content={<div data-testid={`miora-plan-details-${credential.id}`}
        className="space-y-2 p-3">
        <div className="flex items-center justify-between gap-3 border-b border-border pb-1.5 text-xs font-semibold">
          <span>Miora 会员明细</span>
          <ModelListPopupClose aria-label="关闭会员明细"
            className="rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            关闭
          </ModelListPopupClose>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-xs">
          <dt className="text-muted-foreground">当前套餐</dt><dd>{plan.label}</dd>
          <dt className="text-muted-foreground">到期时间</dt><dd className="tabular-nums">
            {plan.expiresAt === null ? plan.label === "免费" ? "免费套餐无会员到期时间" : "暂无已确认到期时间"
              : `${mioraPlanExpiryTime(plan.expiresAt)}（北京时间）`}
          </dd>
          {remaining && <><dt className="text-muted-foreground">会员剩余</dt><dd className={plan.expiring ? "text-warn-ink" : "tabular-nums"}>
            {remaining}{plan.expiring && " · 7 天内到期"}
          </dd></>}
        </dl>
        <p className="max-w-80 text-xs leading-relaxed text-muted-foreground">{plan.description}</p>
      </div>}>
      <button type="button" aria-label={`${credential.nickname || "Miora 账号"}会员明细`}
        data-testid={`miora-plan-trigger-${credential.id}`}
        className="inline-flex flex-col items-start gap-0.5 rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <Badge tone={plan.expiring ? "warn" : plan.tone}>{plan.label}</Badge>
        {remaining && <span data-testid={`miora-plan-remaining-${credential.id}`}
          className={`text-[11px] tabular-nums ${plan.expiring || plan.remainingSeconds === 0 ? "text-warn-ink" : "text-muted-foreground"}`}>
          {remaining}{plan.expiring && " · 临期"}
        </span>}
      </button>
    </ModelListPopup>
  </span>;
}
