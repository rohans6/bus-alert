// Minimal service worker: just enough to receive push notifications and
// let the app be "installed" to the home screen.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = { title: "A2 Bus Alert", body: "Bus timing updated." };
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    // ignore malformed payloads
  }

  const options = {
    body: data.body,
    icon: "icon-192.png",
    badge: "icon-192.png",
    vibrate: [120, 60, 120],
    tag: "bus-alert", // replaces older notifications instead of stacking
    renotify: true,
    data: { url: "./index.html" },
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(event.notification.data?.url || "./index.html");
      }
    })
  );
});
