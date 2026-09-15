/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Google Meet platform handler for the Virtual Participant (GitHub #661).
 *
 * Joins as an anonymous guest via the web client. Mirrors the Chime handler's
 * lifecycle: navigate -> pre-join (name, mute) -> "Ask to join" -> wait for
 * admission -> observe speakers / attendees / chat -> wait for meeting end.
 *
 * In-meeting selectors are taken from the LMA browser extension's Meet
 * provider (public/content_scripts/providers/meet.js), which is known to work
 * against the current Meet UI. Pre-join selectors use text/aria-label matches
 * with the AI DOM-resolver fallback, since Meet's class names are obfuscated
 * and change often.
 */
import { Page } from 'playwright-core';
import { gotoMeetingPage, MEETING_HOST_PATTERNS } from './meeting-navigation.js';
import { details, matchesEndCommand, exitMessagesFor, ExitInfo, MeetingInitOptions } from './details.js';
import { transcriptionService } from './scribe.js';
import { voiceAssistant } from './voice-assistant.js';
import { findElementWithFallback } from './ai-dom-resolver.js';
import { startDialogWatchdog } from './dialog-watchdog.js';

/** Build the join URL from whatever the invite carries: a full URL or a meeting code. */
export function buildMeetUrl(meetingId: string): string {
    const raw = (meetingId || '').trim();
    if (/^https?:\/\//i.test(raw)) return raw;
    if (/^meet\.google\.com\//i.test(raw)) return `https://${raw}`;
    // Meeting codes look like abc-defg-hij; tolerate missing dashes / spaces.
    const code = raw.replace(/\s+/g, '');
    return `https://meet.google.com/${code}`;
}

export default class GoogleMeet {
    private prevSender: string = '';
    private endRequested: Promise<ExitInfo>;
    private requestEnd: (info: ExitInfo) => void = () => {};

    constructor() {
        this.endRequested = new Promise<ExitInfo>((resolve) => {
            this.requestEnd = resolve;
        });
    }

    private async openChatPanel(page: Page): Promise<boolean> {
        const chatBtn = await findElementWithFallback(
            page,
            ['button[aria-label="Chat with everyone"]', 'button[aria-label^="Chat with"]'],
            {
                intent: 'Google Meet in-call toolbar button that opens the chat side panel',
                platform: 'GOOGLE_MEET',
                step: 'meet.chat.open',
            },
            { maxRetries: 5, delayMs: 500 },
        );
        if (!chatBtn) return false;
        const pressed = await chatBtn.element.getAttribute('aria-pressed');
        if (pressed !== 'true') {
            await chatBtn.element.click();
            await new Promise((r) => setTimeout(r, 800));
        }
        return true;
    }

    private async sendMessages(page: Page, messages: string[]): Promise<void> {
        if (!(await this.openChatPanel(page))) {
            console.log('Could not open Google Meet chat panel — skipping sendMessages');
            return;
        }
        const input = await findElementWithFallback(
            page,
            ['textarea[aria-label="Send a message"]', 'textarea[aria-label^="Send a message"]'],
            {
                intent: 'Google Meet chat side panel message compose textarea',
                platform: 'GOOGLE_MEET',
                step: 'meet.chat.input',
            },
            { maxRetries: 5, delayMs: 500 },
        );
        if (!input) {
            console.log('Could not locate Google Meet chat input — skipping sendMessages');
            return;
        }
        for (const message of messages) {
            await input.element.fill(message);
            await input.element.press('Enter');
            await new Promise((r) => setTimeout(r, 300));
        }
    }

    private async clickIfPresent(page: Page, selectors: string[], intent: string, step: string): Promise<boolean> {
        const res = await findElementWithFallback(
            page,
            selectors,
            { intent, platform: 'GOOGLE_MEET', step },
            { maxRetries: 3, delayMs: 500 },
        );
        if (!res) return false;
        await res.element.click().catch(() => {});
        return true;
    }

    /**
     * Meet intermittently shows sign-in nudges / info dialogs to unsigned guests
     * before (and occasionally after) the pre-join form. Dismiss the common ones
     * by label; anything unusual is left to the AI dialog watchdog.
     */
    private async dismissNags(page: Page, rounds = 3): Promise<void> {
        for (let i = 0; i < rounds; i++) {
            const clicked = await page
                .evaluate(() => {
                    const rx = /^(dismiss|not now|no thanks|continue without( an)? (account|signing in)|use without an account|got it|close|skip)$/i;
                    const cands = Array.from(document.querySelectorAll('button, [role=button]')).filter(
                        (b) => (b as HTMLElement).offsetParent !== null,
                    );
                    for (const b of cands) {
                        const text = (b.textContent || '').trim();
                        const aria = b.getAttribute('aria-label') || '';
                        if (rx.test(text) || /^close$/i.test(aria)) {
                            (b as HTMLElement).click();
                            return text || aria;
                        }
                    }
                    return null;
                })
                .catch(() => null);
            if (!clicked) return;
            console.log(`Dismissed Google Meet dialog: ${JSON.stringify(clicked)}`);
            await new Promise((r) => setTimeout(r, 800));
        }
    }

    public async initialize(page: Page, opts: MeetingInitOptions = {}): Promise<ExitInfo> {
        if (opts.prepareAvatar) await opts.prepareAvatar();
        startDialogWatchdog(page, { platform: 'GOOGLE_MEET' });

        const url = buildMeetUrl(details.invite.meetingId);
        console.log(`Getting Google Meet link: ${url}`);
        await gotoMeetingPage(page, url, MEETING_HOST_PATTERNS.meet, 'meet-join');

        // ---- Pre-join -------------------------------------------------------
        await this.dismissNags(page);
        console.log('Entering name.');
        let nameRes = await findElementWithFallback(
            page,
            ['input[aria-label="Your name"]', 'input[placeholder="Your name"]', 'input[type="text"][autocomplete="name"]'],
            {
                intent: 'Google Meet guest pre-join screen "Your name" text input',
                platform: 'GOOGLE_MEET',
                step: 'meet.join.name',
            },
            { maxRetries: 20, delayMs: 500 },
        );
        if (!nameRes) {
            // A sign-in nudge may have covered the form; clear it and look once more.
            await this.dismissNags(page);
            nameRes = await findElementWithFallback(
                page,
                ['input[aria-label="Your name"]', 'input[placeholder="Your name"]'],
                { intent: 'Google Meet guest pre-join screen "Your name" text input (retry)', platform: 'GOOGLE_MEET', step: 'meet.join.name' },
                { maxRetries: 6, delayMs: 500 },
            );
        }
        if (!nameRes) {
            // A signed-in profile has no name field; otherwise the link is bad.
            const blocked = await page.$('text=/You can\'t join this video call/i');
            if (blocked) {
                // Shown instantly, with no name field, either when the host's Google
                // Workspace policy forbids users not signed in to Google, or when Meet
                // has flagged the browser as automated. Check the link in a normal
                // incognito window to tell the two apart.
                console.log('Google Meet refused the guest join before the pre-join screen (policy or automation detection).');
                throw new Error(
                    'Google Meet refused the guest join ("You can\'t join this video call"): either the host\'s ' +
                    'Workspace policy blocks users not signed in to Google, or Meet flagged the browser as automated.',
                );
            }
            const invalid = await page.$('text=/Check your meeting code|Invalid video call name/i');
            if (invalid) {
                console.log('LMA Virtual Participant was unable to join the meeting (invalid link).');
                throw new Error('Meeting not found or invalid meeting ID');
            }
            console.log('No name field found — assuming signed-in profile, continuing.');
        } else {
            await nameRes.element.fill(details.scribeIdentity);
        }

        // Mute mic (unless the voice assistant needs it) and always turn camera off.
        if (!voiceAssistant.isEnabled()) {
            console.log('Turning off microphone.');
            await this.clickIfPresent(
                page,
                ['[role="button"][aria-label*="Turn off microphone"]', 'button[aria-label*="Turn off microphone"]', '[data-is-muted="false"]'],
                'Google Meet pre-join microphone toggle (currently on) to mute',
                'meet.join.mute',
            );
        } else {
            console.log('Voice assistant enabled - leaving microphone on for agent audio');
        }
        console.log('Turning off camera.');
        await this.clickIfPresent(
            page,
            ['[role="button"][aria-label*="Turn off camera"]', 'button[aria-label*="Turn off camera"]'],
            'Google Meet pre-join camera toggle (currently on) to turn camera off',
            'meet.join.camera',
        );

        console.log('Clicking join button.');
        const joinRes = await findElementWithFallback(
            page,
            [
                'button:has-text("Ask to join")',
                'button:has-text("Join now")',
                'button:has-text("Join anyway")',
                '[role="button"]:has-text("Ask to join")',
                '[role="button"]:has-text("Join now")',
            ],
            {
                intent: 'Google Meet pre-join primary button: "Ask to join" (guest) or "Join now"',
                platform: 'GOOGLE_MEET',
                step: 'meet.join.joinButton',
            },
            { maxRetries: 20, delayMs: 500 },
        );
        if (!joinRes) {
            console.log('Could not locate Google Meet join button — aborting');
            return { reason: 'unknown', trigger: 'pre-join:no-join-button' };
        }
        await joinRes.element.click();

        // ---- Admission --------------------------------------------------------
        console.log('Waiting to be admitted.');
        const admitted = await Promise.race([
            page
                .waitForSelector('button[aria-label="Leave call"], button[aria-label^="Leave call"], button[aria-label="Chat with everyone"]', {
                    timeout: details.waitingTimeout,
                })
                .then(() => 'admitted' as const)
                .catch(() => 'timeout' as const),
            page
                .waitForSelector('text=/denied your request|You can\'t join this call|Someone in the call denied|removed from the meeting/i', {
                    timeout: details.waitingTimeout,
                })
                .then(() => 'denied' as const)
                .catch(() => 'timeout' as const),
        ]);
        if (admitted !== 'admitted') {
            console.log(`LMA Virtual Participant was not admitted into the meeting (${admitted}).`);
            throw new Error('Wrong meeting password or permission denied');
        }
        await new Promise((r) => setTimeout(r, 1500));
        console.log('Successfully joined Google Meet meeting');

        // Dismiss the occasional first-join popups ("Got it", "Dismiss").
        await page.$$eval('button', (btns) => {
            for (const b of btns) {
                const t = (b.textContent || '').trim();
                if (t === 'Got it' || t === 'Dismiss') (b as HTMLButtonElement).click();
            }
        }).catch(() => {});

        console.log('Sending introduction messages.');
        await this.sendMessages(page, details.introMessages);

        // ---- Attendee monitoring (poll: Meet re-renders tiles constantly) -------
        await page.exposeFunction('attendeeChange', async (count: number) => {
            if (count <= 1) {
                console.log('LMA Virtual Participant got lonely and left.');
                details.start = false;
                this.requestEnd({ reason: 'alone-in-meeting', trigger: 'attendees-left' });
            }
        });
        console.log('Listening for attendee changes.');
        await page.evaluate(() => {
            let last = -1;
            let aloneSince = 0;
            setInterval(() => {
                const ids = new Set<string>();
                document.querySelectorAll('[data-participant-id]').forEach((el) => {
                    const id = el.getAttribute('data-participant-id');
                    if (id) ids.add(id);
                });
                const n = ids.size;
                if (n === 0) return; // grid not rendered yet / hidden
                if (n <= 1) {
                    // require 60s of being alone before leaving (people rejoin, grid re-renders)
                    if (!aloneSince) aloneSince = Date.now();
                    if (Date.now() - aloneSince > 60_000 && last !== 1) {
                        last = 1;
                        (window as any).attendeeChange(1);
                    }
                } else {
                    aloneSince = 0;
                    last = n;
                }
            }, 5000);
        });

        // ---- Active speaker (selectors from the browser extension) ---------------
        await page.exposeFunction('speakerChange', async (speaker: string) => {
            await transcriptionService.speakerChange(speaker);
        });
        console.log('Listening for speaker changes.');
        await page.evaluate((identity) => { (window as any).__lmaScribeIdentity = identity; }, details.scribeIdentity);
        await page.evaluate(() => {
            let lastActiveSpeaker = '';
            const nameOf = (tile: Element | null): string | null => {
                if (!tile) return null;
                const el = tile.querySelector('[data-tooltip-id][data-tooltip-anchor-boundary-type] span.notranslate') ||
                    tile.querySelector('[data-self-name]') ||
                    tile.querySelector('span.notranslate');
                const txt = (el?.textContent || tile.getAttribute('data-self-name') || '').trim();
                return txt || null;
            };
            // Speaking indicator (verified 2026-09-15 with a live DOM capture): the
            // audio-level bars on the active tile are a div with jsname="QgSmzd" whose
            // class cycles (HX2H7 / Oaajhc / wEsLMd / OgVli) while audio is detected.
            // The older extension marker jscontroller="ES310d" is kept as a fallback.
            const isIndicator = (el: HTMLElement) =>
                el.getAttribute('jsname') === 'QgSmzd' || el.getAttribute('jscontroller') === 'ES310d';
            const selfName = (window as any).__lmaScribeIdentity as string | undefined;
            const observer = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    if (m.type !== 'attributes' || m.attributeName !== 'class') continue;
                    const target = m.target as HTMLElement;
                    if (!isIndicator(target)) continue;
                    if (target.offsetParent === null) continue; // indicator hidden = not speaking
                    const speaker = nameOf(target.closest('div[data-participant-id]'));
                    if (!speaker || speaker === selfName) continue; // ignore the bot's own tile
                    if (speaker !== lastActiveSpeaker) {
                        lastActiveSpeaker = speaker;
                        (window as any).speakerChange(speaker);
                    }
                }
            });
            // Observe the whole document: Meet re-renders the tile container after
            // join, which detaches any observer bound to it. Filtering by the
            // indicator marker keeps this cheap.
            observer.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
        });

        // ---- Chat monitoring ------------------------------------------------------
        await page.exposeFunction('messageChange', async (sender: string | null, text: string | null) => {
            if (!sender) sender = this.prevSender;
            this.prevSender = sender;
            if (!text) return;
            if (matchesEndCommand(text)) {
                console.log(`LMA Virtual Participant has been asked to leave by ${sender || 'a participant'}: ${JSON.stringify(text)}`);
                await this.sendMessages(page, exitMessagesFor(sender));
                details.start = false;
                this.requestEnd({ reason: 'end-command', trigger: 'chat', requestedBy: sender, matchedMessage: text });
            } else if (details.start && text === details.pauseCommand) {
                details.start = false;
                console.log(details.pauseMessages[0]);
                await this.sendMessages(page, details.pauseMessages);
            } else if (!details.start && text === details.startCommand) {
                details.start = true;
                console.log(details.startMessages[0]);
                await this.sendMessages(page, details.startMessages);
                transcriptionService.startTranscription();
            } else if (details.start && !sender?.includes(details.scribeName)) {
                const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
                const message = `[${timestamp}] ${sender}: ${text}`;
                details.messages.push(message);
                console.log('New message:', message);
            }
        });
        console.log('Listening for message changes.');
        await page.evaluate(() => {
            const seen = new WeakSet<Element>();
            const scan = () => {
                document.querySelectorAll('[data-message-text]').forEach((el) => {
                    if (seen.has(el)) return;
                    seen.add(el);
                    const text = el.getAttribute('data-message-text') || el.textContent || '';
                    const holder = el.closest('[data-sender-name]');
                    const sender = holder?.getAttribute('data-sender-name') || null;
                    (window as any).messageChange(sender, text.trim());
                });
            };
            setInterval(scan, 1500);
        });

        // ---- Transcription ---------------------------------------------------------
        if (details.start) {
            console.log(details.startMessages[0]);
            await this.sendMessages(page, details.startMessages);
            transcriptionService.startTranscription();
        }

        // ---- Wait for meeting end ------------------------------------------------
        console.log('Waiting for meeting end.');
        let exitInfo: ExitInfo = { reason: 'unknown' };
        const meetEndDetected = new Promise<ExitInfo>((resolve) => {
            const handle = setInterval(async () => {
                try {
                    const ended = await page.$(
                        'text=/You left the meeting|You\'ve left the meeting|The call ended|has ended the call|Return to home screen|You\'ve been removed from the meeting|removed you from the meeting/i',
                    );
                    if (ended) {
                        clearInterval(handle);
                        const txt = ((await ended.textContent()) || '').toLowerCase();
                        resolve({
                            reason: txt.includes('removed') ? 'removed-from-meeting' : 'host-ended',
                            trigger: 'meeting-ended-text',
                        });
                        return;
                    }
                    const currentUrl = page.url();
                    if (currentUrl === 'about:blank' || !/meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i.test(currentUrl)) {
                        // Left the meeting route (e.g. back to meet.google.com/ landing).
                        const stillInCall = await page.$('button[aria-label^="Leave call"]');
                        if (!stillInCall) {
                            clearInterval(handle);
                            resolve({ reason: 'page-closed', trigger: 'navigated-away' });
                            return;
                        }
                    }
                } catch (error) {
                    clearInterval(handle);
                    resolve({
                        reason: 'page-closed',
                        trigger: `poll-error:${error instanceof Error ? error.message : String(error)}`,
                    });
                }
            }, 3000);
        });
        const meetingTimeout = new Promise<ExitInfo>((resolve) =>
            setTimeout(() => resolve({ reason: 'meeting-timeout', trigger: 'meetingTimeout' }), details.meetingTimeout),
        );
        try {
            exitInfo = await Promise.race([this.endRequested, meetEndDetected, meetingTimeout]);
        } finally {
            details.start = false;
        }
        // Best effort: leave the call so Meet doesn't keep a ghost participant.
        await page.click('button[aria-label^="Leave call"]', { timeout: 3000 }).catch(() => {});
        console.log(`Meeting ended (reason=${exitInfo.reason} trigger=${exitInfo.trigger ?? 'n/a'}).`);
        return exitInfo;
    }
}
