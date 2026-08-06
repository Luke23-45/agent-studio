/**
 * NeryvaWidget — the customer-facing embeddable chat widget.
 *
 * A framework-free custom element with shadow DOM. Configured through
 * attributes:
 *
 *   <neryva-widget
 *     tenant="acme"            required: tenant slug
 *     api-key="nrv_live_…"     required: tenant-bound API key
 *     api-base="/api"          optional: backend base path (default /api)
 *     title="Support"          optional: header title
 *     accent-color="#2563eb"   optional: accent color
 *     session-id="…"           optional: conversation session (generated if absent)
 *   ></neryva-widget>
 *
 * Host pages can call open()/close()/sendMessage(text) on the element and
 * listen for "neryva:reply" / "neryva:error" CustomEvents.
 */

import { sendMessage } from '../api/client';
import { appendMessage, createErrorBubble, createMessage, ChatMessage } from '../state/messages';

const WIDGET_TAG = 'neryva-widget';
const DEFAULT_ACCENT = '#2563eb';
const DEFAULT_API_BASE = '/api';

const STYLES = `
:host { all: initial; }
* { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }
.launcher {
  position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
  width: 60px; height: 60px; border-radius: 50%; border: none; cursor: pointer;
  color: #fff; font-size: 26px; box-shadow: 0 4px 14px rgba(0,0,0,.25);
  display: flex; align-items: center; justify-content: center;
}
.panel {
  position: fixed; right: 20px; bottom: 92px; z-index: 2147483000;
  width: 360px; max-width: calc(100vw - 40px); height: 480px; max-height: calc(100vh - 120px);
  background: #fff; border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,.2);
  display: flex; flex-direction: column; overflow: hidden;
}
.header { padding: 12px 16px; color: #fff; font-weight: 600; font-size: 14px; display: flex; justify-content: space-between; align-items: center; }
.close { background: none; border: none; color: inherit; font-size: 18px; cursor: pointer; }
.messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; background: #f9fafb; }
.msg { max-width: 80%; padding: 8px 12px; border-radius: 10px; font-size: 13px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
.msg.user { align-self: flex-end; background: #2563eb; color: #fff; border-bottom-right-radius: 2px; }
.msg.assistant { align-self: flex-start; background: #fff; color: #111827; border: 1px solid #e5e7eb; border-bottom-left-radius: 2px; }
.msg.error { align-self: flex-start; background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.typing { align-self: flex-start; color: #9ca3af; font-size: 12px; padding: 4px 8px; }
.inputRow { display: flex; gap: 8px; padding: 10px 12px; border-top: 1px solid #e5e7eb; background: #fff; }
.inputRow input { flex: 1; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px 10px; font-size: 13px; outline: none; }
.inputRow input:focus { border-color: #2563eb; }
.inputRow button { border: none; border-radius: 8px; padding: 8px 14px; color: #fff; font-size: 13px; cursor: pointer; }
.inputRow button:disabled { opacity: .5; cursor: default; }
.empty { margin: auto; color: #9ca3af; font-size: 13px; text-align: center; padding: 16px; }
`;

export class NeryvaWidget extends HTMLElement {
  private shadow: ShadowRoot;
  private messages: ChatMessage[] = [];
  private sending = false;
  private open = false;
  private sessionId: string;

  private panelEl!: HTMLDivElement;
  private launcherEl!: HTMLButtonElement;
  private listEl!: HTMLDivElement;
  private inputEl!: HTMLInputElement;

  static get observedAttributes(): string[] {
    return ['tenant', 'api-key', 'api-base', 'title', 'accent-color', 'session-id'];
  }

  constructor() {
    super();
    this.shadow = this.attachShadow({ mode: 'open' });
    this.sessionId =
      this.getAttribute('session-id') ??
      (typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `session-${Date.now()}`);
  }

  connectedCallback(): void {
    this.renderShell();
    this.render();
  }

  attributeChangedCallback(): void {
    if (this.shadow && this.shadow.isConnected) {
      this.renderShell();
      this.render();
    }
  }

  get tenant(): string {
    return this.getAttribute('tenant') ?? '';
  }

  get apiKey(): string {
    return this.getAttribute('api-key') ?? '';
  }

  get apiBase(): string {
    return (this.getAttribute('api-base') ?? DEFAULT_API_BASE).replace(/\/$/, '');
  }

