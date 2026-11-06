import { define, GlassElement } from './base.js';
import { boxOf, Droplet } from './droplet.js';
/**
 * `<gm-segmented value="chat"><button value="chat">Chat</button><button value="voice">Voice</button>
 * </gm-segmented>`: one choice among a few, on a glass bar with a droplet under the chosen one.
 *
 * A radio group: the arrow keys, Home and End move the choice, and only the chosen option is in
 * the tab order. `value` reads and sets the choice; a change by the user fires `gm-change`.
 */
export class GmSegmented extends GlassElement {
    static glassDefaults = { radius: 999, bezel: 14, thickness: 28 };
    static get observedAttributes() {
        return [...super.observedAttributes, 'value'];
    }
    #options = [];
    #droplet;
    #index = 0;
    setup() {
        if (!this.hasAttribute('role'))
            this.setAttribute('role', 'radiogroup');
        const label = this.getAttribute('label');
        if (label && !this.hasAttribute('aria-label'))
            this.setAttribute('aria-label', label);
        this.#options = [...this.querySelectorAll(':scope > [value]')];
        this.#options.forEach((option, i) => {
            option.setAttribute('role', 'radio');
            option.classList.add('gm-segment');
            if (option instanceof HTMLButtonElement)
                option.type = 'button';
            option.addEventListener('click', () => this.#choose(i));
        });
        this.addEventListener('keydown', this.#onKey);
        this.#droplet = new Droplet(this, () => {
            const option = this.#options[this.#index];
            return option ? boxOf(option) : undefined;
        });
        const initial = this.#options.findIndex((o) => o.getAttribute('value') === this.getAttribute('value'));
        this.#render(Math.max(initial, 0));
    }
    teardown() {
        this.removeEventListener('keydown', this.#onKey);
        this.#droplet?.destroy();
    }
    attributeChangedCallback(name, before, after) {
        if (name !== 'value')
            return super.attributeChangedCallback(name, before, after);
        const i = this.#options.findIndex((o) => o.getAttribute('value') === after);
        if (i >= 0 && i !== this.#index)
            this.#render(i);
    }
    /** The chosen option's value. Setting it moves the choice without firing `gm-change`. */
    get value() {
        return this.#options[this.#index]?.getAttribute('value') ?? this.getAttribute('value') ?? '';
    }
    set value(value) {
        this.setAttribute('value', value);
    }
    #choose(i) {
        if (i === this.#index || !this.#options[i])
            return;
        this.#render(i);
        this.dispatchEvent(new CustomEvent('gm-change', {
            detail: { value: this.value, index: i },
            bubbles: true,
        }));
    }
    #render(i) {
        this.#index = i;
        this.#options.forEach((option, j) => {
            option.setAttribute('aria-checked', String(i === j));
            option.tabIndex = i === j ? 0 : -1;
        });
        const value = this.#options[i]?.getAttribute('value');
        if (value !== null && value !== undefined && this.getAttribute('value') !== value) {
            this.setAttribute('value', value);
        }
        this.#droplet?.follow();
    }
    #onKey = (e) => {
        const last = this.#options.length - 1;
        const moves = {
            ArrowRight: this.#index === last ? 0 : this.#index + 1,
            ArrowDown: this.#index === last ? 0 : this.#index + 1,
            ArrowLeft: this.#index === 0 ? last : this.#index - 1,
            ArrowUp: this.#index === 0 ? last : this.#index - 1,
            Home: 0,
            End: last,
        };
        const next = moves[e.key];
        if (next === undefined)
            return;
        e.preventDefault();
        this.#choose(next);
        this.#options[next].focus();
    };
}
define('gm-segmented', GmSegmented);
//# sourceMappingURL=segmented.js.map