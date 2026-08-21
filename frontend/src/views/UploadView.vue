<script setup lang="ts">
import {
  ChevronLeft,
  ChevronRight,
  FileStack,
  Files,
  FolderOpen,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
  UploadCloud,
  X,
} from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { onBeforeRouteLeave, RouterLink, useRoute, useRouter } from "vue-router";
import { randomUUID } from "@/uuid";

import { ApiError, api } from "@/api";
import { useProjectContext } from "@/composables/useProjectContext";
import { useSession } from "@/composables/useSession";
import { formatBytes, shortId } from "@/format";
import {
  isUploadBatchActive,
  markUploadBatchActive,
  markUploadBatchInactive,
} from "@/uploadQueueState";
import type {
  ArtifactUploadResult,
  UploadBatch,
  UploadBatchItem,
  UploadBatchItemStatus,
} from "@/types";

interface MetadataRow {
  id: string;
  key: string;
  value: string;
}

interface QueueTask {
  clientFileId: string;
  file: File | null;
  filename: string;
  relativePath: string;
  size: number;
  mediaType: string;
  status: UploadBatchItemStatus;
  attempt: number;
  loaded: number;
  error: string;
  artifactId: string | null;
  controller: AbortController | null;
  reserved: boolean;
}

interface DroppedFile {
  file: File;
  relativePath: string;
}

const MAX_QUEUE_FILES = 20_000;
const MAX_AUTOMATIC_ATTEMPTS = 6;
const MOLOP_BATCH_MAX_FILES = 32;
const MOLOP_BATCH_MAX_BYTES = 256 * 1024 * 1024;
// Leave the completed batch summary on screen briefly before resetting the
// queue so the user (and the acceptance suite) can observe the final counts.
const BATCH_COMPLETION_RESET_DELAY_MS = 2_000;
const PAGE_SIZE = 100;
const route = useRoute();
const router = useRouter();
const session = useSession();
const projectContext = useProjectContext();
const fileInput = ref<HTMLInputElement | null>(null);
const folderInput = ref<HTMLInputElement | null>(null);
const reattachFileInput = ref<HTMLInputElement | null>(null);
const reattachFolderInput = ref<HTMLInputElement | null>(null);
const selectedProjectId = ref(projectContext.currentProjectId.value ?? "");
const artifactKind = ref<ArtifactUploadResult["artifact_kind"]>("calculation_output");
const concurrency = ref(Number(window.localStorage.getItem("tricycle.uploadConcurrency") || 3));
const metadataRows = ref<MetadataRow[]>([{ id: randomUUID(), key: "", value: "" }]);
const tasks = ref<QueueTask[]>([]);
const batch = ref<UploadBatch | null>(null);
const recentBatches = ref<UploadBatch[]>([]);
const remoteItems = ref<QueueTask[]>([]);
const remoteTotal = ref(0);
const remoteMode = ref(false);
const statusFilter = ref<UploadBatchItemStatus | "all">("all");
const queuePage = ref(0);
const queueRunning = ref(false);
const queuePaused = ref(false);
const queueCancelled = ref(false);
const loadingBatch = ref(false);
const setupError = ref("");
const queueError = ref("");
const readingDrop = ref(false);
let queueRunId = 0;
let viewMounted = false;
let preserveQueueOnUnmount = false;

onBeforeRouteLeave(() => {
  preserveQueueOnUnmount = true;
});

const uploadProjects = computed(() =>
  (session.user.value?.projects ?? []).filter((project) => project.permissions.includes("artifact:upload")),
);
const settingsLocked = computed(() => batch.value !== null);
const localFilteredTasks = computed(() => statusFilter.value === "all"
  ? tasks.value
  : tasks.value.filter((task) => task.status === statusFilter.value));
const visibleTasks = computed(() => remoteMode.value
  ? remoteItems.value
  : localFilteredTasks.value.slice(queuePage.value * PAGE_SIZE, (queuePage.value + 1) * PAGE_SIZE));
const filteredTotal = computed(() => remoteMode.value ? remoteTotal.value : localFilteredTasks.value.length);
const pageCount = computed(() => Math.max(1, Math.ceil(filteredTotal.value / PAGE_SIZE)));
const succeededCount = computed(() => tasks.value.filter((task) => task.status === "succeeded").length);
const failedCount = computed(() => tasks.value.filter((task) => task.status === "failed").length);
const cancelledCount = computed(() => tasks.value.filter((task) => task.status === "cancelled").length);
const uploadedBytes = computed(() => tasks.value.reduce((total, task) => {
  if (task.status === "succeeded") return total + task.size;
  if (task.status === "uploading") return total + Math.min(task.loaded, task.size);
  return total;
}, 0));
const totalBytes = computed(() => tasks.value.reduce((total, task) => total + task.size, 0));
const overallPercent = computed(() => totalBytes.value
  ? Math.round((uploadedBytes.value / totalBytes.value) * 100)
  : 0);
