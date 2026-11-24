const express = require('express');
const fs = require('fs');
const path = require('path');
const pino = require('pino');

const app = express();
app.use(express.json({ limit: '10mb' }));

const WA_PORT = parseInt(process.env.WA_BRIDGE_PORT || '3006', 10);
const PY_PORT = parseInt(process.env.PORT || '7860', 10);
const WA_AUTH_DIR = process.env.WA_AUTH_DIR || path.join(__dirname, '..', 'work_data', 'wa_auth_baileys');
const WA_INVITE_CODE = (process.env.WA_INVITE_CODE || 'Ddh7BKb1atCAhRzogy9l9Y').trim();
const EXPLICIT_GROUP_JID = (process.env.WHATSAPP_GROUP_ID || '').trim();

fs.mkdirSync(WA_AUTH_DIR, { recursive: true });

let makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers, makeCacheableSignalKeyStore;
let sock = null;
let isSocketConnected = false;
let isConnecting = false;
let latestQrData = null;
let resolvedGroupJid = EXPLICIT_GROUP_JID || null;
let resolvedGroupSubject = null;
let cachedWaVersion = [2, 3000, 1043857760];
const groupMetadataCache = new Map();

function notifyPython(eventType, extra = {}) {
    fetch(`http://127.0.0.1:${PY_PORT}/internal/wa_event`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event: eventType, ...extra })
    }).catch(() => {});
}

function withTimeout(promise, ms, label = 'Operation') {
    let timer;
    const timeoutPromise = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    });
    return Promise.race([promise, timeoutPromise]).finally(() => clearTimeout(timer));
}

async function resolveTargetGroupJid(activeSock) {
    if (EXPLICIT_GROUP_JID) {
        resolvedGroupJid = EXPLICIT_GROUP_JID;
        return resolvedGroupJid;
    }
    if (resolvedGroupJid) return resolvedGroupJid;

    if (WA_INVITE_CODE) {
        try {
            const info = await withTimeout(activeSock.groupGetInviteInfo(WA_INVITE_CODE), 12000, 'groupGetInviteInfo');
            if (info && info.id) {
                resolvedGroupJid = info.id.endsWith('@g.us') ? info.id : `${info.id}@g.us`;
                resolvedGroupSubject = info.subject || null;
                console.log(`[WA] Resolved group from invite code: ${resolvedGroupJid} (${resolvedGroupSubject})`);
                return resolvedGroupJid;
            }
        } catch (e) {
            console.warn('[WA] groupGetInviteInfo warning:', e.message);
        }
    }

    try {
        const groups = await withTimeout(activeSock.groupFetchAllParticipating(), 15000, 'groupFetchAllParticipating');
        const entries = Object.values(groups || {});
        const match = entries.find(g => {
            const subj = (g.subject || '').toUpperCase();
            return subj.includes('PIVOK') || subj.includes('PÎVOK') || subj.includes('PİVOK');
        });
        if (match && match.id) {
            resolvedGroupJid = match.id;
            resolvedGroupSubject = match.subject || null;
            console.log(`[WA] Resolved group by subject: ${resolvedGroupJid} (${resolvedGroupSubject})`);
            return resolvedGroupJid;
        }
    } catch (e) {
        console.warn('[WA] groupFetchAllParticipating warning:', e.message);
    }

    return null;
}

async function waitForSocket(maxWaitMs = 20000) {
    const start = Date.now();
    while ((!sock || !isSocketConnected) && (Date.now() - start < maxWaitMs)) {
        await new Promise(r => setTimeout(r, 250));
    }
    if (!sock || !isSocketConnected) {
        throw new Error('WhatsApp is not connected. Please scan the QR code via 📱 WhatsApp QR.');
    }
    return sock;
}

