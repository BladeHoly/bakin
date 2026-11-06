import { define, GlassElement } from './base.js';
/**
 * `<gm-callout type="tip" heading="Before you start">…</gm-callout>`: an aside that stands out
 * from the text around it. Lite glass, since it sits still among paragraphs.
 */
export class GmCallout extends GlassElement {
    static glassDefaults = { radius: 18 };
    static lite = true;
    setup() {
        if (!this.hasAttribute('type'))
            this.setAttribute('type', 'note');
        if (!this.hasAttribute('role'))
            this.setAttribute('role', 'note');
        const heading = this.getAttribute('heading');
        if (heading && !this.querySelector(':scope > .gm-callout-heading')) {
            const p = document.createElement('p');
            p.className = 'gm-callout-heading';
            p.textContent = heading;
            this.prepend(p);
        }
    }
}
define('gm-callout', GmCallout);
//# sourceMappingURL=callout.js.map