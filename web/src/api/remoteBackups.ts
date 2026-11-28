import { request } from "./client";
import type { BackupEntry } from "./types";

export type Destination = "oss" | "sftp";
type Target = { enabled: boolean; directory: string; keep: number };
export type RemoteConfig = {
  enabled: boolean; days: number; time: string; timezone: string;
  oss: Target & { region: string; bucket: string; key_id: string; internal: boolean;
    endpoint: string; key_secret?: string; has_key_secret?: boolean };
  sftp: Target & { host: string; port: number; username: string; auth: string;
    password?: string; private_key?: string; passphrase?: string; fingerprint: string;
    has_password?: boolean; has_private_key?: boolean; has_passphrase?: boolean };
};
export type RemoteStatus = {
  config: RemoteConfig; running: boolean; next_run: number | null;
  last_run: null | { started_at: number; finished_at: number; scheduled: boolean; name: string;
    error?: string; targets?: { target: Destination; status: string; removed?: number; message: string }[] };
};
export const DESTINATIONS = { oss: "阿里云 OSS", sftp: "SSH / SFTP" } as const;
const root = "/api/remote-backups";
const filePath = (target: Destination, name: string) => `${root}/${target}/files/${encodeURIComponent(name)}`;
export const remoteBackups = {
  status: () => request<RemoteStatus>(root),
  save: (draft: RemoteConfig) => {
    const excludeFlags = (value: object) => Object.fromEntries(Object.entries(value).filter(([key]) => !key.startsWith("has_")));
    return request<RemoteStatus>(root, { method: "PUT", body: JSON.stringify({ ...draft,
      oss: excludeFlags(draft.oss), sftp: excludeFlags(draft.sftp) }) });
  },
  run: () => request(`${root}/run`, { method: "POST" }),
  test: (target: Destination) => request(`${root}/${target}/test`, { method: "POST" }),
  files: (target: Destination) => request<{ backups: BackupEntry[] }>(`${root}/${target}/files`),
  downloadUrl: filePath,
  restore: (target: Destination, name: string) => request(`${filePath(target, name)}/restore`, { method: "POST" }, 90_000),
  delete: (target: Destination, name: string) => request(filePath(target, name), { method: "DELETE" }),
};
