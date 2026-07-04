import { createBrowserClient } from "@supabase/ssr";
import { SUPABASE_ANON_KEY, SUPABASE_URL } from "./config";

// Concrete (non-generic) constructor so ReturnType<> below captures the *inferred*
// client type. Annotating with ReturnType<typeof createBrowserClient> directly would
// instantiate that generic to its defaults and collapse .auth.getUser() results to any.
function makeBrowserClient() {
  return createBrowserClient(SUPABASE_URL, SUPABASE_ANON_KEY);
}

let client: ReturnType<typeof makeBrowserClient> | undefined;

/** Browser Supabase client (Client Components). Call only when isSupabaseConfigured.
 *  Memoized to a single module-scope instance — callers (incl. authHeaders()/getAccessToken()
 *  on every poll tick) share one client/GoTrueClient instead of each constructing a new one. */
export function createSupabaseBrowserClient() {
  return (client ??= makeBrowserClient());
}
