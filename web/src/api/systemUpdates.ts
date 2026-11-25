import { request } from "./client";

export interface UpdateVersion { version: string; commit: string }
export interface UpdateJob {
  id: string;
  status: "running" | "success" | "failed" | "rolled_back" | "recovery_failed";
  phase: string;
  progress: number;
  logs: { time: number; message: string }[];
  error: string;
  started_at: number;
  finished_at: number | null;
}
export interface SystemUpdateStatus {
  enabled: boolean;
  source?: string;
  branch?: string;
  current?: UpdateVersion;
  latest?: (UpdateVersion & { ci: "success" | "pending" | "failed" }) | null;
  update_available?: boolean;
  dirty?: boolean;
  job?: UpdateJob | null;
}

export const systemUpdates = {
  status: (signal?: AbortSignal) => request<SystemUpdateStatus>("/api/system-updates", { signal }),
  check: () => request<SystemUpdateStatus>("/api/system-updates/check", { method: "POST" }),
  source: (source: string) => request<SystemUpdateStatus>("/api/system-updates/source", {
    method: "PUT", body: JSON.stringify({ source }),
  }),
  start: (commit: string, force: boolean) => request<SystemUpdateStatus>("/api/system-updates/start", {
    method: "POST", body: JSON.stringify({ commit, force }),
  }),
};