async function connectToWhatsApp() {
    if (isConnecting || !makeWASocket) return;
    isConnecting = true;
    try {
        if (sock) {
            try {
                sock.ev.removeAllListeners();
                sock.end(undefined);
            } catch (_) {}
            sock = null;
        }

        fs.mkdirSync(WA_AUTH_DIR, { recursive: true });
        const { state, saveCreds } = await useMultiFileAuthState(WA_AUTH_DIR);

        let version = cachedWaVersion;
        try {
            const verRes = await withTimeout(fetchLatestBaileysVersion(), 5000, 'fetchLatestBaileysVersion');
            if (verRes && verRes.version) {
                version = verRes.version;
                cachedWaVersion = version;
            }
        } catch (_) {}

        sock = makeWASocket({
            version,
            browser: Browsers.macOS('Desktop'),
            auth: {
                creds: state.creds,
                keys: makeCacheableSignalKeyStore(state.keys, pino({ level: 'silent' }))
            },
            printQRInTerminal: false,
            logger: pino({ level: 'silent' }),
            syncFullHistory: false,
            cachedGroupMetadata: async (jid) => {
                const entry = groupMetadataCache.get(jid);
                if (entry && (Date.now() - entry.ts < 1800000)) return entry.data;
                return undefined;
            },
            getMessage: async () => undefined
        });

        const origGroupMetadata = sock.groupMetadata.bind(sock);
        sock.groupMetadata = async (jid) => {
            const meta = await origGroupMetadata(jid);
            if (meta) groupMetadataCache.set(jid, { data: meta, ts: Date.now() });
            return meta;
        };

        sock.ev.on('creds.update', async () => {
            await saveCreds();
            notifyPython('creds_updated');
        });

        sock.ev.on('connection.update', async (update) => {
            const { connection, lastDisconnect, qr } = update;
            if (qr) {
                latestQrData = qr;
                notifyPython('qr', { qr });
            }
            if (connection === 'close') {
                isSocketConnected = false;
                const statusCode = lastDisconnect?.error?.output?.statusCode;
                if (statusCode === DisconnectReason.connectionReplaced || statusCode === 440) {
                    console.log('[WA] Connection replaced by newer instance — standing down 45s.');
                    setTimeout(connectToWhatsApp, 45000);
                } else if (statusCode !== DisconnectReason.loggedOut) {
                    setTimeout(connectToWhatsApp, 2500);
                } else {
                    try { fs.rmSync(WA_AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
                    fs.mkdirSync(WA_AUTH_DIR, { recursive: true });
                    notifyPython('logged_out');
                    setTimeout(connectToWhatsApp, 2000);
                }
            } else if (connection === 'open') {
                isSocketConnected = true;
                latestQrData = null;
                console.log('[WA] Connected successfully!');
                try {
                    if (typeof sock.refreshMediaConn === 'function') {
                        sock.refreshMediaConn(true).catch(() => {});
                    }
                } catch (_) {}
                await resolveTargetGroupJid(sock);
                notifyPython('connected', {
                    group_jid: resolvedGroupJid,
                    group_subject: resolvedGroupSubject
                });
            }
        });
    } catch (e) {
        console.error('[WA] connectToWhatsApp error:', e.message);
        isSocketConnected = false;
        setTimeout(connectToWhatsApp, 3500);
    } finally {
        isConnecting = false;
    }
}

app.get('/status', async (req, res) => {
    if (isSocketConnected && sock && !resolvedGroupJid) {
        await resolveTargetGroupJid(sock);
    }
    res.json({
        connected: isSocketConnected,
        qr: isSocketConnected ? null : latestQrData,
        group_jid: resolvedGroupJid,
        group_subject: resolvedGroupSubject
    });
});

app.post('/reset-session', async (req, res) => {
    try {
        isSocketConnected = false;
        latestQrData = null;
        if (sock) {
            try { sock.ev.removeAllListeners(); sock.end(undefined); } catch (_) {}
            sock = null;
        }
        try { fs.rmSync(WA_AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
        fs.mkdirSync(WA_AUTH_DIR, { recursive: true });
        connectToWhatsApp();
        res.json({ ok: true });
    } catch (e) {
        res.status(500).json({ ok: false, error: e.message });
    }
});

let sendQueue = Promise.resolve();
function enqueueSend(fn) {
    const next = sendQueue.then(() => fn());
    sendQueue = next.catch(() => {});
    return next;
}

app.post('/send-batch', async (req, res) => {
    try {
        const { target, files, caption, append_dash } = req.body;
        if (!Array.isArray(files) || files.length === 0) {
            return res.status(400).json({ success: false, error: 'No collage files provided' });
        }

        const activeSock = await waitForSocket(20000);
        let chatId = (target || '').trim();
        if (!chatId) {
            chatId = await resolveTargetGroupJid(activeSock);
        }
        if (!chatId) {
            return res.status(400).json({
                success: false,
                error: 'Could not find the PIVOK WhatsApp group on this WhatsApp account. Make sure the linked phone is a member of the PIVOK WhatsApp group.'
            });
        }
        if (!chatId.includes('@')) {
            chatId = `${chatId}@s.whatsapp.net`;
        }

        const payloads = [];
        for (let i = 0; i < files.length; i++) {
            if (fs.existsSync(files[i])) {
                payloads.push({ index: i, buffer: fs.readFileSync(files[i]) });
            }
        }
        if (payloads.length === 0) {
            return res.status(400).json({ success: false, error: 'Collage image files not found on disk' });
        }

        await enqueueSend(async () => {
            for (let idx = 0; idx < payloads.length; idx++) {
                const { index: i, buffer } = payloads[idx];
                let ok = false;
                let lastErr = null;
                for (let attempt = 0; attempt < 3; attempt++) {
                    try {
                        const s = await waitForSocket(20000);
                        await withTimeout(
                            s.sendMessage(chatId, {
                                image: buffer,
                                caption: (i === 0 && caption) ? caption : ''
                            }),
                            30000,
                            `sendMessage ${i}`
                        );
                        ok = true;
                        break;
                    } catch (e) {
                        lastErr = e;
                        await new Promise(r => setTimeout(r, 1500 * (attempt + 1)));
                    }
                }
                if (!ok) throw lastErr || new Error(`Failed sending image ${i}`);
                await new Promise(r => setTimeout(r, 250));
            }

            if (append_dash) {
                await new Promise(r => setTimeout(r, 250));
                try {
                    const s = await waitForSocket(15000);
                    await withTimeout(s.sendMessage(chatId, { text: '-' }), 15000, 'sendMessage dash');
                } catch (_) {}
            }
        });

        res.json({ success: true, target: chatId, count: payloads.length });
    } catch (e) {
        console.error('[WA] send-batch error:', e.message);
        res.status(500).json({ success: false, error: e.message });
    }
});

app.post('/send-video', async (req, res) => {
    try {
        const { target, file_path, caption } = req.body;
        if (!file_path || !fs.existsSync(file_path)) {
            return res.status(400).json({ success: false, error: 'Video file not found on disk' });
        }

        const activeSock = await waitForSocket(20000);
        let chatId = (target || '').trim();
        if (!chatId) {
            chatId = await resolveTargetGroupJid(activeSock);
        }
        if (!chatId) {
            return res.status(400).json({
                success: false,
                error: 'Could not find the PIVOK WhatsApp group on this WhatsApp account.'
            });
        }
        if (!chatId.includes('@')) {
            chatId = `${chatId}@s.whatsapp.net`;
        }

        const buffer = fs.readFileSync(file_path);
        await enqueueSend(async () => {
            const s = await waitForSocket(25000);
            await withTimeout(
                s.sendMessage(chatId, {
                    video: buffer,
                    caption: caption || '',
                    mimetype: 'video/mp4'
                }),
                90000,
                'sendVideo'
            );
        });

        res.json({ success: true, target: chatId });
    } catch (e) {
        console.error('[WA] send-video error:', e.message);
        res.status(500).json({ success: false, error: e.message });
    }
});

app.listen(WA_PORT, '127.0.0.1', async () => {
    console.log(`[WA Bridge] Listening on 127.0.0.1:${WA_PORT}`);
    try {
        const baileys = await import('@whiskeysockets/baileys');
        makeWASocket = baileys.default;
        useMultiFileAuthState = baileys.useMultiFileAuthState;
        DisconnectReason = baileys.DisconnectReason;
        fetchLatestBaileysVersion = baileys.fetchLatestBaileysVersion;
        Browsers = baileys.Browsers;
        makeCacheableSignalKeyStore = baileys.makeCacheableSignalKeyStore;
        connectToWhatsApp();
    } catch (e) {
        console.error('[WA Bridge] Failed to load Baileys:', e);
    }
});
