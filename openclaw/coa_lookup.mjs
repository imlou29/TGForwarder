// Bounded, read-only catalogue query. No Telegram credentials or web search.
// node coa_lookup.mjs --gb Lobster --query RT10
const args = process.argv.slice(2);
const option = name => { const i = args.indexOf(name); return i < 0 ? "" : args[i + 1] || ""; };
const gb = (option("--gb") || option("--group-buy")).trim();
const query = option("--query").trim();
if (!gb) {
  console.log(JSON.stringify({status: "needs_gb", results: []}));
  process.exit(0);
}
if (!query || query.length > 200 || gb.length > 100) {
  console.log(JSON.stringify({status: "invalid_query", results: []}));
  process.exit(1);
}
try {
  const base = new URL(process.env.COA_CATALOG_URL);
  if (base.protocol !== "https:" || !process.env.COA_API_TOKEN) throw Error("configuration");
  const url = new URL("/v1/coas/search", base);
  url.searchParams.set("q", query);
  url.searchParams.set("gb", gb);
  const response = await fetch(url, {
    headers: {Authorization: `Bearer ${process.env.COA_API_TOKEN}`},
    signal: AbortSignal.timeout(15000), redirect: "error"
  });
  if (!response.ok) throw Error("unavailable");
  const text = await response.text();
  if (text.length > 150000) throw Error("response too large");
  console.log(JSON.stringify(JSON.parse(text)));
} catch {
  console.log(JSON.stringify({status: "catalog_unavailable", results: []}));
  process.exitCode = 1;
}