const canStart = computed(() => Boolean(
  selectedProjectId.value
  && tasks.value.some((task) => task.status === "queued" && task.file)
  && !queueRunning.value
  && !queueCancelled.value,
));
const batchCounters = computed(() => batch.value ? {
  succeeded: remoteMode.value ? batch.value.succeeded_count : succeededCount.value,
  failed: remoteMode.value ? batch.value.failed_count : failedCount.value,
  cancelled: remoteMode.value ? batch.value.cancelled_count : cancelledCount.value,
  total: batch.value.total_count,
} : {
  succeeded: succeededCount.value,
  failed: failedCount.value,
  cancelled: cancelledCount.value,
  total: tasks.value.length,
});

function fileRelativePath(file: File): string {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
}

function queueTaskFromRemote(item: UploadBatchItem, file: File | null = null): QueueTask {
  return {
    clientFileId: item.client_file_id,
    file,
    filename: item.original_filename,
    relativePath: item.relative_path,
    size: item.size_bytes,
    mediaType: item.media_type,
    status: item.status,
    attempt: item.attempt_count,
    loaded: item.status === "succeeded" ? item.size_bytes : 0,
    error: item.error_message ?? "",
    artifactId: item.artifact_file_id,
    controller: null,
    reserved: false,
  };
}

function createQueuedTask(file: File, relativePath = fileRelativePath(file)): QueueTask {
  return {
    clientFileId: randomUUID(),
    file,
    filename: file.name,
    relativePath,
    size: file.size,
    mediaType: file.type || "application/octet-stream",
    status: "queued",
    attempt: 0,
    loaded: 0,
    error: "",
    artifactId: null,
    controller: null,
    reserved: false,
  };
}

function setSelectedFiles(files: File[], relativePaths = files.map(fileRelativePath)): void {
  setupError.value = "";
  if (!files.length) return;

  if (relativePaths.length !== files.length) {
    setupError.value = "无法读取拖放文件的相对路径";
    return;
  }
  const incomingPaths = relativePaths;
  const seenIncoming = new Set<string>();
  const duplicateIncoming = incomingPaths.find((path) => {
    if (seenIncoming.has(path)) return true;
    seenIncoming.add(path);
    return false;
  });
  const existingPaths = new Set(tasks.value.map((task) => task.relativePath));
  const duplicateExisting = incomingPaths.find((path) => existingPaths.has(path));
  if (duplicateIncoming || duplicateExisting) {
    setupError.value = duplicateIncoming
      ? "所选文件中存在重复相对路径"
      : `所选文件与队列中的文件重复：${duplicateExisting}`;
    return;
  }
  if (tasks.value.length + files.length > MAX_QUEUE_FILES) {
    setupError.value = `一次队列最多 ${MAX_QUEUE_FILES.toLocaleString()} 个文件`;
    return;
  }

  // A file or folder picker may be used repeatedly. Keep earlier selections
  // in the queue so files and folders can be combined before starting a batch.
  batch.value = null;
  remoteMode.value = false;
  queueCancelled.value = false;
  queuePaused.value = false;
  queuePage.value = 0;
  statusFilter.value = "all";
  tasks.value.push(...files.map((file, index) => createQueuedTask(file, relativePaths[index])));
}

function selectedFiles(event: Event): void {
  const input = event.target as HTMLInputElement;
  setSelectedFiles(Array.from(input.files ?? []));
  input.value = "";
}

