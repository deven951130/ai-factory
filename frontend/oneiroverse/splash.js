// Oneiroverse 啟動動畫。用法：<body> 的第一個元素放 <script src=".../oneiroverse/splash.js"></script>。
// 同資料夾要有 crescent.png、o.png、wordmark.png（packaging/make_splash.py 從 Logo 原圖產生）。
// 重新整理 / 上一頁不會再播；點一下或按任意鍵可跳過。
(() => {
  const nav = performance.getEntriesByType?.("navigation")[0];
  if (nav && (nav.type === "reload" || nav.type === "back_forward")) return;
  const base = new URL(".", document.currentScript.src).href;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  // 圖層以 #06162b 為底色做 color-to-alpha，疊在這個顏色上才會和原圖一樣。
  const conic = "conic-gradient(from calc(352deg - var(--ov-sw)) at 51.1% 38.2%, transparent 0deg, #000 min(14deg, var(--ov-sw)), #000 var(--ov-sw), transparent var(--ov-sw))";

  const style = document.createElement("style");
  style.textContent = `
@property --ov-sw { syntax: "<angle>"; inherits: false; initial-value: 0deg; }
html.ov-lock { overflow: hidden; }
#ov-splash { position: fixed; inset: 0; z-index: 2147483647; display: grid; place-items: center; overflow: hidden;
  user-select: none; transition: opacity .7s ease;
  background: radial-gradient(ellipse 45% 40% at 80% 20%, rgba(70, 90, 160, .10), transparent 70%),
              radial-gradient(ellipse 75% 65% at 50% 46%, #06162b 0%, #051327 45%, #020b18 100%); }
#ov-splash.ov-out { opacity: 0; pointer-events: none; }
#ov-splash .ov-stars { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; }
#ov-splash .ov-logo { position: relative; width: min(560px, 78vw, calc(70vh * 732 / 518)); aspect-ratio: 732 / 518;
  transition: transform .7s cubic-bezier(.4, 0, .2, 1); }
#ov-splash.ov-out .ov-logo { transform: scale(1.05); }
#ov-splash .ov-logo > * { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; -webkit-user-drag: none; }
#ov-splash .ov-crescent { -webkit-mask-image: ${conic}; mask-image: ${conic}; }
#ov-splash .ov-crescent.ov-drawn { -webkit-mask-image: none; mask-image: none; }
#ov-splash .ov-o { transform-origin: 51.9% 34%; }
#ov-splash .ov-shine { mix-blend-mode: plus-lighter;
  background: linear-gradient(100deg, transparent 42%, rgba(235, 248, 255, .9) 50%, transparent 58%) 100% 0 / 300% 100% no-repeat;
  -webkit-mask: var(--ov-word) center / 100% 100% no-repeat; mask: var(--ov-word) center / 100% 100% no-repeat; }
#ov-splash.ov-play .ov-stars { animation: ov-fade 1.4s ease both; }
#ov-splash.ov-play .ov-crescent { animation: ov-fade .35s ease-out .15s both, ov-draw 1.3s cubic-bezier(.45, 0, .2, 1) .15s both,
  ov-pulse 1.1s ease-in-out 2.15s; }
#ov-splash.ov-play .ov-o { animation: ov-o 1s cubic-bezier(.2, .7, .2, 1) .95s both, ov-pulse 1.1s ease-in-out 2.15s; }
#ov-splash.ov-play .ov-word { animation: ov-word 1.1s cubic-bezier(.2, .7, .2, 1) 1.4s both; }
#ov-splash.ov-play .ov-shine { animation: ov-shine 1s ease-in-out 2.2s both; }
@keyframes ov-fade { from { opacity: 0 } to { opacity: 1 } }
@keyframes ov-draw { from { --ov-sw: 0deg } to { --ov-sw: 270deg } }
@keyframes ov-o { from { opacity: 0; transform: scale(.8); filter: blur(10px) brightness(1.8) }
  60% { opacity: 1; filter: blur(0) brightness(1.3) } to { opacity: 1; transform: none; filter: none } }
@keyframes ov-word { from { opacity: 0; clip-path: inset(0 50% 0 50%); filter: blur(6px); transform: translateY(8px) }
  to { opacity: 1; clip-path: inset(0); filter: none; transform: none } }
@keyframes ov-shine { from { opacity: 1; background-position: 100% 0 } to { opacity: 1; background-position: 0 0 } }
@keyframes ov-pulse { 50% { filter: brightness(1.3) } }
@media (prefers-reduced-motion: reduce) {
  #ov-splash .ov-crescent { -webkit-mask-image: none; mask-image: none; }
  #ov-splash.ov-play .ov-stars, #ov-splash.ov-play .ov-logo > :not(.ov-shine) { animation: ov-fade .4s ease both; }
  #ov-splash.ov-out .ov-logo { transform: none; }
}`;
  document.head.appendChild(style);

  const root = document.createElement("div");
  root.id = "ov-splash";
  root.setAttribute("role", "img");
  root.setAttribute("aria-label", "Oneiroverse");
  root.innerHTML = `<canvas class="ov-stars"></canvas><div class="ov-logo">
    <img class="ov-crescent" alt="" src="${base}crescent.png"><img class="ov-o" alt="" src="${base}o.png">
    <img class="ov-word" alt="" src="${base}wordmark.png"><div class="ov-shine"></div></div>`;
  root.querySelector(".ov-shine").style.setProperty("--ov-word", `url("${base}wordmark.png")`);
  const crescent = root.querySelector(".ov-crescent");
  crescent.addEventListener("animationend", (e) => e.animationName === "ov-draw" && crescent.classList.add("ov-drawn"));
  document.body.prepend(root);
  document.documentElement.classList.add("ov-lock");  // 動畫期間藏起底下頁面的捲軸

  let gone = false, raf = 0;
  const exit = (fast) => {
    if (gone) return;
    gone = true;
    // 淡出一開始就還原捲軸：版面寬度的變化發生在動畫層還不透明的時候，看不到跳動。
    document.documentElement.classList.remove("ov-lock");
    if (fast) root.style.transitionDuration = ".25s";
    root.classList.add("ov-out");
    setTimeout(() => { cancelAnimationFrame(raf); root.remove(); style.remove(); }, fast ? 300 : 800);
  };
  root.addEventListener("pointerdown", () => exit(true));
  addEventListener("keydown", () => exit(true), { once: true });
  setTimeout(() => exit(true), 8000);  // 圖片載入異常也不會一直擋住畫面

  const canvas = root.querySelector("canvas"), ctx = canvas.getContext("2d");
  const W = innerWidth, H = innerHeight, dpr = Math.min(devicePixelRatio || 1, 2);
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#cfe0ff";
  const stars = Array.from({ length: Math.round(W * H / 4500) }, () => ({
    x: Math.random() * W, y: Math.random() * H, r: Math.random() * 0.9 + 0.3,
    a: Math.random() * 0.55 + 0.1, p: Math.random() * 6.28, s: Math.random() * 2 + 0.6,
  }));
  const draw = (t) => {
    ctx.clearRect(0, 0, W, H);
    for (const s of stars) {
      ctx.globalAlpha = reduced ? s.a : s.a * (0.55 + 0.45 * Math.sin(t / 1000 * s.s + s.p));
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, 6.283);
      ctx.fill();
    }
    if (!reduced && !gone) raf = requestAnimationFrame(draw);
  };
  draw(0);

  const decoded = Promise.all([...root.querySelectorAll("img")].map((i) => i.decode().catch(() => {})));
  Promise.race([decoded, new Promise((r) => setTimeout(r, 1500))]).then(() => {
    root.classList.add("ov-play");
    if (!reduced) raf = requestAnimationFrame(draw);
    setTimeout(exit, reduced ? 1200 : 3100);
  });
})();
