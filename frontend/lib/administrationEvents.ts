export const ADMIN_CHANGE_EVENT = "workmate:administration-changed";
export const ADMIN_CHANGE_KEY = "workmate_admin_revision";

/** A refresh hint, never an authorization fact or account data cache. */
export function notifyAdministrationChanged() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(ADMIN_CHANGE_EVENT));
  try { localStorage.setItem(ADMIN_CHANGE_KEY, `${Date.now()}:${Math.random()}`); }
  catch { /* Storage restrictions must not turn a committed write into a failure. */ }
}
