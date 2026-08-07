/**
 * DecoAI Amazon URL Builder — HTTP service.
 *
 * Takes a list of missing items and returns Amazon purchase links. Registered as
 * an OpenClaw skill on the X Elite PC; also runnable standalone for testing.
 *
 * POST /purchase-links   body: [{ item_name, color?, quantity? }, ...]
 * GET  /health
 */
const http = require('http')
const { buildLinks } = require('./builder')

const PORT = process.env.PORT || 8004

function sendJson (res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, { 'Content-Type': 'application/json' })
  res.end(payload)
}

const server = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    return sendJson(res, 200, { status: 'ok' })
  }

  if (req.method === 'POST' && req.url === '/purchase-links') {
    let body = ''
    req.on('data', chunk => { body += chunk })
    req.on('end', () => {
      let items
      try {
        items = JSON.parse(body)
      } catch {
        return sendJson(res, 400, { error: 'invalid JSON' })
      }
      if (!Array.isArray(items)) {
        return sendJson(res, 400, { error: 'expected a JSON array of items' })
      }
      try {
        return sendJson(res, 200, buildLinks(items))
      } catch (err) {
        return sendJson(res, 500, { error: String(err) })
      }
    })
    return
  }

  sendJson(res, 404, { error: 'not found' })
})

if (require.main === module) {
  server.listen(PORT, () => console.log(`Amazon URL Builder listening on :${PORT}`))
}

module.exports = { server }
