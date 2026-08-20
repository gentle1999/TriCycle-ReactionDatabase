const activeBatchIds = new Set<string>();

export function markUploadBatchActive(batchId: string): void {
  activeBatchIds.add(batchId);
}

export function markUploadBatchInactive(batchId: string): void {
  activeBatchIds.delete(batchId);
}

export function isUploadBatchActive(batchId: string): boolean {
  return activeBatchIds.has(batchId);
}
