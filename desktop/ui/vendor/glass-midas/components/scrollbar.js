import { define, GlassElement } from './base.js';
const MIN_THUMB = 56;
/**
 * `<gm-scrollbar>`: a glass scroll thumb for the page. It mirrors and drives the page's own
 * scroll, so the keyboard, wheel and touch keep working. With a mouse, the stylesheet hides the
 * native scrollbar while it's present; on touch screens it hides the thumb instead. With a
 * smooth-scroll library, set `scroller` so dragging goes through it.
 */
export class GmScrollbar extends GlassElement {
    // Clear glass with no rim light: a thumb shouldn't call attention to itself.
    static glassDefaults = {
        radius: 999,
        bezel: 4,
        thickness: 8,
        blur: '2px',
        specular: 0,
        specularSaturation: 1,
    };
    /** Scrolls the page to `y` immediately; replace it to route through a smooth-scroll library. */
    scroller = (y) => scrollTo({ top: y, behavior: 'instant' });
    #frame = 0;
    #rest;
    // Shown clearly while the page scrolls, faint again once it rests.
    #onScroll = () => {
        this.#sync();
        this.toggleAttribute('active', true);
        clearTimeout(this.#rest);
        this.#rest = setTimeout(() => this.toggleAttribute('active', false), 900);
    };
    #drag = null;
    #observer;
    #sync = () => {
        cancelAnimationFrame(this.#frame);
        this.#frame = requestAnimationFrame(() => {
            const page = document.documentElement.scrollHeight;
            const view = innerHeight;
            this.hidden = page <= view + 1;
            if (this.hidden)
                return;
            const height = Math.max(MIN_THUMB, (view / page) * (view - 16));
            const top = 8 + (scrollY / (page - view)) * (view - 16 - height);
            this.style.height = `${height}px`;
            this.style.transform = `translateY(${top}px)`;
        });
    };
    setup() {
        this.setAttribute('aria-hidden', 'true');
        addEventListener('scroll', this.#onScroll, { passive: true });
        addEventListener('resize', this.#sync);
        if (typeof ResizeObserver !== 'undefined') {
            this.#observer = new ResizeObserver(this.#sync);
            this.#observer.observe(document.body);
        }
        this.addEventListener('pointerdown', this.#down);
        this.addEventListener('pointermove', this.#move);
        this.addEventListener('pointerup', this.#up);
        this.addEventListener('pointercancel', this.#up);
        this.#sync();
    }
    teardown() {
        cancelAnimationFrame(this.#frame);
        clearTimeout(this.#rest);
        removeEventListener('scroll', this.#onScroll);
        removeEventListener('resize', this.#sync);
        this.#observer?.disconnect();
    }
    #down = (e) => {
        this.setPointerCapture(e.pointerId);
        this.#drag = { startY: e.clientY, startScroll: scrollY };
        this.toggleAttribute('dragging', true);
    };
    #move = (e) => {
        const d = this.#drag;
        if (!d)
            return;
        const track = innerHeight - 16 - this.offsetHeight;
        const ratio = (document.documentElement.scrollHeight - innerHeight) / Math.max(track, 1);
        this.scroller(d.startScroll + (e.clientY - d.startY) * ratio);
    };
    #up = () => {
        this.#drag = null;
        this.toggleAttribute('dragging', false);
    };
}
define('gm-scrollbar', GmScrollbar);
//# sourceMappingURL=scrollbar.js.map