function readFileEntry(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readDirectoryEntries(entry: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = entry.createReader();
  const entries: FileSystemEntry[] = [];
  return new Promise((resolve, reject) => {
    const readNext = (): void => {
      reader.readEntries((batch) => {
        if (!batch.length) {
          resolve(entries);
          return;
        }
        entries.push(...batch);
        readNext();
      }, reject);
    };
    readNext();
  });
}

async function collectDroppedEntry(entry: FileSystemEntry, relativePath = entry.name): Promise<DroppedFile[]> {
  if (entry.isFile) {
    return [{ file: await readFileEntry(entry as FileSystemFileEntry), relativePath }];
  }
  const children = await readDirectoryEntries(entry as FileSystemDirectoryEntry);
  const files: DroppedFile[] = [];
  for (const child of children) {
    files.push(...await collectDroppedEntry(child, `${relativePath}/${child.name}`));
  }
  return files;
}

async function droppedFiles(event: DragEvent): Promise<void> {
  if (readingDrop.value) return;
  const transfer = event.dataTransfer;
  if (!transfer) return;
  readingDrop.value = true;
  setupError.value = "";
  try {
    const entries = Array.from(transfer.items ?? [])
      .filter((item) => item.kind === "file")
      .map((item) => item.webkitGetAsEntry?.())
      .filter((entry): entry is FileSystemEntry => Boolean(entry));
    if (!entries.length) {
      setSelectedFiles(Array.from(transfer.files ?? []));
      return;
    }
    const dropped = (await Promise.all(entries.map((entry) => collectDroppedEntry(entry)))).flat();
    setSelectedFiles(dropped.map((item) => item.file), dropped.map((item) => item.relativePath));
  } catch (error) {
    setupError.value = error instanceof Error ? `读取拖放文件夹失败：${error.message}` : "读取拖放文件夹失败";
  } finally {
    readingDrop.value = false;
  }
}

function addMetadataRow(): void {
  metadataRows.value.push({ id: randomUUID(), key: "", value: "" });
}

function removeMetadataRow(id: string): void {
  metadataRows.value = metadataRows.value.filter((row) => row.id !== id);
  if (!metadataRows.value.length) addMetadataRow();
}

function sharedMetadata(): Record<string, string> | null {
  const metadata: Record<string, string> = {};
  for (const row of metadataRows.value) {
    const key = row.key.trim();
    if (!key && !row.value.trim()) continue;
    if (!key) {
      setupError.value = "元数据键不能为空";
      return null;
    }
    if (Object.hasOwn(metadata, key)) {
      setupError.value = `元数据键重复：${key}`;
      return null;
    }
    metadata[key] = row.value;
  }
  return metadata;
}

async function refreshRecentBatches(): Promise<void> {
  if (!selectedProjectId.value) return;
  try {
    recentBatches.value = (await api.uploadBatches({
      projectId: selectedProjectId.value,
      limit: 8,
    })).items;
  } catch {
    recentBatches.value = [];
  }
}

async function refreshBatch(): Promise<void> {
  if (!batch.value) return;
  batch.value = await api.uploadBatch(batch.value.id);
}

async function refreshRemoteItems(): Promise<void> {
  if (!remoteMode.value || !batch.value) return;
  const page = await api.uploadBatchItems(batch.value.id, {
    status: statusFilter.value === "all" ? undefined : statusFilter.value,
    limit: PAGE_SIZE,
    offset: queuePage.value * PAGE_SIZE,
  });
  remoteItems.value = page.items.map((item) => queueTaskFromRemote(item));
  remoteTotal.value = page.total;
}

async function openBatch(batchId: string): Promise<void> {
  loadingBatch.value = true;
  queueError.value = "";
  queueRunId += 1;
  try {
    batch.value = await api.uploadBatch(batchId);
    selectedProjectId.value = batch.value.project_id;
    artifactKind.value = batch.value.artifact_kind;
    remoteMode.value = true;
    tasks.value = [];
    queuePage.value = 0;
    statusFilter.value = "all";
    await refreshRemoteItems();
    window.localStorage.setItem("tricycle.lastUploadBatch", batchId);
    await router.replace({ query: { ...route.query, project_id: batch.value.project_id, batch: batchId } });
  } catch (error) {
    queueError.value = error instanceof Error ? error.message : "上传批次加载失败";
  } finally {
    loadingBatch.value = false;
  }
}

async function fetchAllBatchItems(batchId: string): Promise<UploadBatchItem[]> {
  const items: UploadBatchItem[] = [];
  while (true) {
    const page = await api.uploadBatchItems(batchId, { limit: 200, offset: items.length });
    items.push(...page.items);
    if (items.length >= page.total) return items;
  }
}

async function reattachSelectedFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!batch.value || !files.length) return;
  loadingBatch.value = true;
  setupError.value = "";
  try {
    let items = await fetchAllBatchItems(batch.value.id);
    if (items.some((item) => item.status === "uploading") && !isUploadBatchActive(batch.value.id)) {
      batch.value = await api.recoverUploadBatch(batch.value.id);
      items = await fetchAllBatchItems(batch.value.id);
      setupError.value = "检测到中断的上传，已恢复到等待队列";
    }
    // Reattach can also be done in several rounds (for example one folder at
    // a time). Preserve files already associated with the queue and merge the
    // latest selection by relative path.
    const fileMap = new Map(
      tasks.value.flatMap((task) => task.file ? [[task.relativePath, task.file] as const] : []),
    );
    for (const file of files) fileMap.set(fileRelativePath(file), file);
    tasks.value = items.map((item) => {
      const file = fileMap.get(item.relative_path) ?? null;
      return queueTaskFromRemote(item, file && file.size === item.size_bytes ? file : null);
    });
    const missing = tasks.value.filter((task) => task.status === "queued" && !task.file).length;
    remoteMode.value = false;
    queuePage.value = 0;
    if (missing) setupError.value = `${missing.toLocaleString()} 个待上传文件未重新选中`;
  } catch (error) {
    setupError.value = error instanceof Error ? error.message : "文件重新关联失败";
  } finally {
    loadingBatch.value = false;
  }
}

