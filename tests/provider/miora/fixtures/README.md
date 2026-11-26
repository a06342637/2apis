Miora 目录 fixture 来自已登录的普通账号只读请求，已去除账号与运行时数据。

- `public-catalog.json`：ACP 模型与网页 tiers 目录，收费倍率不代表 VIP 权限。
- `media-catalog.json`：2026-11-26 `/api/ai/public/cloud-agent/media-models` 的精选字段。同期 `/api/miora-billing/subscription` 返回 `tierCode=free`。9 个视频模型明确下发 `available=false`、`unavailableReason=tier_locked`、`requiredTier=standard`；MiniMax H3 Max、Vidu Q2 Pro 和当前图像模型明确可用。

这是该账号与该时间点的权限证据。实现以每个账号最新目录字段为准，不维护静态 VIP 模型名单，不把 `priceTier`、`billingFactor` 或模型名称转成套餐权限。
