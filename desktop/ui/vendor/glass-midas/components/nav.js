import { define, GlassElement } from './base.js';
import { boxOf, Droplet } from './droplet.js';
/**
 * `<gm-nav orientation="vertical" spy><a href="#intro">Intro</a>…</gm-nav>`: a glass bar of
 * links with a droplet under the current one.
 *
 * - The current link is the one with `aria-current`, or with `spy`, the section of the page
 *   that has scrolled past a third of the viewport.
 * - Hovering or focusing a link moves the droplet there.
 * - Clicking an in-page link fires a cancelable `gm-navigate` event. Cancel it to scroll your
 *   own way (a smooth-scroll library, for example); otherwise the browser jumps as usual. The
 *   clicked link stays current until the scroll rests on it, so the droplet never steps back
 *   through the sections in between.
 */
export class GmNav extends GlassElement {
    static glassDefaults = { radius: 28, bezel: 18, thickness: 36 };
    #links = [];
    #droplet;
    #current = -1;
    #hovered = null;
    #pinned = null;
    #release;
    #onScroll = () => this.#spy();
    setup() {
        if (!this.hasAttribute('role'))
            this.setAttribute('role', 'navigation');
        this.#links = [...this.querySelectorAll(':scope a[href]')];
        this.#droplet = new Droplet(this, () => {
            const a = this.#links[this.#hovered ?? this.#pinned ?? this.#current];
            return a ? boxOf(a) : undefined;
        });
        this.#links.forEach((a, i) => {
            a.addEventListener('pointerenter', () => this.#hover(i));
            a.addEventListener('focus', () => this.#hover(i));
            a.addEventListener('blur', () => this.#hover(null));
            a.addEventListener('click', (e) => this.#click(e, i));
        });
        this.addEventListener('pointerleave', () => this.#hover(null));
        this.#current = Math.max(0, this.#links.findIndex((a) => a.hasAttribute('aria-current')));
        if (this.hasAttribute('spy')) {
            addEventListener('scroll', this.#onScroll, { passive: true });
            addEventListener('resize', this.#onScroll);
            this.#spy();
        }
        this.#render();
    }
    teardown() {
        removeEventListener('scroll', this.#onScroll);
        removeEventListener('resize', this.#onScroll);
        clearTimeout(this.#release);
        this.#droplet?.destroy();
    }
    #hover(i) {
        this.#hovered = i;
        this.#droplet?.follow();
    }
    #click(e, i) {
        const href = this.#links[i].getAttribute('href') ?? '';
        if (!href.startsWith('#'))
            return;
        const target = document.getElementById(decodeURIComponent(href.slice(1)));
        const event = new CustomEvent('gm-navigate', {
            detail: { href, target },
            cancelable: true,
            bubbles: true,
        });
        if (!this.dispatchEvent(event)) {
            e.preventDefault();
            history.replaceState(null, '', href);
        }
        if (!this.hasAttribute('spy')) {
            // Nothing will report the scroll: the clicked link is simply current now.
            this.#links[this.#current]?.removeAttribute('aria-current');
            this.#links[i].setAttribute('aria-current', 'true');
            this.#current = i;
            this.#droplet?.follow();
            return;
        }
        this.#pinned = i;
        this.#render();
        this.#settle();
    }
    // Release the pin once the scroll has rested on the target, or give up after a while.
    #settle() {
        clearTimeout(this.#release);
        const arrived = this.#pinned === this.#current;
        this.#release = setTimeout(() => {
            this.#pinned = null;
            this.#render();
        }, arrived ? 300 : 2500);
    }
    #spy() {
        const line = innerHeight * 0.35;
        let current = 0;
        this.#links.forEach((a, i) => {
            const id = a.hash ? decodeURIComponent(a.hash.slice(1)) : '';
            const section = id ? document.getElementById(id) : null;
            if (section && section.getBoundingClientRect().top <= line)
                current = i;
        });
        // The last section may be too short to reach the line: the page bottom counts as its own.
        if (innerHeight + scrollY >= document.documentElement.scrollHeight - 2) {
            current = this.#links.length - 1;
        }
        if (current === this.#current)
            return;
        this.#current = current;
        if (this.#pinned !== null)
            this.#settle();
        this.#render();
    }
    #render() {
        const shown = this.#pinned ?? this.#current;
        this.#links.forEach((a, i) => {
            if (!this.hasAttribute('spy'))
                return;
            if (i === shown)
                a.setAttribute('aria-current', 'location');
            else
                a.removeAttribute('aria-current');
        });
        this.#droplet?.follow();
    }
}
define('gm-nav', GmNav);
//# sourceMappingURL=nav.js.map