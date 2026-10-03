// Build-time LaTeX -> MathML conversion with the vendored Temml (MIT). Never shipped to the site.
// stdin:  JSON array of {tex, display}
// stdout: JSON array of {ok, mathml} or {ok: false, error}
"use strict";
const temml = require("./vendor/temml.cjs");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const items = JSON.parse(input);
  const out = items.map(({ tex, display }) => {
    try {
      const mathml = temml.renderToString(tex, {
        displayMode: !!display,
        throwOnError: true,
        annotate: false,
        trust: false,
        xml: false,
      });
      return { ok: true, mathml };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  });
  process.stdout.write(JSON.stringify(out));
});