async function recoverInterruptedUpload(): Promise<void> {
  if (!batch.value || isUploadBatchActive(batch.value.id)) return;
  loadingBatch.value = true;
  setupError.value = "";
  try {
    batch.value = await api.recoverUploadBatch(batch.value.id);
    await refreshRemoteItems();
    setupError.value = "检测到中断的上传，已恢复到等待队列。请重新选择未完成的本地文件后继续。";
  } catch (error) {
    setupError.value = error instanceof Error ? error.message : "中断上传恢复失败";
  } finally {
    loadingBatch.value = false;
  }
}

async function ensureBatch(): Promise<UploadBatch> {
  if (batch.value) return batch.value;
  const metadata = sharedMetadata();
  if (metadata === null) throw new Error(setupError.value);
  if (!selectedProjectId.value) throw new Error("请选择项目");
  if (!tasks.value.length) throw new Error("请选择文件");
  const created = await api.createUploadBatch({
    project_id: selectedProjectId.value,
    artifact_kind: artifactKind.value,
    shared_metadata: metadata,
    files: tasks.value.map((task) => ({
      client_file_id: task.clientFileId,
      original_filename: task.filename,
      relative_path: task.relativePath,
      size_bytes: task.size,
      media_type: task.mediaType,
    })),
  });
  batch.value = created;
  window.localStorage.setItem("tricycle.lastUploadBatch", created.id);
  await router.replace({ query: { ...route.query, project_id: created.project_id, batch: created.id } });
  await refreshRecentBatches();
  return created;
}

function applyItem(task: QueueTask, item: UploadBatchItem): void {
  task.status = item.status;
  task.attempt = item.attempt_count;
  task.loaded = item.status === "succeeded" ? task.size : 0;
  task.error = item.error_message ?? "";
  task.artifactId = item.artifact_file_id;
}

function updateBatchProgress(selected: QueueTask[], loaded: number, total: number): void {
  const selectedBytes = selected.reduce((sum, task) => sum + task.size, 0);
  let remaining = total > 0
    ? Math.min(selectedBytes, Math.round((loaded / total) * selectedBytes))
    : 0;
  for (const task of selected) {
    task.loaded = Math.min(task.size, remaining);
    remaining = Math.max(0, remaining - task.size);
  }
}

function claimTaskBatch(): QueueTask[] {
  const selected: QueueTask[] = [];
  let selectedBytes = 0;
  for (const task of tasks.value) {
    if (task.status !== "queued" || !task.file || task.reserved) continue;
    if (selected.length >= MOLOP_BATCH_MAX_FILES) break;
    if (selected.length && selectedBytes + task.size > MOLOP_BATCH_MAX_BYTES) continue;
    task.reserved = true;
    selected.push(task);
    selectedBytes += task.size;
  }
  return selected;
}

