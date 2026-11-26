import { providerLabel } from "../api/providers";
import type { BatchImportEntry, Provider } from "../api/types";

/**
 * 「JSON 导入」输入解析：把用户粘贴的东西归一成 `{provider, credential}`。
 *
 * 支持四种粘法，目的是省掉「先选渠道、再手拼 JSON」这一步：
 * 1. 渠道 JSON 对象（`{"token":"…"}`、`{"accessToken":…,"uid":…}` 等）；
 * 2. 整段 Cookie（`wps_sid=…; uid=…`，可带 `Cookie:` 前缀）；
 * 3. 裸 token（JWT / bearer）；
 * 4. DevTools 的「Copy as cURL」整段命令——里面带 `Cookie:` / `Authorization:`
 *    头，HttpOnly cookie 也能拿到，这是浏览器里唯一零安装的取凭据方式。
 *
 * 渠道识别只认**无歧义特征**：`wps_sid` → 灵犀、`xhx_token` 或
 * `iss=xiaohuanxiong` → 小浣熊、`authToken` / `iss=https://miora.design` → Miora、
 * `accessToken`/`refreshToken` → TRAE、
 * AK/SK → CodeArts、`model_app_key` → OfficeAce。CodeBuddy / Qoder 的
 * `token` / `access_token` 长得一样，故意不猜，沿用下拉选中的渠道——猜错
 * 会把凭证落到错池，直到一次 401 才暴露。
 */

/** 手工导入下拉里的渠道；zen / kilo 无凭证，不在其中。 */
export const IMPORT_PROVIDERS: Provider[] = [
  "codebuddy",
  "codebuddy_intl",
  "trae",
  "qoder",
  "codearts",
  "officeace",
  "raccoon",
  "lingxi",
  "miora",
];

export type ParsedCredential =
  | {
      ok: true;
      /** 实际用于导入的渠道（识别优先，其次下拉选中）。 */
      provider: Provider;
      credential: Record<string, unknown>;
      /** 识别来源，供界面提示用。 */
      detected: Provider | null;
    }
  | { ok: false; error: string };

