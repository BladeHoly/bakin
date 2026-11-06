import { anchorTo, follow } from './anchor.js';
import { define, GlassElement } from './base.js';
const stored = (key) => {
    try {
        return localStorage.getItem(`gm-tip:${key}`);
    }
    catch {
        return null;
    }
};
/**
 * `<gm-tip heading="Search everything" remember="search" anchor="search">…</gm-tip>`: a tip that
 * teaches one thing, on frosted glass, with a close button.
 *
 * - In the flow of the page by default; with `anchor`, it floats next to the element with that
 *   id, `placement="bottom"` or `"top"`, with an arrow pointing at it.
 * - Closing it fires a cancelable `gm-dismiss`. With `remember`, a closed tip stays closed on
 *   later visits.
 * - A child with `slot="icon"` becomes its icon; `.gm-tip-actions` holds buttons (chips).
 *
 * The host only positions: the glass is `.gm-tip-card`, and the arrow is its sibling with its own
 * frost, since glass inside glass can't see the page.
 */
export class GmTip extends GlassElement {
    static glassDefaults = { radius: 22, bezel: 16, thickness: 30 };
    #card;
    #stop;
    get glassTarget() {
        return this.#card ?? this;
    }
    setup() {
        if (!this.hasAttribute('material'))
            this.setAttribute('material', 'frost');
        if (!this.hasAttribute('role'))
            this.setAttribute('role', 'note');
        const key = this.getAttribute('remember');
        if (key && stored(key) === 'dismissed')
            this.hidden = true;
        const card = document.createElement('div');
        card.className = 'gm-tip-card';
        const icon = this.querySelector(':scope > [slot="icon"]');
        card.append(...[...this.childNodes].filter((n) => n !== icon));
        const heading = this.getAttribute('heading');
        if (heading) {
            const p = document.createElement('p');
            p.className = 'gm-tip-heading';
            p.textContent = heading;
            card.prepend(p);
        }
        if (icon)
            card.prepend(icon);
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'gm-tip-close';
        close.setAttribute('aria-label', this.getAttribute('label-close') ?? 'Dismiss tip');
        close.innerHTML =
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
        close.addEventListener('click', () => this.dismiss());
        card.append(close);
        this.#card = card;
        if (this.hasAttribute('anchor')) {
            const arrow = document.createElement('span');
            arrow.className = 'gm-tip-arrow';
            arrow.setAttribute('aria-hidden', 'true');
            this.append(arrow, card);
            this.#stop = follow(() => this.#position());
        }
        else {
            this.append(card);
        }
    }
    teardown() {
        this.#stop?.();
        // Put the content back as it was written, so a reconnect builds the tip again.
        const card = this.#card;
        if (card) {
            card.querySelector(':scope > .gm-tip-heading')?.remove();
            card.querySelector(':scope > .gm-tip-close')?.remove();
            this.replaceChildren(...card.childNodes);
        }
        this.#card = undefined;
    }
    /** Closes the tip, unless a `gm-dismiss` listener cancels it; remembers it with `remember`. */
    dismiss() {
        const event = new CustomEvent('gm-dismiss', { cancelable: true, bubbles: true });
        if (!this.dispatchEvent(event))
            return;
        this.hidden = true;
        const key = this.getAttribute('remember');
        if (key) {
            try {
                localStorage.setItem(`gm-tip:${key}`, 'dismissed');
            }
            catch { }
        }
    }
    /** Shows the tip again, and forgets that it was closed. */
    show() {
        const key = this.getAttribute('remember');
        if (key) {
            try {
                localStorage.removeItem(`gm-tip:${key}`);
            }
            catch { }
        }
        this.hidden = false;
        if (this.hasAttribute('anchor'))
            this.#position();
    }
    // Next to the anchor, flipped when its side has no room, the arrow on the anchor's center.
    #position() {
        const target = document.getElementById(this.getAttribute('anchor') ?? '');
        if (!target || this.hidden)
            return;
        const x = anchorTo(this, target, this.getAttribute('placement') === 'top');
        this.style.setProperty('--gm-tip-arrow', `${x}px`);
    }
}
define('gm-tip', GmTip);
//# sourceMappingURL=tip.js.map