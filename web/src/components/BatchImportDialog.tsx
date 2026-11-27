import { useEffect, useMemo, useRef, useState } from "react";
import { FileUp, TriangleAlert, Upload } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { BatchImportEntry, BatchImportResult, Provider } from "../api/types";
import { providerLabel } from "../api/providers";
import { IMPORT_PROVIDERS, parseBatchCredentialInput } from "../lib/credentialImport";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Notice } from "../ui";

type ParseError = { line: number; message: string };

/**
 * 批量导入文本 → 条目 + 逐行错误。
 *
 * 接受 JSON 数组 / JSONL / 相邻完整 JSON、独立行 Cookie / token，
 * 邮箱可放在凭证前或上一行作为昵称。
 *
 * 解析与校验分开：某一行坏了只记该行，其余照常导入（后端也是同一口径）。
 * 行号从 1 开始，数组模式下用元素序号。
 */
export function parseBatchText(text: string, fallback: Provider = "miora"): {
  entries: BatchImportEntry[];
  errors: ParseError[];
} {
  const { entries, errors } = parseBatchCredentialInput(text, fallback);
  return { entries, errors };
}

/** 批量导入对话框：粘贴 / 选文件 → 预览 → 提交 → 失败行回填，便于重试。 */
export function BatchImportDialog({
  onClose,
  onImported,
  onNotice,
}: {
  onClose: () => void;
  onImported: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const working = useRef(false);
  const dialogRef = useDialogFocus<HTMLDivElement>(() => { if (!working.current) onClose(); });
  const fileRef = useRef<HTMLInputElement>(null);
  const fileReadGeneration = useRef(0);
  const fileReadInFlight = useRef(false);
  const [text, setText] = useState("");
  const [fallback, setFallback] = useState<Provider>("miora");
  const [pending, setPending] = useState(false);
  const [readingFile, setReadingFile] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BatchImportResult | null>(null);
  const parsed = useMemo(() => parseBatchCredentialInput(text, fallback), [text, fallback]);

  useEffect(() => () => { fileReadGeneration.current += 1; }, []);

  const readFile = async (file: File | undefined) => {
    if (!file || working.current) return;
    const generation = ++fileReadGeneration.current;
    fileReadInFlight.current = true;
    setReadingFile(true);
    try {
      const contents = await file.text();
      // 再次选择文件或关闭弹窗后，旧读取结果不能覆盖新内容。
      if (generation !== fileReadGeneration.current) return;
      setText(contents);
      setError(null);
      setResult(null);
    } catch {
      if (generation === fileReadGeneration.current) setError("读取文件失败");
    } finally {
      if (generation === fileReadGeneration.current) {
        fileReadInFlight.current = false;
        setReadingFile(false);
      }
    }
  };

  const submit = async () => {
    if (working.current || fileReadInFlight.current) return;
    if (!parsed.entries.length) {
      setError("没有可导入的条目");
      return;
    }
    working.current = true;
    setPending(true);
    setError(null);
    setResult(null);
    try {
      const response = await api.batchImportCredentials(parsed.entries);
      setResult(response);
      if (response.created.length) {
        onImported(`批量导入完成：成功 ${response.created.length} 条`);
      }
      if (response.failed.length || parsed.unparsed.length) {
        // 失败行回填成 JSONL：用户可以改完直接再提交，不用重新粘贴全部
        setText([
          ...response.failed.map(({ index }) => JSON.stringify(parsed.entries[index])),
          ...parsed.unparsed,
        ].join("\n"));
      }
      if (response.skipped.length && !response.created.length && !response.failed.length) {
        onNotice(`全部为重复凭证（${response.skipped.length} 条），未新增`);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "批量导入失败，请稍后重试");
    } finally {
      working.current = false;
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="批量导入凭证"
        data-testid="batch-import-dialog"
        className="max-h-[calc(100dvh-2rem)] w-full max-w-2xl space-y-3 overflow-y-auto overscroll-contain rounded-xl border border-border bg-card p-5 shadow-xl"
      >
        <div className="flex items-center gap-2">
          <Upload className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">批量导入凭证</h2>
          <span className="ml-auto text-xs text-muted-foreground">最多 100 条</span>
        </div>
        <p className="text-xs text-muted-foreground">
          粘贴多份 JSON（可换行）、一行一个 Cookie 或完整 token，也支持 JSON 数组/JSONL。
          Miora 也支持每行粘贴 <code>{"邮箱{authToken=...; refreshToken=...}"}</code>，
          或让邮箱紧贴 JSON 对象。邮箱会作为账号备注（昵称），也可单独放在上一行。
          新 Miora 账号未填昵称时按北京时间自动编号，
          重新导入保留旧昵称；同批重复凭证会跳过。
        </p>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          原始 token 默认渠道
          <select
            data-testid="batch-provider"
            aria-label="原始 token 默认渠道"
            autoComplete="off"
            value={fallback}
            disabled={pending}
            onChange={(event) => setFallback(event.target.value as Provider)}
            className="rounded border border-border bg-background px-2 py-1 text-foreground"
          >
            {IMPORT_PROVIDERS.map((provider) => (
              <option key={provider} value={provider}>{providerLabel(provider)}</option>
            ))}
          </select>
          <span>JSON/Cookie 的明确渠道优先</span>
        </label>

        <div className="flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".json,.jsonl,.txt"
            hidden
            disabled={pending}
            className="hidden"
            data-testid="batch-file"
            onChange={(event) => void readFile(event.target.files?.[0])}
          />
          <Button
            size="sm"
            variant="ghost"
            data-testid="batch-pick-file"
            disabled={pending}
            onClick={() => fileRef.current?.click()}
          >
            <FileUp className="size-4" /> 选择文件
          </Button>
          <span className="text-xs text-muted-foreground" data-testid="batch-count">
            识别 {parsed.entries.length} 条
            {parsed.errors.length > 0 && `，${parsed.errors.length} 行有问题`}
          </span>
        </div>

        <textarea
          name="credential-batch-text"
          data-lpignore="true"
          data-1p-ignore="true"
          data-bwignore="true"
          data-testid="batch-text"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setResult(null);
          }}
          rows={8}
          disabled={pending || readingFile}
          autoComplete="off"
          spellCheck={false}
          placeholder={'mail@example.com{authToken=TOKEN; refreshToken=REFRESH}\nanother@example.com{"authToken":"ANOTHER_TOKEN","refreshToken":"ANOTHER_REFRESH"}'}
          className="w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-xs"
        />
        {parsed.entries.length > 0 && (
          <div data-testid="batch-preview" className="text-xs text-muted-foreground">
            {parsed.entries.slice(0, 5).map((entry, index) => (
              <div key={index}>
                {index + 1}. {providerLabel(entry.provider as Provider)} ·{" "}
                {entry.nickname || (entry.provider === "miora" ? "新增时自动编号" : "未设置昵称")}
              </div>
            ))}
          </div>
        )}
        {parsed.entries.length > 100 && <Notice tone="warn">一次最多导入 100 条，请分批粘贴。</Notice>}

        {parsed.errors.length > 0 && (
          <div data-testid="batch-parse-errors" className="text-xs">
            <Notice tone="warn">
              {parsed.errors.slice(0, 5).map((item, index) => (
                <div key={item.line + "-" + index} className="inline-flex items-center gap-1.5">
                  <TriangleAlert className="size-3" />第 {item.line} 行：{item.message}
                </div>
              ))}
              {parsed.errors.length > 5 && <div>…另有 {parsed.errors.length - 5} 行</div>}
            </Notice>
          </div>
        )}

        {result && (
          <div data-testid="batch-result" className="text-xs">
            <Notice tone={result.failed.length ? "warn" : "ok"}>
              成功 {result.created.length} 条，跳过 {result.skipped.length} 条
              {result.failed.length > 0 && `，失败 ${result.failed.length} 条（已回填到输入框）`}
            </Notice>
          </div>
        )}

        {error && (
          <div data-testid="batch-error">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" data-testid="batch-cancel" onClick={onClose} disabled={pending}>
            关闭
          </Button>
          <Button
            variant="default"
            data-testid="batch-submit"
            onClick={() => void submit()}
            disabled={pending || readingFile || parsed.entries.length === 0 || parsed.entries.length > 100}
          >
            {pending ? "导入中…" : readingFile ? "读取文件中…" : `导入 ${parsed.entries.length} 条`}
          </Button>
        </div>
      </div>
    </div>
  );
}
