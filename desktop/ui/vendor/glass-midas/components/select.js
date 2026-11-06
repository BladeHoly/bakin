import { Base, define } from './base.js';
import { boxOf, Droplet } from './droplet.js';
import { GmPopover } from './popover.js';
let ids = 0;
const CHEVRON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
/**
 * `<label for="country">Country</label> <gm-select><select id="country" name="country">…</select>
 * </gm-select>`: a native select, with its list on frosted glass instead of the browser's popup.
 *
 * The `<select>` stays, hidden under the button, so forms, `required` and resets work as always,
 * and without the script it's simply the native select. Its id moves to the button, so the label
 * names it. The arrow keys, Home, End and typing a letter move through the options; Enter or
 * Space picks one; Escape and a click outside close the list. Picking fires `input` and `change`
 * on the select, and `gm-change` on this element.
 *
 * An option with an empty value is the placeholder: shown on the button, not in the list.
 */
export class GmSelect extends Base {
    #select = null;
    #trigger;
    #list;
    #items = [];
    #active = -1;
    #droplet;
    #observer;
    #id = '';
    #typed = '';
    #typedAt = 0;
    connectedCallback() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.isConnected && this.#build(), {
                once: true,
            });
        }
        else
            this.#build();
    }
    disconnectedCallback() {
        this.#observer?.disconnect();
        this.#droplet?.destroy();
        this.#trigger?.remove();
        this.#list?.remove();
        const select = this.#select;
        if (select) {
            select.id = this.#id;
            select.classList.remove('gm-select-native');
            select.removeAttribute('tabindex');
            select.removeAttribute('aria-hidden');
            select.removeEventListener('change', this.sync);
            select.removeEventListener('focus', this.#focusTrigger);
            select.form?.removeEventListener('reset', this.#onReset);
        }
        this.#trigger = this.#list = this.#droplet = undefined;
    }
    #build() {
        const select = this.querySelector(':scope > select');
        if (!select || this.#trigger)
            return;
        this.#select = select;
        const n = ++ids;
        this.#id = select.id;
        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'gm-input gm-select-trigger';
        trigger.id = select.id || `gm-select-${n}`;
        trigger.innerHTML = `<span class="gm-select-value"></span>${CHEVRON}`;
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        select.id = '';
        select.classList.add('gm-select-native');
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');
        const list = document.createElement('gm-popover');
        list.id = `gm-select-list-${n}`;
        list.className = 'gm-select-list';
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-labelledby', trigger.id);
        trigger.setAttribute('popovertarget', list.id);
        trigger.setAttribute('aria-controls', list.id);
        this.append(trigger, list);
        this.#trigger = trigger;
        this.#list = list;
        trigger.addEventListener('keydown', this.#onTriggerKey);
        list.addEventListener('keydown', this.#onListKey);
        list.addEventListener('beforetoggle', (e) => {
            if (e.newState === 'open')
                list.style.minWidth = `${trigger.offsetWidth}px`;
        });
        list.addEventListener('toggle', (e) => {
            const open = e.newState === 'open';
            trigger.setAttribute('aria-expanded', String(open));
            if (open) {
                this.#active = -1;
                this.#focus(Math.max(this.#selectedIndex(), 0));
            }
        });
        select.addEventListener('change', this.sync);
        // The browser points at an invalid control by focusing it: hand that to the button.
        select.addEventListener('focus', this.#focusTrigger);
        select.form?.addEventListener('reset', this.#onReset);
        this.#droplet = new Droplet(list, () => {
            const item = this.#items[this.#active];
            return item ? boxOf(item) : undefined;
        }, 10);
        this.#observer = new MutationObserver(() => this.sync());
        this.#observer.observe(select, {
            childList: true,
            subtree: true,
            characterData: true,
            attributes: true,
        });
        this.sync();
    }
    /** The chosen option's value; setting it chooses without firing events. */
    get value() {
        return this.#select?.value ?? '';
    }
    set value(value) {
        if (!this.#select)
            return;
        this.#select.value = value;
        this.sync();
    }
    /** Rebuilds the list and the button from the select; call it after changing its value in code. */
    sync = () => {
        const select = this.#select;
        const list = this.#list;
        const trigger = this.#trigger;
        if (!select || !list || !trigger)
            return;
        this.#observer?.takeRecords();
        const items = [];
        const nodes = [];
        for (const child of select.children) {
            if (child instanceof HTMLOptGroupElement) {
                const heading = document.createElement('div');
                heading.className = 'gm-select-group';
                heading.setAttribute('role', 'presentation');
                heading.textContent = child.label;
                nodes.push(heading);
                for (const option of child.children)
                    this.#option(option, items, nodes);
            }
            else
                this.#option(child, items, nodes);
        }
        this.#items = items;
        const droplet = this.#droplet?.el;
        list.replaceChildren(...(droplet ? [droplet] : []), ...nodes);
        const chosen = select.options[select.selectedIndex] ??
            select.querySelector('option[selected]');
        const value = trigger.querySelector('.gm-select-value');
        value.textContent = chosen ? chosen.label || chosen.text : '';
        trigger.classList.toggle('gm-select-placeholder', !chosen || chosen.value === '');
        trigger.disabled = select.disabled;
        // What describes the select describes the button: its error, its hint, its state.
        for (const name of ['aria-label', 'aria-describedby', 'aria-invalid', 'aria-required']) {
            const v = select.getAttribute(name);
            if (v === null)
                trigger.removeAttribute(name);
            else
                trigger.setAttribute(name, v);
        }
        if (select.required)
            trigger.setAttribute('aria-required', 'true');
    };
    #option(option, items, nodes) {
        if (!(option instanceof HTMLOptionElement) || option.hidden || option.value === '')
            return;
        const item = document.createElement('div');
        item.className = 'gm-select-option';
        item.id = `${this.#list.id}-${items.length}`;
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', String(option.value === this.#select.value));
        if (option.disabled)
            item.setAttribute('aria-disabled', 'true');
        item.tabIndex = -1;
        item.textContent = option.label || option.text;
        item.dataset.value = option.value;
        const i = items.length;
        item.addEventListener('click', () => this.#pick(i));
        item.addEventListener('pointerenter', () => this.#focus(i));
        items.push(item);
        nodes.push(item);
    }
    #selectedIndex() {
        return this.#items.findIndex((item) => item.dataset.value === this.#select?.value);
    }
    #focus(i) {
        const item = this.#items[i];
        if (!item)
            return;
        this.#active = i;
        if (document.activeElement !== item)
            item.focus();
        this.#droplet?.follow();
    }
    #pick(i) {
        const item = this.#items[i];
        const select = this.#select;
        if (!item || !select || item.hasAttribute('aria-disabled'))
            return;
        const changed = select.value !== item.dataset.value;
        select.value = item.dataset.value;
        this.#list?.close();
        if (!changed)
            return;
        this.sync();
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
        this.dispatchEvent(new CustomEvent('gm-change', {
            detail: { value: select.value, label: item.textContent ?? '' },
            bubbles: true,
        }));
    }
    #focusTrigger = () => this.#trigger?.focus();
    // A reset puts the select back after this event, so read it on the next task.
    #onReset = () => setTimeout(this.sync);
    #onTriggerKey = (e) => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')
            return;
        e.preventDefault();
        this.#list?.open();
    };
    #onListKey = (e) => {
        const last = this.#items.length - 1;
        const moves = {
            ArrowDown: Math.min(this.#active + 1, last),
            ArrowUp: Math.max(this.#active - 1, 0),
            Home: 0,
            End: last,
            PageDown: Math.min(this.#active + 10, last),
            PageUp: Math.max(this.#active - 10, 0),
        };
        const next = moves[e.key];
        if (next !== undefined) {
            e.preventDefault();
            this.#focus(next);
        }
        else if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            this.#pick(this.#active);
        }
        else if (e.key === 'Tab') {
            this.#list?.close();
        }
        else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
            // Typing jumps to the first option that starts with what was typed in the last second.
            const now = performance.now();
            this.#typed = (now - this.#typedAt < 1000 ? this.#typed : '') + e.key.toLowerCase();
            this.#typedAt = now;
            const i = this.#items.findIndex((item) => item.textContent?.toLowerCase().startsWith(this.#typed));
            if (i >= 0)
                this.#focus(i);
        }
    };
}
define('gm-select', GmSelect);
//# sourceMappingURL=select.js.map