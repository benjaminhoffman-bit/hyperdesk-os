/* SilvaDes house ads (neutral names: blocker lists hide /ads.js and #ad... ids), only in a shared copy (status "ads"): one card in the sidebar, another on the empty page.
   Plain links and nothing else: no network calls, images or storage; silvades.com links carry utm tags saying which card. */
(() => {
  "use strict";
  const ADS = [  // Ben's approved copy, word for word
    {id: "custom-parts", tag: "Custom parts", title: "Need a part nobody makes?", body: "I design it in CAD and 3D print it. Quoted per part, not per hour.", cta: "Get a quote", url: "https://silvades.com/contact#custom-parts"},
    {id: "pa6gf", tag: "Materials", title: "PA6-GF is my flagship material", body: "Same material family as OEM under-hood parts. I also print PA6, PETG-CF and TPU, on enclosed Bambu Lab X1 Carbon printers.", cta: "Start a part", url: "https://silvades.com/contact#custom-parts"},
    {id: "t6-intake", tag: "Volvo T6", title: "T6 cold air intake · $139", body: "Stock mounting points, no cutting. Keeps every factory emissions hose connected.", fine: "No CARB EO number; not legal for sale or use in California.", cta: "See the install guide", url: "https://silvades.com/updates-blog/volvo-t6-cold-air-intake-install"},
    {id: "photo", tag: "Car photo & video", title: "Photo and video of your car", body: "Reel $75 · Feature $150 · Showcase $250. Based in Washington, IL.", cta: "Book a shoot", url: "https://silvades.com/contact#photography"},
    {id: "aerial", tag: "Aerial", title: "Drone photo and video", body: "FAA Part 107 certified. Aerial stills from $225, photo + motion from $375.", cta: "Plan a flight", url: "https://silvades.com/contact#aerial"},
    {id: "e550", tag: "Mercedes E550", title: "E550 intake tubes · $79/pair", body: "Version 1. Long-term testing is in progress.", cta: "silvades.com", url: "https://silvades.com/"},
    {id: "made-by", tag: "Made by SilvaDes", title: "I built HyperDesk", body: "SilvaDes does custom parts, car photo and video, and aerial work out of Washington, IL.", cta: "silvades.com", url: "https://silvades.com/"},
    {id: "tiktok", tag: "Follow the builds", title: "Parts, installs and shoots", body: "On TikTok and Instagram: @silvadesllc.", cta: "Open TikTok", url: "https://www.tiktok.com/@silvadesllc"},
  ];
  const MARK = '<svg class="sdc-mark" viewBox="0 0 1339 737" aria-hidden="true" focusable="false"><polygon points="126,0 509,0 647,138 647,194 193,194 193,275 535,275 664,404 664,620 547,737 156,737 0,581 0,531 487,531 487,457 135,457 0,322 0,126"/><polygon points="689,0 1165,0 1339,174 1339,567 1169,737 702,737 702,428 834,560 1077,560 1145,492 1145,261 1077,193 874,193 874,523 854,523 689,358"/></svg>';
  const CSS = `
#sdside { padding: 8px; flex: none }
#sdhello { margin-top: 24px }
.sdc { display: flex; gap: 20px; align-items: center; padding: 18px 20px; background: linear-gradient(110deg, #111a0e, var(--raise) 65%); border: 1px solid #39ff1433; border-radius: var(--r); color: var(--text); text-decoration: none; transition: border-color var(--ease) }
.sdc:hover, .sdc:focus-visible { border-color: var(--accent) }
.sdc:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }
.sdc-mark { width: 72px; height: auto; flex: none; fill: currentColor; color: var(--accent) }
.sdc-copy { display: flex; flex-direction: column; gap: 3px; min-width: 0 }
.sdc-top { display: flex; flex-wrap: wrap; gap: 0 10px; font: 500 10px/1.6 var(--mono); letter-spacing: 1.5px; text-transform: uppercase }
.sdc-label { color: var(--accent) } .sdc-tag { color: var(--muted) }
.sdc-title { font: 600 18px/1.25 var(--disp); letter-spacing: .3px }
.sdc-body { color: var(--muted); font: 13px/1.5 var(--sans) }
.sdc-fine { color: var(--muted); font: 11.5px/1.45 var(--sans) }
.sdc-cta { margin-top: 4px; font: 500 11px/1.5 var(--mono); letter-spacing: 1px; text-transform: uppercase; color: var(--accent) }
#sdside .sdc { display: block; position: relative; padding: 12px }
#sdside .sdc-mark { position: absolute; top: 13px; right: 12px; width: 26px }
#sdside .sdc-top { padding-right: 32px }
#sdside .sdc-title { font-size: 15px; margin: 2px 0 }
#sdside .sdc-body { font-size: 12px; line-height: 1.45 }
#sdside .sdc-fine { font-size: 11px }
@media (max-height: 600px) { #sdside { display: none } }  /* a short window keeps the sidebar for conversations */
@media (prefers-reduced-motion: reduce) { .sdc { transition: none } }`;
  // silvades.com links say they came from HyperDesk and which card (before any #fragment); other sites get nothing
  const link = ad => ad.url.replace(/^(https:\/\/silvades\.com\/[^#]*)/, `$1?utm_source=hyperdesk&amp;utm_medium=app&amp;utm_campaign=house_ads&amp;utm_content=${ad.id}`);
  // one link per card (never href="#...": the page keeps its API token in the address's #); its text is its accessible name
  const card = ad => `<a class="sdc" href="${link(ad)}" target="_blank" rel="noopener noreferrer">${MARK}<span class="sdc-copy">`
    + `<span class="sdc-top"><span class="sdc-label">Ad · SilvaDes</span> <span class="sdc-tag">${ad.tag}</span></span> <span class="sdc-title">${ad.title}</span> <span class="sdc-body">${ad.body}</span>`
    + `${ad.fine ? ` <span class="sdc-fine">${ad.fine}</span>` : ""} <span class="sdc-cta">${ad.cta} <span aria-hidden="true">→</span></span></span></a>`;
  let at = Math.floor(Date.now() / 60000) % ADS.length, timer = 0, styled = false;  // the sidebar's ad; each open starts on another
  const show = (el, i) => { if (!el || el._sdc === i) return; el._sdc = i; el.innerHTML = card(ADS[i]); el.hidden = false; };  // unchanged: untouched
  function paint() {
    const hello = document.querySelector("#sdhello");
    show(document.querySelector("#sdside"), at);
    if (hello) show(hello, hello._sdc != null && hello._sdc !== at ? hello._sdc : (at + (ADS.length >> 1)) % ADS.length);  // never the sidebar's
  }
  function next() {
    if ([...document.querySelectorAll("#sdside, #sdhello")].some(el => el.matches(":hover, :focus-within"))) return;  // never swap a card being read or tabbed to
    at = (at + 1) % ADS.length; paint();
  }
  function draw(on) {  // renderStatus calls this every 4 s
    if (!on) {
      clearInterval(timer); timer = 0;
      for (const el of [document.querySelector("#sdside"), document.querySelector("#sdhello")]) if (el && !el.hidden) { el.innerHTML = ""; el.hidden = true; el._sdc = null; }
      return;
    }
    if (!styled) { const s = document.createElement("style"); s.id = "sdccss"; s.textContent = CSS; document.head.append(s); styled = true; }
    if (!timer) timer = setInterval(next, 45000);
    paint();
  }
  window.sdCards = {draw};
})();
