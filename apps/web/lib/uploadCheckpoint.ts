// Best-effort IndexedDB checkpoint for resumable multipart uploads. Keyed by fileFingerprint, it
// remembers the R2 multipart (job_id + upload_id + part_size) and the ETags of the parts already
// landed, so an interrupted 5 GB upload can resume (across a page reload) instead of restarting.
//
// EVERY operation is best-effort: any IndexedDB failure (private-mode block, quota, no-IDB env,
// version error) degrades to null / no-op and NEVER throws into the upload path. Resume is a bonus,
// not a dependency — the upload itself works without it. That is why this module has no unit test
// (it's a thin IO shim, exercised by the E2E); the reconciliation LOGIC it feeds lives in the pure,
// tested uploadResilience helpers.

export type CompletedPart = { part_number: number; etag: string };

export type UploadCheckpoint = {
  job_id: string;
  upload_id: string;
  part_size: number;
  completed: CompletedPart[];
};

const DB_NAME = "quip-uploads";
const STORE = "checkpoints";
const DB_VERSION = 1;

/** Open (and lazily create) the object store. Resolves null on ANY failure so callers no-op. */
function openDb(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    try {
      if (typeof indexedDB === "undefined") {
        resolve(null);
        return;
      }
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE); // out-of-line keys
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}

/** Run `work` inside a transaction, resolving `fallback` on any error. Closes the db when done. */
function withStore<T>(
  mode: IDBTransactionMode,
  fallback: T,
  work: (store: IDBObjectStore) => IDBRequest | null,
): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve) => {
        if (!db) {
          resolve(fallback);
          return;
        }
        try {
          const tx = db.transaction(STORE, mode);
          const req = work(tx.objectStore(STORE));
          let value: T = fallback;
          if (req) req.onsuccess = () => (value = req.result as T);
          tx.oncomplete = () => {
            db.close();
            resolve(value);
          };
          tx.onerror = () => {
            db.close();
            resolve(fallback);
          };
          tx.onabort = () => {
            db.close();
            resolve(fallback);
          };
        } catch {
          try {
            db.close();
          } catch {
            /* ignore */
          }
          resolve(fallback);
        }
      }),
  );
}

/** The checkpoint for this file fingerprint, or null if none / on any IndexedDB error. */
export function loadCheckpoint(fingerprint: string): Promise<UploadCheckpoint | null> {
  return withStore<UploadCheckpoint | null>("readonly", null, (store) => store.get(fingerprint)).then(
    (v) => (v && typeof v === "object" ? v : null),
  );
}

/** Persist (overwrite) the whole checkpoint. Best-effort. */
export function saveCheckpoint(fingerprint: string, cp: UploadCheckpoint): Promise<void> {
  return withStore<void>("readwrite", undefined, (store) => store.put(cp, fingerprint));
}

/** Record one freshly-uploaded part's ETag, merging into the existing checkpoint (dedup by
 *  part_number). No-op if there is no checkpoint yet or IndexedDB is unavailable. Best-effort. */
export async function updatePart(fingerprint: string, part: CompletedPart): Promise<void> {
  const cp = await loadCheckpoint(fingerprint);
  if (!cp) return;
  const completed = cp.completed.filter((p) => p.part_number !== part.part_number);
  completed.push(part);
  completed.sort((a, b) => a.part_number - b.part_number);
  await saveCheckpoint(fingerprint, { ...cp, completed });
}

/** Drop the checkpoint (call on full success or a clean start-over). Best-effort. */
export function clearCheckpoint(fingerprint: string): Promise<void> {
  return withStore<void>("readwrite", undefined, (store) => store.delete(fingerprint));
}
