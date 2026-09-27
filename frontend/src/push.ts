// Service worker registration + Web Push subscription for this device.

import { delWithBody, get, post } from "./api";

export type PushState = "unsupported" | "needs-install" | "denied" | "off" | "on";

const supported = () =>
  "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

const isIos = () =>
  /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

const isStandalone = () =>
  window.matchMedia("(display-mode: standalone)").matches ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true;

function base64UrlToBytes(value: string) {
  const b64 = (value + "=".repeat((4 - (value.length % 4)) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function sameKey(a: ArrayBuffer | null, b: Uint8Array): boolean {
  if (!a || a.byteLength !== b.length) return false;
  const view = new Uint8Array(a);
  return view.every((v, i) => v === b[i]);
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.getRegistration();
  return (await reg?.pushManager.getSubscription()) ?? null;
}

export async function registerServiceWorker(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  try {
    await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    /* e.g. sw.js fetch redirected to the Access login — retried next load */
  }
}

export async function pushState(): Promise<PushState> {
  // iOS exposes Web Push only to Home Screen-installed web apps.
  if (isIos() && !isStandalone()) return "needs-install";
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  if (Notification.permission !== "granted") return "off";
  return (await currentSubscription()) ? "on" : "off";
}

export async function enablePush(): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notification permission was not granted");
  const reg = await navigator.serviceWorker.ready;
  const { public_key } = await get<{ public_key: string }>("/push/vapid-public-key");
  const key = base64UrlToBytes(public_key);
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options.applicationServerKey, key)) {
    // Made with a previous server key (e.g. after a config purge) — can't be reused.
    await sub.unsubscribe();
    sub = null;
  }
  sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  await post("/push/subscriptions", sub.toJSON());
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await delWithBody("/push/subscriptions", { endpoint: sub.endpoint });
  await sub.unsubscribe();
}

/** Re-register this device's subscription (idempotent) so a rotated or
 * server-side-dropped endpoint heals on the next app open. */
export async function syncPush(): Promise<void> {
  if (!supported() || Notification.permission !== "granted") return;
  const sub = await currentSubscription();
  if (sub) await post("/push/subscriptions", sub.toJSON());
}
