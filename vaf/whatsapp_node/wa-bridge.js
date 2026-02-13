#!/usr/bin/env node
/**
 * VAF WhatsApp Bridge - Baileys-based, stdio JSON IPC.
 *
 * Usage: node wa-bridge.js --auth-dir <path>
 *
 * Reads JSON lines from stdin:  { "cmd": "send", "to": "<jid>", "text": "..." }
 * Writes JSON lines to stdout:  { "type": "qr", "qr": "..." }
 *                               { "type": "connected", "selfJid": "..." }
 *                               { "type": "message", "from": "<jid>", "body": "...", "senderJid": "...", "chatType": "dm"|"group" }
 *                               { "type": "error", "message": "..." }
 *
 * DMs only for Phase 1; groups ignored.
 */
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, makeCacheableSignalKeyStore, DisconnectReason, isJidGroup } from "@whiskeysockets/baileys";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";

const LOG_PREFIX = "[wa-bridge]";

/** Emit JSON line to stdout. Uses sync write to avoid pipe buffering (Python must receive "connected" immediately). */
function emit(obj) {
  fs.writeSync(1, JSON.stringify(obj) + "\n");
}

function parseArgs() {
  const args = process.argv.slice(2);
  let authDir = null;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--auth-dir" && args[i + 1]) {
      authDir = args[++i];
      break;
    }
  }
  if (!authDir) {
    emit({ type: "error", message: "Missing --auth-dir" });
    process.exit(1);
  }
  return { authDir };
}

function resolveWebCredsPath(authDir) {
  return path.join(authDir, "creds.json");
}

function maybeRestoreCredsFromBackup(authDir) {
  const credsPath = resolveWebCredsPath(authDir);
  const backupPath = path.join(authDir, "creds.json.bak");
  try {
    const raw = fs.existsSync(credsPath) ? fs.readFileSync(credsPath, "utf-8") : null;
    if (raw) {
      JSON.parse(raw);
      return;
    }
    const backupRaw = fs.existsSync(backupPath) ? fs.readFileSync(backupPath, "utf-8") : null;
    if (!backupRaw) return;
    JSON.parse(backupRaw);
    fs.copyFileSync(backupPath, credsPath);
    try { fs.chmodSync(credsPath, 0o600); } catch (_) {}
  } catch (_) {}
}

function extractText(msg) {
  if (!msg?.message) return "";
  const m = msg.message;
  if (m.conversation) return m.conversation;
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text;
  if (m.imageMessage?.caption) return m.imageMessage.caption;
  if (m.videoMessage?.caption) return m.videoMessage.caption;
  if (m.documentMessage?.caption) return m.documentMessage.caption;
  return "";
}

function getContentType(msg) {
  if (!msg?.message) return "text";
  const m = msg.message;
  if (m.conversation || m.extendedTextMessage) return "text";
  if (m.imageMessage) return "image";
  if (m.videoMessage) return "video";
  if (m.audioMessage) return "audio";
  if (m.documentMessage) return "document";
  if (m.stickerMessage) return "sticker";
  return "unknown";
}

