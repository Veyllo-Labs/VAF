#!/usr/bin/env node
/**
 * VAF WhatsApp Bridge - Baileys-based, stdio JSON IPC.
 *
 * Usage: node wa-bridge.js --auth-dir <path>
 *
 * Reads JSON lines from stdin:  { "cmd": "send", "to": "<jid>", "text": "..." }
 *                               { "cmd": "getChats" } -> responds with { "type": "chats", "chats": [...] }
 * Writes JSON lines to stdout:  { "type": "qr", "qr": "..." }
 *                               { "type": "connected", "selfJid": "..." }
 *                               { "type": "message", "from": "<jid>", "body": "...", "senderJid": "...", "chatType": "dm"|"group" }
 *                               { "type": "error", "message": "..." }
 *
 * DMs only for Phase 1; groups ignored.
 */
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, makeCacheableSignalKeyStore, DisconnectReason, isJidGroup, Browsers, downloadContentFromMessage, toBuffer } from "@whiskeysockets/baileys";
import fs from "node:fs";
import os from "node:os";
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

let currentSock = null;

/** Chat store: jid -> { jid, name, phone, is_group, last_ts } for all WhatsApp chats. */
const chatStore = new Map();

/** Recently sent text (self-chat echo prevention). Bot’s own replies must be ignored. */
const echoSent = new Map(); // text -> timestamp
const ECHO_TTL_MS = 90_000;

function rememberSentText(text) {
  if (!text || typeof text !== "string") return;
  const t = text.trim();
  if (!t) return;
  echoSent.set(t, Date.now());
  if (echoSent.size > 50) {
    const now = Date.now();
    for (const [k, ts] of echoSent.entries()) {
      if (now - ts > ECHO_TTL_MS) echoSent.delete(k);
    }
  }
}

function isEcho(text) {
  if (!text || typeof text !== "string") return false;
  const t = text.trim();
  const ts = echoSent.get(t);
  if (!ts) return false;
  if (Date.now() - ts > ECHO_TTL_MS) {
    echoSent.delete(t);
    return false;
  }
  echoSent.delete(t);
  return true;
}

function jidToPhone(jid) {
  if (!jid || typeof jid !== "string") return "";
  if (jid.endsWith("@lid") || jid.endsWith("@broadcast") || jid.endsWith("@status")) return "";
  const part = jid.split("@")[0].split(":")[0].trim();
  if (!part || !/^\d+$/.test(part)) return "";
  if (part.length < 7 || part.length > 15) return ""; // E.164 reasonable range
  return "+" + part;
}

/** Resolve @lid JID to E.164 via Baileys lidMapping (like clawdbot). Returns null if unresolved. */
async function resolveLidToE164(sock, jid) {
  if (!jid || !/(@lid|@hosted\.lid)$/.test(jid)) return null;
  const mapping = sock?.signalRepository?.lidMapping;
  if (!mapping?.getPNForLID || typeof mapping.getPNForLID !== "function") return null;
  try {
    const pnJid = await mapping.getPNForLID(jid);
    return pnJid ? jidToPhone(pnJid) : null;
  } catch (_) {
    return null;
  }
}

/** Check if remoteJid is the self-chat (messages to yourself). Baileys uses @lid for self-chat on newer sessions. */
function isSelfChat(remoteJid, selfJid, fromMe) {
  if (!remoteJid || typeof remoteJid !== "string") return false;
  // LID format: @lid chat is ALWAYS self-chat (saved messages); fromMe can be true or false on linked device
  if (remoteJid.endsWith("@lid")) return true;
  if (!selfJid || typeof selfJid !== "string") return false;
  const r = remoteJid.split("@")[0].split(":")[0].trim();
  const s = selfJid.split("@")[0].split(":")[0].trim();
  return r && s && r === s && remoteJid.includes("@s.whatsapp.net");
}

