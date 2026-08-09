// Test rapide : l'endpoint de recherche Pinterest (resource BaseSearchResource) est-il accessible sans clé ?
const axios = require('axios');

async function pinterestSearch(query, limit = 5) {
  const opts = {
    article_types: [],
    price_currency: 'EUR',
    query,
    rs: 'typed',
    scope: 'pins',
    page_size: Math.max(limit, 25),
  };
  const data = JSON.stringify({ options: opts, context: {} });
  const url = 'https://www.pinterest.com/resource/BaseSearchResource/get/';
  const headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': `https://www.pinterest.com/search/pins/?q=${encodeURIComponent(query)}`,
  };

  // 1) Prendre les cookies de la page d'accueil
  let cookie = '';
  try {
    const home = await axios.get('https://www.pinterest.com/', {
      headers: headers, timeout: 20000, maxRedirects: 5,
    });
    const setCookies = home.headers['set-cookie'] || [];
    cookie = setCookies.map((c) => c.split(';')[0]).join('; ');
  } catch (e) {
    console.log('STEP1 home:', e.message);
  }

  // 2) Appel resource
  const res = await axios.get(url, {
    params: { source_url: `/search/pins/?q=${encodeURIComponent(query)}`, data },
    headers: { ...headers, Cookie: cookie || '' },
    timeout: 25000,
  });
  const body = res.data;
  const results = body?.resource_response?.data?.results || [];
  console.log('HTTP', res.status, '| résultats bruts:', results.length);
  const urls = results
    .filter((p) => p?.images?.orig?.url)
    .slice(0, limit)
    .map((p) => p.images.orig.url);
  return urls;
}

(async () => {
  try {
    const urls = await pinterestSearch('chat mignon', 5);
    console.log('URLS:');
    urls.forEach((u) => console.log(' -', u));
    if (!urls.length) console.log('RESULTAT: AUCUNE IMAGE (endpoint bloqué ?)');
  } catch (e) {
    console.log('ERREUR GLOBALE:', e.message, e.response ? 'HTTP ' + e.response.status : '');
  }
})();
