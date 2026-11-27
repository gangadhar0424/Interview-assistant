// Windows-only inspection helpers (M-10, D-77): certificate preflight and Authenticode verification.
// They call PowerShell and signtool; the certificate password is passed through the child's
// environment (never on a command line, never printed).

import { execFile } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const POWERSHELL = "powershell.exe";
const PS_ARGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"];

async function powershell(script, env = {}) {
  const { stdout } = await run(POWERSHELL, [...PS_ARGS, script], {
    env: { ...process.env, ...env },
    maxBuffer: 4 * 1024 * 1024,
    windowsHide: true,
    timeout: 120_000,
  });
  return stdout.trim();
}

const CERT_SCRIPT = `
$ErrorActionPreference = 'Stop'
try {
  $pw = ConvertTo-SecureString $env:IA_PFX_PASSWORD -AsPlainText -Force
  $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:IA_PFX_PATH, $pw, 'EphemeralKeySet')
} catch {
  @{ error = 'unreadable' } | ConvertTo-Json -Compress
  exit 0
}
$eku = $c.Extensions | Where-Object { $_ -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] }
$code = $false
if ($eku) { $code = [bool]($eku.EnhancedKeyUsages | Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.3' }) }
$chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
$chain.ChainPolicy.RevocationMode = 'NoCheck'
$built = $chain.Build($c)
$status = @($chain.ChainStatus | ForEach-Object { $_.Status.ToString() })
@{
  notBefore = $c.NotBefore.ToUniversalTime().ToString('o')
  notAfter = $c.NotAfter.ToUniversalTime().ToString('o')
  hasPrivateKey = [bool]$c.HasPrivateKey
  codeSigningEku = $code
  chainValid = [bool]$built
  chainStatus = $status
} | ConvertTo-Json -Compress
`;

/**
 * Read a PFX certificate file. Returns { error: "unreadable" } for a wrong password or a corrupt file
 * (the two are deliberately not distinguished), else the facts `evaluateCertificate` needs.
 */
export async function inspectCertificate(pfxPath, password) {
  const out = await powershell(CERT_SCRIPT, { IA_PFX_PATH: pfxPath, IA_PFX_PASSWORD: password });
  const parsed = JSON.parse(out);
  if (parsed.error) return { error: parsed.error };
  return {
    notBefore: parsed.notBefore,
    notAfter: parsed.notAfter,
    hasPrivateKey: parsed.hasPrivateKey,
    codeSigningEku: parsed.codeSigningEku,
    chainValid: parsed.chainValid,
    chainStatus: Array.isArray(parsed.chainStatus) ? parsed.chainStatus : parsed.chainStatus ? [parsed.chainStatus] : [],
  };
}

const SIGNATURE_SCRIPT = `
$ErrorActionPreference = 'Stop'
$s = Get-AuthenticodeSignature -LiteralPath $env:IA_FILE
$signer = $null
if ($s.SignerCertificate) { $signer = @{ subject = $s.SignerCertificate.Subject; notAfter = $s.SignerCertificate.NotAfter.ToUniversalTime().ToString('o'); thumbprint = $s.SignerCertificate.Thumbprint } }
@{ status = $s.Status.ToString(); hasTimestamp = [bool]$s.TimeStamperCertificate; signer = $signer } | ConvertTo-Json -Compress -Depth 4
`;

/** Newest signtool.exe from the Windows SDK, or from SIGNTOOL_PATH; null when none is installed. */
export function findSigntool(env = process.env) {
  if (env.SIGNTOOL_PATH && existsSync(env.SIGNTOOL_PATH)) return env.SIGNTOOL_PATH;
  const root = join(env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)", "Windows Kits", "10", "bin");
  if (!existsSync(root)) return null;
  const versions = readdirSync(root).filter((d) => /^10\./.test(d)).sort().reverse();
  for (const version of versions) {
    const candidate = join(root, version, "x64", "signtool.exe");
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

/** Digest algorithm of the primary signature from `signtool verify /pa /v` output. */
export function parseSigntoolDigest(output) {
  const match = /Hash of file \((\w+)\)/i.exec(output);
  return match ? match[1].toLowerCase() : null;
}

/**
 * Authenticode facts for one file: status, timestamp, signer expiry and (when signtool is available)
 * the digest algorithm.
 */
export async function inspectSignature(file, env = process.env) {
  const parsed = JSON.parse(await powershell(SIGNATURE_SCRIPT, { IA_FILE: file }));
  let digestAlgorithm = null;
  if (parsed.status !== "NotSigned") {
    const signtool = findSigntool(env);
    if (signtool) {
      try {
        const { stdout } = await run(signtool, ["verify", "/pa", "/v", file], { windowsHide: true, timeout: 120_000 });
        digestAlgorithm = parseSigntoolDigest(stdout);
      } catch (error) {
        // signtool exits non-zero for an invalid signature but still prints the details.
        digestAlgorithm = parseSigntoolDigest(String(error?.stdout ?? ""));
      }
    }
  }
  return {
    status: parsed.status,
    hasTimestamp: parsed.hasTimestamp === true,
    signerNotAfter: parsed.signer?.notAfter,
    signerSubject: parsed.signer?.subject ?? null,
    signerThumbprint: parsed.signer?.thumbprint ?? null,
    digestAlgorithm,
  };
}

/** Product and file version from the executable's version resource. */
export async function inspectVersionInfo(file) {
  const script = `$v = (Get-Item -LiteralPath $env:IA_FILE).VersionInfo; @{ productName = $v.ProductName; fileVersion = $v.FileVersion; productVersion = $v.ProductVersion; companyName = $v.CompanyName } | ConvertTo-Json -Compress`;
  return JSON.parse(await powershell(script, { IA_FILE: file }));
}
