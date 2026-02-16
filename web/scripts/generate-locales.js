/**
 * Generate locale JSON files from the master (messages/de.json).
 * Usage: node scripts/generate-locales.js [locale-code ...]
 * Example: node scripts/generate-locales.js en fr
 * If no args are given, creates placeholder files for all codes in locale-codes.json (except de).
 *
 * After adding a new locale:
 * 1. Add it to web/lib/languages.ts
 * 2. Run this script to create messages/<code>.json
 * 3. Add the new locale to IntlProviderWrapper (messagesMap) and to localeStore (getStoredLocale check)
 */

const fs = require("fs");
const path = require("path");

const scriptDir = __dirname;
const rootDir = path.join(scriptDir, "..");
const messagesDir = path.join(rootDir, "messages");
const masterPath = path.join(messagesDir, "de.json");
const localeCodesPath = path.join(scriptDir, "locale-codes.json");

function main() {
  const args = process.argv.slice(2);
  let codes = args.length > 0 ? args : [];

  if (codes.length === 0 && fs.existsSync(localeCodesPath)) {
    try {
      const data = JSON.parse(fs.readFileSync(localeCodesPath, "utf-8"));
      codes = Array.isArray(data) ? data : (data.codes || []);
    } catch (e) {
      console.error("Could not read locale-codes.json:", e.message);
      process.exit(1);
    }
  }

  if (codes.length === 0) {
    console.log("Usage: node scripts/generate-locales.js [locale-code ...]");
    console.log("Example: node scripts/generate-locales.js en");
    console.log("Or create scripts/locale-codes.json with an array of codes.");
    process.exit(0);
  }

  if (!fs.existsSync(masterPath)) {
    console.error("Master file not found:", masterPath);
    process.exit(1);
  }

  const masterContent = fs.readFileSync(masterPath, "utf-8");
  let parsed;
  try {
    parsed = JSON.parse(masterContent);
  } catch (e) {
    console.error("Invalid JSON in master file:", e.message);
    process.exit(1);
  }

  if (!fs.existsSync(messagesDir)) {
    fs.mkdirSync(messagesDir, { recursive: true });
  }

  const force = process.argv.includes("--force");
  let created = 0;
  for (const code of codes) {
    if (code === "de") continue;
    const filePath = path.join(messagesDir, `${code}.json`);
    if (fs.existsSync(filePath) && !force) {
      console.log("  " + code + ".json (exists, skip; use --force to overwrite)");
      continue;
    }
    fs.writeFileSync(filePath, JSON.stringify(parsed, null, 2), "utf-8");
    created++;
    console.log("  " + code + ".json");
  }

  console.log("\nGenerated " + created + " locale file(s) from de.json.");
  console.log("Remember to add new locales to web/lib/languages.ts, IntlProviderWrapper (messagesMap), and localeStore (getStoredLocale).");
}

main();
