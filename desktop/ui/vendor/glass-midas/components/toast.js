import { define, GlassElement } from './base.js';
/**
 * `<gm-toast duration="4000">Saved</gm-toast>`: a short message on frosted glass that goes away on
 * its own, waiting while the pointer or focus is on it. Usually made with `toast()`.
 * Dismissing it fires `gm-dismiss`.
 */
export class GmToast extends GlassElement {
    static glassDefaults = { radius: 22, bezel: 14, thickness: 26 };
    #timer;
    #left = 0;
    #since = 0;
    #pause = () => {
        clearTimeout(this.#timer);
        this.#left -= Date.now() - this.#since;
    };
    #resume = () => this.#start(this.#left);
    setup() {
        if (!this.hasAttribute('material'))
            this.setAttribute('material', 'frost');
        this.setAttribute('role', 'status');
        for (const type of ['pointerenter', 'focusin'])
            this.addEventListener(type, this.#pause);
        for (const type of ['pointerleave', 'focusout'])
            this.addEventListener(type, this.#resume);
        this.#start(Number(this.getAttribute('duration') ?? 4000));
    }
    teardown() {
        clearTimeout(this.#timer);
    }
    #start(ms) {
        clearTimeout(this.#timer);
        this.#left = ms;
        this.#since = Date.now();
        if (ms > 0)
            this.#timer = setTimeout(() => this.dismiss(), ms);
    }
    /** Changes its message and starts its time again (`toast()` does it for a repeated `key`). */
    update(message, duration = Number(this.getAttribute('duration') ?? 4000)) {
        const text = this.querySelector(':scope > span');
        if (text)
            text.textContent = message;
        this.setAttribute('duration', String(duration));
        this.#start(duration);
    }
    /** Fades it out and removes it. */
    dismiss() {
        if (this.hasAttribute('leaving'))
            return;
        this.setAttribute('leaving', '');
        this.dispatchEvent(new CustomEvent('gm-dismiss', { bubbles: true }));
        setTimeout(() => this.remove(), 200);
    }
}
/**
 * Shows a toast at the bottom of the page, above the others (three at most), and returns it.
 * The region they stack in is announced politely to screen readers. It's a manual popover, so it
 * sits in the top layer like a modal `gm-dialog`, and it's raised over one that opened after it.
 */
export function toast(message, options = {}) {
    let region = document.querySelector('.gm-toasts');
    if (!region) {
        region = document.createElement('div');
        region.className = 'gm-toasts';
        region.setAttribute('aria-live', 'polite');
        document.body.append(region);
    }
    raise(region);
    if (options.key) {
        const same = [...region.querySelectorAll('gm-toast:not([leaving])')].find((t) => t.dataset.key === options.key);
        if (same) {
            same.querySelector(':scope > button.gm-chip')?.remove();
            addAction(same, options.action);
            same.update(message, options.duration ?? 4000);
            return same;
        }
    }
    const el = document.createElement('gm-toast');
    if (options.key)
        el.dataset.key = options.key;
    el.setAttribute('duration', String(options.duration ?? 4000));
    const text = document.createElement('span');
    text.textContent = message;
    el.append(text);
    addAction(el, options.action);
    region.append(el);
    const shown = region.querySelectorAll('gm-toast:not([leaving])');
    if (shown.length > 3)
        shown[0].dismiss();
    return el;
}
// The top layer stacks in the order things entered it: shown again, the region goes over any
// modal opened since. Without the popover API the toasts still show, under modals.
function raise(region) {
    if (typeof region.showPopover !== 'function')
        return;
    if (!region.hasAttribute('popover'))
        region.setAttribute('popover', 'manual');
    try {
        if (region.matches(':popover-open') && document.querySelector('dialog:modal'))
            region.hidePopover();
        if (!region.matches(':popover-open'))
            region.showPopover();
    }
    catch {
        // not in the document, or the browser refused: the toast shows anyway
    }
}
function addAction(el, action) {
    if (!action)
        return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'gm-chip';
    button.textContent = action.label;
    button.addEventListener('click', () => {
        action.onClick();
        el.dismiss();
    });
    el.append(button);
}
define('gm-toast', GmToast);
//# sourceMappingURL=toast.js.map