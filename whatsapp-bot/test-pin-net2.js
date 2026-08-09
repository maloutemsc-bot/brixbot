const axios = require('axios');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36';

// --- 1) Pinterest avec cookies csrftoken + header X-CSRFToken ---
async function pinterestTry2(query) {
  try {
    const home = await axios.get('https://www.pinterest.com/', {
      headers: { 'User-Agent': UA, 'Accept': 'text/html' },
      timeout: 20000, maxRedirects: 5,
    });
    const cookies = (home.headers['set-cookie'] || []).map((c) => c.split(';')[0]);
    const csrf = cookies.find((c) => c.startsWith('csrftoken='))?.split('=')[1] || '';
    const cookie = cookies.join('; ');
    const opts = { options: { article_types: [], price_currency: 'EUR', query, rs: 'typed', scope: 'pins', page_size: 25 }, context: {} };
    const res = await axios.get('https://www.pinterest.com/resource/BaseSearchResource/get/', {
      params: { source_url: `/search/pins/?q=${encodeURIComponent(query)}`, data: JSON.stringify(opts) },
      headers: {
        'User-Agent': UA, 'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest', 'Cookie': cookie,
        'X-CSRFToken': csrf, 'Referer': `https://www.pinterest.com/search/pins/?q=${encodeURIComponent(query)}`,
      },
      timeout: 25000,
    });
    const results = res.data?.resource_response?.data?.results || [];
    return results.filter((p) => p?.images?.orig?.url).slice(0, 3).map((p) => p.images.orig.url);
  } catch (e) {
    return 'ERR: ' + (e.response ? 'HTTP ' + e.response.status : e.message);
  }
}

// --- 2) Wikimedia Commons (API officielle, 100% gratuite, sans clé) ---
async function wikimediaTry(query) {
  try {
    const res = await axios.get('https://commons.wikimedia.org/w/api.php', {
      params: {
        action: 'query', format: 'json', generator: 'search',
        gsrsearch: query, gsrnamespace: 6, gsrlimit: 5,
        prop: 'imageinfo', iiprop: 'url|size', iiurlwidth: 800,
      },
      headers: { 'User-Agent': 'BrixBot/1.0 (WhatsApp bot; contact: admin@example.com)' },
      timeout: 25000,
    });
    const pages = Object.values(res.data?.query?.pages || {});
    return pages.map((p) => p.imageinfo?.[0]?.thumburl || p.imageinfo?.[0]?.url).filter(Boolean).slice(0, 3);
  } catch (e) {
    return 'ERR: ' + (e.response ? 'HTTP ' + e.response.status : e.message);
  }
}

// --- 3) DuckDuckGo Images (sans clé, token vqd) ---
async function ddgTry(query) {
  try {
    const page = await axios.get('https://duckduckgo.com/', {
      params: { q: query, ia: 'web' },
      headers: { 'User-Agent': UA },
      timeout: 20000,
    });
    const vqd = page.data.match(/vqd="([^"]+)"/)?.[1] || '';
    if (!vqd) return 'ERR: pas de vqd';
    const res = await axios.get('https://duckduckgo.com/i.js', {
      params: { l: 'us-en', o: 'json', q: query, vqd },
      headers: { 'User-Agent': UA, 'Referer': `https://duckduckgo.com/?q=${encodeURIComponent(query)}` },
      timeout: 25000,
    });
    const results = res.data?.results || [];
    return results.map((r) => r.image).filter(Boolean).slice(0, 3);
  } catch (e) {
    return 'ERR: ' + (e.response ? 'HTTP ' + e.response.status : e.message);
  }
}

(async () => {
  console.log('== PINTEREST (csrf) ==');
  const p = await pinterestTry2('chat mignon');
  console.log(Array.isArray(p) ? p : p);

  console.log('== WIKIMEDIA ==');
  const w = await wikimediaTry('cat');
  console.log(Array.isArray(w) ? w : w);

  console.log('== DUCKDUCKGO ==');
  const d = await ddgTry('cat');
  console.log(Array.isArray(d) ? d : d);
})();
