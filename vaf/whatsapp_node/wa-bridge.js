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
// v6 stable import. When Baileys v7 ships stable, rename package to "baileys" and run:
//   npm install baileys@^7.0.0 && npm uninstall @whiskeysockets/baileys
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, makeCacheableSignalKeyStore, DisconnectReason, isJidGroup, Browsers, downloadContentFromMessage, toBuffer } from "@whiskeysockets/baileys";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

const LOG_PREFIX = "[wa-bridge]";

/**
 * Emit JSON line to stdout. Uses sync write to avoid pipe buffering (Python must receive
 * "connected" immediately). stdout is a pipe Node opens non-blocking, so when Python has
 * not drained it yet (the history sync right after linking pushes hundreds of chat lists
 * of several KB each) writeSync raises EAGAIN. Uncaught, that killed the whole bridge one
 * second after "connected" (live 2026-09-04). Retry the remaining bytes until the pipe
 * accepts them; a short spin is fine here because the reader is a dedicated thread.
 */
function emit(obj) {
  const buf = Buffer.from(JSON.stringify(obj) + "\n", "utf8");
  let off = 0;
  let spins = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off, buf.length - off);
    } catch (err) {
      if (err && err.code === "EAGAIN" && spins < 20000) {
        spins += 1;
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2);
        continue;
      }
      throw err;
    }
  }
}

/** Chat-list emits are coalesced: the history sync fires chats.update per batch, and each
 *  emit serialises the WHOLE store. One line 300 ms after the last change is what Python
 *  needs; hundreds of near-identical multi-KB lines are what filled the pipe. */
let chatsEmitTimer = null;
function scheduleEmitChats() {
  if (chatsEmitTimer) return;
  chatsEmitTimer = setTimeout(() => {
    chatsEmitTimer = null;
    emitChatsNow();
  }, 300);
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
  if (m.reactionMessage) return "reaction";
  if (m.viewOnceMessage) return "view_once";
  if (m.contactMessage || m.contactsArrayMessage) return "contact";
  if (m.locationMessage) return "location";
  if (m.pollCreationMessage) return "poll";
  if (m.buttonsMessage || m.templateButtonReplyMessage) return "button";
  return "other";
}

let currentSock = null;
/** "open" | "connecting" | "close" | null. True connection state from Baileys. */
let connectionState = null;

/** Chat store: jid -> { jid, name, phone, is_group, last_ts } for all WhatsApp chats. */
const chatStore = new Map();

/** LID → E.164 from events (senderPn, chats.phoneNumberShare). Used when Baileys lidMapping has no entry. */
const lidToE164Map = new Map();

/**
 * Names the way WhatsApp Web shows them: the address-book name the phone synced
 * (contacts.upsert from the history sync and app-state contactAction), the verified
 * business name, or the push name the person set for themselves (contacts.update,
 * msg.pushName). Keyed by JID; a LID and its phone JID both point at the same entry.
 */
const contactStore = new Map(); // jid -> { name?, notify?, verifiedName? }
let currentAuthDir = null;
let currentAuth = null;
let contactsSaveTimer = null;

/** The contact store outlives a restart: WhatsApp sends address-book names only with a
 *  full sync (first link) and app-state deltas, so without a cache every restart would
 *  show numbers until each person writes again. */
function contactsCachePath() {
  return currentAuthDir ? path.join(currentAuthDir, "contacts-cache.json") : null;
}
function loadContactCache() {
  const p = contactsCachePath();
  if (!p || !fs.existsSync(p)) return;
  try {
    const data = JSON.parse(fs.readFileSync(p, "utf-8"));
    for (const [jid, c] of Object.entries(data?.contacts || {})) contactStore.set(jid, c);
    for (const [lid, e164] of Object.entries(data?.lids || {})) if (!lidToE164Map.has(lid)) lidToE164Map.set(lid, e164);
    // Chats (with their names and last activity) come only with a full history sync,
    // which WhatsApp sends once per link; without the cache a restart shows none.
    for (const [jid, c] of Object.entries(data?.chats || {})) if (!chatStore.has(jid) && c && c.jid) chatStore.set(jid, c);
    try { fs.writeSync(2, `${LOG_PREFIX} contact cache loaded: ${contactStore.size} names, ${lidToE164Map.size} lid mappings\n`); } catch (_) {}
  } catch (err) {
    try { fs.writeSync(2, `${LOG_PREFIX} contact cache unreadable: ${err?.message ?? err}\n`); } catch (_) {}
  }
}
function scheduleSaveContacts() {
  if (contactsSaveTimer) return;
  contactsSaveTimer = setTimeout(() => {
    contactsSaveTimer = null;
    const p = contactsCachePath();
    if (!p) return;
    try {
      const data = { contacts: Object.fromEntries(contactStore), lids: Object.fromEntries(lidToE164Map), chats: Object.fromEntries(chatStore) };
      fs.writeFileSync(p, JSON.stringify(data), { mode: 0o600 });
    } catch (err) {
      try { fs.writeSync(2, `${LOG_PREFIX} contact cache not saved: ${err?.message ?? err}\n`); } catch (_) {}
    }
  }, 2000);
}

