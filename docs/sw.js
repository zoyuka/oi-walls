// Levels service worker — push relay only. Deliberately NO fetch handler:
// the page rebuilds every ~10 minutes and must never fight an offline cache.
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data.json(); }
  catch (err) { d = { title: "Levels", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Levels", {
    body: d.body || "",
    icon: "icon.png",
    badge: "icon.png",
    tag: d.tag || undefined,
    data: { url: d.url || "./" }
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil((async () => {
    const wins = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if ("focus" in w) { try { return await w.focus(); } catch (err) {} }
    }
    return clients.openWindow((e.notification.data && e.notification.data.url) || "./");
  })());
});
