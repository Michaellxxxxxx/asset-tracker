const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const read = (f) => fs.readFileSync(path.join(__dirname, "..", "static", f), "utf8");

test("app.js api() sets Authorization Bearer and is exposed as AssetTrackerApi.request", () => {
  const src = read("app.js");
  const body = src.slice(src.indexOf("async function api("), src.indexOf("function expireSession("));
  assert.match(body, /headers\.Authorization = `Bearer \$\{state\.token\}`/);
  assert.match(body, /window\.AssetTrackerApi = \{ request: api \}/);
});

test("loop_card loop request goes through the authenticated shared api", async () => {
  const src = read("loop_card.js");
  assert.match(src, /window\.AssetTrackerApi/);
  assert.match(src, /shared\.request\(path\)/);
  assert.match(src, /`\/lending\/positions\/\$\{encodeURIComponent\(meta\.id\)\}\/loop`/);
});