async function runTaskBatch(selected: QueueTask[], runId: number): Promise<void> {
  if (!batch.value || !selected.length) return;
  try {
    while (runId === queueRunId && !queueCancelled.value) {
      for (const task of selected) {
        task.status = "uploading";
        task.attempt += 1;
        task.loaded = 0;
        task.error = "";
      }
      const controller = new AbortController();
      for (const task of selected) task.controller = controller;
      try {
        const items = await api.uploadBatchFiles(
          batch.value.id,
          selected.map((task) => ({ clientFileId: task.clientFileId, file: task.file! })),
          (loaded, total) => updateBatchProgress(selected, loaded, total),
          controller.signal,
        );
        const itemsByClientId = new Map(items.map((item) => [item.client_file_id, item]));
        for (const task of selected) {
          const item = itemsByClientId.get(task.clientFileId);
          if (!item) throw new Error(`批量上传未返回文件结果：${task.filename}`);
          applyItem(task, item);
        }
        return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          for (const task of selected) {
            task.status = "cancelled";
            task.error = "";
          }
          return;
        }
        const retryable = error instanceof ApiError
          && (error.status === 0 || error.status === 429 || error.status === 502 || error.status === 503 || error.status === 504);
        if (!retryable || selected.some((task) => task.attempt >= MAX_AUTOMATIC_ATTEMPTS)) {
          for (const task of selected) {
            task.status = "failed";
            task.error = error instanceof Error ? error.message : "上传失败";
          }
          return;
        }
        const attempt = Math.max(...selected.map((task) => task.attempt));
        const retrySeconds = error.retryAfterSeconds ?? Math.min(30, 2 ** (attempt - 1));
        for (const task of selected) {
          task.status = "queued";
          task.error = `${retrySeconds} 秒后自动重试`;
        }
        const retryAt = Date.now() + retrySeconds * 1000;
        while (
          Date.now() < retryAt
          && runId === queueRunId
          && !queuePaused.value
          && !queueCancelled.value
        ) {
          await new Promise((resolve) => window.setTimeout(resolve, Math.min(1000, retryAt - Date.now())));
        }
        if (queuePaused.value || queueCancelled.value || runId !== queueRunId) return;
      } finally {
        for (const task of selected) task.controller = null;
      }
    }
  } finally {
    for (const task of selected) {
      task.reserved = false;
      if (runId !== queueRunId && task.status === "uploading") task.status = "cancelled";
    }
  }
}

async function queueWorker(runId: number): Promise<void> {
  while (runId === queueRunId && !queuePaused.value && !queueCancelled.value) {
    const selected = claimTaskBatch();
    if (!selected.length) return;
    await runTaskBatch(selected, runId);
  }
}

async function startQueue(): Promise<void> {
  if (!canStart.value) return;
  setupError.value = "";
  queueError.value = "";
  try {
    const activeBatch = await ensureBatch();
    if (activeBatch.status === "paused") {
      batch.value = await api.updateUploadBatchStatus(activeBatch.id, "active");
    }
    queuePaused.value = false;
    queueCancelled.value = false;
    queueRunning.value = true;
    const runId = ++queueRunId;
    markUploadBatchActive(activeBatch.id);
    try {
      await Promise.all(Array.from({ length: concurrency.value }, () => queueWorker(runId)));
      if (runId === queueRunId && viewMounted) {
        await refreshBatch();
        await refreshRecentBatches();
        if (
          batch.value?.status === "completed"
          && batch.value.failed_count === 0
          && batch.value.cancelled_count === 0
        ) {
          // Keep the completed summary visible briefly, then reset the queue
          // for the next batch. The summary is rendered from the still-present
          // batch/task state so users can see the finished counts.
          const completedBatchId = batch.value.id;
          window.setTimeout(() => {
            if (viewMounted && batch.value?.id === completedBatchId) void newBatch();
          }, BATCH_COMPLETION_RESET_DELAY_MS);
        }
      }
    } finally {
      markUploadBatchInactive(activeBatch.id);
    }
  } catch (error) {
    queueError.value = error instanceof Error ? error.message : "队列启动失败";
  } finally {
    queueRunning.value = false;
  }
}

async function pauseQueue(): Promise<void> {
  if (!batch.value || queuePaused.value) return;
  queuePaused.value = true;
  try {
    batch.value = await api.updateUploadBatchStatus(batch.value.id, "paused");
  } catch (error) {
    queueError.value = error instanceof Error ? error.message : "暂停失败";
  }
}

async function cancelQueue(): Promise<void> {
  if (!batch.value || queueCancelled.value) return;
  queueCancelled.value = true;
  queuePaused.value = true;
  queueRunId += 1;
  for (const task of tasks.value) {
    if (task.status === "queued") task.status = "cancelled";
    task.controller?.abort();
  }
  try {
    batch.value = await api.cancelUploadBatch(batch.value.id);
    await refreshRecentBatches();
  } catch (error) {
    queueError.value = error instanceof Error ? error.message : "取消失败";
  }
}

async function retryTask(task: QueueTask): Promise<void> {
  if (!batch.value || !task.file || task.status !== "failed") return;
  try {
    const item = await api.retryUploadBatchItem(batch.value.id, task.clientFileId);
    applyItem(task, item);
    await refreshBatch();
    queueCancelled.value = false;
    await startQueue();
  } catch (error) {
    task.error = error instanceof Error ? error.message : "重试失败";
  }
}

async function retryFailed(): Promise<void> {
  if (!batch.value) return;
  try {
    batch.value = await api.retryFailedUploadBatchItems(batch.value.id);
    for (const task of tasks.value) {
      if (task.status === "failed" && task.file) {
        task.status = "queued";
        task.error = "";
      }
    }
    queueCancelled.value = false;
    await startQueue();
  } catch (error) {
    queueError.value = error instanceof Error ? error.message : "重试失败";
  }
}