/** JWT payload；解不开返回 null（不验签，只读元数据）。 */
function jwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const text = new TextDecoder().decode(bytes);
    const claims: unknown = JSON.parse(text);
    return typeof claims === "object" && claims !== null
      ? (claims as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** 从 JWT 的 `iss` 认已知网页渠道；只读元数据，不替代服务端验证。 */
function providerFromToken(token: string): Provider | null {
  const claims = jwtClaims(token.trim());
  const issuer = claims?.iss;
  if (typeof issuer === "string" && issuer.toLowerCase().includes("xiaohuanxiong")) {
    return "raccoon";
  }
  if (issuer === "https://miora.design") return "miora";
  return null;
}

/** Cookie 头里的特征键；只认能唯一定位渠道的。 */
function providerFromCookie(cookie: string): Provider | null {
  const pairs = cookie
    .split(";")
    .map((part) => part.split("=")[0]?.trim().toLowerCase())
    .filter(Boolean);
  if (pairs.includes("wps_sid")) return "lingxi";
  if (pairs.includes("xhx_token")) return "raccoon";
  if (pairs.includes("authtoken")) return "miora";
  return null;
}

function providerFromObject(value: Record<string, unknown>): Provider | null {
  const has = (key: string) => Object.prototype.hasOwnProperty.call(value, key);
  // Miora 同样用 refreshToken，必须先认 authToken，避免误入 TRAE 池。
  if (has("authToken")) return "miora";
  const token = [value.access_token, value.accessToken, value.token]
    .find((candidate) => typeof candidate === "string" && candidate.trim().length > 0);
  if (typeof token === "string" && providerFromToken(token) === "miora") return "miora";
  if (has("accessToken") || has("refreshToken")) return "trae";
  if (has("access_key_id") || has("secret_access_key")) return "codearts";
  if (has("model_app_key") || has("model_app_secret") || has("model_base_url")) {
    return "officeace";
  }
  const provider = value.provider;
  if (typeof provider === "string" && IMPORT_PROVIDERS.includes(provider as Provider)) {
    return provider as Provider;
  }
  return null;
}

/** 只认 cURL 的目标主机，不能被其他站点头或请求体里的链接误导。 */
function providerFromCurlTarget(text: string): Provider | null {
  const target = /^curl\s+(?:(?:--location|-L)\s+)?(['"])(https?:\/\/[^\s]+?)\1/i.exec(text);
  if (!target) return null;
  try {
    return new URL(target[2]).hostname === "miora.design" ? "miora" : null;
  } catch {
    return null;
  }
}

/**
 * 从 cURL 命令里抽 Cookie / Authorization。
 *
 * 只解析 `-H/--header`、`-b/--cookie` 的引号参数：Chrome / Edge 的
 * 「Copy as cURL」就是这种形态。拿不到就返回 null，交给后面的普通解析。
 */
export function headersFromCurl(text: string): {
  cookie: string | null;
  authorization: string | null;
} {
  const cookie: string[] = [];
  let authorization: string | null = null;
  const pattern = /(?:^|\s)(?:-H|--header|-b|--cookie)\s+(['"])([\s\S]*?)\1/g;
  for (const match of text.matchAll(pattern)) {
    const value = match[2];
    const colon = value.indexOf(":");
    if (colon === -1) {
      // `-b 'a=1; b=2'`：整个参数就是 Cookie 值
      cookie.push(value);
      continue;
    }
    const name = value.slice(0, colon).trim().toLowerCase();
    const headerValue = value.slice(colon + 1).trim();
    if (name === "cookie") cookie.push(headerValue);
    if (name === "authorization") authorization = headerValue;
  }
  return { cookie: cookie.length ? cookie.join("; ") : null, authorization };
}

/** 看起来像 Cookie 头：至少一个 `name=value`，且只由 cookie 字符组成。 */
function looksLikeCookie(text: string): boolean {
  const trimmed = text.trim();
  if (!/^[A-Za-z0-9_\-]+=[^;\s]*(\s*;\s*[A-Za-z0-9_\-]+=[^;\s]*)*$/.test(trimmed)) {
    return false;
  }
  return providerFromCookie(trimmed) !== null || /[;]/.test(trimmed);
}

/** 裸 token：无空白、非 JSON、通常以 `eyJ` 开头（JWT）或足够长的随机串。 */
function looksLikeToken(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed || /\s/.test(trimmed)) return false;
  if (trimmed.startsWith("{") || trimmed.startsWith("[") || trimmed.startsWith("curl")) {
    return false;
  }
  return trimmed.length >= 16;
}

/** 按渠道把裸 token / Cookie 包成该渠道的最小凭证对象。 */
function wrapRaw(
  provider: Provider,
  source: { cookie: string | null; token: string | null },
  recognized: boolean,
): ParsedCredential {
  const detected = recognized ? provider : null;
  if (provider === "miora") {
    return {
      ok: true,
      provider,
      credential: {
        ...(source.cookie !== null ? { cookie: source.cookie } : {}),
        ...(source.token ? { authToken: source.token } : {}),
      },
      detected,
    };
  }
  if (source.cookie !== null) {
    if (provider === "lingxi" || provider === "raccoon") {
      return {
        ok: true,
        provider,
        credential: { cookie: source.cookie },
        detected,
      };
    }
    return {
      ok: false,
      error: `${providerLabel(provider)} 不使用 Cookie 登录：请在 DevTools 里复制 Authorization 头或该渠道的 JSON 凭证`,
    };
  }
  const token = source.token ?? "";
  if (provider === "raccoon") {
    return { ok: true, provider, credential: { access_token: token }, detected };
  }
  if (provider === "lingxi") {
    return { ok: true, provider, credential: { cookie: token }, detected };
  }
  if (provider === "codebuddy" || provider === "codebuddy_intl" || provider === "qoder") {
    return { ok: true, provider, credential: { token }, detected };
  }
  return {
    ok: false,
    error: `${providerLabel(provider)} 需要完整 JSON 凭证（含多个字段），不能只粘一个 token`,
  };
}

/**
 * 解析导入框内容。
 *
 * @param text 用户粘贴的原文
 * @param fallback 下拉选中的渠道；识别不出时用它
 */
export function parseCredentialInput(
  text: string,
  fallback: Provider,
  detect = true,
): ParsedCredential {
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, error: "请先粘贴凭证 JSON、Cookie 或 DevTools 的 Copy as cURL" };
  }

  // cURL：先抽头，再走和裸内容相同的分支
  if (/^curl(\s|$)/i.test(trimmed)) {
    const { cookie, authorization } = headersFromCurl(trimmed);
    const bearer = authorization?.replace(/^Bearer\s+/i, "") ?? null;
    if (!cookie && !bearer) {
      return {
        ok: false,
        error: "这条 cURL 里没有 Cookie 或 Authorization 头，换一条带登录态的请求再复制",
      };
    }
    const token = bearer?.trim() || null;
    const detected = detect
      ? (cookie ? providerFromCookie(cookie) : null)
        ?? (token ? providerFromToken(token) : null)
        ?? providerFromCurlTarget(trimmed)
      : null;
    return wrapRaw(detected ?? fallback, { cookie, token }, detected !== null);
  }

  // JSON：单个对象继续；数组提示走批量导入
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return {
        ok: false,
        error: "凭证必须是合法 JSON（也可粘贴整段 Cookie 或 Copy as cURL）",
      };
    }
    if (Array.isArray(parsed)) {
      return {
        ok: false,
        error: "检测到多条凭证：请用凭证页右上角的「批量导入」",
      };
    }
    if (typeof parsed !== "object" || parsed === null) {
      return {
        ok: false,
        error: "凭证必须是合法 JSON 对象（也可粘贴整段 Cookie 或 Copy as cURL）",
      };
    }
    const object = parsed as Record<string, unknown>;
    // 批量导入同形的 `{provider, credential}` 也收：用户从导出/脚本拿到的
    // 就是这种形态，没必要先删掉外层。
    const nested = object.credential;
    if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) {
      const declared = object.provider;
      const declaredProvider = detect
        ? typeof declared === "string" && IMPORT_PROVIDERS.includes(declared as Provider)
          ? (declared as Provider)
          : providerFromObject(object)
        : null;
      return {
        ok: true,
        provider: declaredProvider ?? fallback,
        credential: nested as Record<string, unknown>,
        detected: declaredProvider,
      };
    }
    const detected = detect ? providerFromObject(object) : null;
    return {
      ok: true,
      provider: detected ?? fallback,
      credential: object,
      detected,
    };
  }

  // 整段 Cookie / `Cookie:` 头
  const cookieText = trimmed.replace(/^cookie\s*:\s*/i, "").trim();
  if (looksLikeCookie(cookieText)) {
    const detected = detect ? providerFromCookie(cookieText) : null;
    return wrapRaw(detected ?? fallback, { cookie: cookieText, token: null }, detected !== null);
  }

  // 裸 token / 复制的 Bearer 值
  const token = trimmed.replace(/^Bearer\s+/i, "").trim();
  if (looksLikeToken(token)) {
    const detected = detect ? providerFromToken(token) : null;
    return wrapRaw(detected ?? fallback, { cookie: null, token }, detected !== null);
  }

  return {
    ok: false,
    error: "无法识别：凭证必须是合法 JSON，或粘贴整段 Cookie / DevTools「Copy as cURL」",
  };
}