function rememberContact(c) {
  if (!c) return;
  const ids = [c.id, c.jid, c.lid].filter((v) => typeof v === "string" && v.includes("@"));
  if (ids.length === 0) return;
  const patch = {};
  if (c.name) patch.name = c.name;
  if (c.notify) patch.notify = c.notify;
  if (c.verifiedName) patch.verifiedName = c.verifiedName;
  for (const id of ids) {
    const key = id.split(":")[0].includes("@") ? id.replace(/:\d+@/, "@") : id;
    contactStore.set(key, { ...(contactStore.get(key) || {}), ...patch });
  }
  if (c.lid && c.jid) {
    const lidJid = toLidJid(c.lid);
    const e164 = toE164(c.jid);
    if (lidJid && e164 && !lidToE164Map.has(lidJid)) lidToE164Map.set(lidJid, e164);
  }
  scheduleSaveContacts();
}

/** Display name for a chat JID from the contact store; empty when nothing is known. */
function displayNameFor(jid) {
  if (!jid || typeof jid !== "string") return "";
  const candidates = [jid];
  if (jid.endsWith("@lid")) {
    const e164 = lidToE164Map.get(jid);
    if (e164) candidates.push(`${digitsOnly(e164)}@s.whatsapp.net`);
  }
  for (const key of candidates) {
    const c = contactStore.get(key);
    if (!c) continue;
    const name = c.name || c.verifiedName || c.notify;
    if (name) return String(name).trim();
  }
  return "";
}

/** Recently sent text (self-chat echo prevention). Bot’s own replies must be ignored. */
const echoSent = new Map(); // text -> timestamp
const ECHO_TTL_MS = 90_000;

/** Per-jid: last message we (the bridge) sent, so we don't treat our own echo as owner_sent. */
const lastSentByUs = new Map(); // jid -> { text?: string, voiceTs?: number, ts: number }
const SENT_BY_US_TTL_MS = 95_000;

/** Normalize for echo match: trim and collapse repeated whitespace so minor differences don't break matching. */
function normalizeForEcho(text) {
  if (!text || typeof text !== "string") return "";
  return text.trim().replace(/\s+/g, " ");
}

function rememberSentText(text) {
  const t = normalizeForEcho(text);
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
  const t = normalizeForEcho(text);
  if (!t) return false;
  const ts = echoSent.get(t);
  if (!ts) return false;
  if (Date.now() - ts > ECHO_TTL_MS) {
    echoSent.delete(t);
    return false;
  }
  echoSent.delete(t);
  return true;
}

/** Record that we sent a text message to jid (so we don't emit owner_sent when the echo arrives). */
function rememberSentToJid(jid, text) {
  if (!jid) return;
  const now = Date.now();
  lastSentByUs.set(jid, { text: normalizeForEcho(text || ""), ts: now });
  for (const [k, v] of lastSentByUs.entries()) {
    if (now - v.ts > SENT_BY_US_TTL_MS) lastSentByUs.delete(k);
  }
}

/** Record that we sent a voice message to jid. */
function rememberSentVoiceToJid(jid) {
  if (!jid) return;
  const now = Date.now();
  const existing = lastSentByUs.get(jid) || { ts: 0 };
  lastSentByUs.set(jid, { ...existing, voiceTs: now, ts: now });
  for (const [k, v] of lastSentByUs.entries()) {
    if (now - v.ts > SENT_BY_US_TTL_MS) lastSentByUs.delete(k);
  }
}

/** True if this fromMe message is our own echo (we recently sent this to this jid). */
function isOurEchoToJid(remoteJid, body, contentType) {
  const rec = lastSentByUs.get(remoteJid);
  if (!rec || (Date.now() - rec.ts) > SENT_BY_US_TTL_MS) return false;
  const normBody = normalizeForEcho(body || "");
  if (normBody && rec.text && normBody === rec.text) return true;
  if ((!normBody || normBody === "<voice>" || normBody === "<media:audio>") && rec.voiceTs && (Date.now() - rec.voiceTs) < SENT_BY_US_TTL_MS) return true;
  return false;
}

