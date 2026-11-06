import { define, GlassElement } from './base.js';
/**
 * `<gm-button><button>Analyze docs</button></gm-button>`: a pill of glass around a native button
 * or link, for actions that float on their own over a backdrop. Inside another glass surface,
 * use the `gm-chip` class instead: glass inside glass can't see the page.
 */
export class GmButton extends GlassElement {
    static glassDefaults = { radius: 999, bezel: 12, thickness: 24 };
}
define('gm-button', GmButton);
//# sourceMappingURL=button.js.map