  get title(): string {
    return this.getAttribute('title') ?? 'Neryva Assistant';
  }

  get accentColor(): string {
    return this.getAttribute('accent-color') ?? DEFAULT_ACCENT;
  }

  get currentSessionId(): string {
    return this.sessionId;
  }

  openPanel(): void {
    this.open = true;
    this.render();
  }

  closePanel(): void {
    this.open = false;
    this.render();
  }

  toggle(): void {
    this.open ? this.closePanel() : this.openPanel();
  }

  async sendMessage(text: string): Promise<void> {
    const message = text.trim();
    if (!message || this.sending) return;
    if (!this.tenant || !this.apiKey) {
      this.showError('Widget is missing the tenant or api-key attribute.');
      return;
    }

    this.sending = true;
    this.messages = appendMessage(this.messages, createMessage('user', message));
    this.inputEl.value = '';
    this.render();

    try {
      const result = await sendMessage({
        apiBase: this.apiBase,
        tenantSlug: this.tenant,
        apiKey: this.apiKey,
        message,
        sessionId: this.sessionId,
      });
      this.sessionId = result.sessionId;
      this.setAttribute('session-id', result.sessionId);
      this.messages = appendMessage(
        this.messages,
        createMessage('assistant', result.response),
      );
      this.dispatchEvent(
        new CustomEvent('neryva:reply', {
          detail: {
            response: result.response,
            confidence: result.confidence,
            handoffRequired: result.handoffRequired,
            sessionId: result.sessionId,
          },
        }),
      );
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'Request failed';
      this.showError(detail);
    } finally {
      this.sending = false;
      this.render();
    }
  }

  private showError(detail: string): void {
    this.messages = appendMessage(this.messages, createErrorBubble(detail));
    this.dispatchEvent(
      new CustomEvent('neryva:error', { detail: { message: detail } }),
    );
  }

  private renderShell(): void {
    const accent = this.accentColor;
    this.shadow.innerHTML = `
      <style>${STYLES}</style>
      <button class="launcher" style="background:${accent}" aria-label="Open chat">💬</button>
      <div class="panel" style="display:none">
        <div class="header" style="background:${accent}">
          <span>${escapeHtml(this.title)}</span>
          <button class="close" aria-label="Close chat">×</button>
        </div>
        <div class="messages"></div>
        <form class="inputRow">
          <input placeholder="Type a message…" aria-label="Message" autocomplete="off" />
          <button type="submit" style="background:${accent}">Send</button>
        </form>
      </div>
    `;

    this.panelEl = this.shadow.querySelector('.panel') as HTMLDivElement;
    this.launcherEl = this.shadow.querySelector('.launcher') as HTMLButtonElement;
    this.listEl = this.shadow.querySelector('.messages') as HTMLDivElement;
    this.inputEl = this.shadow.querySelector('input') as HTMLInputElement;

    this.launcherEl.addEventListener('click', () => this.toggle());
    this.shadow.querySelector('.close')!.addEventListener('click', () => this.closePanel());
    this.shadow
      .querySelector('.inputRow')!
      .addEventListener('submit', (e) => {
        e.preventDefault();
        void this.sendMessage(this.inputEl.value);
      });
  }

  private render(): void {
    if (!this.panelEl) return;
    this.panelEl.style.display = this.open ? 'flex' : 'none';
    this.launcherEl.style.display = this.open ? 'none' : 'flex';
    this.listEl.innerHTML = '';

    if (this.messages.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'How can we help?';
      this.listEl.appendChild(empty);
    } else {
      for (const message of this.messages) {
        const node = document.createElement('div');
        node.className = `msg ${message.role}${message.error ? ' error' : ''}`;
        node.textContent = message.content;
        this.listEl.appendChild(node);
      }
    }

    if (this.sending) {
      const typing = document.createElement('div');
      typing.className = 'typing';
      typing.textContent = '…';
      this.listEl.appendChild(typing);
      this.listEl.scrollTop = this.listEl.scrollHeight;
    }

    if (this.open) {
      this.inputEl.focus();
    }
  }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => {
    const map: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    };
    return map[ch];
  });
}

declare global {
  interface HTMLElementTagNameMap {
    [WIDGET_TAG]: NeryvaWidget;
  }
}

export { WIDGET_TAG };