function taskStatusLabel(status: UploadBatchItemStatus): string {
  return {
    queued: "等待",
    uploading: "上传中",
    succeeded: "完成",
    failed: "失败",
    cancelled: "取消",
  }[status];
}

function batchStatusLabel(status: UploadBatch["status"]): string {
  return { active: "进行中", paused: "已暂停", completed: "已结束", cancelled: "已取消" }[status];
}

async function newBatch(): Promise<void> {
  queueRunId += 1;
  for (const task of tasks.value) task.controller?.abort();
  batch.value = null;
  tasks.value = [];
  remoteItems.value = [];
  remoteMode.value = false;
  remoteTotal.value = 0;
  queuePage.value = 0;
  statusFilter.value = "all";
  queuePaused.value = false;
  queueCancelled.value = false;
  queueError.value = "";
  setupError.value = "";
  window.localStorage.removeItem("tricycle.lastUploadBatch");
  const query = { ...route.query };
  delete query.batch;
  await router.replace({ query });
}

function previousPage(): void {
  queuePage.value = Math.max(0, queuePage.value - 1);
}

function nextPage(): void {
  queuePage.value = Math.min(pageCount.value - 1, queuePage.value + 1);
}

watch(concurrency, (value) => {
  const normalized = Math.min(6, Math.max(1, Number(value) || 1));
  concurrency.value = normalized;
  window.localStorage.setItem("tricycle.uploadConcurrency", String(normalized));
});
watch(selectedProjectId, () => void refreshRecentBatches());
watch([statusFilter, queuePage], () => {
  if (remoteMode.value) void refreshRemoteItems();
});
watch(statusFilter, () => { queuePage.value = 0; });

onMounted(async () => {
  viewMounted = true;
  await refreshRecentBatches();
  const routeBatch = typeof route.query.batch === "string" ? route.query.batch : null;
  const lastBatch = window.localStorage.getItem("tricycle.lastUploadBatch");
  if (routeBatch) await openBatch(routeBatch);
  else if (lastBatch && recentBatches.value.some((item) => item.id === lastBatch)) await openBatch(lastBatch);
});

onBeforeUnmount(() => {
  viewMounted = false;
  if (preserveQueueOnUnmount) return;
  queueRunId += 1;
  for (const task of tasks.value) task.controller?.abort();
});
</script>

