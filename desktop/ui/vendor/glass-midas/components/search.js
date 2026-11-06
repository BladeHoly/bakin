import { createGlass } from '@glass-midas/core';
import { define, GlassElement } from './base.js';
let uid = 0;
/**
 * `<gm-search placeholder="Search docs" hotkey="k">`: a glass search box with a list of
 * results under it. Set its `source` to a function from query to results (static data,
 * Pagefind, an API). Picking a result fires a cancelable `gm-select` event; unless canceled,
 * the page navigates to the result's `href`.
 */
export class GmSearch extends GlassElement {
    static glassDefaults = { radius: 999 };
    /** Where results come from. Called with an empty query for suggestions on focus. */
    source = () => [];
    #field;
    #input;
    #list;
    #listGlass;
    #results = [];
    #selected = 0;
    #request = 0;
    #debounce;
    #onKey = (e) => {
        const hotkey = this.getAttribute('hotkey');
        if (hotkey && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === hotkey.toLowerCase()) {
            e.preventDefault();
            this.#input?.focus();
        }
    };
    get glassTarget() {
        return this.#field ?? this;
    }
    setup() {
        const id = `gm-search-${++uid}`;
        const field = document.createElement('div');
        field.className = 'gm-search-field';
        field.innerHTML =
            '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>';
        const input = document.createElement('input');
        input.type = 'search';
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('aria-controls', `${id}-list`);
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-label', this.getAttribute('label') ?? 'Search');
        input.placeholder = this.getAttribute('placeholder') ?? 'Search';
        input.autocomplete = 'off';
        field.append(input);
        const hotkey = this.getAttribute('hotkey');
        if (hotkey) {
            const kbd = document.createElement('kbd');
            kbd.setAttribute('aria-hidden', 'true');
            kbd.textContent = `Ctrl ${hotkey.toUpperCase()}`;
            field.append(kbd);
        }
        const list = document.createElement('div');
        list.className = 'gm-search-results';
        list.id = `${id}-list`;
        list.setAttribute('role', 'listbox');
        list.hidden = true;
        input.addEventListener('focus', () => this.#query());
        input.addEventListener('input', () => {
            clearTimeout(this.#debounce);
            this.#debounce = setTimeout(() => this.#query(), 120);
        });
        input.addEventListener('keydown', (e) => this.#onInputKey(e));
        // Close when focus leaves the field and its list, not on every click inside them.
        this.addEventListener('focusout', (e) => {
            if (!this.contains(e.relatedTarget))
                this.close();
        });
        this.append(field, list);
        this.#field = field;
        this.#input = input;
        this.#list = list;
        addEventListener('keydown', this.#onKey);
    }
    teardown() {
        removeEventListener('keydown', this.#onKey);
        clearTimeout(this.#debounce);
        this.#listGlass?.destroy();
        this.#listGlass = undefined;
        this.#field?.remove();
        this.#list?.remove();
    }
    close() {
        if (!this.#list || !this.#input)
            return;
        this.#list.hidden = true;
        this.#input.setAttribute('aria-expanded', 'false');
        this.#input.removeAttribute('aria-activedescendant');
    }
    async #query() {
        const input = this.#input;
        if (!input)
            return;
        const request = ++this.#request;
        const results = await this.source(input.value.trim());
        if (request !== this.#request || document.activeElement !== input)
            return; // stale or left
        this.#results = results;
        this.#selected = 0;
        this.#renderList();
    }
    #renderList() {
        const list = this.#list;
        const input = this.#input;
        list.replaceChildren(...(this.#results.length
            ? this.#results.map((r, i) => {
                const option = document.createElement('div');
                option.className = 'gm-search-result';
                option.id = `${list.id}-${i}`;
                option.setAttribute('role', 'option');
                const label = document.createElement('span');
                label.textContent = r.label;
                option.append(label);
                if (r.hint) {
                    const hint = document.createElement('small');
                    hint.textContent = r.hint;
                    option.append(hint);
                }
                option.addEventListener('pointerenter', () => this.#select(i));
                // Keep focus in the input until the click lands.
                option.addEventListener('pointerdown', (e) => e.preventDefault());
                option.addEventListener('click', () => this.#pick(i));
                return option;
            })
            : [
                Object.assign(document.createElement('p'), {
                    className: 'gm-search-empty',
                    textContent: this.getAttribute('empty') ?? 'No results',
                }),
            ]));
        list.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        // The list's glass is created on first show, when it has a size to refract.
        const blur = this.getAttribute('material') === 'frost' ? 'var(--gm-blur-lg, 28px)' : '6px';
        this.#listGlass ??= createGlass(list, { radius: 24, bezel: 18, thickness: 32, blur });
        this.#select(this.#selected);
    }
    #select(i) {
        this.#selected = i;
        const options = this.#list?.querySelectorAll('[role="option"]') ?? [];
        options.forEach((o, j) => o.setAttribute('aria-selected', String(i === j)));
        const active = options[i];
        if (active)
            this.#input?.setAttribute('aria-activedescendant', active.id);
    }
    #pick(i) {
        const result = this.#results[i];
        if (!result)
            return;
        const event = new CustomEvent('gm-select', {
            detail: result,
            cancelable: true,
            bubbles: true,
        });
        this.close();
        if (this.#input)
            this.#input.value = '';
        this.#input?.blur();
        if (this.dispatchEvent(event))
            location.href = result.href;
    }
    #onInputKey(e) {
        const last = this.#results.length - 1;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            this.#select(Math.min(this.#selected + 1, last));
        }
        else if (e.key === 'ArrowUp') {
            e.preventDefault();
            this.#select(Math.max(this.#selected - 1, 0));
        }
        else if (e.key === 'Enter') {
            e.preventDefault();
            this.#pick(this.#selected);
        }
        else if (e.key === 'Escape') {
            this.close();
            this.#input?.blur();
        }
    }
}
define('gm-search', GmSearch);
//# sourceMappingURL=search.js.map