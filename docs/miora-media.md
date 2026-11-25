# Miora 图片与视频 API

模型清单从当前账号的 Film Studio 媒体目录读取，按会员可用性过滤，包含基础型号与质量变体。
`GET /v1/models` 的 `gateway_endpoints` 表示真正可调用的接口，`output_modalities` 表示输出种类。
对话模型使用聊天接口，媒体模型使用下方接口；官网的 3D、音频、视频编辑和参考图输入尚未接出。
账号套餐、可选参数和模型清单变化以实时目录为准。调用产生上游媒体费用，未返回的信用点用量保持未知。

## 生图

```sh
curl https://your-domain/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: image-unique-request-1' \
  -d '{"model":"gem-3.1","prompt":"A small red circle on a plain white background","size":"1024x1024","n":1,"response_format":"url"}'
```

返回 `{"created":...,"data":[{"url":"..."}]}`。也支持 `response_format="b64_json"`。
支持 OpenAI Python SDK 的 `client.images.generate(...)`，型号须使用当前目录中的 Miora 媒体 ID。
可用 `aspect_ratio`、`resolution` 指定官网参数；`size` 会映射到比例和分辨率。
不支持的参数会明确拒绝，不偷偷更换模型。图片最多四张，总下载大小受 `MEDIA_MAX_BYTES` 限制。

## 视频

```sh
curl https://your-domain/v1/videos \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: video-unique-request-1' \
  -d '{"model":"vidu-q2-pro-video","prompt":"A red circle gently moves across a white background","seconds":"1","size":"1280x720"}'
```

返回 `video` 对象及 `id`，不会重复提交生成请求。用相同 Key 查询 `GET /v1/videos/{id}`，
完成后下载 `GET /v1/videos/{id}/content`。支持标准 SDK `videos.create` 的 multipart 文本字段，
拒绝文件/参考图输入。`seconds`、`size` 必须符合所选型号的实时参数。
任务保存在 `DATA_DIR/media`，重启后查询恢复同一任务，不重新生成。

## Codex、dsh 与 Anthropic 客户端

配置网关 URL、API Key 和一个 Miora **对话模型**。默认开启服务端图片/视频工具，
用户在普通对话中明确要求生成媒体时，模型可以选用 `miora_generate_image` / `miora_generate_video`。
网关执行生成并返回图片 Markdown 或视频下载链接。原有代码/文件函数仍交给客户端执行。
客户端也可以定义自己的函数，通过上面的标准接口调用媒体。

设置页「网关配置 → Miora 账号」可以关闭「Miora 对话自动生成媒体」，标准媒体接口继续可用。
`tool_choice="none"` 或强制客户端函数时不注入媒体工具。
Responses 请求可声明 `tools:[{"type":"image_generation"}]`；仅声明时由模型判断，
`tool_choice:{"type":"image_generation"}` 才强制生成。返回真实资源链接文本，
当前没有原生 `image_generation_call` 输出项、局部预览、透明背景或转码参数。

## 账号、限额与资源链接

创建/编辑 Key 可勾选账号，空列表表示全部账号。绑定后只使用所选账号和它们拥有的模型，
且仍叠加渠道绑定、Key/用户模型白名单和账号限额。无法生成时不扩大到其他账号。
请求等待本地节流后会复核权限，绑定/权限更新立即约束后续生成。

资源保存在服务器私有目录，链接以 APP_SECRET 派生 HMAC 签名并有有效期。
下载时还会复核 Key 是否存在、是否到期、用户是否启用及当前账号/模型权限。
签名链接本身是短期下载凭证，可以供第三方图片代理读取；Key 的来源 IP 白名单约束签发 API，
资源链接下载不再次限制来源 IP。全站网关暂停仍会停止资源下载。
暂停账号停止新生成，已生成资源在权限仍允许时可以下载。
可用 `Idempotency-Key` 防止同一 Key 重复提交同一请求；改变请求正文应使用新值。
结果未知或超时不会自动重放产生费用的生成 POST。

Miora 限速默认每账号起点间隔 5 秒、并发 1、每 60 秒窗口 3 次。
这是本地保守配置，上游仍可能返回 429，网关会保留 Retry-After 并冷却该账号。
各账号独立排队，绑定多个账号在集合内均衡使用。
