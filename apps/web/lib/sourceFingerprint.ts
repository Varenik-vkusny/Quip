import { createSHA256 } from "hash-wasm";

/**
 * Per-user upload-dedup fingerprint of a file: `<size-in-bytes>:<sha256-hex-lowercase>`.
 *
 * Matches the server contract exactly (`dedup.is_valid_fingerprint`: `^\d{1,20}:[0-9a-f]{64}$`) so
 * the same file uploaded twice by the same user produces the SAME string → the server reuses the R2
 * source instead of re-transferring the bytes.
 *
 * Why streamed, not `crypto.subtle.digest`: Web Crypto's digest is one-shot — it needs the whole
 * file buffered in memory, which OOMs on multi-GB videos. We read the file in bounded slices and feed
 * them to a WASM SHA-256 (incremental), so peak memory stays at one chunk regardless of file size.
 *
 * `onProgress(0..1)` drives the "checking…" UI; the hash runs BEFORE upload (that's how a match skips
 * the transfer), so surfacing progress keeps a large-file check from looking frozen.
 */
export async function computeSourceFingerprint(
  file: File,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  const hasher = await createSHA256();
  hasher.init();
  const CHUNK = 8 * 1024 * 1024; // 8 MB: bounds peak memory, few enough read calls to stay fast
  let offset = 0;
  while (offset < file.size) {
    if (signal?.aborted) throw new DOMException("Fingerprint aborted", "AbortError");
    const end = Math.min(offset + CHUNK, file.size);
    hasher.update(new Uint8Array(await file.slice(offset, end).arrayBuffer()));
    offset = end;
    onProgress?.(file.size > 0 ? offset / file.size : 1);
  }
  return `${file.size}:${hasher.digest("hex")}`;
}
