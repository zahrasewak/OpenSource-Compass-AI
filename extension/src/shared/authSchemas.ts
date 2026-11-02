import { z } from "zod";

// Kept apart from schemas.ts so the credential response shape is never bundled into the content script.
/** Response of /auth/exchange and /auth/refresh. These are credentials: they never leave the background worker. */
export const tokenSchema = z.object({
  access_token: z.string().min(1),
  refresh_token: z.string().min(1),
  token_type: z.string(),
  expires_in: z.number().int().positive(),
  refresh_expires_in: z.number().int().positive(),
  session_expires_at: z.string(),
});
export type TokenPair = z.infer<typeof tokenSchema>;
