/** Local recovery is separate from the server draft and the published LIVE. */
const DATABASE = 'kid-te-studio-recovery';
const STORE = 'snapshots';
const MAX_AGE_MS = 30 * 86400000;
const LIMIT = 20;
let opening;
let generation = 0;

function database() {
  if (!opening) opening = new Promise((resolve, reject) => {
    const token = ++generation;
    let settled = false;
    const request = indexedDB.open(DATABASE, 2);
    request.onupgradeneeded = () => {
      const store = request.result.objectStoreNames.contains(STORE)
        ? request.transaction.objectStore(STORE) : request.result.createObjectStore(STORE, {keyPath: 'snapshot_id'});
      if (!store.indexNames.contains('document_key')) store.createIndex('document_key', 'document_key');
      if (!store.indexNames.contains('saved_at_ms')) store.createIndex('saved_at_ms', 'saved_at_ms');
    };
    request.onsuccess = () => {
      const db = request.result;
      if (settled || token !== generation) {db.close(); return;}
      settled = true;
      db.onversionchange = () => {db.close(); if (token === generation) opening = null;};
      resolve(db);
    };
    request.onerror = () => {settled = true; if (token === generation) opening = null; reject(request.error);};
    request.onblocked = () => {settled = true; if (token === generation) opening = null; reject(new Error('recovery_database_blocked'));};
  });
  return opening;
}

export async function saveRecovery(documentKey, snapshot) {
  const db = await database();
  const row = {...snapshot, document_key: documentKey, saved_at_ms: Date.now(),
    snapshot_id: crypto.randomUUID()};
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const expired = store.index('saved_at_ms').openCursor(IDBKeyRange.upperBound(row.saved_at_ms - MAX_AGE_MS, true));
    expired.onsuccess = () => {const cursor = expired.result; if (cursor) {cursor.delete(); cursor.continue();}};
    const request = store.index('document_key').getAll(documentKey);
    request.onsuccess = () => {
      const existing = request.result;
      const same = existing.filter(old => old.document_key === documentKey && old.saved_at_ms >= row.saved_at_ms - MAX_AGE_MS);
      same.sort((a, b) => b.saved_at_ms - a.saved_at_ms);
      for (const old of same.slice(LIMIT - 1)) store.delete(old.snapshot_id);
      store.put(row);
    };
    tx.oncomplete = () => resolve(row);
    tx.onabort = () => reject(tx.error || new Error('recovery_save_failed'));
    tx.onerror = () => {}; // onabort carries quota/IO errors to the UI.
  });
}

export async function listRecovery(documentKey) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const request = tx.objectStore(STORE).index('document_key').getAll(documentKey);
    let rows = [];
    request.onsuccess = () => {rows = request.result.filter(row => row.saved_at_ms >= Date.now() - MAX_AGE_MS)
      .sort((a, b) => b.saved_at_ms - a.saved_at_ms).slice(0, LIMIT);};
    tx.oncomplete = () => resolve(rows);
    tx.onabort = () => reject(tx.error || new Error('recovery_read_failed'));
  });
}