<template>
  <main class="upload-page">
    <header class="upload-page-header">
      <div>
        <span class="eyebrow">Artifact ingestion</span>
        <h1>批量文件上传</h1>
      </div>
      <div class="upload-page-header-actions">
        <button v-if="batch" class="command-button command-button-secondary" type="button" :disabled="queueRunning" @click="newBatch">
          <Plus :size="16" aria-hidden="true" />新建批次
        </button>
        <RouterLink class="command-button command-button-secondary" :to="{ name: 'artifacts', query: { project_id: selectedProjectId } }">
          <FileStack :size="16" aria-hidden="true" />文件目录
        </RouterLink>
      </div>
    </header>

    <section class="upload-setup" aria-labelledby="upload-setup-title">
      <div class="upload-settings">
        <header><h2 id="upload-setup-title">批次设置</h2><span v-if="batch">{{ shortId(batch.id) }}</span></header>
        <label>
          <span>项目</span>
          <select v-model="selectedProjectId" :disabled="settingsLocked" aria-label="上传项目">
            <option v-for="project in uploadProjects" :key="project.project_id" :value="project.project_id">
              {{ project.organization_name }} / {{ project.project_name }}
            </option>
          </select>
        </label>
        <label>
          <span>文件类型</span>
          <select v-model="artifactKind" :disabled="settingsLocked" aria-label="上传文件类型">
            <option value="calculation_output">计算输出</option>
            <option value="input">计算输入</option>
            <option value="workflow_manifest">Workflow manifest</option>
            <option value="auxiliary">辅助文件</option>
          </select>
        </label>
        <label>
          <span>并发数 <strong>{{ concurrency }}</strong></span>
          <input v-model.number="concurrency" type="range" min="1" max="6" step="1" aria-label="上传并发数">
        </label>
      </div>

      <div class="upload-metadata">
        <header><h2>共享元数据</h2><span>{{ metadataRows.filter((row) => row.key.trim()).length }} 项</span></header>
        <div class="metadata-rows">
          <div v-for="row in metadataRows" :key="row.id" class="metadata-row">
            <input v-model="row.key" :disabled="settingsLocked" type="text" maxlength="128" placeholder="键" aria-label="元数据键">
            <input v-model="row.value" :disabled="settingsLocked" type="text" maxlength="2048" placeholder="值" aria-label="元数据值">
            <button class="icon-button" type="button" title="删除元数据" aria-label="删除元数据" :disabled="settingsLocked" @click="removeMetadataRow(row.id)">
              <Trash2 :size="15" aria-hidden="true" />
            </button>
          </div>
        </div>
        <button class="command-button command-button-secondary metadata-add" type="button" :disabled="settingsLocked" @click="addMetadataRow">
          <Plus :size="15" aria-hidden="true" />添加字段
        </button>
      </div>

      <div class="upload-source">
        <header><h2>文件来源</h2><span>{{ tasks.length.toLocaleString() }} 个</span></header>
        <div class="upload-dropzone" :aria-busy="readingDrop" @dragover.prevent @drop.prevent="droppedFiles">
          <UploadCloud :size="25" aria-hidden="true" />
          <strong>拖放文件</strong>
          <span>{{ readingDrop ? "正在读取文件夹..." : formatBytes(totalBytes) }}</span>
        </div>
        <div class="upload-source-actions">
          <input ref="fileInput" class="sr-only" type="file" multiple @change="selectedFiles">
          <input ref="folderInput" class="sr-only" type="file" multiple webkitdirectory @change="selectedFiles">
          <button class="command-button command-button-secondary" type="button" title="可重复选择，后续选择会追加到队列" :disabled="settingsLocked" @click="fileInput?.click()">
            <Files :size="16" aria-hidden="true" />选择文件
          </button>
          <button class="command-button command-button-secondary" type="button" title="可重复选择多个文件夹，后续选择会追加到队列" :disabled="settingsLocked" @click="folderInput?.click()">
            <FolderOpen :size="16" aria-hidden="true" />选择文件夹
          </button>
        </div>
        <div v-if="remoteMode && batch?.status !== 'completed' && batch?.status !== 'cancelled'" class="reattach-files">
          <input ref="reattachFileInput" class="sr-only" type="file" multiple @change="reattachSelectedFiles">
          <input ref="reattachFolderInput" class="sr-only" type="file" multiple webkitdirectory @change="reattachSelectedFiles">
          <button class="command-button command-button-secondary" type="button" title="可重复选择，后续选择会追加到队列" :disabled="loadingBatch" @click="reattachFileInput?.click()">
            <Files :size="16" aria-hidden="true" />重新选择文件
          </button>
          <button class="command-button command-button-secondary" type="button" title="可重复选择多个文件夹，后续选择会追加到队列" :disabled="loadingBatch" @click="reattachFolderInput?.click()">
            <FolderOpen :size="16" aria-hidden="true" />重新选择文件夹
          </button>
          <button
            v-if="batch && batch.uploading_count && !isUploadBatchActive(batch.id)"
            class="command-button command-button-secondary"
            type="button"
            :disabled="loadingBatch"
            @click="recoverInterruptedUpload"
          >
            <RotateCcw :size="16" aria-hidden="true" />恢复中断上传
          </button>
        </div>
      </div>
    </section>

    <p v-if="setupError" class="inline-error upload-page-message" role="alert">{{ setupError }}</p>
    <p v-if="queueError" class="inline-error upload-page-message" role="alert">{{ queueError }}</p>

    <section class="upload-queue-workspace" aria-labelledby="upload-queue-title">
      <header class="upload-queue-toolbar">
        <div>
          <span class="eyebrow">Queue</span>
          <h2 id="upload-queue-title">上传队列</h2>
        </div>
        <div class="upload-queue-actions">
          <button v-if="!queueRunning" class="command-button" type="button" :disabled="!canStart" @click="startQueue">
            <Play :size="16" aria-hidden="true" />{{ batch?.status === "paused" ? "继续" : "开始上传" }}
          </button>
          <button v-else class="command-button command-button-secondary" type="button" :disabled="queuePaused" @click="pauseQueue">
            <Pause :size="16" aria-hidden="true" />暂停
          </button>
          <button v-if="batchCounters.failed && !remoteMode" class="command-button command-button-secondary" type="button" :disabled="queueRunning" @click="retryFailed">
            <RotateCcw :size="16" aria-hidden="true" />重试失败项
          </button>
          <button v-if="batch && batch.status !== 'completed' && batch.status !== 'cancelled'" class="icon-button" type="button" title="取消批次" aria-label="取消批次" @click="cancelQueue">
            <X :size="17" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div class="upload-progress-band">
        <div><strong>{{ batchCounters.succeeded.toLocaleString() }}</strong><span>完成</span></div>
        <div><strong>{{ batchCounters.failed.toLocaleString() }}</strong><span>失败</span></div>
        <div><strong>{{ batchCounters.cancelled.toLocaleString() }}</strong><span>取消</span></div>
        <div><strong>{{ batchCounters.total.toLocaleString() }}</strong><span>总计</span></div>
        <div class="upload-progress-total">
          <span>{{ remoteMode ? batchStatusLabel(batch?.status ?? "active") : `${overallPercent}% · ${formatBytes(uploadedBytes)}` }}</span>
          <progress :value="remoteMode ? batchCounters.succeeded : uploadedBytes" :max="remoteMode ? Math.max(1, batchCounters.total) : Math.max(1, totalBytes)"></progress>
        </div>
      </div>

      <div class="upload-list-controls">
        <label>
          <span class="sr-only">队列状态筛选</span>
          <select v-model="statusFilter" aria-label="队列状态筛选">
            <option value="all">全部状态</option>
            <option value="queued">等待</option>
            <option value="uploading">上传中</option>
            <option value="succeeded">完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">取消</option>
          </select>
        </label>
        <span>{{ filteredTotal.toLocaleString() }} 项 · 第 {{ queuePage + 1 }} / {{ pageCount }} 页</span>
        <button class="icon-button" type="button" title="上一页" aria-label="上一页" :disabled="queuePage === 0" @click="previousPage">
          <ChevronLeft :size="16" aria-hidden="true" />
        </button>
        <button class="icon-button" type="button" title="下一页" aria-label="下一页" :disabled="queuePage + 1 >= pageCount" @click="nextPage">
          <ChevronRight :size="16" aria-hidden="true" />
        </button>
        <button v-if="remoteMode" class="icon-button" type="button" title="刷新批次" aria-label="刷新批次" :disabled="loadingBatch" @click="refreshBatch().then(refreshRemoteItems)">
          <RefreshCw :size="16" aria-hidden="true" />
        </button>
      </div>

      <div class="upload-task-list" role="list" aria-label="上传文件状态">
        <div v-if="loadingBatch" class="table-loading">正在加载上传批次</div>
        <div v-else-if="!visibleTasks.length" class="compact-empty">队列为空</div>
        <div v-for="task in visibleTasks" v-else :key="task.clientFileId" class="upload-task-row" :class="`is-${task.status}`" role="listitem">
          <span class="upload-task-status" :title="taskStatusLabel(task.status)"></span>
          <div class="upload-task-name"><strong>{{ task.filename }}</strong><span>{{ task.relativePath }}</span></div>
          <span class="upload-task-size">{{ formatBytes(task.size) }}</span>
          <div class="upload-task-progress">
            <progress :value="task.status === 'succeeded' ? task.size : task.loaded" :max="Math.max(1, task.size)"></progress>
            <span>{{ taskStatusLabel(task.status) }}<template v-if="task.attempt"> · {{ task.attempt }} 次</template></span>
          </div>
          <span class="upload-task-error" :title="task.error">{{ task.error }}</span>
          <RouterLink
            v-if="task.artifactId"
            class="icon-button"
            :to="{ name: 'artifact-detail', params: { artifactId: task.artifactId }, query: route.query }"
            title="查看文件"
            aria-label="查看文件"
          >
            <FileStack :size="15" aria-hidden="true" />
          </RouterLink>
          <button v-else-if="task.status === 'failed' && task.file" class="icon-button" type="button" title="重试文件" aria-label="重试文件" :disabled="queueRunning" @click="retryTask(task)">
            <RotateCcw :size="15" aria-hidden="true" />
          </button>
          <span v-else class="upload-task-action-placeholder"></span>
        </div>
      </div>
    </section>

    <section class="upload-history" aria-labelledby="upload-history-title">
      <header><div><span class="eyebrow">Recent batches</span><h2 id="upload-history-title">最近批次</h2></div></header>
      <div class="upload-history-list">
        <button v-for="item in recentBatches" :key="item.id" type="button" :class="{ 'is-active': item.id === batch?.id }" @click="openBatch(item.id)">
          <span><strong>{{ shortId(item.id) }}</strong><small>{{ new Date(item.created_at).toLocaleString() }}</small></span>
          <span>{{ item.succeeded_count.toLocaleString() }} / {{ item.total_count.toLocaleString() }}</span>
          <span :class="`status-${item.status}`">{{ batchStatusLabel(item.status) }}</span>
        </button>
        <div v-if="!recentBatches.length" class="compact-empty">暂无上传批次</div>
      </div>
    </section>
  </main>
</template>
