/**
 * Entra ID sign-in, browser side.
 *
 * The API is a resource server and stays one: it validates tokens and never
 * issues them, so obtaining a token is entirely this file's job. Authorization
 * code + PKCE through MSAL, as a public client — there is no secret here and
 * there must never be one, since anything shipped to the browser is public.
 *
 * This needs its own app registration in Entra, of type SPA, separate from the
 * one the API uses as its audience. Its redirect URI is this app's own origin.
 */
import {
  PublicClientApplication,
  InteractionRequiredAuthError,
  type AccountInfo,
} from "@azure/msal-browser";

export type AuthConfig = {
  tenantId: string;
  clientId: string;
  /** The API's own client id — the audience we need a token *for*. */
  apiClientId: string;
};

let msal: PublicClientApplication | null = null;
let scopes: string[] = [];

export async function initAuth(config: AuthConfig): Promise<AccountInfo | null> {
  msal = new PublicClientApplication({
    auth: {
      clientId: config.clientId,
      authority: `https://login.microsoftonline.com/${config.tenantId}`,
      redirectUri: window.location.origin,
    },
    cache: {
      // sessionStorage, not localStorage: the token dies with the tab instead
      // of sitting on the origin for any later script to read.
      //
      // Not memoryStorage either, tempting as it is. A redirect flow leaves the
      // page entirely and comes back, and the PKCE code verifier and state have
      // to survive that round trip — in memory they die with the page, and the
      // sign-in fails after the redirect with nothing to show for it.
      cacheLocation: "sessionStorage",
      storeAuthStateInCookie: false,
    },
  });
  await msal.initialize();

  scopes = [`api://${config.apiClientId}/.default`];

  const result = await msal.handleRedirectPromise();
  if (result?.account) {
    msal.setActiveAccount(result.account);
    return result.account;
  }
  const [account] = msal.getAllAccounts();
  if (account) {
    msal.setActiveAccount(account);
    return account;
  }
  return null;
}

/**
 * Start the redirect sign-in. Rejects rather than failing silently.
 *
 * The caller must surface the error. A sign-in button that swallows its own
 * failure is the worst thing this file can do: nothing moves, nothing is
 * logged, and there is no way to tell a misconfiguration from a dead click.
 */
export async function signIn(): Promise<void> {
  if (!msal) throw new Error("autenticação não inicializada");
  // select_account because signing out is local: the Microsoft session survives
  // it, so without a prompt the next click would silently return the same
  // account and "Sair" would look like it did nothing. This is also the only
  // way to switch users on a shared machine.
  await msal.loginRedirect({ scopes, prompt: "select_account" });
}

/**
 * Leave this app, and only this app.
 *
 * `onRedirectNavigate: () => false` clears MSAL's cache but cancels the trip to
 * Microsoft's logout endpoint. Without it, signing out of an internal document
 * tool would end the user's Entra session across the whole browser — Office,
 * Teams, everything — and greet them with a "choose an account to sign out"
 * page they never asked for.
 *
 * There is no navigation, so the caller renders the signed-out view itself.
 */
export async function signOut(): Promise<void> {
  await msal?.logoutRedirect({ onRedirectNavigate: () => false });
}

export function account(): AccountInfo | null {
  return msal?.getActiveAccount() ?? null;
}

/**
 * A valid access token, renewed silently when it has expired.
 *
 * Called before every request rather than once at start-up: answers take
 * seconds and conversations last much longer than a token does, so a token
 * captured at sign-in would expire mid-session.
 */
export async function accessToken(): Promise<string> {
  if (!msal) throw new Error("auth not initialised");
  const activeAccount = msal.getActiveAccount();
  if (!activeAccount) throw new Error("not signed in");
  try {
    const result = await msal.acquireTokenSilent({
      scopes,
      account: activeAccount,
    });
    return result.accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      // Consent revoked, password changed, conditional access — none of which
      // a silent call can resolve. Hand it back to Entra.
      await msal.acquireTokenRedirect({ scopes, account: activeAccount });
    }
    throw error;
  }
}
