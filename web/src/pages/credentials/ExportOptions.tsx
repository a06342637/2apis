import { Copy, Download } from "lucide-react";
import { DropdownMenuItem } from "../../components/ui/dropdown-menu";
import type { CredentialExportMode } from "./export";

export function CredentialExportOptions({ busy, onChoose }: {
  busy: boolean;
  onChoose: (mode: CredentialExportMode) => void;
}) {
  return <>
    <DropdownMenuItem disabled={busy} onSelect={() => onChoose("download")}>
      <Download className="size-4" /> 下载 JSON
    </DropdownMenuItem>
    <DropdownMenuItem disabled={busy} onSelect={() => onChoose("clipboard")}>
      <Copy className="size-4" /> 复制到剪贴板
    </DropdownMenuItem>
  </>;
}
