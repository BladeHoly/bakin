import { Base, define } from './base.js';
const ICON = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
/**
 * `<gm-quantity><input type="number" min="1" max="9" value="1" aria-label="Quantity"></gm-quantity>`:
 * a number with a button either side to take one off or add one.
 *
 * The native input does the work: typing, the arrow keys, forms, `min`, `max` and `step`. The
 * buttons step it, stop at its limits, and fire `input` and `change` like a person typing would.
 * Their labels come from `label-decrease` and `label-increase`.
 */
export class GmQuantity extends Base {
    #input = null;
    #less;
    #more;
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
        this.#input?.removeEventListener('input', this.sync);
        this.#less?.remove();
        this.#more?.remove();
        this.#less = this.#more = undefined;
    }
    #build() {
        const input = this.querySelector(':scope > input');
        if (!input || this.#less)
            return;
        this.#input = input;
        input.inputMode = 'numeric';
        const button = (label, d, step) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'gm-quantity-step';
            b.setAttribute('aria-label', label);
            b.innerHTML = ICON(d);
            b.addEventListener('click', () => this.#step(step));
            return b;
        };
        this.#less = button(this.getAttribute('label-decrease') ?? 'One less', 'M6 12h12', -1);
        this.#more = button(this.getAttribute('label-increase') ?? 'One more', 'M6 12h12M12 6v12', 1);
        input.before(this.#less);
        input.after(this.#more);
        input.addEventListener('input', this.sync);
        this.sync();
    }
    #step(direction) {
        const input = this.#input;
        if (!input)
            return;
        const step = Number(input.step) || 1;
        const min = input.min === '' ? -Infinity : Number(input.min);
        const max = input.max === '' ? Infinity : Number(input.max);
        const next = Math.min(max, Math.max(min, (Number(input.value) || 0) + direction * step));
        if (String(next) === input.value)
            return;
        // Through the prototype's setter, so a framework watching the input (React) sees the change.
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, String(next));
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    /** Enables the buttons by where the value is; call it after setting the value from code. */
    sync = () => {
        const input = this.#input;
        if (!input || !this.#less || !this.#more)
            return;
        const value = Number(input.value);
        this.#less.disabled = input.disabled || (input.min !== '' && value <= Number(input.min));
        this.#more.disabled = input.disabled || (input.max !== '' && value >= Number(input.max));
    };
}
define('gm-quantity', GmQuantity);
//# sourceMappingURL=quantity.js.map