async function main() {
  const { authDir } = parseArgs();
  if (!fs.existsSync(authDir)) {
    fs.mkdirSync(authDir, { recursive: true });
  }
  maybeRestoreCredsFromBackup(authDir);

  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();

  const logger = { fatal: () => {}, error: () => {}, warn: () => {}, info: () => {}, debug: () => {}, trace: () => {}, child: () => logger };
  const sock = makeWASocket({
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
    version,
    logger,
    printQRInTerminal: false,
    browser: ["vaf", "wa-bridge", "1.0"],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    connectTimeoutMs: 90000,
    defaultQueryTimeoutMs: 60000,
  });

  sock.ev.on("creds.update", () => {
    saveCreds().then(() => {
      try {
        const credsPath = resolveWebCredsPath(authDir);
        if (fs.existsSync(credsPath)) fs.chmodSync(credsPath, 0o600);
      } catch (_) {}
    }).catch(() => {});
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    const status = lastDisconnect?.error?.output?.statusCode ?? lastDisconnect?.error?.status;
    try {
      fs.writeSync(2, `${LOG_PREFIX} connection.update: connection=${connection ?? "null"} qr=${!!qr} status=${status ?? "null"}\n`);
    } catch (_) {}
    if (qr) emit({ type: "qr", qr });
    if (connection === "open") {
      const selfJid = sock.user?.id ?? null;
      emit({ type: "connected", selfJid });
    }
    if (connection === "close") {
      const status = lastDisconnect?.error?.output?.statusCode ?? lastDisconnect?.error?.status;
      const conflict = lastDisconnect?.error?.output?.content?.[0];
      const conflictType = conflict?.attrs?.type;
      if (status === DisconnectReason.loggedOut) {
        try {
          const credsPath = resolveWebCredsPath(authDir);
          const backupPath = path.join(authDir, "creds.json.bak");
          if (fs.existsSync(credsPath)) fs.unlinkSync(credsPath);
          if (fs.existsSync(backupPath)) fs.unlinkSync(backupPath);
          const files = fs.readdirSync(authDir);
          for (const f of files) {
            if (f.startsWith("app-state-sync-")) {
              fs.unlinkSync(path.join(authDir, f));
            }
          }
        } catch (_) {}
        emit({ type: "error", message: "Logged out. Auth cleared. Use Reset to get a new QR code." });
      } else if (status === 515 || status === 516) {
        /* 515 = restart required, 516 = stream:error after scan. Normal during QR scan. Baileys auto-retries; do not emit error. */
      } else if (status === 401 || conflictType === "device_removed") {
        try {
          const credsPath = resolveWebCredsPath(authDir);
          const backupPath = path.join(authDir, "creds.json.bak");
          if (fs.existsSync(credsPath)) fs.unlinkSync(credsPath);
          if (fs.existsSync(backupPath)) fs.unlinkSync(backupPath);
          const files = fs.readdirSync(authDir);
          for (const f of files) {
            if (f.startsWith("app-state-sync-")) {
              fs.unlinkSync(path.join(authDir, f));
            }
          }
        } catch (_) {}
        emit({
          type: "error",
          message: "Login failed (401/device_removed). Try: disable VPN, different network, or wait 24h. See docs/CONNECTIONS.md.",
        });
      } else if (status != null) {
        emit({ type: "error", message: `Connection closed (code ${status}). Reset & get new QR code.` });
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const msg of messages) {
      if (msg.key?.fromMe) continue;
      const remoteJid = msg.key?.remoteJid;
      if (!remoteJid) continue;
      const isGroup = isJidGroup(remoteJid);
      if (isGroup) continue; // Phase 1: DMs only
      const senderJid = msg.key.participant ?? msg.key.remoteJid;
      const contentType = getContentType(msg);
      let body = extractText(msg);
      if (!body && contentType !== "text") {
        body = `<media:${contentType}>`;
      }
      if (!body) continue;
      emit({
        type: "message",
        from: remoteJid,
        senderJid,
        body: body.trim(),
        chatType: "dm",
        messageId: msg.key?.id,
      });
    }
  });

  readline.createInterface({ input: process.stdin }).on("line", (line) => {
    try {
      const obj = JSON.parse(line);
      if (obj?.cmd === "send" && obj?.to && typeof obj?.text === "string") {
        sock.sendMessage(obj.to, { text: obj.text }).catch((err) => {
          emit({ type: "error", message: `Send failed: ${err.message}` });
        });
      }
    } catch (_) {}
  });

  process.on("SIGINT", () => process.exit(0));
  process.on("SIGTERM", () => process.exit(0));
}

main().catch((err) => {
  emit({ type: "error", message: err.message ?? String(err) });
  process.exit(1);
});
