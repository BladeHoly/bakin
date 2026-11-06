import { define, GlassElement } from './base.js';
let uid = 0;
/**
 * `<gm-dialog id="confirm" heading="Delete this page?">…</gm-dialog>`: a modal of frosted glass on
 * the browser's `<dialog>`, which brings the top layer, the dimmed backdrop, focus trapping and
 * Escape. `placement="bottom"` makes it a sheet that rises from the bottom edge; `"end"` or
 * `"start"`, a drawer down that side.
 *
 * Open it with `show()`. Buttons inside a `<form method="dialog">` close it with their `value`;
 * so do Escape (`"cancel"`) and a click on the backdrop (`"dismiss"`). Closing fires `gm-close`
 * with that value.
 *
 * Lite by default: a dialog is a big surface, and refraction over a large area costs frames
 * whenever something changes behind or inside it (a list scrolling, images arriving). Set
 * `variant="full"` for refraction on a small, still one.
 */
export class GmDialog extends GlassElement {
    static glassDefaults = { radius: 24, bezel: 18, thickness: 32 };
    static lite = true;
    #panel;
    get glassTarget() {
        return this.#panel ?? this;
    }
    setup() {
        if (!this.hasAttribute('material'))
            this.setAttribute('material', 'frost');
        const panel = document.createElement('dialog');
        panel.className = 'gm-dialog-panel';
        panel.append(...this.childNodes);
        const heading = this.getAttribute('heading');
        if (heading) {
            const h = document.createElement('h2');
            h.className = 'gm-dialog-heading';
            h.id = `gm-dialog-${++uid}`;
            h.textContent = heading;
            panel.prepend(h);
            panel.setAttribute('aria-labelledby', h.id);
        }
        // A click on the backdrop lands on the dialog itself, outside its box.
        panel.addEventListener('click', (e) => {
            const r = panel.getBoundingClientRect();
            const outside = e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
            if (e.target === panel && outside)
                panel.close('dismiss');
        });
        // Escape: Chromium resets returnValue as it closes, so the reason is kept here.
        let cancelled = false;
        panel.addEventListener('cancel', () => (cancelled = true));
        panel.addEventListener('close', () => {
            const value = cancelled ? 'cancel' : panel.returnValue;
            cancelled = false;
            this.dispatchEvent(new CustomEvent('gm-close', { detail: { value }, bubbles: true }));
        });
        this.append(panel);
        this.#panel = panel;
    }
    teardown() {
        const panel = this.#panel;
        if (panel) {
            panel.querySelector(':scope > .gm-dialog-heading')?.remove();
            this.replaceChildren(...panel.childNodes);
        }
        this.#panel = undefined;
    }
    get open() {
        return this.#panel?.open ?? false;
    }
    /** Opens it as a modal. */
    show() {
        const panel = this.#panel;
        if (panel && !panel.open) {
            panel.returnValue = '';
            panel.showModal();
        }
    }
    /** Closes it, with `value` as the reason `gm-close` reports. */
    close(value = '') {
        this.#panel?.close(value);
    }
}
define('gm-dialog', GmDialog);
//# sourceMappingURL=dialog.js.map