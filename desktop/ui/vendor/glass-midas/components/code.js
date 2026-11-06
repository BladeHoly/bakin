import { define, GlassElement } from './base.js';
const ICON = (path) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
const COPY = ICON('<rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9"/>');
const DONE = ICON('<path d="m5 12.5 4.5 4.5L19 7.5"/>');
/**
 * `<gm-code><pre>…</pre></gm-code>`: a code block on lite glass with a copy button, an icon that
 * turns into a check once copied. `label-copy` and `label-copied` name it for screen readers.
 */
export class GmCode extends GlassElement {
    static glassDefaults = { radius: 16 };
    static lite = true;
    #button;
    #reset;
    setup() {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'gm-copy';
        button.addEventListener('click', () => this.copy());
        this.append(button);
        this.#button = button;
        this.#show(false);
    }
    teardown() {
        clearTimeout(this.#reset);
        this.#button?.remove();
    }
    /** Copies the code to the clipboard and confirms it on the button. */
    async copy() {
        const code = this.querySelector('pre')?.innerText ?? '';
        if (!this.#button)
            return;
        try {
            await navigator.clipboard.writeText(code);
            this.#show(true);
        }
        catch {
            // No clipboard access (an insecure page, a denied permission): select it to copy by hand.
            const pre = this.querySelector('pre');
            if (pre)
                getSelection()?.selectAllChildren(pre);
        }
        clearTimeout(this.#reset);
        this.#reset = setTimeout(() => this.#show(false), 1600);
    }
    #show(copied) {
        const button = this.#button;
        const label = copied
            ? (this.getAttribute('label-copied') ?? 'Copied')
            : (this.getAttribute('label-copy') ?? 'Copy');
        button.innerHTML = copied ? DONE : COPY;
        button.setAttribute('aria-label', label);
        button.title = label;
        button.toggleAttribute('data-copied', copied);
    }
}
define('gm-code', GmCode);
//# sourceMappingURL=code.js.map