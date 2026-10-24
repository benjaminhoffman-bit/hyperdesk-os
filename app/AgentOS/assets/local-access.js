/* Internet permissions are independent of recording analysis and cloud APIs. */
const localWeb = {enabled:false,unrestricted:false,busy:false};
function drawLocalWeb() {
  const button = $('#webstate');
  if (button) {
    button.textContent = localWeb.enabled ? 'Local web · on' : 'Local web · off';
    button.setAttribute('aria-checked',String(localWeb.enabled));
    button.disabled = localWeb.busy;
  }
  for (const key of ['enabled','unrestricted']) {
    const el = $('#web-' + key);
    if (el) { el.checked=localWeb[key]; el.disabled=localWeb.busy; }
  }
  if ($('#web-summary')) $('#web-summary').textContent = !localWeb.enabled ? 'Offline answers. Public web access is off.' :
    localWeb.unrestricted ? 'Public web research is available to all local chat models, including unrestricted models.' :
    'Public web research is available. Unrestricted models remain offline.';
}
async function loadLocalWeb() {
  try { Object.assign(localWeb,await api('local-web')); drawLocalWeb(); }
  catch { if ($('#webstate')) { $('#webstate').textContent='Local web · unavailable'; $('#webstate').disabled=true; } }
}
async function saveLocalWeb(next) {
  if (localWeb.busy) return;
  localWeb.busy=true; drawLocalWeb();
  try {
    await api('action',{do:'localweb.save',settings:next});
    Object.assign(localWeb,next);
    toast(next.enabled ? 'Local models can use public web research.' : 'Internet access is off. Further web requests stop.');
  } catch(e) { toast(e.message,true); }
  finally { localWeb.busy=false; drawLocalWeb(); }
}
JS.localwebtoggle = () => saveLocalWeb({enabled:!localWeb.enabled,unrestricted:localWeb.unrestricted});
const originalModelsRender = VIEWS.models.render;
VIEWS.models.render = async function() {
  await originalModelsRender.call(this);
  if (page!=='models' || !$('#mtable')) return;
  $('#mtable').insertAdjacentHTML('beforebegin',`<section class="access-panel" aria-labelledby="web-heading">
    <div class="eyebrow">Your local AI / Internet access</div><h2 id="web-heading">Local brains. Live information.</h2>
    <p id="web-summary" class="small muted"></p>
    <label class="access-switch"><span><strong>Allow public web research</strong><small>One search and up to two pages per request, with source links. Free search; no API key.</small></span><input id="web-enabled" type="checkbox" role="switch"></label>
    <label class="access-switch"><span><strong>Include unrestricted models</strong><small>Also allow the models listed under Unrestricted chat to use the same public search and page-reading tools.</small></span><input id="web-unrestricted" type="checkbox" role="switch"></label>
    <p class="help">A local model can turn your current question into a search query sent to Bing. Attachments, saved Knowledge and conversation history are excluded. Video understanding stays local and uses neither switch. Page reading does not sign in, publish or run website scripts.</p>
    <p class="help">Turn on public research, then choose a model beside the message box and ask it to search. The separate Claude research mode remains available.</p></section>`);
  await loadLocalWeb();
  for (const key of ['enabled','unrestricted']) $('#web-'+key).onchange = e => {
    const next={enabled:localWeb.enabled,unrestricted:localWeb.unrestricted}; next[key]=e.target.checked; saveLocalWeb(next);
  };
};
loadLocalWeb();
