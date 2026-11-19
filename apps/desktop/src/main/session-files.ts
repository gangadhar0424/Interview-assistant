import { app, dialog, type BrowserWindow } from "electron";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { SessionFiles } from "./ipc";

/**
 * The native dialogs behind session delete and export (M-9). They live in the main process so a
 * compromised renderer can neither skip the confirmation nor choose where a file is written.
 */
export function createSessionFiles(window: BrowserWindow): SessionFiles {
  return {
    async confirmDelete() {
      const result = await dialog.showMessageBox(window, {
        type: "warning",
        title: "Delete this session?",
        message: "Delete this session and its question history?",
        detail:
          "The session, its questions and its answers are removed from your account and cannot be " +
          "recovered. Your resume and documents are not deleted.",
        buttons: ["Delete session", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      return result.response === 0;
    },

    async chooseExportFile(defaultName, format) {
      const confirm = await dialog.showMessageBox(window, {
        type: "warning",
        title: "Export this session?",
        message: "Save this session to a file on this computer?",
        detail:
          "The file will contain the session details, the job description you entered, your " +
          "answer preferences, and every question and answer in this session, as readable text. " +
          "It will not contain your resume or document text, audio, screenshots, passwords, " +
          "tokens or API keys.\n\n" +
          "Anyone who can open the file can read it, and it is not removed when you delete the " +
          "session. Nothing is sent anywhere.",
        buttons: ["Choose where to save", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      if (confirm.response !== 0) return null;
      const chosen = await dialog.showSaveDialog(window, {
        title: "Save session export",
        defaultPath: join(app.getPath("documents"), defaultName),
        filters:
          format === "markdown"
            ? [{ name: "Markdown", extensions: ["md"] }]
            : [{ name: "JSON", extensions: ["json"] }],
      });
      return chosen.canceled || !chosen.filePath ? null : chosen.filePath;
    },

    async writeFile(path, text) {
      await writeFile(path, text, { encoding: "utf8", mode: 0o600 });
    },
  };
}