/** 仅用于界面提示：从当前输入猜渠道，猜不出返回 null。 */
export function detectProvider(text: string): Provider | null {
  const parsed = parseCredentialInput(text, "codebuddy");
  return parsed.ok ? parsed.detected : null;
}

export interface ParsedBatchCredentials {
  entries: BatchImportEntry[];
  errors: { line: number; message: string }[];
  /** 原样保留本地解析失败的记录，提交成功后仍能修正重试。 */
  unparsed: string[];
}

const EMAIL_PREFIX = /^([A-Za-z0-9.!#$%&'*+/=?^_{}|~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,})(?:[ \t\r]+|$)/;

function cleanAuthToken(value: string): string {
  return value.trim().replace(/^["']|["']$/g, "").trim().replace(/^Bearer\s+/i, "").trim();
}

function completeBatchJwt(token: string): boolean {
  const parts = token.split(".");
  if (parts.length !== 3 || parts.some((part) => !/^[A-Za-z0-9_-]+$/.test(part))) return false;
  const claims = jwtClaims(token);
  if (!claims || Array.isArray(claims)) return false;
  try {
    const header = JSON.parse(atob(parts[0].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof header === "object" && header !== null && !Array.isArray(header);
  } catch {
    return false;
  }
}

function looksLikeBatchJwt(token: string): boolean {
  return token.startsWith("eyJ") || providerFromToken(token) !== null;
}

function mioraBatchCredential(raw: Record<string, unknown>): {
  credential?: Record<string, unknown>;
  error?: string;
} {
  const auth = new Set<string>();
  const refresh = new Set<string>();
  const cookies: string[] = [];
  for (const key of ["access_token", "authToken", "accessToken", "token", "raw"]) {
    const value = raw[key];
    if (typeof value !== "string" || !value.trim()) continue;
    const token = cleanAuthToken(value);
    if (token.includes("=") || /^cookie\s*:/i.test(token)) cookies.push(token);
    else auth.add(token);
  }
  if (typeof raw.cookie === "string") cookies.push(raw.cookie);
  for (const key of ["refresh_token", "refreshToken"]) {
    if (typeof raw[key] === "string" && raw[key].trim()) refresh.add(raw[key].trim());
  }
  for (const cookie of cookies) {
    for (const part of cookie.replace(/^cookie\s*:\s*/i, "").split(";")) {
      const split = part.indexOf("=");
      if (split < 0) continue;
      const key = part.slice(0, split).trim().toLowerCase();
      const value = part.slice(split + 1).trim().replace(/^["']|["']$/g, "").trim();
      if (!value) continue;
      if (key === "authtoken") auth.add(cleanAuthToken(value));
      if (key === "refreshtoken") refresh.add(value);
    }
  }
  if (!auth.size) return { error: "Miora 凭证缺少 authToken，不能拼接其它记录补齐" };
  if (auth.size > 1 || refresh.size > 1) {
    return { error: "同一条记录含多份不同票据，请按账号分开粘贴" };
  }
  const token = [...auth][0];
  if (looksLikeBatchJwt(token) && !completeBatchJwt(token)) {
    return { error: "JWT 不完整或格式错误，请复制完整 token，不要拆行" };
  }
  return {
    credential: {
      ...raw,
      authToken: token,
      ...(refresh.size ? { refreshToken: [...refresh][0] } : {}),
    },
  };
}

/** 只按完整JSON和独立行分界，绝不把相邻账号/换行JWT拼成一份凭证。 */
export function parseBatchCredentialInput(
  text: string,
  fallback: Provider = "miora",
): ParsedBatchCredentials {
  const result: ParsedBatchCredentials = { entries: [], errors: [], unparsed: [] };
  let position = 0;
  let line = 1;
  let pendingEmail: { value: string; line: number } | null = null;
  let blockedContinuation = false;
  const consume = (end: number) => {
    for (; position < end; position++) if (text[position] === "\n") line++;
  };
  const reject = (at: number, message: string, source: string) => {
    result.errors.push({ line: at, message });
    result.unparsed.push(source);
  };
  const accept = (raw: unknown, at: number, email: string, source: string) => {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
      reject(at, "必须是对象", source);
      return;
    }
    const item = raw as Record<string, unknown>;
    blockedContinuation = false;
    let provider: string;
    let credential: Record<string, unknown>;
    if ("credential" in item) {
      if (typeof item.provider !== "string" || !item.provider.trim()) {
        reject(at, "缺少 provider", source);
        return;
      }
      if (typeof item.credential !== "object" || item.credential === null
          || Array.isArray(item.credential)) {
        reject(at, "credential 必须是对象", source);
        return;
      }
      provider = item.provider.trim();
      credential = item.credential as Record<string, unknown>;
    } else {
      if (typeof item.refreshToken === "string"
          && !["authToken", "access_token", "accessToken", "token", "cookie", "raw"]
            .some((key) => typeof item[key] === "string" && item[key].trim())) {
        reject(at, "只有 refreshToken，缺少当前账号的 authToken/accessToken", source);
        return;
      }
      const parsed = parseCredentialInput(JSON.stringify(item), fallback);
      if (!parsed.ok) {
        reject(at, parsed.error, source);
        return;
      }
      provider = parsed.provider;
      credential = parsed.credential;
    }
    if (provider === "miora") {
      const parsed = mioraBatchCredential(credential);
      if (!parsed.credential) {
        if (parsed.error?.startsWith("JWT ")) blockedContinuation = true;
        reject(at, parsed.error!, source);
        return;
      }
      credential = parsed.credential;
    }
    result.entries.push({
      provider, credential,
      nickname: email || (typeof item.nickname === "string"
        ? (provider === "miora" ? item.nickname.trim() : item.nickname) : ""),
      tags: Array.isArray(item.tags) ? item.tags.map(String) : [],
    });
  };
  while (position < text.length) {
    let newlines = 0;
    while (position < text.length && /\s/.test(text[position])) {
      if (text[position] === "\n") newlines++;
      consume(position + 1);
    }
    if (newlines > 1) blockedContinuation = false;
    if (position >= text.length) break;
    const recordLine = line;
    const lineEnd = text.indexOf("\n", position);
    const endOfLine = lineEnd < 0 ? text.length : lineEnd;
    const prefix = EMAIL_PREFIX.exec(text.slice(position, endOfLine));
    if (prefix) {
      if (pendingEmail) reject(pendingEmail.line, "邮箱后缺少凭证", pendingEmail.value);
      pendingEmail = { value: prefix[1], line: recordLine };
      blockedContinuation = false;
      consume(position + prefix[0].length);
      if (!text.slice(position, endOfLine).trim()) {
        consume(endOfLine);
        continue;
      }
    }
    const email = pendingEmail?.value ?? "";
    const emailSource = email ? email + "\n" : "";
    pendingEmail = null;
    const start = position;
    if (text[position] === "{" || text[position] === "[") {
      blockedContinuation = false;
      const root = text[position];
      let depth = 0;
      let quoted = false;
      let escaped = false;
      let previous = "";
      let end = text.length;
      for (let index = position; index < text.length; index++) {
        const char = text[index];
        if (quoted) {
          if (escaped) escaped = false;
          else if (char === "\\") escaped = true;
          else if (char === '"') quoted = false;
          continue;
        }
        if (char === "\n" && depth === 1 && previous !== ":") {
          let next = index + 1;
          while (/[ \t\r]/.test(text[next] ?? "")) next++;
          const nextEnd = text.indexOf("\n", next);
          const nextLine = text.slice(next, nextEnd < 0 ? text.length : nextEnd);
          const nextToken = cleanAuthToken(nextLine);
          if ((root === "{" && text[next] === "{") || EMAIL_PREFIX.test(nextLine)
              || /^(?:cookie\s*:|authToken\s*=)/i.test(nextLine)
              || (nextToken.startsWith("eyJ") && completeBatchJwt(nextToken))) {
            end = index;
            break;
          }
        }
        if (char === '"') quoted = true;
        else if (char === "{" || char === "[") depth++;
        else if (char === "}" || char === "]") {
          depth--;
          if (!depth) { end = index + 1; break; }
        }
        if (!/\s/.test(char)) previous = char;
      }
      const source = text.slice(start, end);
      consume(end);
      let parsed: unknown;
      try { parsed = JSON.parse(source); } catch {
        reject(recordLine, "JSON 解析失败", emailSource + source);
        continue;
      }
      if (Array.isArray(parsed)) {
        if (email && parsed.length !== 1) {
          reject(recordLine, "一个邮箱只能对应一条凭证，请为数组各条设置 nickname",
            emailSource + source);
        } else {
          parsed.forEach((raw, index) => accept(raw, recordLine + index, email,
            emailSource + JSON.stringify([raw])));
        }
      } else accept(parsed, recordLine, email, emailSource + source);
      continue;
    }
    const source = text.slice(position, endOfLine).trim();
    consume(endOfLine);
    const token = cleanAuthToken(source);
    const jwtLike = looksLikeBatchJwt(token);
    if (jwtLike && !completeBatchJwt(token)) {
      blockedContinuation = true;
      reject(recordLine, "JWT 不完整或格式错误，请复制完整 token，不要拆行", emailSource + source);
      continue;
    }
    const boundary = jwtLike || /^(?:cookie\s*:|authToken\s*=|curl\s)/i.test(source);
    if (blockedContinuation && !boundary) {
      reject(recordLine, "上一条 JWT 不完整，无法把续行当作新账号，请用空行分开",
        emailSource + source);
      continue;
    }
    if (boundary) blockedContinuation = false;
    if ((!/^curl\s/i.test(source) && /[{}\[\]]/.test(source)) || /\\$/.test(source)) {
      reject(recordLine, "凭证边界不清楚，请保持原始 token/Cookie 独立一行",
        emailSource + source);
      continue;
    }
    const parsed = parseCredentialInput(source, fallback);
    if (!parsed.ok) {
      reject(recordLine, parsed.error, emailSource + source);
      continue;
    }
    accept({ provider: parsed.provider, credential: parsed.credential },
      recordLine, email, emailSource + source);
  }
  if (pendingEmail) reject(pendingEmail.line, "邮箱后缺少凭证", pendingEmail.value);
  return result;
}
