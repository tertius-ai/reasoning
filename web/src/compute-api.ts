import { ApiError, organizationPath, request } from "./organization-api";

export type ComputeResources = {
  cpu: number; memory_bytes: number; disk_bytes: number; pids: number; wall_time_seconds: number; artifact_bytes: number;
};
export type ComputeImage = {
  id: string; name: string; reference: string; languages: string[]; source_revision: string;
  capabilities_digest: string; sbom_digest: string; capabilities_path: string; sbom_path: string;
  maximum_resources: ComputeResources;
};
export type ComputePolicy = { allowed_images: string[]; monthly_cpu_seconds: number; max_active_jobs: number;
  max_pending_jobs?: number | null; requests_per_user_15_minutes?: number | null };
export type ComputeConfiguration = { policy: ComputePolicy; images: ComputeImage[];
  balance: { monthly_cpu_seconds: number; used_cpu_seconds: number; held_cpu_seconds: number; available_cpu_seconds: number } };
export type ComputeAttempt = { client_id: string; image_id: string; language: string; program: string; resources: ComputeResources };
/** A topic file pinned by identity and SHA-256 as a job input, read-only in the sandbox at /work/in/<path>. */
export type ComputeInputFile = { identity: string; sha256: string; topic_id: string; name: string; mime: string; size_bytes: number; path: string };
export type ComputeJob = {
  sequence?: number;
  id: string; organization_id: string; topic_id: string | null; project_id?: string; requested_by: string; client_id: string;
  parameters: Omit<ComputeAttempt, "client_id"> & { attachments?: ComputeInputFile[] }; image: ComputeImage; program_digest: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "outcome_unknown";
  cancel_requested: number; error_code: string | null; exit_code: number | null; elapsed_ms: number | null;
  created_at: number; started_at: number | null; finished_at: number | null; worker_id: string | null;
  reserved_cpu_seconds: number; used_cpu_seconds: number | null; accounted_at: number | null; log_bytes: number;
};
export type ComputeEvent = { sequence: number; created_at: number; event: { type: string; text?: string; status?: string } };
export type ComputeArtifact = { id: string; name: string; size_bytes: number; digest: string; committed_at: number };
export type ComputeWorker = { id: string; organization_id: string; name: string; image_ids: string[]; resources: ComputeResources;
  created_at: number; last_seen_at: number | null; draining: number; revoked_at: number | null;
  retired_at: number | null; retirement_id: string | null };
export type ComputeScaler = { id: string; organization_id: string; authorized_by: string; name: string;
  image_ids: string[]; resources: ComputeResources; maximum_workers: number; enrolled_workers: number; scopes: string[];
  created_at: number; expires_at: number; revoked_at: number | null; enabled: boolean };

export function captureScalerGrant(name: string, ids: string[], resources: ComputeResources, maximumWorkers: number, days: number, catalog: ComputeImage[]) {
  if (!name.trim() || Array.from(name).length > 200 || name.includes("\0") || !ids.length || ids.length > 32 || new Set(ids).size !== ids.length) {
    throw new Error("Choose a scaler name and distinct approved images.");
  }
  if (!Number.isInteger(maximumWorkers) || maximumWorkers < 1 || maximumWorkers > 64 || !Number.isInteger(days) || days < 1 || days > 365) {
    throw new Error("Choose one to 64 worker enrollments and one to 365 days.");
  }
  const limits = validateComputeResources(resources);
  for (const id of ids) {
    const image = catalog.find((item) => item.id === id);
    if (!image) throw new Error("Select only installation-approved images.");
    validateComputeResources(limits, image.maximum_resources);
  }
  return { name, image_ids: [...ids].sort(), resources: limits, maximum_workers: maximumWorkers, lifetime_seconds: days * 86400 };
}

export function computeWorkerStatus(worker: Pick<ComputeWorker, "retired_at" | "revoked_at" | "draining">): string {
  return worker.retired_at != null ? "Retired (execution access revoked)" : worker.revoked_at !== null ? "Revoked" : worker.draining ? "Draining" : "Accepting claims";
}

