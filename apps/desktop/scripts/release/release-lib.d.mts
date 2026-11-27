export const DEFAULT_TIMESTAMP_URL: string;
export const MIN_CERT_VALID_DAYS_WARNING: number;
export const X64_MACHINE: number;
export const REQUIRED_FUSES: Readonly<Record<string, boolean>>;
export const REQUIRED_UNPACKED_FILES: readonly string[];

export type Env = Record<string, string | undefined>;

export function isValidSemver(value: string): boolean;
export function signingRequired(env: Env): boolean;
export function resolveSigningMode(env: Env): { mode: "pfx" | "azure" | "none"; problems: string[] };
export function resolveTimestampUrl(env: Env): string;
export function resolveFeedUrl(env: Env): string | null;
export function resolvePublisherName(env: Env): string | null;
export function resolveManifestPublicKey(env: Env): string | null;
export function scrubbedEnv(env: Env): Env;
export function commonNameOf(subject: string | null | undefined): string | null;
export function signerConsistencyErrors(
  installerSubject: string | null | undefined,
  appSubject: string | null | undefined,
  publisherName: string | null | undefined,
): string[];
export function sha512Base64File(path: string): Promise<string>;
export function preflight(
  env: Env,
  packageVersion: string,
): {
  ok: boolean;
  kind: "signed" | "unsigned-internal";
  mode: string;
  errors: string[];
  warnings: string[];
};

export type CertificateInfo = {
  notBefore: string;
  notAfter: string;
  hasPrivateKey: boolean;
  codeSigningEku: boolean;
  chainValid: boolean;
  chainStatus?: string[];
};
export function evaluateCertificate(cert: CertificateInfo, now: Date): { errors: string[]; warnings: string[] };

export type SignatureInfo = {
  status: string;
  signerNotAfter?: string;
  hasTimestamp: boolean;
  digestAlgorithm?: string | null;
};
export function evaluateSignature(
  info: SignatureInfo,
  options: { requireSigned: boolean; now: Date },
): { errors: string[]; warnings: string[] };

export function fileVersionMatches(fileVersion: string | null | undefined, version: string): boolean;
export function peMachine(buffer: Uint8Array | Buffer): number | null;
export function sha256File(path: string): Promise<string>;
export function formatSums(entries: { name: string; sha256: string }[]): string;
export function parseSums(text: string): { name: string; sha256: string }[];
export function compareSums(
  expected: { name: string; sha256: string }[],
  actualByName: Record<string, string | undefined>,
): string[];
export function evaluateFuses(actual: Record<string, boolean>): string[];
export function missingFiles(present: string[]): string[];
export function expectedInstallerName(productName: string, version: string): string;
export function interruptHandler(child: { kill: () => void } | null, log: (message: string) => void): (signal: string) => number;
export function cleanReleaseDir(root: string): void;
export function builderConfig(
  pkgBuild: Record<string, unknown>,
  env: Env,
  mode: "pfx" | "azure" | "none",
  manifestKeyFile?: string | null,
): { win: Record<string, unknown> } & Record<string, unknown>;
