(function () {
  if (window.__owfyWidgetLoaded) return;
  window.__owfyWidgetLoaded = true;

  const scriptEl = document.currentScript;
  const baseUrl = scriptEl
    ? new URL(scriptEl.src).origin
    : window.location.origin;

  let tenantSlug = "default";
  if (scriptEl) {
    tenantSlug = scriptEl.getAttribute("data-tenant") ||
      new URL(scriptEl.src).searchParams.get("tenant") ||
      "default";
  }

  const style = document.createElement("style");
  style.textContent = `
    #owfy-launcher {
      position: fixed; bottom: 22px; right: 22px; z-index: 99999;
      width: 54px; height: 54px; border-radius: 999px;
      background: #141413; color: #ffffff; border: 1px solid rgba(255,255,255,0.2);
      box-shadow: 0 10px 25px rgba(0,0,0,0.18);
      cursor: pointer; font-size: 22px; display: grid; place-items: center;
      transition: transform 0.15s ease, background 0.2s ease;
    }
    #owfy-launcher:hover { transform: scale(1.05); }
    #owfy-frame-wrap {
      position: fixed; bottom: 88px; right: 22px; z-index: 99999;
      width: 400px; max-width: calc(100vw - 32px);
      height: 600px; max-height: calc(100vh - 110px);
      border-radius: 16px; overflow: hidden;
      border: 1px solid #e6e4de;
      box-shadow: 0 18px 45px rgba(0,0,0,0.16);
      background: #faf9f6;
      display: none;
    }
    #owfy-frame-wrap.open { display: block; }
    #owfy-frame { width: 100%; height: 100%; border: none; }
  `;
  document.head.appendChild(style);

  const wrap = document.createElement("div");
  wrap.id = "owfy-frame-wrap";
  const iframe = document.createElement("iframe");
  iframe.id = "owfy-frame";
  iframe.src = `${baseUrl}/chat?tenant=${encodeURIComponent(tenantSlug)}`;
  iframe.title = "Customer Support Chat";
  wrap.appendChild(iframe);

  const btn = document.createElement("button");
  btn.id = "owfy-launcher";
  btn.setAttribute("aria-label", "Open Customer Support");
  btn.textContent = "💬";
  btn.onclick = function () {
    const isOpen = wrap.classList.toggle("open");
    btn.textContent = isOpen ? "✕" : "💬";
  };

  document.body.appendChild(wrap);
  document.body.appendChild(btn);

  // Fetch tenant customization to adjust widget accent color
  if (tenantSlug) {
    fetch(`${baseUrl}/api/public/tenant/${encodeURIComponent(tenantSlug)}`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data && data.widget_color) {
          btn.style.background = data.widget_color;
        }
      })
      .catch(() => {});
  }
})();