export const RESOURCE_FIELDS = [
  ["cpu", "CPU cores", 1, 64],
  ["memory_bytes", "Memory (bytes)", 64 * 1024 ** 2, 256 * 1024 ** 3],
  ["disk_bytes", "Scratch disk (bytes)", 8 * 1024 ** 2, 64 * 1024 ** 3],
  ["pids", "Process limit", 16, 4096],
  ["wall_time_seconds", "Wall time (seconds)", 1, 86400],
  ["artifact_bytes", "Artifact allowance (bytes)", 1, 1024 ** 3],
] as const;
export const DEFAULT_COMPUTE_RESOURCES: ComputeResources = {
  cpu: 1, memory_bytes: 1024 ** 3, disk_bytes: 256 * 1024 ** 2, pids: 128, wall_time_seconds: 120, artifact_bytes: 1024 ** 2,
};
export const activeCompute = (job: ComputeJob) => ["queued", "running", "outcome_unknown"].includes(job.status);
export const canCancelCompute = (job: ComputeJob, userId: string, canWrite: boolean, canManage: boolean) =>
  canWrite && activeCompute(job) && !job.cancel_requested && (canManage || job.requested_by === userId);
export const computeJobPath = (organizationId: string, jobId: string) => organizationPath(organizationId) + "/compute-jobs/" + encodeURIComponent(jobId);

export function validateComputeResources(value: ComputeResources, maximum?: ComputeResources): ComputeResources {
  if (Object.keys(value).sort().join() !== RESOURCE_FIELDS.map(([key]) => key).sort().join()) throw new Error("Supply all six resource limits.");
  for (const [key, label, minimum, upper] of RESOURCE_FIELDS) {
    if (!Number.isSafeInteger(value[key]) || value[key] < minimum || value[key] > upper || (maximum && value[key] > maximum[key])) {
      throw new Error(label + " must be a whole number within the approved limits.");
    }
  }
  if (value.artifact_bytes > Math.floor(value.disk_bytes / 2)) throw new Error("Artifact allowance cannot exceed half the scratch disk.");
  return { ...value };
}

export function initialComputeResources(maximum: ComputeResources, defaults: ComputeResources = DEFAULT_COMPUTE_RESOURCES): ComputeResources {
  const limits = { ...defaults };
  for (const [key] of RESOURCE_FIELDS) limits[key] = Math.min(limits[key], maximum[key]);
  limits.artifact_bytes = Math.min(limits.artifact_bytes, Math.floor(limits.disk_bytes / 2));
  return validateComputeResources(limits, maximum);
}

export function captureComputeAttempt(image: ComputeImage, language: string, program: string, resources: ComputeResources, clientId: string): ComputeAttempt {
  if (!image.languages.includes(language)) throw new Error("Select a language supported by this image.");
  if (!program.trim() || Array.from(program).length > 16000 || program.includes("\0") || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(program)) {
    throw new Error("Program must contain 1–16000 valid Unicode characters without NUL bytes.");
  }
  if (!clientId || clientId.length > 128) throw new Error("A stable request ID is required.");
  const attempt = { client_id: clientId, image_id: image.id, language, program, resources: validateComputeResources(resources, image.maximum_resources) };
  if (new TextEncoder().encode(JSON.stringify(attempt)).byteLength > 65536) throw new Error("Encoded program request exceeds the server's 64 KiB limit.");
  return attempt;
}

export function matchesComputeAttempt(job: ComputeJob, attempt: ComputeAttempt, organizationId: string, topicId: string, userId: string): boolean {
  return job.organization_id === organizationId && job.topic_id === topicId && job.requested_by === userId && job.client_id === attempt.client_id
    && job.parameters.image_id === attempt.image_id && job.parameters.language === attempt.language && job.parameters.program === attempt.program
    && RESOURCE_FIELDS.every(([key]) => job.parameters.resources[key] === attempt.resources[key]);
}

// A lost or malformed receipt never authorizes a new request ID or new parameters.
export async function deliverComputeAttempt(organizationId: string, topicId: string, userId: string, attempt: ComputeAttempt): Promise<ComputeJob> {
  const job = await request<ComputeJob>(organizationPath(organizationId) + "/topics/" + encodeURIComponent(topicId) + "/compute-jobs", { method: "POST", body: attempt });
  if (!job || !job.parameters || !matchesComputeAttempt(job, attempt, organizationId, topicId, userId)) {
    throw new ApiError(502, "The job receipt does not match this request. Retain its ID and retry the unchanged request.");
  }
  return job;
}

export function appendComputeEvents(existing: ComputeEvent[], page: ComputeEvent[], after: number): ComputeEvent[] {
  if (page.length > 200 || page.some((entry, index) => !Number.isSafeInteger(entry.sequence) || entry.sequence <= (index ? page[index - 1].sequence : after))) {
    throw new Error("Invalid compute event cursor order.");
  }
  // Server log chunks are capped at 1 MiB and 4096 entries; retain a bounded event tail.
  return [...existing, ...page].slice(-4300);
}
