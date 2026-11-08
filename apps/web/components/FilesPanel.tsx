"use client";

import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Ingestion = {
  status: "pending" | "processing" | "completed" | "failed";
  error_code: string | null;
  error_message: string | null;
};

type FileItem = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  ingestion: Ingestion | null; // text extraction
  indexing: Ingestion | null; // chunking + embeddings (search index)
};

const POLL_MS = 3000;

type Display = { text: string; color: string; failed: Ingestion | null; active: boolean };

// A file is "Ready" only once it is both extracted AND indexed for search.
function describe(item: FileItem): Display {
  const { ingestion, indexing } = item;
  if (ingestion?.status === "failed")
    return { text: "Failed", color: "#b00020", failed: ingestion, active: false };
  if (ingestion?.status === "completed") {
    if (indexing?.status === "completed")
      return { text: "Ready", color: "#1b7f3b", failed: null, active: false };
    if (indexing?.status === "failed")
      return { text: "Indexing failed", color: "#b00020", failed: indexing, active: false };
    return { text: "Indexing…", color: "#8a6d00", failed: null, active: true };
  }
  if (ingestion?.status === "processing")
    return { text: "Processing…", color: "#8a6d00", failed: null, active: true };
  return { text: "Queued…", color: "#555", failed: null, active: true };
}

type Props = {
  /** API collection: "resumes" or "documents". */
  kind: "resumes" | "documents";
  title: string;
  /** Allowed extensions (lower-case, with dot). The server is the authority; this is UX only. */
  extensions: string[];
  maxMb: number;
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FilesPanel({ kind, title, extensions, maxMb }: Props) {
  const { authedRequest } = useAuth();
  const [items, setItems] = useState<FileItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const base = `/api/v1/${kind}`;

  const load = useCallback(async () => {
    try {
      const result = await authedRequest<{ items: FileItem[] }>(base);
      setItems(result.items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load your files.");
    }
  }, [authedRequest, base]);

  // Initial load (state is only set from the promise callbacks, never synchronously).
  useEffect(() => {
    let cancelled = false;
    authedRequest<{ items: FileItem[] }>(base)
      .then((result) => {
        if (cancelled) return;
        setItems(result.items);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load your files.");
      });
    return () => {
      cancelled = true;
    };
  }, [authedRequest, base]);

  // While any file is still being processed, refresh its status every few seconds.
  const hasActive = items?.some((i) => describe(i).active);
  useEffect(() => {
    if (!hasActive) return;
    const timer = setTimeout(() => {
      void load();
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [hasActive, items, load]);

  async function onRetry(item: FileItem) {
    setError(null);
    try {
      await authedRequest(`${base}/${item.id}/retry`, { method: "POST", body: {} });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Retry failed. Please try again.");
    }
  }

  async function onUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // allow re-selecting the same file later
    if (!file) return;

    const dot = file.name.lastIndexOf(".");
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (!extensions.includes(extension)) {
      setError(`Unsupported file type. Allowed: ${extensions.join(", ")}.`);
      return;
    }
    if (file.size > maxMb * 1024 * 1024) {
      setError(`The file is too large (maximum ${maxMb} MB).`);
      return;
    }

    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      await authedRequest(base, { method: "POST", formData: form });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function onDownload(item: FileItem) {
    setError(null);
    try {
      const blob = await authedRequest<Blob>(`${base}/${item.id}/file`, { asBlob: true });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = item.filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Download failed. Please try again.");
    }
  }

  async function onDelete(item: FileItem) {
    if (!window.confirm(`Delete "${item.filename}"? This cannot be undone.`)) return;
    setError(null);
    try {
      await authedRequest(`${base}/${item.id}`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed. Please try again.");
    }
  }

  return (
    <section aria-labelledby={`${kind}-title`} style={{ width: "100%", maxWidth: "40rem" }}>
      <h2 id={`${kind}-title`} style={{ marginBottom: "0.25rem" }}>
        {title}
      </h2>
      <p style={{ margin: "0 0 0.5rem", color: "#666", fontSize: "0.875rem" }}>
        Allowed: {extensions.join(", ")} (max {maxMb} MB)
      </p>

      <input
        ref={inputRef}
        type="file"
        accept={extensions.join(",")}
        onChange={onUpload}
        disabled={busy}
        aria-label={`Upload ${title.toLowerCase()}`}
      />
      {busy && <span style={{ marginLeft: "0.5rem" }}>Uploading…</span>}

      {error && (
        <p role="alert" style={{ color: "#b00020" }}>
          {error}
        </p>
      )}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p style={{ color: "#666" }}>Nothing uploaded yet.</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "0.5rem" }}>
          {items.map((item) => (
            <li
              key={item.id}
              style={{
                display: "flex",
                gap: "0.75rem",
                alignItems: "center",
                justifyContent: "space-between",
                borderBottom: "1px solid #ddd",
                paddingBottom: "0.5rem",
              }}
            >
              <span style={{ overflowWrap: "anywhere" }}>
                {item.filename}{" "}
                <span style={{ color: "#666", fontSize: "0.8rem" }}>
                  · {formatSize(item.size_bytes)} · {new Date(item.created_at).toLocaleDateString()}
                </span>
                <br />
                <span
                  data-testid="ingestion-status"
                  style={{ color: describe(item).color, fontSize: "0.85rem" }}
                >
                  {describe(item).text}
                  {describe(item).failed?.error_message ? ` — ${describe(item).failed?.error_message}` : ""}
                </span>
              </span>
              <span style={{ display: "flex", gap: "0.5rem", flexShrink: 0 }}>
                {describe(item).failed && (
                  <button type="button" onClick={() => onRetry(item)}>
                    Retry
                  </button>
                )}
                <button type="button" onClick={() => onDownload(item)}>
                  Download
                </button>
                <button type="button" onClick={() => onDelete(item)}>
                  Delete
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
