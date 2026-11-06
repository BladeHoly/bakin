import { Base, define } from './base.js';
/**
 * `<gm-slider><input type="range" min="0" max="100" value="40"></gm-slider>`: a range on a light
 * track, filled up to its value, with a knob that stretches while held. The native input does the
 * work (keyboard, forms, screen readers); this paints the fill. It isn't glass: like a chip, it
 * sits inside glass.
 *
 * With two inputs inside (same `min`, `max` and `step`; label each), it's a range from one to
 * the other, like a price filter: the fill runs between the knobs, and neither knob passes the
 * other.
 */
export class GmSlider extends Base {
    #inputs = [];
    connectedCallback() {
        this.#inputs = [...this.querySelectorAll(':scope > input[type="range"]')];
        this.toggleAttribute('data-range', this.#inputs.length > 1);
        for (const input of this.#inputs)
            input.addEventListener('input', this.#onInput);
        this.sync();
    }
    disconnectedCallback() {
        for (const input of this.#inputs)
            input.removeEventListener('input', this.#onInput);
    }
    #onInput = (e) => {
        const [low, high] = this.#inputs;
        if (low && high) {
            // The knob being moved stops where the other one is.
            if (e.target === low && Number(low.value) > Number(high.value))
                low.value = high.value;
            if (e.target === high && Number(high.value) < Number(low.value))
                high.value = low.value;
            // The one moved last sits on top, so two knobs at the same spot can still part.
            low.style.zIndex = e.target === low ? '1' : '';
        }
        this.sync();
    };
    /** Repaints the fill; call it after setting a value from code. */
    sync = () => {
        const [first, second] = this.#inputs;
        if (!first)
            return;
        const min = Number(first.min || 0);
        const max = Number(first.max || 100);
        const at = (input) => `${max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0}%`;
        this.style.setProperty('--gm-from', second ? at(first) : '0%');
        this.style.setProperty('--gm-fill', at(second ?? first));
    };
}
define('gm-slider', GmSlider);
//# sourceMappingURL=slider.js.map