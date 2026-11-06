import { Base, define } from './base.js';
/**
 * `<label for="code">Code</label> <gm-otp><input id="code" name="code" maxlength="6"></gm-otp>`:
 * a one-time code shown as one box per character.
 *
 * It's a single real input, drawn as boxes: SMS and email autofill, password managers, paste and
 * screen readers see one ordinary field. Digits only unless `inputmode="text"`; `maxlength` sets
 * how many boxes. Filling the last box fires `gm-complete` with the code.
 */
export class GmOtp extends Base {
    #input = null;
    #cells;
    connectedCallback() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.isConnected && this.#build(), {
                once: true,
            });
        }
        else
            this.#build();
    }
    #build() {
        const input = this.querySelector(':scope > input');
        if (!input || this.#cells)
            return;
        this.#input = input;
        input.classList.add('gm-otp-input');
        if (!input.maxLength || input.maxLength < 1)
            input.maxLength = 6;
        if (!input.inputMode)
            input.inputMode = 'numeric';
        if (!input.autocomplete)
            input.autocomplete = 'one-time-code';
        input.spellcheck = false;
        const cells = document.createElement('div');
        cells.className = 'gm-otp-cells';
        cells.setAttribute('aria-hidden', 'true');
        cells.append(...Array.from({ length: input.maxLength }, () => document.createElement('span')));
        this.append(cells);
        this.#cells = cells;
        input.addEventListener('input', this.#onInput);
        for (const type of ['focus', 'blur', 'keyup', 'click']) {
            input.addEventListener(type, this.sync);
        }
        this.sync();
    }
    disconnectedCallback() {
        const input = this.#input;
        input?.removeEventListener('input', this.#onInput);
        for (const type of ['focus', 'blur', 'keyup', 'click']) {
            input?.removeEventListener(type, this.sync);
        }
        this.#cells?.remove();
        this.#cells = undefined;
    }
    #onInput = () => {
        const input = this.#input;
        const clean = input.inputMode === 'text' ? /[^\p{L}\p{N}]/gu : /\D/g;
        const value = input.value.replace(clean, '').slice(0, input.maxLength);
        if (value !== input.value)
            input.value = value;
        this.sync();
        if (value.length === input.maxLength) {
            this.dispatchEvent(new CustomEvent('gm-complete', { detail: { value }, bubbles: true }));
        }
    };
    /** Redraws the boxes; call it after setting the input's value from code. */
    sync = () => {
        const input = this.#input;
        if (!input || !this.#cells)
            return;
        const focused = document.activeElement === input;
        // The caret is always at the end: the box after the last character is the one being typed.
        if (focused)
            input.setSelectionRange(input.value.length, input.value.length);
        const at = Math.min(input.value.length, input.maxLength - 1);
        [...this.#cells.children].forEach((cell, i) => {
            cell.textContent = input.value[i] ?? '';
            cell.classList.toggle('gm-otp-active', focused && i === at);
        });
    };
}
define('gm-otp', GmOtp);
//# sourceMappingURL=otp.js.map