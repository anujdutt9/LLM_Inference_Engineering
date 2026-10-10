// Deck v5 runtime: scaling, keyboard navigation, videos that play when their slide opens,
// speaker notes (N), overview (O), presenter window (P) synced over BroadcastChannel.
(() => {
  const slides = [...document.querySelectorAll('.slide')];
  const stage = document.getElementById('stage');
  const main = slides.filter(s => s.dataset.act !== 'appx').length;
  let cur = 0;
  const chan = 'BroadcastChannel' in window ? new BroadcastChannel('deck-v5') : null;
  const presenter = new URLSearchParams(location.search).has('presenter');

  // numbering, progress bars and footers
  slides.forEach((s, i) => {
    const k = s.querySelector('.kicker .num');
    if (k) k.textContent = s.dataset.act === 'appx' ? `A${i - main + 1}` : `${String(i + 1).padStart(2, '0')} / ${main}`;
    if (s.dataset.act !== 'appx' && !s.classList.contains('cover')) {
      const p = document.createElement('div'); p.className = 'progress';
      for (let j = 0; j < main; j++) {
        const b = document.createElement('span');
        if (j < i) b.className = 'done'; if (j === i) b.className = 'now';
        p.appendChild(b);
      }
      s.appendChild(p);
    }
  });

  // cropped videos: data-crop="x0 y0 x1 y1" (fractions of the frame), data-w/data-h = the recording's pixels
  document.querySelectorAll('.crop').forEach(c => {
    const [x0, y0, x1, y1] = c.dataset.crop.split(' ').map(Number);
    const W = +c.dataset.w, H = +c.dataset.h;
    c.style.aspectRatio = `${(x1 - x0) * W} / ${(y1 - y0) * H}`;
    const v = c.querySelector('video');
    v.style.width = `${100 / (x1 - x0)}%`;
    v.style.left = `${-100 * x0 / (x1 - x0)}%`;
    v.style.top = `${-100 * y0 / (y1 - y0)}%`;
  });

  // cumulative time budget: each main slide's data-time, summed → "should end by"
  let acc = 0;
  slides.forEach(s => {
    if (!s.dataset.time) return;
    const [m, sec] = s.dataset.time.split(':').map(Number); acc += m * 60 + sec;
    s.dataset.at = `${Math.floor(acc / 60)}:${String(acc % 60).padStart(2, '0')}`;
  });

  function fit() {
    const k = Math.min(innerWidth / 1920, innerHeight / 1080);
    stage.style.transform = `translate(-50%, -50%) scale(${k})`;
  }
  addEventListener('resize', fit); fit();

  function videos(s) { return [...s.querySelectorAll('video')]; }
  // The videos are not in the repository: they are in the shared Google Drive folder (its video/ folder goes into
  // docs/presentation/video/). A video whose file is missing keeps its poster (a frame of the clip) and a badge;
  // clicking it opens the Drive folder. Put the folder's link here:
  const DRIVE_URL = '';
  function missing(v) {
    if (v.dataset.missing) return;
    v.dataset.missing = '1';
    v.removeAttribute('src'); v.load();  // back to showing the poster
    const box = v.parentElement;
    if (getComputedStyle(box).position === 'static') box.style.position = 'relative';
    const badge = document.createElement(DRIVE_URL ? 'a' : 'div');
    badge.className = 'vdrive';
    badge.textContent = DRIVE_URL ? '▶ Watch in Google Drive' : '▶ Video in the shared Drive folder (see the README)';
    if (DRIVE_URL) { badge.href = DRIVE_URL; badge.target = '_blank'; badge.rel = 'noopener'; }
    badge.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);padding:10px 18px;' +
      'border-radius:999px;background:rgba(17,24,39,.82);color:#fff;font-size:16px;text-decoration:none;' +
      'white-space:nowrap;pointer-events:' + (DRIVE_URL ? 'auto' : 'none');
    box.appendChild(badge);
  }
  function load(v) {  // set a video's source once; a file that isn't there falls back to its poster
    if (v.src || !v.dataset.src || v.dataset.missing) return;
    v.addEventListener('error', () => missing(v), { once: true });
    v.src = v.dataset.src;
  }
  function startVideos(s) {
    videos(s).forEach(v => {
      load(v);
      if (v.dataset.missing) return;
      const go = () => {
        try { v.currentTime = parseFloat(v.dataset.start || 0); } catch (e) {}
        v.playbackRate = parseFloat(v.dataset.rate || 1);
        v.muted = true;
        if (v.dataset.autoplay !== 'off') v.play().catch(() => {});
      };
      if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, { once: true });
    });
  }
  function stopVideos(s) { videos(s).forEach(v => v.pause()); }

  function show(i, from) {
    i = Math.max(0, Math.min(slides.length - 1, i));
    if (slides[cur]) { slides[cur].classList.remove('active'); stopVideos(slides[cur]); }
    cur = i;
    slides[cur].classList.add('active');
    startVideos(slides[cur]);
    history.replaceState(null, '', (presenter ? '?presenter' : location.pathname.split('/').pop()) + '#' + (cur + 1));
    renderNotes();
    if (chan && from !== 'remote') chan.postMessage({ i: cur });
    // preload the next two slides' videos
    [slides[cur + 1], slides[cur + 2]].forEach(n => n && videos(n).forEach(v => { if (!v.src) v.preload = 'auto'; load(v); }));
  }

  // speaker notes
  const notesEl = document.getElementById('notes');
  const t0 = Date.now();
  function noteHTML(s) { const a = s.querySelector('aside.notes'); return a ? a.innerHTML : '<i>No notes.</i>'; }
  function title(s) { const h = s.querySelector('h1'); return h ? h.textContent.trim() : ''; }
  function renderNotes() {
    const s = slides[cur];
    const budget = s.dataset.time ? ` · budget ${s.dataset.time}` : '';
    notesEl.innerHTML = `<div class="meta">${cur + 1}. ${title(s)}${budget}</div>${noteHTML(s)}`;
    if (presenter) renderPresenter();
  }
  function fmt(ms) { const t = Math.floor(ms / 1000); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; }
  function renderPresenter() {
    const s = slides[cur], n = slides[cur + 1];
    document.getElementById('pv').innerHTML = `
      <div class="top"><div>Slide ${cur + 1} of ${slides.length} ${s.dataset.time ? '· budget ' + s.dataset.time : ''}
        ${s.dataset.at ? '· should end by ' + s.dataset.at : ''}</div><div class="clock" id="clk">${fmt(Date.now() - t0)}</div></div>
      <h2>${title(s)}</h2><div class="txt">${noteHTML(s)}</div>
      <div class="nxt">Next: ${n ? title(n) : '(end)'}</div>`;
  }
  if (presenter) {
    document.body.classList.add('presenter');
    setInterval(() => { const c = document.getElementById('clk'); if (c) c.textContent = fmt(Date.now() - t0); }, 1000);
  }
  if (chan) chan.onmessage = e => { if (typeof e.data.i === 'number' && e.data.i !== cur) show(e.data.i, 'remote'); };

  // overview
  const ov = document.getElementById('overview');
  slides.forEach((s, i) => {
    const o = document.createElement('div'); o.className = 'o';
    o.style.borderLeftColor = getComputedStyle(s).getPropertyValue('--act') || '#555';
    o.innerHTML = `<b>${s.dataset.act === 'appx' ? 'A' + (i - main + 1) : i + 1}</b>${title(s) || 'Cover'}`;
    o.onclick = () => { ov.classList.remove('on'); show(i); };
    ov.appendChild(o);
  });

  addEventListener('keydown', e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (lb.classList.contains('on')) { if (e.key === 'Escape') closeViewer(); return; }
    const k = e.key;
    if (['ArrowRight', 'PageDown', ' ', 'Enter'].includes(k)) { e.preventDefault(); show(cur + 1); }
    else if (['ArrowLeft', 'PageUp', 'Backspace'].includes(k)) { e.preventDefault(); show(cur - 1); }
    else if (k === 'Home') show(0);
    else if (k === 'End') show(main - 1);
    else if (k === 'a' || k === 'A') show(main);
    else if (k === 'n' || k === 'N') notesEl.classList.toggle('on');
    else if (k === 'o' || k === 'O') { ov.classList.toggle('on'); [...ov.children].forEach((c, i) => c.classList.toggle('cur', i === cur)); }
    else if (k === 'p' || k === 'P') window.open(location.pathname + '?presenter#' + (cur + 1), 'presenter', 'width=1400,height=900');
    else if (k === 'f' || k === 'F') { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen(); }
    else if (k === 'r' || k === 'R') startVideos(slides[cur]);
    else if (k === 'Escape') { ov.classList.remove('on'); notesEl.classList.remove('on'); }
  });
  // viewer: click an image or video to see it full screen (Esc / click outside / ✕ closes)
  const lb = document.getElementById('lb'), lbBody = document.getElementById('lbbody');
  const lbCap = document.getElementById('lbcap'), lbOpen = document.getElementById('lbopen');
  function openViewer(el) {
    const media = el.closest('.media');
    const tag = media && media.querySelector('.tag');
    lbCap.textContent = tag ? tag.textContent : (el.getAttribute('alt') || '');
    lbBody.innerHTML = '';
    let url;
    if (el.tagName === 'VIDEO') {
      url = el.dataset.src || el.src;
      const v = document.createElement('video');
      v.src = url; v.controls = true; if (el.dataset.start) v.addEventListener('loadedmetadata', () => { v.currentTime = +el.dataset.start; }, {once: true}); v.autoplay = true; v.loop = true; v.muted = true; v.playsInline = true;
      lbBody.appendChild(v);
    } else if (el.dataset.href) {
      url = el.dataset.href;
      const f = document.createElement('iframe'); f.src = url; lbBody.appendChild(f);
    } else {
      url = el.getAttribute('src');
      const i = document.createElement('img'); i.src = url;
      // tall images (e.g. report screenshots) scroll; others fit the screen
      i.onload = () => { if (i.naturalHeight / i.naturalWidth > 1.2) lbBody.classList.add('tall'); };
      lbBody.appendChild(i);
    }
    lbOpen.href = url;
    lbBody.classList.remove('tall');
    stopVideos(slides[cur]);
    lb.classList.add('on');
  }
  function closeViewer() {
    lb.classList.remove('on'); lbBody.innerHTML = ''; startVideos(slides[cur]);
  }
  window.closeViewer = closeViewer;
  stage.addEventListener('click', e => {
    const el = e.target.closest('.media img, .media video, .media .crop');
    if (!el) return;
    const vid = el.tagName === 'VIDEO' ? el : el.querySelector && el.querySelector('video');
    if (vid && vid.dataset.missing) { if (DRIVE_URL) window.open(DRIVE_URL, '_blank', 'noopener'); return; }
    openViewer(el.classList && el.classList.contains('crop') ? el.querySelector('video') : el);
  });
  document.getElementById('lbx').onclick = closeViewer;

  // hover buttons on every image and video: full screen, and a new tab for videos
  document.querySelectorAll('.media').forEach(m => {
    const target = m.querySelector('video, img'); if (!target) return;
    const bar = document.createElement('div'); bar.className = 'mbtns';
    const fs = document.createElement('button'); fs.innerHTML = '⤢<span>Full screen</span>'; fs.title = 'Full screen';
    fs.onclick = e => { e.stopPropagation(); openViewer(target); };
    bar.appendChild(fs);
    if (target.tagName === 'VIDEO') {
      const nt = document.createElement('button'); nt.className = 'qt'; nt.innerHTML = '↗<span>New tab</span>'; nt.title = 'Open the video in a new tab';
      nt.onclick = e => { e.stopPropagation(); const url = target.dataset.missing ? DRIVE_URL : target.dataset.src; if (url) window.open(url, '_blank', 'noopener'); };
      bar.appendChild(nt);
    }
    m.appendChild(bar);
  });
  lb.addEventListener('click', e => { if (e.target === lb || e.target === lbBody) closeViewer(); });

  const h = parseInt((location.hash || '#1').slice(1), 10);
  show(isNaN(h) ? 0 : h - 1);
})();