function jidToPhone(jid) {
  if (!jid || typeof jid !== "string") return "";
  if (jid.endsWith("@lid") || jid.endsWith("@broadcast") || jid.endsWith("@status")) return "";
  const part = jid.split("@")[0].split(":")[0].trim();
  if (!part || !/^\d+$/.test(part)) return "";
  if (part.length < 7 || part.length > 15) return ""; // E.164 reasonable range
  return "+" + part;
}

/** Normalize sender_pn / JID to E.164 string. */
function toE164(value) {
  if (!value || typeof value !== "string") return "";
  const s = value.trim();
  if (s.startsWith("+") && /^\+?\d{7,15}$/.test(s.replace(/\s/g, ""))) return s.replace(/\s/g, "");
  return jidToPhone(s);
}

/** Ensure LID is full JID (e.g. 123456@lid). */
function toLidJid(lid) {
  if (!lid || typeof lid !== "string") return "";
  const s = lid.trim();
  if (s.endsWith("@lid") || s.endsWith("@hosted.lid")) return s;
  const digits = s.replace(/\D/g, "");
  return digits ? `${digits}@lid` : "";
}

/** Resolve @lid JID to E.164: first from event-built map (senderPn, phoneNumberShare), then Baileys lidMapping. */
async function resolveLidToE164(sock, jid) {
  if (!jid || !/(@lid|@hosted\.lid)$/.test(jid)) return null;
  const fromMap = lidToE164Map.get(jid);
  if (fromMap) return fromMap;
  const mapping = sock?.signalRepository?.lidMapping;
  if (!mapping?.getPNForLID || typeof mapping.getPNForLID !== "function") return null;
  for (const waitMs of [0, 400]) {
    if (waitMs > 0) await new Promise((r) => setTimeout(r, waitMs));
    try {
      const pnJid = await mapping.getPNForLID(jid);
      if (pnJid) return jidToPhone(pnJid);
    } catch (_) {
      /* ignore */
    }
  }
  return null;
}

/** Digits only for phone comparison (E.164 / JID). */
function digitsOnly(str) {
  return (str || "").replace(/\D/g, "");
}

/**
 * Check if remoteJid is the self-chat (messages to yourself).
 * For @s.whatsapp.net we compare numeric parts. For @lid we do NOT assume self-chat:
 * WhatsApp uses LID for multiple 1:1 chats (not only saved messages), so we must resolve
 * LID to E.164 and compare to self; only then treat as self-chat. Caller must handle @lid
 * via resolveLidToE164 + compare (see messages.upsert).
 */
function isSelfChat(remoteJid, selfJid, fromMe) {
  if (!remoteJid || typeof remoteJid !== "string") return false;
  if (!selfJid || typeof selfJid !== "string") return false;
  // Do NOT treat all @lid as self-chat – LID is used for other 1:1 chats too
  if (remoteJid.endsWith("@lid")) return false;
  const r = remoteJid.split("@")[0].split(":")[0].trim();
  const s = selfJid.split("@")[0].split(":")[0].trim();
  return r && s && r === s && remoteJid.includes("@s.whatsapp.net");
}

function normalizeChat(chat) {
  const id = chat?.id || chat?.jid || "";
  if (!id) return null;
  const isGroup = id.includes("@g.us");
  const phone = isGroup ? "" : jidToPhone(id);
  const name = displayNameFor(id) || chat?.name || chat?.pushName || chat?.notify || (phone || id);
  const rawTs = chat?.conversationTimestamp ?? chat?.lastMessageRecvTimestamp;
  const ts = rawTs ? (Number(rawTs) > 1e12 ? Math.floor(Number(rawTs) / 1000) : Number(rawTs)) : 0;
  return { jid: id, name: name || (phone || id), phone: phone || id, is_group: isGroup, last_ts: ts };
}

function emitChatsNow() {
  const list = Array.from(chatStore.values()).sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
  emit({ type: "chats", chats: list });
}

/** Event-driven chat-list update: coalesced (see scheduleEmitChats). An explicit getChats
 *  command answers immediately through emitChatsNow. */
function emitChats() {
  scheduleSaveContacts();
  scheduleEmitChats();
}

