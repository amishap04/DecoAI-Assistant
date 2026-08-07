/**
 * Amazon URL builder — turns missing inventory items into Amazon purchase links.
 *
 * Wraps the `amazon-url-builder` npm package. Given a list of missing items
 * (from the Inventory Manager / Cost Estimator), produces an Amazon search URL
 * for each. This is the entry point OpenClaw calls as a skill.
 *
 * Env:
 *   AMAZON_AFFILIATE_TAG  optional, appended as &tag=... on every link
 */
const { AmazonUrlBuilder } = require('amazon-url-builder')

/**
 * @typedef {Object} MissingItem
 * @property {string} item_name
 * @property {string} [color]
 * @property {number} [quantity]
 */

/**
 * Build a single Amazon search link for a missing item.
 * @param {MissingItem} item
 */
function buildLink (item) {
  const terms = [item.color, item.item_name].filter(Boolean).join(' ').trim()

  const params = {}
  const tag = process.env.AMAZON_AFFILIATE_TAG
  if (tag) params.tag = tag

  const url = AmazonUrlBuilder.buildUrlSearchByTerm(terms, Object.keys(params).length ? params : null)

  return {
    item_name: item.item_name,
    color: item.color ?? null,
    quantity: item.quantity ?? 1,
    search_terms: terms,
    url
  }
}

/**
 * Build Amazon purchase links for a list of missing items.
 * @param {MissingItem[]} items
 */
function buildLinks (items) {
  return items.map(buildLink)
}

module.exports = { buildLink, buildLinks }
