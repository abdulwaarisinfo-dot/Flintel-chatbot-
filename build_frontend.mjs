// Frontend build: templates/ (readable source) -> dist/templates + dist/assets (protected)
//
// - Inline <script> bodies  -> terser (minify+mangle) -> javascript-obfuscator -> /assets/<name>.<hash>.js
// - Inline <style> bodies   -> clean-css                                         -> /assets/<name>.<hash>.css
// - Scripts smaller than SMALL_INLINE bytes stay inline (theme/FOUC bootstraps) but are still minified+obfuscated.
// - <script type="application/ld+json"> and <script src=...> are left alone. Jinja tags are never touched.
// The source templates/ folder is NOT modified.

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { minify } from "terser";
import JavaScriptObfuscator from "javascript-obfuscator";
import CleanCSS from "clean-css";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(ROOT, "templates");
const OUT_T = path.join(ROOT, "dist", "templates");
const OUT_A = path.join(ROOT, "dist", "assets");
const SMALL_INLINE = 1200;
const OBFUSCATE = process.env.OBFUSCATE !== "0";

fs.rmSync(path.join(ROOT, "dist"), { recursive: true, force: true });
fs.mkdirSync(OUT_T, { recursive: true });
fs.mkdirSync(OUT_A, { recursive: true });

const hash = (s) => crypto.createHash("sha256").update(s).digest("hex").slice(0, 10);

// Moderate settings: keeps the 200 KB scripts fast. renameGlobals stays false so
// anything shared between scripts via global names keeps working.
const OBF = {
  compact: true,
  renameGlobals: false,
  identifierNamesGenerator: "hexadecimal",
  stringArray: true,
  stringArrayThreshold: 0.75,
  stringArrayEncoding: ["base64"],
  stringArrayRotate: true,
  stringArrayShuffle: true,
  splitStrings: false,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  selfDefending: false,
  debugProtection: false,
  disableConsoleOutput: false,
  transformObjectKeys: false,
  numbersToExpressions: false,
  simplify: true,
  target: "browser",
  sourceMap: false,
};

async function protectJs(code, label) {
  const min = await minify(code, {
    compress: { passes: 2 },
    mangle: { toplevel: false }, // top-level names stay: other scripts may read them
    format: { comments: false },
  });
  if (min.error || !min.code) throw new Error(`terser failed on ${label}`);
  return OBFUSCATE ? JavaScriptObfuscator.obfuscate(min.code, OBF).getObfuscatedCode() : min.code;
}

const css = new CleanCSS({ level: 1 });

for (const file of fs.readdirSync(SRC).filter((f) => f.endsWith(".html"))) {
  const name = path.basename(file, ".html");
  let html = fs.readFileSync(path.join(SRC, file), "utf8");
  let jsN = 0, cssN = 0;

  // ---- scripts ----
  const scripts = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
  let out = "", last = 0;
  for (const m of scripts) {
    const [full, attrs, body] = m;
    out += html.slice(last, m.index);
    last = m.index + full.length;
    const skip = /\bsrc\s*=/.test(attrs) || /type\s*=\s*["'](?!text\/javascript|module)/.test(attrs) || !body.trim();
    if (skip) { out += full; continue; }
    if (/\{\{|\{%/.test(body)) { console.warn(`  ! ${file}: Jinja inside script, left as-is`); out += full; continue; }
    const code = await protectJs(body, `${file}#${jsN}`);
    if (body.length < SMALL_INLINE) {
      out += `<script${attrs}>${code}</script>`;
    } else {
      const fn = `${name}.${jsN}.${hash(code)}.js`;
      fs.writeFileSync(path.join(OUT_A, fn), code);
      out += `<script${attrs} src="/assets/${fn}"></script>`;
    }
    jsN++;
  }
  html = out + html.slice(last);

  // ---- styles ----
  html = html.replace(/<style([^>]*)>([\s\S]*?)<\/style>/g, (full, attrs, body) => {
    if (/\{\{|\{%/.test(body)) return full;
    const min = css.minify(body);
    if (min.errors.length) throw new Error(`css error in ${file}: ${min.errors[0]}`);
    const fn = `${name}.${cssN++}.${hash(min.styles)}.css`;
    fs.writeFileSync(path.join(OUT_A, fn), min.styles);
    return `<link rel="stylesheet" href="/assets/${fn}">`;
  });

  fs.writeFileSync(path.join(OUT_T, file), html);
  console.log(`${file}: ${jsN} js, ${cssN} css extracted  (${fs.statSync(path.join(SRC, file)).size} -> ${fs.statSync(path.join(OUT_T, file)).size} bytes html)`);
}
console.log("done ->", path.relative(ROOT, path.join(ROOT, "dist")));