function normalizeChat(chat) {
  const id = chat?.id || chat?.jid || "";
  if (!id) return null;
  const isGroup = id.includes("@g.us");
  const phone = isGroup ? "" : jidToPhone(id);
  const name = chat?.name || chat?.pushName || chat?.notify || (phone || id);
  const rawTs = chat?.conversationTimestamp ?? chat?.lastMessageRecvTimestamp;
  const ts = rawTs ? (Number(rawTs) > 1e12 ? Math.floor(Number(rawTs) / 1000) : Number(rawTs)) : 0;
  return { jid: id, name: name || (phone || id), phone: phone || id, is_group: isGroup, last_ts: ts };
}

function emitChats() {
  const list = Array.from(chatStore.values()).sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
  emit({ type: "chats", chats: list });
}

async function connect(authDir) {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();
  const logger = { fatal: () => {}, error: () => {}, warn: () => {}, info: () => {}, debug: () => {}, trace: () => {}, child: () => logger };

  const sock = makeWASocket({
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
    version,
    logger,
    printQRInTerminal: false,
    browser: Browsers.ubuntu("Desktop"), // Desktop client for reliable messaging-history.set
    syncFullHistory: true, // must be true or messaging-history.set never fires – only way to get full chat list
    markOnlineOnConnect: false,
    connectTimeoutMs: 90000,
    defaultQueryTimeoutMs: 60000,
  });
  currentSock = sock;

  sock.ev.on("creds.update", () => {
    saveCreds().then(() => {
      try {
        const credsPath = resolveWebCredsPath(authDir);
        if (fs.existsSync(credsPath)) fs.chmodSync(credsPath, 0o600);
      } catch (_) {}
    }).catch(() => {});
  });

  sock.ev.on("connection.update", async (update) => {
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
      const statusCode = lastDisconnect?.error?.output?.statusCode ?? lastDisconnect?.error?.status;
      const conflict = lastDisconnect?.error?.output?.content?.[0];
      const conflictType = conflict?.attrs?.type;
      if (statusCode === DisconnectReason.loggedOut) {
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
      } else if (statusCode === 515 || statusCode === 516) {
        try {
          fs.writeSync(2, `${LOG_PREFIX} 515/516: saving creds and creating new socket...\n`);
        } catch (_) {}
        await saveCreds();
        await new Promise((r) => setTimeout(r, 1500));
        try {
          fs.writeSync(2, `${LOG_PREFIX} Creating new socket with saved credentials\n`);
        } catch (_) {}
        await connect(authDir);
      } else if (statusCode === 401 || conflictType === "device_removed") {
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
      } else if (statusCode != null) {
        emit({ type: "error", message: `Connection closed (code ${statusCode}). Reset & get new QR code.` });
      }
    }
  });

  sock.ev.on("messaging-history.set", (evt) => {
    const newChats = evt?.chats || evt?.conversations;
    try {
      fs.writeSync(2, `${LOG_PREFIX} messaging-history.set: ${Array.isArray(newChats) ? newChats.length : 0} chats\n`);
    } catch (_) {}
    if (Array.isArray(newChats)) {
      for (const c of newChats) {
        const n = normalizeChat(c);
        if (n) chatStore.set(n.jid, n);
      }
      emitChats();
    }
  });

  sock.ev.on("chats.upsert", (chats) => {
    if (Array.isArray(chats)) {
      for (const c of chats) {
        const n = normalizeChat(c);
        if (n) chatStore.set(n.jid, n);
      }
      emitChats();
    }
  });

  sock.ev.on("chats.update", (updates) => {
    if (Array.isArray(updates)) {
      for (const u of updates) {
        const jid = u?.id || u?.jid;
        if (!jid) continue;
        const existing = chatStore.get(jid);
        const merged = existing ? { ...existing, ...normalizeChat(u) } : normalizeChat(u);
        if (merged) chatStore.set(jid, merged);
      }
      emitChats();
    }
  });

  sock.ev.on("messages.upsert", async ({ messages }) => {
    const selfJid = sock.user?.id ?? "";
    for (const msg of messages) {
      const remoteJid = msg.key?.remoteJid;
      if (!remoteJid) continue;
      const isGroup = isJidGroup(remoteJid);
      const existing = chatStore.get(remoteJid);
      if (!existing) {
        const n = normalizeChat({
          id: remoteJid,
          pushName: msg.pushName,
          lastMessageRecvTimestamp: Math.floor(Date.now() / 1000),
        });
        if (n) chatStore.set(remoteJid, n);
      }
      const selfChat = isSelfChat(remoteJid, selfJid, !!msg.key?.fromMe);
      if (msg.key?.fromMe && !selfChat) continue; // skip own msgs except in self-chat
      if (isGroup) continue; // Phase 1: DMs only
      const senderJid = msg.key.participant ?? msg.key.remoteJid;
      const contentType = getContentType(msg);
      let body = extractText(msg);
      let voicePath = null;
      if (contentType === "audio") {
        const isPtt = msg.message?.audioMessage?.ptt === true;
        const dlType = isPtt ? "ptt" : "audio";
        try {
          const stream = await downloadContentFromMessage(msg, dlType);
          const buf = await toBuffer(stream);
          const ext = isPtt ? ".ogg" : ".opus";
          const tmpFile = path.join(os.tmpdir(), `vaf_wa_voice_${Date.now()}_${Math.random().toString(36).slice(2)}${ext}`);
          fs.writeFileSync(tmpFile, buf);
          voicePath = tmpFile;
          body = "<voice>";
        } catch (err) {
          try { fs.writeSync(2, `${LOG_PREFIX} voice download failed: ${err?.message ?? err}\n`); } catch (_) {}
          body = body || "<media:audio>";
        }
      } else if (!body && contentType !== "text") {
        body = `<media:${contentType}>`;
      }
      if (!body) continue;
      if (selfChat && msg.key?.fromMe && isEcho(body)) continue; // ignore our own reply (echo)
      // Resolve @lid to E.164 via Baileys lidMapping (clawdbot pattern) for whitelist/session
      let fromE164 = jidToPhone(remoteJid);
      if (!fromE164 && remoteJid.endsWith("@lid")) {
        fromE164 = (await resolveLidToE164(sock, remoteJid)) || null;
      }
      const payload = {
        type: "message",
        from: remoteJid,
        senderJid,
        body: body.trim(),
        chatType: "dm",
        messageId: msg.key?.id,
        selfChat: selfChat,
      };
      if (fromE164) payload.fromE164 = fromE164;
      if (voicePath) payload.voice_path = voicePath;
      emit(payload);
    }
  });

}

