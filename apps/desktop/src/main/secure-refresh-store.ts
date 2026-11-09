import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";

export type SafeStoragePort = {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer;
  decryptString(value: Buffer): string;
};

export class SecureRefreshTokenStore {
  constructor(
    private readonly filePath: string,
    private readonly safeStorage: SafeStoragePort,
  ) {}

  load(): string | null {
    if (!existsSync(this.filePath)) return null;
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error("Windows secure storage is unavailable. Sign-in data cannot be opened.");
    }
    try {
      const encrypted = readFileSync(this.filePath);
      const token = this.safeStorage.decryptString(encrypted);
      return token.length > 0 ? token : null;
    } catch (error) {
      throw new Error("Could not read protected sign-in data.", { cause: error });
    }
  }

  save(token: string): void {
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error("Windows secure storage is unavailable. Sign-in data was not saved.");
    }
    const encrypted = this.safeStorage.encryptString(token);
    const tempPath = join(dirname(this.filePath), `.refresh-${randomUUID()}.tmp`);
    try {
      writeFileSync(tempPath, encrypted, { mode: 0o600 });
      renameSync(tempPath, this.filePath);
    } catch (error) {
      throw new Error("Could not save protected sign-in data.", { cause: error });
    }
  }

  clear(): void {
    try {
      unlinkSync(this.filePath);
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "ENOENT") return;
      throw new Error("Could not clear protected sign-in data.", { cause: error });
    }
  }
}
