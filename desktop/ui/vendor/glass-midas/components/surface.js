import { define, GlassElement } from './base.js';
/**
 * `<gm-surface>`: any block as a glass panel. Full refraction by default; add
 * `variant="lite"` for large panels, which then use the cheaper blur tier.
 */
export class GmSurface extends GlassElement {
    static glassDefaults = { radius: 24 };
}
define('gm-surface', GmSurface);
//# sourceMappingURL=surface.js.map