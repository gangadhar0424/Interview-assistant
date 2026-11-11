// Request body for POST /api/v1/auth/signup (Phase 13A Batch 2). The invitation code is sent only
// when the user typed one, trimmed; an empty field means "none" (the server decides if it is needed).

export interface SignupBody {
  email: string;
  password: string;
  invite_code?: string;
}

export function buildSignupBody(email: string, password: string, inviteCode?: string): SignupBody {
  const code = inviteCode?.trim();
  return code ? { email, password, invite_code: code } : { email, password };
}
