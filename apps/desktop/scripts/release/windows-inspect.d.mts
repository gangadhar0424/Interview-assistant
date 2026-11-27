type Env = Record<string, string | undefined>;

/** The child environment for Windows PowerShell 5.1, without any (PowerShell 7) PSModulePath. */
export function powershellEnv(base: Env, extra?: Env): Env;
