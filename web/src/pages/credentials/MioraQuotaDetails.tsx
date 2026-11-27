import type { Credential } from "../../api/types";
import { formatDuration, formatNumber } from "../../api/display";
import { Button } from "../../ui";
import { mioraPlanExpiryTime } from "./mioraAccountPlan";
import { ModelListPopup, ModelListPopupClose } from "./ModelListPopup";

/** 每个积分包独立到期；会员日期属于会员明细，不作为积分到期日期。 */
export function MioraQuotaDetails({ credential, now }: { credential: Credential; now: number }) {
  const metadata = credential.quota_detail?.miora_credits;
  const detailsProvided = metadata?.packages_status === "ok" || Array.isArray(credential.quota_packages);
  const packages = (credential.quota_packages ?? []).filter(item => item.total - item.used > 0);
  const validEnd = (end: number | null): end is number => typeof end === "number" && end > 0
    && Number.isFinite(new Date(end * 1000).getTime());
  const summary = metadata?.next_expiring;
  const next = summary && summary.credit > 0 && Number.isFinite(summary.credit) && validEnd(summary.expires_at)
    ? summary : null;
  // 旧快照只保留最早积分日期时，不能用整账号余额冒充该批次的金额。
  const legacyEnd = !metadata && validEnd(credential.quota_cycle_end) ? credential.quota_cycle_end : null;
  const ordered = [...packages].sort((left, right) =>
    (validEnd(left.end) ? left.end : Infinity) - (validEnd(right.end) ? right.end : Infinity));
  return <ModelListPopup label="Miora" contentLabel="Miora 积分明细" className="w-[28rem]"
    content={<div data-testid={`packages-${credential.id}`} className="space-y-3 p-3">
      <div className="flex items-center justify-between gap-3 border-b border-border pb-2">
        <span className="text-xs font-semibold">Miora 积分明细</span>
        <ModelListPopupClose aria-label="关闭积分明细"
          className="rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          关闭
        </ModelListPopupClose>
      </div>
      <p className="text-xs text-muted-foreground">账号积分余额：
        <span className="tabular-nums text-foreground">{formatNumber(credential.quota_remaining)} / {formatNumber(credential.quota_total)}</span>
      </p>
      {ordered.length > 0 ? <ul className="divide-y divide-border/60">
        {ordered.map((item, index) => {
          const end = validEnd(item.end) ? item.end : null;
          const seconds = end !== null ? end - now : null;
          const expired = seconds !== null && seconds <= 0;
          const usable = item.usable !== false;
          const expiring = usable && seconds !== null && seconds > 0 && seconds <= 7 * 86400;
          const expiringCredits = expired || !usable ? 0 : item.expiring ?? (expiring ? item.total - item.used : 0);
          return <li key={`${item.name}-${item.end}-${index}`} data-testid={`package-${credential.id}`}
            className="space-y-1.5 py-2 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs">
              <span className="font-medium">{item.name || "积分包"}</span>
              <span className="tabular-nums">剩 {formatNumber(item.total - item.used)} / {formatNumber(item.total)}
                <span className="text-muted-foreground">（已用 {formatNumber(item.used)}）</span>
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              {end !== null ? `到期时间：${mioraPlanExpiryTime(end)}（北京时间）` : "上游未提供到期时间"}
            </p>
            {seconds !== null && <p className={`text-xs tabular-nums ${expired || expiring ? "text-warn-ink" : "text-muted-foreground"}`}>
              {expired ? "已到期" : `剩余 ${formatDuration(Math.ceil(seconds))}`}
            </p>}
            {expiringCredits > 0 && <p data-testid={`package-expiring-${credential.id}`} className="text-xs text-warn-ink">
              7 天内到期 {formatNumber(expiringCredits)} 积分
            </p>}
            {item.usable === false && !expired && <p className="text-xs text-warn-ink">暂不可用</p>}
          </li>;
        })}
      </ul> : detailsProvided ? <p className="text-xs text-muted-foreground">当前没有剩余额度的积分包。</p>
        : next ? <div data-testid={`packages-summary-${credential.id}`} className="space-y-2 text-xs">
          <p className="text-muted-foreground">仅获取到最早到期积分，完整积分包明细暂不可用。</p>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5">
            <dt className="text-muted-foreground">最早到期积分</dt><dd className="tabular-nums">{formatNumber(next.credit)} 积分</dd>
            <dt className="text-muted-foreground">到期时间</dt><dd>{mioraPlanExpiryTime(next.expires_at)}（北京时间）</dd>
          </dl>
          <p className="text-warn-ink tabular-nums">{next.expires_at <= now ? "已到期"
            : `剩余 ${formatDuration(Math.ceil(next.expires_at - now))}`}</p>
        </div> : legacyEnd !== null ? <div className="space-y-2 text-xs">
          <p>最早积分到期时间：{mioraPlanExpiryTime(legacyEnd)}（北京时间）</p>
          <p className="text-muted-foreground">上游未提供该批次到期金额及完整积分包明细。</p>
        </div> : <p className="text-xs text-muted-foreground">上游未提供积分包明细及积分到期时间。</p>}
      <p className="border-t border-border pt-2 text-xs leading-relaxed text-muted-foreground">
        试用、购买等积分按各自期限到期，调用时仍按账号实际可用余额扣减。
      </p>
    </div>}>
    <Button type="button" variant="link" aria-label={`${credential.nickname || "Miora 账号"}积分明细`}
      data-testid={`packages-toggle-${credential.id}`} className="h-auto px-0 text-left text-xs decoration-dotted underline-offset-2">
      {packages.length ? `积分明细 ${packages.length} 项` : detailsProvided ? "积分明细"
        : next || legacyEnd !== null ? "积分到期摘要" : "积分到期未提供"}
    </Button>
  </ModelListPopup>;
}