async function connect(authDir) {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  currentAuthDir = authDir;
  currentAuth = state;
  if (contactStore.size === 0) loadContactCache();
  const { version } = await fetchLatestBaileysVersion();
  // Baileys' own log lines about the app-state sync go to stderr (Python mirrors it into
  // whatsapp_qr.log); everything else stays silent. Those lines are the only way to see
  // whether a contact resync fetched a snapshot, how many mutations it carried, or why
  // it gave up, and that question came up live.
  const forward = (level) => (...args) => {
    try {
      const text = args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
      if (/sync|mutation|snapshot|contact|app.?state/i.test(text)) fs.writeSync(2, `${LOG_PREFIX} baileys.${level}: ${text.slice(0, 600)}\n`);
    } catch (_) {}
  };
  const logger = { fatal: forward("fatal"), error: forward("error"), warn: forward("warn"), info: forward("info"), debug: forward("debug"), trace: () => {}, child: () => logger };

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
    // Required for history sync / fetchMessageHistory so Baileys does not crash (e.g. reading remoteJid on undefined)
    getMessage: async () => undefined,
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
    if (connection != null) connectionState = connection;
    if (connection === "open") {
      const selfJid = sock.user?.id ?? null;
      emit({ type: "connected", selfJid });
    }
    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode ?? lastDisconnect?.error?.status;
      try {
        fs.writeSync(2, `${LOG_PREFIX} connection=close statusCode=${statusCode ?? "null"} – see WHATSAPP_INTEGRATION.md for 401/515/516\n`);
      } catch (_) {}
      emit({ type: "connection_closed", statusCode: statusCode ?? null });
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
          message: "Login failed (401/device_removed). Try: disable VPN, different network, or wait 24h. See docs/integrations/CONNECTIONS.md.",
        });
      } else if (statusCode != null) {
        emit({ type: "error", message: `Connection closed (code ${statusCode}). Reset & get new QR code.` });
      }
    }
  });

  sock.ev.on("messaging-history.set", (evt) => {
    // The sync carries the people too: address-book names (or push names) plus the
    // LID/phone pair of every conversation. That, not the app-state contact
    // collection, is where WhatsApp hands over names (live: the app-state records
    // carry only timestamps).
    const syncContacts = evt?.contacts;
    if (Array.isArray(syncContacts)) {
      for (const c of syncContacts) rememberContact(c);
      try { fs.writeSync(2, `${LOG_PREFIX} messaging-history.set: ${syncContacts.length} contacts (store ${contactStore.size})\n`); } catch (_) {}
    }
    const newChats = evt?.chats || evt?.conversations;
    const count = Array.isArray(newChats) ? newChats.length : 0;
    try {
      fs.writeSync(2, `${LOG_PREFIX} messaging-history.set: ${count} chats in this batch\n`);
    } catch (_) {}
    if (Array.isArray(newChats)) {
      for (const c of newChats) {
        const n = normalizeChat(c);
        if (n) chatStore.set(n.jid, n);
      }
      try {
        fs.writeSync(2, `${LOG_PREFIX} messaging-history.set: chatStore total now ${chatStore.size} chats\n`);
      } catch (_) {}
      emitChats();
    }
    // Emit history messages so Python can store them (chat history download)
    const historyMessages = evt?.messages;
    if (Array.isArray(historyMessages) && historyMessages.length > 0) {
      const BATCH = 200;
      for (let i = 0; i < historyMessages.length; i += BATCH) {
        const batch = historyMessages.slice(i, i + BATCH).map((msg) => {
          if (!msg?.key?.remoteJid) return null;
          const remoteJid = msg.key.remoteJid;
          if (isJidGroup(remoteJid)) return null; // Phase 1: DMs only
          const chatId = jidToPhone(remoteJid) || remoteJid;
          const body = extractText(msg) || (getContentType(msg) !== "text" ? `<media:${getContentType(msg)}>` : "");
          const direction = msg.key.fromMe ? "out" : "in";
          const ts = msg.messageTimestamp ? (Number(msg.messageTimestamp) > 1e12 ? Math.floor(Number(msg.messageTimestamp) / 1000) : Number(msg.messageTimestamp)) : 0;
          const messageId = msg.key.id || "";
          const contentType = getContentType(msg) || "text";
          return { chat_id: chatId, body, direction, ts, message_id: messageId, content_type: contentType };
        }).filter(Boolean);
        if (batch.length > 0) emit({ type: "history_messages", messages: batch });
      }
      try {
        fs.writeSync(2, `${LOG_PREFIX} messaging-history.set: emitted ${historyMessages.length} history messages to Python\n`);
      } catch (_) {}
    }
  });

  sock.ev.on("contacts.upsert", (contacts) => {
    if (!Array.isArray(contacts)) return;
    for (const c of contacts) rememberContact(c);
    try { fs.writeSync(2, `${LOG_PREFIX} contacts.upsert: ${contacts.length} (store ${contactStore.size})\n`); } catch (_) {}
    emitChats();
  });

  sock.ev.on("contacts.update", (updates) => {
    if (!Array.isArray(updates)) return;
    for (const c of updates) rememberContact(c);
    emitChats();
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

  sock.ev.on("chats.phoneNumberShare", ({ lid, jid }) => {
    const lidJid = toLidJid(lid);
    const e164 = jid ? toE164(jid) : "";
    if (lidJid && e164) {
      lidToE164Map.set(lidJid, e164);
      try { fs.writeSync(2, `${LOG_PREFIX} LID→E.164 from phoneNumberShare: ${lidJid} → ${e164}\n`); } catch (_) {}
      emitChats();
    }
  });

  // Baileys v7: new event fires whenever LID↔PN mappings are discovered (replaces / supplements phoneNumberShare)
  sock.ev.on("lid-mapping.update", (mappings) => {
    if (!Array.isArray(mappings)) return;
    let changed = false;
    for (const entry of mappings) {
      // entry shape: { lid: string, pn: string } (PN is the phone-number JID like 491234567890@s.whatsapp.net)
      const lidJid = entry?.lid ? toLidJid(entry.lid) : "";
      const e164 = entry?.pn ? toE164(entry.pn) : "";
      if (lidJid && e164 && !lidToE164Map.has(lidJid)) {
        lidToE164Map.set(lidJid, e164);
        changed = true;
        try { fs.writeSync(2, `${LOG_PREFIX} LID→E.164 from lid-mapping.update: ${lidJid} → ${e164}\n`); } catch (_) {}
      }
    }
    if (changed) emitChats();
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
      let selfChat = isSelfChat(remoteJid, selfJid, !!msg.key?.fromMe);
      if (msg.key?.fromMe && !selfChat) {
        const bodyForEcho = extractText(msg);
        const ct = getContentType(msg);
        const bodyOrVoice = (bodyForEcho && bodyForEcho.trim()) || (ct === "audio" ? "<voice>" : "");
        if (!isGroup && !isOurEchoToJid(remoteJid, bodyOrVoice, ct)) {
          emit({ type: "owner_sent", from: remoteJid, ts: Math.floor(Date.now() / 1000) });
        }
        continue; // skip own msgs except in self-chat
      }
      if (isGroup) continue; // Phase 1: DMs only
      // Status updates, newsletter posts and broadcast lists are not people writing to the
      // agent; handed to Python they would only be refused and logged as rejected senders.
      if (remoteJid === "status@broadcast" || remoteJid.endsWith("@newsletter") || remoteJid.endsWith("@broadcast")) continue;
      const senderJid = msg.key.participant ?? msg.key.remoteJid;
      if (msg.pushName && !msg.key?.fromMe) rememberContact({ id: remoteJid, notify: msg.pushName });
      const contentType = getContentType(msg);
      let body = extractText(msg);
      let voicePath = null;
      if (contentType === "audio") {
        const isPtt = msg.message?.audioMessage?.ptt === true;
        const dlType = isPtt ? "ptt" : "audio";
        try {
          const stream = await downloadContentFromMessage(msg.message.audioMessage, dlType);
          const buf = await toBuffer(stream);
          const ext = isPtt ? ".ogg" : ".opus";
          const tmpFile = path.join(os.tmpdir(), `vaf_wa_voice_${Date.now()}_${Math.random().toString(36).slice(2)}${ext}`);
          fs.writeFileSync(tmpFile, buf);
          try { fs.writeSync(2, `${LOG_PREFIX} voice downloaded: ${tmpFile} (${buf.length} bytes)\n`); } catch (_) {}
          voicePath = tmpFile;
          body = "<voice>";
        } catch (err) {
          try { fs.writeSync(2, `${LOG_PREFIX} voice download failed: ${err?.message ?? err}\n`); } catch (_) {}
          body = body || "<media:audio>";
        }
      } else if (!body && contentType !== "text") {
        // Use a short label so the agent can say "I can't process this type" (not "audio")
        body = `<media:${contentType}>`;
      }
      if (!body) continue;
      let fromE164 = null;
      try {
        if (remoteJid.endsWith("@lid") && msg.key?.senderPn) {
          const e164 = toE164(msg.key.senderPn);
          if (e164) lidToE164Map.set(remoteJid, e164);
        }
        fromE164 = jidToPhone(remoteJid);
        if (!fromE164 && remoteJid.endsWith("@lid")) {
          fromE164 = (await resolveLidToE164(sock, remoteJid)) || null;
          if (!fromE164) {
            try { fs.writeSync(2, `${LOG_PREFIX} LID unresolved (no fromE164): ${remoteJid} – add number to whitelist or ask contact to send again after sync\n`); } catch (_) {}
          }
        }
        if (remoteJid.endsWith("@lid")) {
          const selfPhone = jidToPhone(selfJid);
          selfChat = !!(fromE164 && selfPhone && digitsOnly(fromE164) === digitsOnly(selfPhone));
        }
      } catch (err) {
        try { fs.writeSync(2, `${LOG_PREFIX} message resolve error: ${err?.message ?? err}\n`); } catch (_) {}
      }
      if (selfChat && msg.key?.fromMe && isEcho(body)) continue; // ignore our own reply (echo)
      try {
        const payload = {
          type: "message",
          from: remoteJid,
          senderJid,
          body: body.trim(),
          chatType: "dm",
          messageId: msg.key?.id,
          selfChat: selfChat,
          pushName: displayNameFor(remoteJid) || msg.pushName || "",
        };
        if (fromE164) payload.fromE164 = fromE164;
        if (voicePath) payload.voice_path = voicePath;
        try { fs.writeSync(2, `${LOG_PREFIX} emitting message to Python from=${remoteJid}\n`); } catch (_) {}
        emit(payload);
      } catch (err) {
        try { fs.writeSync(2, `${LOG_PREFIX} message emit failed: ${err?.message ?? err}\n`); } catch (_) {}
      }
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
      const reqId = obj?.req_id || null;
      if (obj?.cmd === "send" && obj?.to && typeof obj?.text === "string") {
        try {
          fs.writeSync(2, `${LOG_PREFIX} send to=${obj.to} len=${(obj.text || "").length}\n`);
        } catch (_) {}
        if (connectionState !== "open") {
          const msg = "WhatsApp not connected";
          if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
          else emit({ type: "error", message: msg });
        } else {
          const text = obj.text;
          // Mark as sent NOW so when the echo arrives (often before sendMessage resolves) we skip it
          rememberSentText(text);
          rememberSentToJid(obj.to, text);
          const sendPromise = currentSock.sendMessage(obj.to, { text });
          const timeoutMs = 12000;
          const timeoutPromise = new Promise((_, reject) => {
            setTimeout(() => reject(new Error(`Send timeout after ${timeoutMs / 1000}s`)), timeoutMs);
          });
          Promise.race([sendPromise, timeoutPromise]).then(() => {
            if (reqId) emit({ type: "send_result", req_id: reqId, success: true });
          }).catch((err) => {
            const msg = `Send failed: ${err?.message ?? err}`;
            if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
            else emit({ type: "error", message: msg });
          });
        }
      } else if (obj?.cmd === "send_voice" && obj?.to && obj?.path) {
        if (connectionState !== "open") {
          const msg = "WhatsApp not connected";
          if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
          else emit({ type: "error", message: msg });
        } else if (!fs.existsSync(obj.path)) {
          const msg = `Voice file not found: ${obj.path}`;
          if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
          else emit({ type: "error", message: msg });
        } else {
          rememberSentVoiceToJid(obj.to);
          const p = obj.path;
          const buf = fs.readFileSync(p);
          try { fs.unlinkSync(p); } catch (_) {}
          const mimetype = p.toLowerCase().endsWith(".ogg") ? "audio/ogg; codecs=opus" : "audio/mpeg";
          const sendPromise = currentSock.sendMessage(obj.to, { audio: buf, mimetype }, { sendAudioAsVoice: true });
          const timeoutMs = 12000;
          const timeoutPromise = new Promise((_, reject) => {
            setTimeout(() => reject(new Error(`Send timeout after ${timeoutMs / 1000}s`)), timeoutMs);
          });
          Promise.race([sendPromise, timeoutPromise]).then(() => {
            if (reqId) emit({ type: "send_result", req_id: reqId, success: true });
          }).catch((err) => {
            const msg = `Voice send failed: ${err?.message ?? err}`;
            if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
            else emit({ type: "error", message: msg });
          });
        }
      } else if (obj?.cmd === "send_document" && obj?.to && obj?.path) {
        try {
          fs.writeSync(2, `${LOG_PREFIX} send_document to=${obj.to} path=${obj.path}\n`);
        } catch (_) {}
        if (connectionState !== "open") {
          const msg = "WhatsApp not connected";
          if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
          else emit({ type: "error", message: msg });
        } else if (!fs.existsSync(obj.path)) {
          const msg = `Document not found: ${obj.path}`;
          if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
          else emit({ type: "error", message: msg });
        } else {
          const buf = fs.readFileSync(obj.path);
          const base = path.basename(obj.path);
          const ext = path.extname(obj.path).toLowerCase();
          const mimeMap = { ".pdf": "application/pdf", ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png" };
          const mimetype = mimeMap[ext] || "application/octet-stream";
          const opts = obj.caption ? { caption: String(obj.caption) } : {};
          const sendPromise = currentSock.sendMessage(obj.to, { document: buf, mimetype, fileName: obj.fileName || base }, opts);
          const timeoutMs = 30000;
          const timeoutPromise = new Promise((_, reject) => {
            setTimeout(() => reject(new Error(`Document send timeout after ${timeoutMs / 1000}s`)), timeoutMs);
          });
          Promise.race([sendPromise, timeoutPromise]).then(() => {
            if (reqId) emit({ type: "send_result", req_id: reqId, success: true });
          }).catch((err) => {
            const msg = `Document send failed: ${err?.message ?? err}`;
            if (reqId) emit({ type: "send_result", req_id: reqId, success: false, error: msg });
            else emit({ type: "error", message: msg });
          });
        }
      } else if (obj?.cmd === "getChats") {
        try {
          fs.writeSync(2, `${LOG_PREFIX} getChats: emitting ${chatStore.size} chats\n`);
        } catch (_) {}
        emitChatsNow();
      } else if (obj?.cmd === "syncChats") {
        // Note: Baileys fetchMessageHistory(count, oldestMsgKey, oldestMsgTimestamp) is per-chat (more messages), not "full chat list".
        // The full chat list only comes from messaging-history.set (on connect). We just re-emit current chatStore so the dashboard refreshes.
        try {
          fs.writeSync(2, `${LOG_PREFIX} syncChats: emitting ${chatStore.size} chats (full list comes from WhatsApp on connect)\n`);
        } catch (_) {}
        emitChatsNow();
      } else if (obj?.cmd === "getLidMappings") {
        (async () => {
          const out = [];
          const seenLid = new Set();
          for (const [lid, e164] of lidToE164Map) {
            if (e164) { out.push({ lid, e164 }); seenLid.add(lid); }
          }
          const mapping = currentSock?.signalRepository?.lidMapping;
          if (mapping?.getPNForLID) {
            for (const chat of chatStore.values()) {
              const jid = chat?.jid || chat?.id || "";
              if (!jid || !jid.endsWith("@lid") || seenLid.has(jid)) continue;
              try {
                const pnJid = await mapping.getPNForLID(jid);
                const e164 = pnJid ? jidToPhone(pnJid) : null;
                if (e164) { out.push({ lid: jid, e164 }); seenLid.add(jid); }
              } catch (_) {}
            }
          }
          try { fs.writeSync(2, `${LOG_PREFIX} getLidMappings: ${out.length} resolved (${lidToE164Map.size} from events)\n`); } catch (_) {}
          emit({ type: "lid_mappings", mappings: out });
        })();
      } else if (obj?.cmd === "fetchHistory" && obj?.jid) {
        // On-demand history for ONE chat: ask the phone for `count` messages older than the
        // oldest one we hold. The messages themselves do not come back here; WhatsApp
        // answers later with a messaging-history.set of syncType ON_DEMAND, which the
        // handler above forwards as history_messages like any other batch. This result
        // only says whether the request left.
        (async () => {
          if (connectionState !== "open" || !currentSock) {
            if (reqId) emit({ type: "fetch_history_result", req_id: reqId, success: false, error: "WhatsApp not connected" });
            return;
          }
          try {
            const key = { remoteJid: obj.jid, fromMe: !!obj.oldestFromMe, id: obj.oldestId || "" };
            const tsMs = Number(obj.oldestTs || 0) > 1e12 ? Number(obj.oldestTs) : Math.floor(Number(obj.oldestTs || 0) * 1000);
            const count = Math.max(1, Math.min(Number(obj.count || 50), 200));
            try { fs.writeSync(2, `${LOG_PREFIX} fetchHistory jid=${obj.jid} count=${count} before=${key.id || "?"}@${tsMs}\n`); } catch (_) {}
            await currentSock.fetchMessageHistory(count, key, tsMs);
            if (reqId) emit({ type: "fetch_history_result", req_id: reqId, success: true });
          } catch (err) {
            const msg = err?.message ?? String(err);
            try { fs.writeSync(2, `${LOG_PREFIX} fetchHistory failed: ${msg}\n`); } catch (_) {}
            if (reqId) emit({ type: "fetch_history_result", req_id: reqId, success: false, error: msg });
          }
        })();
      } else if (obj?.cmd === "getAvatar" && obj?.jid) {
        // Profile picture the way WhatsApp Web shows it: a short-lived CDN URL from the
        // profile-picture query, downloaded here so Python only ever sees bytes. A
        // person who hides their picture answers with no URL (or 401/404); that is a
        // normal "found: false", not an error.
        (async () => {
          if (connectionState !== "open" || !currentSock) {
            if (reqId) emit({ type: "avatar", req_id: reqId, success: false, error: "WhatsApp not connected" });
            return;
          }
          try {
            let url = null;
            try {
              url = await currentSock.profilePictureUrl(obj.jid, obj.full ? "image" : "preview", 4000);
            } catch (err) {
              const code = err?.output?.statusCode ?? err?.data?.statusCode;
              const text = String(err?.message ?? err ?? "").toLowerCase();
              if (code === 401 || code === 404 || code === 403 || /item-not-found|not-authorized|forbidden|not-allowed/.test(text)) url = null;
              else throw err;
            }
            if (!url) {
              if (reqId) emit({ type: "avatar", req_id: reqId, success: true, found: false });
              return;
            }
            const res = await fetch(url);
            if (!res.ok) {
              if (reqId) emit({ type: "avatar", req_id: reqId, success: true, found: false });
              return;
            }
            const buf = Buffer.from(await res.arrayBuffer());
            const mime = res.headers.get("content-type") || "image/jpeg";
            if (reqId) emit({ type: "avatar", req_id: reqId, success: true, found: true, mime, b64: buf.toString("base64") });
          } catch (err) {
            const msg = err?.message ?? String(err);
            try { fs.writeSync(2, `${LOG_PREFIX} getAvatar failed for ${obj.jid}: ${msg}\n`); } catch (_) {}
            if (reqId) emit({ type: "avatar", req_id: reqId, success: false, error: msg });
          }
        })();
      } else if (obj?.cmd === "getContacts") {
        // Everything the contact store knows, as phone -> name, so Python can name the
        // chats it lists from the message store and fill the contact book even when
        // the Node's own chat list is empty (it is, after every restart without a sync).
        const out = [];
        const seen = new Set();
        for (const [jid, c] of contactStore) {
          const name = (c?.name || c?.verifiedName || c?.notify || "").toString().trim();
          if (!name) continue;
          let e164 = jidToPhone(jid);
          if (!e164 && jid.endsWith("@lid")) e164 = lidToE164Map.get(jid) || "";
          if (!e164 || seen.has(e164)) continue;
          seen.add(e164);
          out.push({ jid, e164, name, source: c?.name ? "addressbook" : c?.verifiedName ? "business" : "push" });
        }
        try { fs.writeSync(2, `${LOG_PREFIX} getContacts: ${out.length} named numbers (store ${contactStore.size})\n`); } catch (_) {}
        emit({ type: "contacts", contacts: out });
      } else if (obj?.cmd === "resyncContacts") {
        // Names come from the app-state "contact" collection (critical_unblock_low). A
        // reconnect only fetches deltas since the stored version, so a bridge that missed
        // the first sync never learns the existing names. Forgetting the local version of
        // that one collection makes the next resync a full snapshot: every contactAction
        // again, which is exactly what a fresh link receives.
        (async () => {
          if (connectionState !== "open" || !currentSock || !currentAuth) {
            if (reqId) emit({ type: "resync_contacts_result", req_id: reqId, success: false, error: "WhatsApp not connected" });
            return;
          }
          try {
            // Through the SOCKET's key store, not the file store underneath: the socket
            // wraps the files in a cache, and resyncAppState reads the collection version
            // from that cache. Clearing only the file left the old version in memory, the
            // snapshot then counted as "already known" and not one contact was emitted
            // (live 2026-09-04: "re-synced (store 0)").
            const keys = currentSock.authState?.keys ?? currentAuth.keys;
            await keys.set({ "app-state-sync-version": { critical_unblock_low: null } });
            await currentSock.resyncAppState(["critical_unblock_low"], true);
            try { fs.writeSync(2, `${LOG_PREFIX} resyncContacts: contact collection re-synced (store ${contactStore.size})\n`); } catch (_) {}
            emitChats();
            if (reqId) emit({ type: "resync_contacts_result", req_id: reqId, success: true, names: contactStore.size });
          } catch (err) {
            const msg = err?.message ?? String(err);
            try { fs.writeSync(2, `${LOG_PREFIX} resyncContacts failed: ${msg}\n`); } catch (_) {}
            if (reqId) emit({ type: "resync_contacts_result", req_id: reqId, success: false, error: msg });
          }
        })();
      } else if (obj?.cmd === "ping") {
        emit({ type: "pong", connected: connectionState === "open", state: connectionState });
      }
    } catch (_) {}
  });

  await connect(authDir);
}

main().catch((err) => {
  emit({ type: "error", message: err.message ?? String(err) });
  process.exit(1);
});
