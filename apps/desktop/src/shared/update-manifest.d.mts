export const MAX_MANIFEST_BYTES: number;
export const MAX_INSTALLER_BYTES: number;
export const MIN_FREE_BYTES_MARGIN: number;
export type Semver = { major: number; minor: number; patch: number; pre: string[] };
export type LatestFile = { url: string; sha512: string; size: number };
export type LatestManifest = {
  version: string;
  path: string;
  sha512: string;
  releaseDate: string | null;
  files: LatestFile[];
};
export function parseSemver(value: unknown): Semver | null;
export function compareSemver(a: string, b: string): -1 | 0 | 1 | null;
export function isSafeArtifactName(name: unknown): boolean;
export function parseLatestYml(text: string): LatestManifest;
export function validateManifest(
  manifest: LatestManifest,
  options: { currentVersion: string; allowPrerelease?: boolean },
): { errors: string[]; newer: boolean; version: string | null };
export type AppUpdateConfig = { provider: string; url: string; publisherName: string[] };
export function parseAppUpdateYml(text: string): AppUpdateConfig;
export function evaluateUpdateConfig(config: AppUpdateConfig): {
  enabled: boolean;
  reason: "not_configured" | "insecure_feed" | "unsigned_build" | null;
  feedUrl: string | null;
};
export function requiredFreeBytes(installerSize: number): number;
export const MANIFEST_SIGNATURE_DOMAIN: string;
export const MANIFEST_SIGNATURE_FILE: string;
export const MANIFEST_KEY_FILE: string;
export function parseManifestPublicKey(text: unknown): object | null;
export function verifyManifestSignature(manifestText: string, signatureText: unknown, publicKeyText: unknown): boolean;
export function signManifest(manifestText: string, privateKeyPem: string): string;
export function generateManifestKeyPair(): { publicKey: string; privateKeyPem: string };
export function publicKeyOfPrivate(privateKeyPem: string): string | null;
