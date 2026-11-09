// Exécute le moteur JS du dashboard sur des cas JSON (stdin) et renvoie les résultats (stdout).
const E = require(process.argv[2]);
let buf = "";
process.stdin.on("data", c => { buf += c; });
process.stdin.on("end", () => {
  const cases = JSON.parse(buf);
  const out = cases.map(c => {
    let inp = c.inputs;
    if (c.changes) inp = E.applyChanges(inp, c.changes, c.value);
    const res = { result: E.runModel(inp) };
    if (c.targets) res.max_price = E.maxPrice(inp, c.targets);
    if (c.changes) {
      const r = {}; for (const k of Object.keys(inp)) if (!k.startsWith("_")) r[k] = inp[k];
      res.changed = JSON.parse(JSON.stringify(r, (k, v) => (v && typeof v === "object" && "y" in v && "m" in v && "d" in v)
        ? `${v.y}-${String(v.m).padStart(2, "0")}-${String(v.d).padStart(2, "0")}` : v));
    }
    return res;
  });
  process.stdout.write(JSON.stringify(out));
});
