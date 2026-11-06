import { createGlass, detectTier } from '@glass-midas/core';
// On a server there's no HTMLElement; an empty class lets the module load there (Astro, SSR)
// without defining anything.
export const Base = (typeof HTMLElement === 'undefined' ? class {
} : HTMLElement);
const NUMBERS = [
    'radius',
    'bezel',
    'thickness',
    'ior',
    'specular',
    'specular-saturation',
    'light-angle',
    'curvature',
    'magnify',
    'chroma',
];
export const GLASS_ATTRIBUTES = [...NUMBERS, 'tier', 'blur', 'caustics', 'variant', 'material'];
const camel = (s) => s.replace(/-(\w)/g, (_, c) => c.toUpperCase());
/**
 * Glass options from an element's attributes, over the component's defaults. A lite surface
 * (`variant="lite"`, or a component that's lite by default) steps down from refraction to the
 * blur tier: right for large surfaces that barely move against their backdrop. It never steps
 * up, so a browser or user that needs the solid tier still gets it.
 *
 * `material="frost"` frosts the backdrop heavily (and the stylesheet adds a smoke tint), for
 * glass that carries text over a busy background. The default, clear glass, shows the backdrop.
 */
export function glassOptions(el, defaults = {}, lite = false) {
    const options = { ...defaults };
    for (const name of NUMBERS) {
        const value = el.getAttribute(name);
        if (value !== null && value !== '' && !Number.isNaN(Number(value))) {
            options[camel(name)] = Number(value);
        }
    }
    const variant = el.getAttribute('variant');
    const tier = el.getAttribute('tier');
    if (tier === 'refract' || tier === 'blur' || tier === 'solid')
        options.tier = tier;
    else if ((lite && variant !== 'full') || variant === 'lite') {
        const detected = detectTier();
        options.tier = detected === 'refract' ? 'blur' : detected;
    }
    // Frost: heavy frosting so text and icons on the glass always read; the rim stays crisp.
    if (el.getAttribute('material') === 'frost')
        options.blur = 'var(--gm-blur-lg, 28px)';
    const blur = el.getAttribute('blur');
    if (blur)
        options.blur = /^[\d.]+$/.test(blur) ? `${blur}px` : blur;
    const caustics = el.getAttribute('caustics');
    if (caustics !== null)
        options.caustics = caustics !== 'false';
    return options;
}
/**
 * Base for every glass component: a light-DOM custom element whose glass options come from
 * its attributes. Subclasses set `glassDefaults`, build their markup in `setup()` and clean up
 * in `teardown()`.
 */
export class GlassElement extends Base {
    static glassDefaults = {};
    /** Lite by default: blur instead of refraction, unless `variant="full"`. */
    static lite = false;
    static get observedAttributes() {
        return GLASS_ATTRIBUTES;
    }
    glass;
    #ready = false;
    /** The element that becomes glass; the host itself unless a component says otherwise. */
    get glassTarget() {
        return this;
    }
    setup() { }
    teardown() { }
    connectedCallback() {
        // While the page is still parsing, an element defined early connects before its children
        // exist; build it once they do.
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.isConnected && this.connectedCallback(), {
                once: true,
            });
            return;
        }
        if (!this.#ready) {
            this.setup();
            this.#ready = true;
        }
        this.#attach();
    }
    disconnectedCallback() {
        this.glass?.destroy();
        this.glass = undefined;
        this.teardown();
        this.#ready = false;
    }
    attributeChangedCallback(name, before, after) {
        if (!this.glass || before === after)
            return;
        // A surface keeps its tier for life, so a new tier or variant means a new surface.
        if (name === 'tier' || name === 'variant') {
            this.glass.destroy();
            this.#attach();
        }
        else {
            this.glass.update(this.#options());
        }
    }
    #options() {
        const { glassDefaults, lite } = this.constructor;
        return glassOptions(this, glassDefaults, lite);
    }
    #attach() {
        this.glass = createGlass(this.glassTarget, this.#options());
    }
}
/** Registers a component once, and only where custom elements exist. */
export function define(name, element) {
    if (typeof customElements !== 'undefined' && !customElements.get(name)) {
        customElements.define(name, element);
    }
}
//# sourceMappingURL=base.js.map