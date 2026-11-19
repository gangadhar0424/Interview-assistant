import { join } from "node:path";
import { SettingsStore } from "../src/main/settings-store";

/** A settings store in `dir` whose user has already accepted the current acceptable-use notice. */
export function acknowledgedSettings(dir: string, userId: string): SettingsStore {
  const store = new SettingsStore(join(dir, "settings.json"));
  store.acknowledge(userId, "2026-11-18T09:00:00.000Z");
  return store;
}
