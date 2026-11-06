import { define } from './base.js';
import { boxOf, Droplet } from './droplet.js';
import { GmPopover } from './popover.js';
/**
 * `<button popovertarget="actions">…</button> <gm-menu id="actions"><button value="rename">Rename
 * </button>…</gm-menu>`: a popover of actions, with a droplet under the focused one.
 *
 * Opening it focuses the first item; the arrow keys, Home and End move through them; Escape and
 * a click outside close it. Picking an item fires a cancelable `gm-select` and closes the menu;
 * links simply follow their href. An `<hr>` separates groups, and a `.gm-menu-label` names one
 * (it's not an item). Items added, removed or hidden later are picked up on their own.
 */
export class GmMenu extends GmPopover {
    #items = [];
    #wired = new WeakSet();
    #active = -1;
    #droplet;
    #observer;
    setup() {
        super.setup();
        this.setAttribute('role', 'menu');
        this.refresh();
        if (typeof MutationObserver !== 'undefined') {
            this.#observer = new MutationObserver(() => this.refresh());
            this.#observer.observe(this, { childList: true, subtree: true, attributeFilter: ['hidden'] });
        }
        this.addEventListener('keydown', this.#onKey);
        this.#droplet = new Droplet(this, () => {
            const item = this.#items[this.#active];
            return item ? boxOf(item) : undefined;
        }, 12);
    }
    teardown() {
        super.teardown();
        this.#observer?.disconnect();
        this.removeEventListener('keydown', this.#onKey);
        this.#droplet?.destroy();
    }
    /** Takes the items again: called by itself when its children change, or by hand. */
    refresh() {
        this.#items = [...this.querySelectorAll(':scope > :is(button, a)')].filter((i) => !i.hidden);
        for (const item of this.#items) {
            if (this.#wired.has(item))
                continue;
            this.#wired.add(item);
            item.setAttribute('role', 'menuitem');
            item.tabIndex = -1;
            item.addEventListener('pointerenter', () => this.#focus(this.#items.indexOf(item)));
            item.addEventListener('focus', () => this.#focus(this.#items.indexOf(item)));
            item.addEventListener('click', () => this.#pick(item));
        }
        for (const hr of this.querySelectorAll(':scope > hr'))
            hr.setAttribute('role', 'separator');
        for (const label of this.querySelectorAll(':scope > .gm-menu-label'))
            label.setAttribute('role', 'presentation');
        if (this.#active >= this.#items.length)
            this.#active = -1;
    }
    opened() {
        this.#active = -1;
        this.#focus(0);
    }
    #focus(i) {
        if (this.#active === i)
            return;
        this.#active = i;
        if (document.activeElement !== this.#items[i])
            this.#items[i]?.focus();
        this.#droplet?.follow();
    }
    #pick(item) {
        const detail = {
            value: item.getAttribute('value') ?? item.textContent?.trim() ?? '',
            label: item.textContent?.trim() ?? '',
        };
        const go = this.dispatchEvent(new CustomEvent('gm-select', { detail, cancelable: true, bubbles: true }));
        if (go)
            this.close();
    }
    #onKey = (e) => {
        const last = this.#items.length - 1;
        const moves = {
            ArrowDown: this.#active >= last ? 0 : this.#active + 1,
            ArrowUp: this.#active <= 0 ? last : this.#active - 1,
            Home: 0,
            End: last,
        };
        const next = moves[e.key];
        if (next === undefined)
            return;
        e.preventDefault();
        this.#focus(next);
    };
}
define('gm-menu', GmMenu);
//# sourceMappingURL=menu.js.map