// MailTriage service worker: Web Push only.
//
// Deliberately no fetch handler and no caching: the app sits behind Cloudflare
// Access, and a cached shell could pin a login page or outlive a session. The
// app is online-only; this worker exists for installability and notifications.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "MailTriage", {
      body: data.body || "",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      icon: "/icons/icon-192.png",
      badge: "/icons/badge-96.png",
      data: { url: typeof data.url === "string" && data.url.startsWith("/") ? data.url : "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const route = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const client = windows.find((c) => new URL(c.url).origin === self.location.origin);
      if (client) {
        await client.focus();
        client.postMessage({ type: "open", path: route });
        return;
      }
      // ?open= (not #route) so the link survives a Cloudflare Access re-login.
      const url = route === "/" ? "/" : `/?open=${encodeURIComponent(route)}`;
      await self.clients.openWindow(url);
    })(),
  );
});

self.addEventListener("pushsubscriptionchange", (event) => {
  // Best effort: the app also re-registers its subscription on every open.
  event.waitUntil(
    (async () => {
      const res = await fetch("/api/v1/push/vapid-public-key", { credentials: "same-origin" });
      if (!res.ok) return;
      const { public_key } = await res.json();
      const b64 = (public_key + "===".slice((public_key.length + 3) % 4))
        .replace(/-/g, "+")
        .replace(/_/g, "/");
      const key = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key,
      });
      await fetch("/api/v1/push/subscriptions", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
    })().catch(() => {}),
  );
});
