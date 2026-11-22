// Live frames arrive about 20 times a second. Re-sorting the feature list on every frame makes
// names with close scores swap places constantly, so the list cannot be read. Instead the
// order (the ranking) is refreshed on a slower clock, and the values shown are always the
// latest frame's. Values are never smoothed or averaged.

export const RERANK_MS = 1000

// Names ordered from highest to lowest score, leaving out names that must not be displayed as control.
export function rankFeatures(features, excluded = []) {
  return Object.entries(features)
    .filter(([name]) => !excluded.includes(name))
    .sort((a, b) => b[1] - a[1])
    .map(([name]) => name)
}

// The latest frame's [name, value] pairs, in the held ranking's order. Names the ranking does
// not know yet (it was taken from a frame that lacked them) follow, sorted by their current value.
export function orderFeatures(features, ranking, excluded = []) {
  const ranked = ranking.filter((name) => name in features && !excluded.includes(name))
  const known = new Set(ranked)
  const rest = rankFeatures(features, excluded).filter((name) => !known.has(name))
  return [...ranked, ...rest].map((name) => [name, features[name]])
}
