const axios = require('axios');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36';

async function pinterestHtml(query) {
  try {
    const res = await axios.get(`https://www.pinterest.com/search/pins/?q=${encodeURIComponent(query)}&rs=typed`, {
      headers: { 'User-Agent': UA, 'Accept': 'text/html' },
      timeout: 20000, maxRedirects: 5,
    });
    const html = res.data;
    console.log('HTML length:', html.length);
    // cherche les URLs d'images i.pinimg.com
    const urls = [...html.matchAll(/https:\/\/i\.pinimg\.com\/[^"'\\\s]+\.(?:jpg|png|jpeg|webp)/gi)]
      .map((m) => m[0]).filter((u, i, a) => a.indexOf(u) === i);
    return urls.slice(0, 10);
  } catch (e) {
    return 'ERR: ' + (e.response ? 'HTTP ' + e.response.status : e.message);
  }
}

(async () => {
  const u = await pinterestHtml('chat mignon');
  console.log(Array.isArray(u) ? 'FOUND ' + u.length : u);
  if (Array.isArray(u)) u.forEach((x) => console.log(' -', x));
})();
