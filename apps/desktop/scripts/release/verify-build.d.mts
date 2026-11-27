export const DESKTOP_ROOT: string;
export function verifyBuild(options: { root?: string; requireSigned: boolean; now?: Date; env?: Record<string, string | undefined> }): Promise<{
  ok: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
  warnings: string[];
  installer: string | null;
}>;
export function checkSums(root?: string): Promise<string[]>;
