(async function () {
  const base = (window.PUBLIC_BASE_URL || (location.origin + location.pathname.replace(/\/site\/.*$/, ''))).replace(/\/$/, '');
  const feedUrl = `${base}/feed.xml`;
  const notesBase = `${base}/shownotes`;
  const audio = document.getElementById('player');
  const latestTitle = document.getElementById('latest-title');
  const list = document.getElementById('episode-list');
  const rssLink = document.getElementById('rss-link');
  const notesLink = document.getElementById('notes-link');

  rssLink.href = feedUrl;

  try {
    const res = await fetch(feedUrl, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Feed fetch ${res.status}`);
    const xml = await res.text();
    const doc = new window.DOMParser().parseFromString(xml, 'application/xml');

    const items = [...doc.querySelectorAll('channel > item')];
    if (!items.length) throw new Error('No items in feed');

    // Build episodes array
    const episodes = items.map(item => {
      const title = item.querySelector('title')?.textContent?.trim() || 'Episode';
      const pubDate = item.querySelector('pubDate')?.textContent || '';
      const enclosure = item.querySelector('enclosure')?.getAttribute('url') || '';
      // try to infer date slug for shownotes like YYYY-MM-DD.html
      const dateMatch = title.match(/\d{4}-\d{2}-\d{2}/) || pubDate.match(/\d{4}-\d{2}-\d{2}/);
      const dateSlug = dateMatch ? dateMatch[0] : '';
      const notesHref = dateSlug ? `${notesBase}/${dateSlug}.html` : notesBase + '/';
      return { title, pubDate, enclosure, notesHref, dateSlug };
    });

    // Latest
    const latest = episodes[0];
    latestTitle.textContent = latest.title;
    if (latest.enclosure) audio.src = latest.enclosure;
    notesLink.href = latest.notesHref;

    // Recent list (limit ~10)
    list.innerHTML = '';
    episodes.slice(0, 10).forEach(ep => {
      const li = document.createElement('li');
      const left = document.createElement('div');
      const right = document.createElement('div');

      const a = document.createElement('a');
      a.className = 'ep-title';
      a.href = ep.enclosure || '#';
      a.textContent = ep.title;
      a.onclick = (e) => {
        if (ep.enclosure) {
          e.preventDefault();
          audio.src = ep.enclosure;
          audio.play().catch(()=>{});
          latestTitle.textContent = ep.title;
          notesLink.href = ep.notesHref;
          window.scrollTo({ top: 0, behavior: 'smooth' });
        }
      };

      const meta = document.createElement('div');
      meta.className = 'ep-meta';
      meta.textContent = ep.pubDate;

      left.appendChild(a);
      left.appendChild(meta);

      const notesA = document.createElement('a');
      notesA.className = 'btn btn-ghost';
      notesA.textContent = 'Notes';
      notesA.href = ep.notesHref;
      notesA.target = '_blank';
      notesA.rel = 'noopener';

      right.appendChild(notesA);

      li.appendChild(left);
      li.appendChild(right);
      list.appendChild(li);
    });
  } catch (err) {
    latestTitle.textContent = 'Could not load feed.';
    console.error(err);
  }
})();