async function main() {
  const { authDir } = parseArgs();
  if (!fs.existsSync(authDir)) {
    fs.mkdirSync(authDir, { recursive: true });
  }
  maybeRestoreCredsFromBackup(authDir);

  process.on("SIGINT", () => process.exit(0));
  process.on("SIGTERM", () => process.exit(0));

  readline.createInterface({ input: process.stdin }).on("line", (line) => {
    try {
      const obj = JSON.parse(line);
      if (obj?.cmd === "send" && obj?.to && typeof obj?.text === "string" && currentSock) {
        const text = obj.text;
        currentSock.sendMessage(obj.to, { text }).then(() => {
          rememberSentText(text);
        }).catch((err) => {
          emit({ type: "error", message: `Send failed: ${err.message}` });
        });
      } else if (obj?.cmd === "send_voice" && obj?.to && obj?.path && currentSock) {
        const p = obj.path;
        if (fs.existsSync(p)) {
          const buf = fs.readFileSync(p);
          try { fs.unlinkSync(p); } catch (_) {}
          const mimetype = p.toLowerCase().endsWith(".ogg") ? "audio/ogg" : "audio/mpeg";
          currentSock.sendMessage(obj.to, { audio: buf, mimetype }, { sendAudioAsVoice: true }).catch((err) => {
            emit({ type: "error", message: `Voice send failed: ${err.message}` });
          });
        } else {
          emit({ type: "error", message: `Voice file not found: ${p}` });
        }
      } else if (obj?.cmd === "getChats") {
        try {
          fs.writeSync(2, `${LOG_PREFIX} getChats: emitting ${chatStore.size} chats\n`);
        } catch (_) {}
        emitChats();
      }
    } catch (_) {}
  });

  await connect(authDir);
}

main().catch((err) => {
  emit({ type: "error", message: err.message ?? String(err) });
  process.exit(1);
});
