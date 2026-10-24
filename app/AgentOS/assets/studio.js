/* A guided local webcam take. Media is streamed to disk, never sent to a cloud model. */
I.studio = I.video;
const studioState = {stream: null, recorder: null, busy: false, pending: [], count: 0, rid: '',
  pumping: null, error: '', misses: 0, started: 0, timer: null, rows: [], form: {}, auto: true, questionList: []};
try { studioState.form = JSON.parse(localStorage.getItem('studio-brief') || '{}'); } catch {}
const studioUrl = (id, name) => `/api/studio/file?id=${id}&name=${encodeURIComponent(name)}&t=${T}`;
const studioMessage = message => { if ($('#stmessage')) $('#stmessage').textContent = message; };
function studioTracksOff() {
  studioState.stream?.getTracks().forEach(track => track.stop());
  studioState.stream = null;
}
window.studioCanLeave = () => {
  if (studioState.busy || studioState.recorder || studioState.pending.length) {
    toast('Finish recording and saving before leaving this page.', true); return false;
  }
  if ($('#stgoal')) {  // keep the brief, answered questions folded into notes, for when Ben comes back
    const f = studioState.form;
    for (const key of ['goal', 'audience', 'keep', 'remove', 'target', 'seconds']) f[key] = $('#st' + key).value;
    f.notes = [$('#stnotes').value, ...studioState.questionList.map((q, i) => $('#stanswer' + i)?.value.trim() ? `${q} ${$('#stanswer' + i).value.trim()}` : '')].filter(Boolean).join('\n').slice(0, 2000);
    f.visual = $('#stvisual').checked;
  }
  studioTracksOff(); return true;
};
addEventListener('beforeunload', e => {
  if (studioState.busy || studioState.recorder || studioState.pending.length) { e.preventDefault(); e.returnValue = ''; }
  else studioTracksOff();
});
function studioBrief() {
  const f = {};
  for (const key of ['goal', 'audience', 'keep', 'remove', 'target', 'seconds']) f[key] = $('#st' + key).value.trim();
  f.notes = [$('#stnotes').value, ...studioState.questionList.map((q, i) => `${q} ${$('#stanswer' + i)?.value || ''}`)].join('\n').slice(0, 2000);
  f.visual = $('#stvisual')?.checked === true;
  if (!f.goal || !f.keep) throw Error('Tell me what you are making and which moments matter first.');
  if (+f.seconds < 10 || +f.seconds > 180) throw Error('Choose a clip length between 10 and 180 seconds.');
  studioState.form = f;
  if ($('#stremember').checked) localStorage.setItem('studio-brief', JSON.stringify(f));
  return f;
}
function studioControls() {
  const s = studioState, recording = !!s.recorder;
  for (const id of ['stcamera', 'stmicrophone', 'stquality', 'stconnect', 'stquestions', 'strecord']) {
    if ($('#' + id)) $('#' + id).disabled = recording || s.busy;
  }
  if ($('#strecord')) $('#strecord').disabled ||= !s.stream;
  if ($('#ststop')) $('#ststop').disabled = !recording;
  if ($('#stmark')) $('#stmark').disabled = !recording;
  if ($('#stretry')) $('#stretry').hidden = !s.error;
  for (const key of ['goal', 'audience', 'keep', 'remove', 'notes', 'target', 'seconds', 'auto', 'remember', 'visual']) {
    if ($('#st' + key)) $('#st' + key).disabled = recording || s.busy;
  }
}
async function studioDevices() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  for (const [id, kind, noun] of [['stcamera', 'videoinput', 'Camera'], ['stmicrophone', 'audioinput', 'Microphone']]) {
    const el = $('#' + id), previous = el.value;
    el.innerHTML = devices.filter(d => d.kind === kind).map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || noun + ' ' + (i+1))}</option>`).join('');
    if ([...el.options].some(o => o.value === previous)) el.value = previous;
  }
}
async function studioCamera() {
  const s = studioState;
  if (s.recorder || s.busy) return;
  s.busy = true; studioControls();
  studioTracksOff();
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw Error('Open HyperDesk in Microsoft Edge to record with a webcam.');
    const camera = $('#stcamera').value, mic = $('#stmicrophone').value;
    const height = Number($('#stquality').value) || 2160;
    s.stream = await navigator.mediaDevices.getUserMedia({
      video: {width: {ideal: Math.round(height*16/9)}, height: {ideal: height}, frameRate: {ideal: 30, max: 30}, ...(camera ? {deviceId: {exact: camera}} : {})},
      audio: {echoCancellation: true, ...(mic ? {deviceId: {exact: mic}} : {})}});
    if (page !== 'studio') { studioTracksOff(); return; }
    $('#stpreview').srcObject = s.stream;
    await studioDevices();
    const actualCamera = s.stream.getVideoTracks()[0]?.getSettings().deviceId;
    const actualMic = s.stream.getAudioTracks()[0]?.getSettings().deviceId;
    if (actualCamera) $('#stcamera').value = actualCamera;
    if (actualMic) $('#stmicrophone').value = actualMic;
    for (const track of s.stream.getTracks()) track.addEventListener('ended', () => {
      if (s.recorder?.state === 'recording') { studioMessage('A camera or microphone disconnected. Saving what was recorded…'); s.recorder.stop(); }
    });
    const settings = s.stream.getVideoTracks()[0]?.getSettings() || {};
    studioMessage(`Camera ready · ${settings.width || '?'} × ${settings.height || '?'} · ${Math.round(settings.frameRate || 30)} fps. Check your framing and microphone.`);
    // A live level meter makes the selected microphone visible without playing it back.
    s.audio?.close(); s.audio = new AudioContext();
    const input = s.audio.createMediaStreamSource(s.stream), analyser = s.audio.createAnalyser();
    analyser.fftSize = 256; input.connect(analyser);
    const levels = new Uint8Array(analyser.fftSize);
    clearInterval(s.meter);
    s.meter = setInterval(() => {
      if (!s.stream) { clearInterval(s.meter); s.audio?.close(); return; }
      analyser.getByteTimeDomainData(levels);
      const peak = Math.max(...levels.map(v => Math.abs(v-128))) / 128;
      if ($('#stlevel')) $('#stlevel').value = peak;
    }, 150);
  } catch(e) { studioTracksOff(); studioMessage('Camera could not open: ' + e.message + ' Allow camera and microphone access, then try again.'); }
  finally { s.busy = false; studioControls(); }
}
async function studioDrain() {
  const s = studioState;
  if (s.pumping) return s.pumping;
  s.pumping = (async () => {
    while (s.pending.length) {
      const blob = s.pending[0];
      await api('studio/chunk', {id: s.rid, number: s.count, data: await toBase64(blob)});
      s.count++; s.pending.shift();
    }
  })();
  // counted once per pump: callers that join a running pump return above
  try { await s.pumping; s.misses = 0; } catch (e) { s.misses++; throw e; } finally { s.pumping = null; }
}
async function studioSaved() {
  const s = studioState;
  s.busy = true; s.error = ''; studioControls(); studioMessage('Saving the last part of your recording…');
  try {
    await studioDrain();
    await api('studio/finish', {id: s.rid, count: s.count});
    studioMessage('Recording saved. Your original is kept on this PC.');
    if (s.auto) {
      try { await api('studio/edit', {id: s.rid}); studioMessage('Saved. Your edit is queued; progress appears below.'); }
      catch(e) { studioMessage('Recording saved. Editing could not start: ' + e.message + ' Use Create edit below.'); }
    }
    await VIEWS.studio.refresh();
  } catch(e) { s.error = e.message; studioMessage('Saving paused: ' + e.message + ' Keep this window open and press Retry saving.'); }
  finally { s.busy = false; studioControls(); }
}
async function studioRecord() {
  const s = studioState;
  if (s.busy || s.recorder || !s.stream) return;
  try {
    const brief = studioBrief();
    s.auto = $('#stauto').checked;
    s.busy = true; studioControls();
    const mime = ['video/mp4;codecs=avc1.640033,mp4a.40.2', 'video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4'].find(x => MediaRecorder.isTypeSupported(x));
    if (!mime) throw Error('This browser cannot record a supported video format. Try Edge.');
    const r = await api('studio/start', {brief, mime});
    Object.assign(s, {rid: r.id, count: 0, pending: [], error: '', misses: 0, started: performance.now()});
    const pixels = s.stream.getVideoTracks?.()[0]?.getSettings?.() || {width:1920,height:1080};
    const bitrate = pixels.width*pixels.height > 2560*1440 ? 35000000 : pixels.width*pixels.height > 1280*720 ? 12000000 : 8000000;
    const recorder = new MediaRecorder(s.stream, {mimeType: mime, videoBitsPerSecond: bitrate, audioBitsPerSecond: 256000});
    s.recorder = recorder;
    recorder.ondataavailable = e => {
      for (let offset = 0; offset < e.data.size; offset += 1024*1024) s.pending.push(e.data.slice(offset, offset + 1024*1024));
      if (s.pending.reduce((sum, b) => sum + b.size, 0) > 64*1024*1024 && recorder.state === 'recording') recorder.stop();
      studioDrain().catch(e => {  // the failed chunk stays queued and the server skips a resend: retry on the next events
        s.error = e.message;
        if (s.misses >= 3 && recorder.state === 'recording') recorder.stop();
      });
    };
    recorder.onerror = e => {
      studioMessage('The recorder stopped unexpectedly. Saving available footage…');
      if (recorder.state !== 'inactive') recorder.stop();
    };
    recorder.onstop = async () => {
      clearInterval(s.timer); s.recorder = null; studioTracksOff();
      await studioSaved();
    };
    recorder.start(1500);
    s.timer = setInterval(() => {
      const elapsed = Math.floor((performance.now()-s.started)/1000);
      studioMessage(`Recording · ${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')} · saving to this PC`);
      if (elapsed >= 1800 && recorder.state === 'recording') recorder.stop();
    }, 500);
    studioMessage('Recording…');
  } catch(e) { toast(e.message, true); }
  finally { s.busy = false; studioControls(); }
}
VIEWS.studio = {
  loading: false, last: '',
  async render() {
    const f = studioState.form;
    studioState.questionList = [];
    show(`<div class="pagehead"><div><div class="eyebrow">Create / Recording studio</div><h1>Record once. Keep the good parts.</h1><p>Tell your editor what matters. Record here, then get a clean video and up to three clips.</p></div></div>
      <div class="studio-grid recording-grid"><section class="surface"><div class="workflow-step"><b>01</b> Tell me what we’re making</div>
      <div class="field"><label for="stgoal">What are you trying to make?</label><textarea id="stgoal" rows="3" maxlength="2000" placeholder="A walkthrough of my new product. I want a useful YouTube video and quick clips showing the best features.">${esc(f.goal || '')}</textarea></div>
      <button class="btn quiet" id="stquestions" data-js="stquestions">Help me shape the brief</button><p class="help">Optional: your local AI asks up to three questions tailored to your idea.</p><div id="stfollowup"></div>
      <div class="field"><label for="staudience">Who is watching?</label><input id="staudience" maxlength="2000" value="${esc(f.audience || '')}" placeholder="New customers, beginners, my followers…"></div>
      <div class="field"><label for="stkeep">Which moments or topics must stay?</label><textarea id="stkeep" rows="2" maxlength="2000" placeholder="The reveal, the three main steps, and the final result…">${esc(f.keep || '')}</textarea></div>
      <div class="field"><label for="stremove">What should the editor cut?</label><textarea id="stremove" rows="2" maxlength="2000" placeholder="Long pauses, repeated takes, mistakes, off-topic talking…">${esc(f.remove || 'Long pauses, repeated takes and off-topic filler.')}</textarea></div>
      <div class="field"><label for="stnotes">Anything else the editor should know?</label><textarea id="stnotes" rows="2" maxlength="2000" placeholder="Tone, pacing, or answers saved from your last brief…">${esc(f.notes || '')}</textarea></div>
      <label class="vision-option"><span><strong>Understand what happens on screen</strong><small>Follow actions, connect them to your words, and keep important silent moments.</small><span class="tag ok">On this PC · no API charges</span></span><input type="checkbox" role="switch" id="stvisual" ${f.visual!==false?'checked':''}></label>
      <p class="help">Visual review samples ordered frames and rechecks up to three clips. Allow extra processing time; very brief actions can be missed. Up to 15 minutes per take.</p>
      <div class="bar"><div class="field grow"><label for="sttarget">What do you want back?</label><select id="sttarget">${[['both','Full edit + vertical clips'],['full','Cleaned-up full video'],['clips','Vertical clips only']].map(([v,l])=>`<option value="${v}" ${f.target===v?'selected':''}>${l}</option>`).join('')}</select></div>
      <div class="field"><label for="stseconds">Max seconds per clip</label><input id="stseconds" type="number" min="10" max="180" value="${esc(f.seconds || 45)}" style="width:110px"></div></div>
      <label class="small"><input type="checkbox" id="stremember"> Remember this brief on this browser for next time</label>
      </section><section class="surface"><div class="workflow-step"><b>02</b> Choose your camera & microphone</div>
      <video id="stpreview" autoplay muted playsinline style="display:block;width:100%;aspect-ratio:16/9;background:#050505;border:1px solid var(--line);border-radius:4px;object-fit:contain"></video>
      <div class="field"><label for="stcamera">Webcam</label><select id="stcamera"><option value="">Default camera · connect to see names</option></select></div>
      <div class="field"><label for="stmicrophone">Microphone</label><select id="stmicrophone"><option value="">Default microphone</option></select></div>
      <div class="field"><label for="stquality">Recording quality</label><select id="stquality"><option value="2160">Best available · up to 4K</option><option value="1080">Full HD · smaller files</option><option value="720">HD · lighter recording</option></select><p class="help">Uses the closest quality your camera supports. The preview shows the actual resolution. Best quality needs about 8 GB per 30-minute take.</p></div>
      <div class="bar"><button class="btn" id="stconnect" data-js="stconnect">Connect camera</button><label class="small grow" for="stlevel">Mic level <meter id="stlevel" min="0" max="1" value="0" style="width:100%"></meter></label></div>
      <p class="help">Allow camera and microphone access when asked. Selecting a different device reconnects the preview. No recording starts until you click below.</p>
      <div class="workflow-step" style="margin-top:24px"><b>03</b> Record your take</div>
      <label class="small"><input id="stauto" type="checkbox" checked> Create my edit when I finish recording</label>
      <div class="bar" style="margin-top:16px"><button class="btn go" id="strecord" data-js="strecord" disabled>Start recording</button><button class="btn" id="stmark" data-js="stmark" disabled>Mark moment</button><button class="btn" id="ststop" data-js="ststop" disabled>Finish recording</button><button class="btn" id="stretry" data-js="stretry" hidden>Retry saving</button></div>
      <p id="stmessage" role="status" aria-live="polite" class="note">Your camera is off.</p>
      <p class="help">Mark moment protects the previous 10 seconds and next 5 seconds. Visual review connects sampled scenes with your brief and speech; turn it off for faster speech-only edits. Originals stay on this PC. Review before publishing.</p>
      <p id="sttools" class="small muted">Checking installed tools…</p></section></div>
      <div class="sectionhead"><h2>Your recordings & edits</h2><button class="btn quiet" data-js="strefresh">Refresh</button></div><div id="stlibrary"></div>`);
    $('#stcamera').onchange = studioCamera; $('#stmicrophone').onchange = studioCamera; $('#stquality').onchange = () => { if (studioState.stream) studioCamera(); };
    const r = await api('studio?tools=1').catch(e => (toast(e.message,true), null));
    if (!r || page !== 'studio') return;
    const missing = Object.entries(r.tools).filter(([,v])=>!v).map(([k])=>k);
    $('#sttools').textContent = missing.length ? 'Needs setup: ' + missing.join(', ') : 'Quality setup ready · Whisper large-v3 · original-resolution edits · Full HD portrait clips · captions';
    this.draw(r.recordings); studioControls();
  },
  async refresh() {
    if (this.loading || page !== 'studio') return;
    this.loading = true;
    try { const r = await api('studio'); if (page === 'studio') this.draw(r.recordings); }
    catch(e) { toast(e.message,true); } finally { this.loading = false; }
  },
  live() { if (!studioState.recorder && !studioState.busy) this.refresh(); },
  draw(rows) {
    studioState.rows = rows;
    const box = $('#stlibrary');
    if (!box) return;
    if (!rows.length) { box.innerHTML = '<div class="empty"><b>Your first take starts here.</b>Fill in the brief and connect your camera.</div>'; return; }
    // one card per take, redrawn only when that take changed: an edit's progress must not stop a video playing in another
    const old = new Map([...box.children].filter(c => c.dataset.id).map(c => [c.dataset.id, c]));
    box.querySelector('.empty')?.remove();
    let prev = null;
    for (const r of rows) {
      const sig = JSON.stringify(r);
      let el = old.get(r.id);
      old.delete(r.id);
      if (!el) { el = document.createElement('article'); el.className = 'projectcard'; el.dataset.id = r.id; }
      if (el._sig !== sig) { el._sig = sig; el.innerHTML = this.card(r); }
      if ((prev ? prev.nextElementSibling : box.firstElementChild) !== el) prev ? prev.after(el) : box.prepend(el);
      prev = el;
    }
    old.forEach(el => el.remove());
  },
  card(r) {
    return `<div class="bar"><h3 class="grow">${esc(r.brief.goal)}</h3><span class="tag">${esc(r.state)}</span></div><p class="small">${esc(r.message || '')}</p>
      ${r.analytics ? `<div class="bar"><span class="tag">${Math.round(r.analytics.original_seconds)}s recorded</span><span class="tag">${Math.round(r.analytics.removed_seconds)}s removed from full edit</span><span class="tag">${r.analytics.words_per_minute} words/min</span><span class="tag">${r.analytics.uncertain_words} words to check</span></div><p class="help">Speech recognition confidence suggests words to review; these are editing measurements, not audience performance.</p>` : ''}
      ${r.visual_events?.length ? `<details class="visual-review"><summary>What the editor saw · ${r.visual_events.length} observations</summary><p class="help">${esc(r.visual_coverage?.note || '')} These are AI observations to review, not guaranteed facts.</p><div class="evidence-grid">${r.visual_events.map(e=>`<article class="evidence-card"><img loading="lazy" alt="Frame sampled at ${Math.round(e.frame_times[0])} seconds" src="${studioUrl(r.id,e.thumbnail)}"><div><span class="mono">${e.start.toFixed(1)}–${e.end.toFixed(1)}s</span><span class="tag ${e.confidence==='low'?'warn':''}">${esc(e.confidence)} confidence</span><p>${esc(e.description)}</p><p class="small muted">${esc(e.reason)}</p><span class="tag ${e.keep?'ok':''}">${e.keep?'Kept for your brief':'Context only'}</span></div></article>`).join('')}</div></details>` : ''}
      <div class="bar">${r.bytes ? `<a class="btn quiet" href="${studioUrl(r.id,r.source)}" download>Save original</a>` : ''}
      ${r.job ? `<button class="btn quiet" data-js="stcancel" data-job="${r.job}">Stop editing</button>` : ''}
      ${!['queued','editing','recording'].includes(r.state) ? `<button class="btn" data-js="stedit" data-id="${r.id}">${r.state==='done'?'Create another edit':'Create edit'}</button><button class="btn quiet" data-js="strevise" data-id="${r.id}">Use current brief & edit</button>` : ''}
      ${r.state==='recording' && r.id!==studioState.rid && r.bytes && Date.now()/1000-(r.updated || r.created)>60 ? `<button class="btn quiet" data-js="strecover" data-id="${r.id}" data-count="${r.next}">Recover saved portion</button>` : ''}</div>
      ${(r.exports || []).map(e=>`<details class="disclosure"><summary>${esc(e.title)} · ${Math.round(e.seconds)}s</summary>${e.reason?`<p class="small">${esc(e.reason)}</p>`:''}<video controls preload="none" playsinline style="max-height:420px;width:100%;background:#050505" src="${studioUrl(r.id,e.file)}">${e.captions ? `<track kind="captions" label="English" srclang="en" default src="${studioUrl(r.id,e.captions)}">` : ''}</video><a class="btn quiet" href="${studioUrl(r.id,e.file)}" download>Save MP4</a>${e.captions ? `<a class="btn quiet" href="${studioUrl(r.id,e.captions)}" download>Save captions</a>` : ''}</details>`).join('')}`;
  }
};
Object.assign(JS, {
  stconnect: studioCamera, strecord: studioRecord, strefresh: () => VIEWS.studio.refresh(),
  stcancel: async d => { await act({do:'job.cancel',id:d.job},true); VIEWS.studio.refresh(); },
  ststop: () => { if (studioState.recorder?.state === 'recording') studioState.recorder.stop(); },
  stretry: studioSaved,
  stmark: async () => { try { await api('studio/mark', {id:studioState.rid,seconds:(performance.now()-studioState.started)/1000}); toast('Moment marked: 10 seconds before, 5 after.'); } catch(e) { toast(e.message,true); } },
  stedit: async d => { try { await api('studio/edit',{id:d.id}); VIEWS.studio.refresh(); } catch(e) { toast(e.message,true); } },
  strevise: async d => { try { await api('studio/edit',{id:d.id,brief:studioBrief()}); VIEWS.studio.refresh(); } catch(e) { toast(e.message,true); } },
  strecover: async d => { try { await api('studio/finish',{id:d.id,count:+d.count}); VIEWS.studio.refresh(); } catch(e) { toast(e.message,true); } },
  stquestions: async () => {
    const button = $('#stquestions'); button.disabled = true; button.textContent = 'Thinking of useful questions…';
    try {
      const r = await api('studio/questions', {goal:$('#stgoal').value});
      if (page !== 'studio') return;
      studioState.questionList = r.questions;
      $('#stfollowup').innerHTML = r.questions.map((q,i)=>`<div class="field"><label for="stanswer${i}">${esc(q)}</label><textarea id="stanswer${i}" rows="2" maxlength="600"></textarea></div>`).join('');
    } catch(e) { toast(e.message,true); }
    finally { button.disabled = false; button.textContent = 'Help me shape the brief'; }
  }
});
renderTools();
