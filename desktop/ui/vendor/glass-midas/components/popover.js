import { anchorTo, follow } from './anchor.js';
import { define, GlassElement } from './base.js';
/**
 * `<button popovertarget="more">…</button> <gm-popover id="more">…</gm-popover>`: frosted glass
 * that floats next to the button that opens it, on the browser's popover API: the top layer,
 * closing on Escape and on a click outside, and opening with `popovertarget`, no script needed.
 *
 * It sits below its button (`placement="top"` for above), flipped when there's no room. The
 * button is the one whose `popovertarget` names it, or the element with the id in `anchor`.
 */
export class GmPopover extends GlassElement {
    static glassDefaults = { radius: 18, bezel: 14, thickness: 26 };
    #stop;
    #opener = null;
    #hadFocus = false;
    setup() {
        if (!this.hasAttribute('popover'))
            this.setAttribute('popover', 'auto');
        if (!this.hasAttribute('material'))
            this.setAttribute('material', 'frost');
        this.addEventListener('beforetoggle', this.#beforeToggle);
        this.addEventListener('toggle', this.#onToggle);
    }
    teardown() {
        this.removeEventListener('beforetoggle', this.#beforeToggle);
        this.removeEventListener('toggle', this.#onToggle);
        this.#stop?.();
    }
    /** The element it floats next to. */
    get opener() {
        const id = this.getAttribute('anchor');
        return id
            ? document.getElementById(id)
            : document.querySelector(`[popovertarget="${this.id}"]`);
    }
    open() {
        if (!this.matches(':popover-open'))
            this.showPopover();
    }
    close() {
        if (this.matches(':popover-open'))
            this.hidePopover();
    }
    // Opening: hidden until it's placed, so it never flashes at the popover's default spot.
    // Closing: note whether focus is inside, before the browser moves it.
    #beforeToggle = (e) => {
        if (e.newState === 'open')
            this.style.visibility = 'hidden';
        else
            this.#hadFocus = this.contains(document.activeElement);
    };
    #onToggle = (e) => {
        const open = e.newState === 'open';
        this.#stop?.();
        this.#stop = undefined;
        if (open) {
            this.#opener = this.opener;
            const place = () => {
                if (this.#opener)
                    anchorTo(this, this.#opener, this.getAttribute('placement') === 'top', 8);
                this.style.visibility = '';
            };
            place();
            this.#stop = follow(place);
            this.opened();
        }
        else {
            // Focus goes back to the button if it was inside, wherever the browser would put it
            // (Safari doesn't focus a clicked button, so it would land elsewhere).
            if (this.#hadFocus)
                this.#opener?.focus();
        }
    };
    /** Called once it's open and placed; menus move focus in here. */
    opened() { }
}
define('gm-popover', GmPopover);
//# sourceMappingURL=popover.js.map