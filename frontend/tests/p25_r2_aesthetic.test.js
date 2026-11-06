const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const js = fs.readFileSync(path.join(__dirname, "../static/loop_card.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/loop_card.css"), "utf8");

test("alert triangle is a 16px outline icon with stroke 1.5", () => {
  const svg = js.match(/<svg class="p25-alert-svg"[^>]*>/)[0];
  assert.match(svg, /width="16"/);
  assert.match(svg, /stroke-width="1.5"/);
  assert.match(svg, /fill="none"/);
  assert.doesNotMatch(js, /stroke-width="1\.3"/);
  assert.match(css, /\.p25-alert \.pos-liq-ico svg \{[^}]*stroke: #ffb340;[^}]*stroke-width: 1\.5;/);
});

test("empty window label is 12px white 48% on the text baseline", () => {
  const h5 = css.match(/\.p25-window--empty h5 \{[^}]*\}/)[0];
  assert.match(h5, /font-size: 12px/);
  assert.match(h5, /rgba\(255, 255, 255, 0\.48\)/);
  assert.match(css, /\.p25-window--empty \{[^}]*align-items: baseline;/);
});
