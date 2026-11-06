import { define, GlassElement } from './base.js';
import { boxOf, Droplet } from './droplet.js';
let uid = 0;
const SYNC_EVENT = 'gm-tabs-sync';
const stored = (key) => {
    try {
        return localStorage.getItem(`gm-tabs:${key}`);
    }
    catch {
        return null;
    }
};
/**
 * `<gm-tabs sync="pkg"><gm-tab label="pnpm">…</gm-tab><gm-tab label="npm">…</gm-tab></gm-tabs>`:
 * tabs on a glass bar with a droplet under the selected one. Tabs sharing a `sync` key switch
 * together across the page and remember the choice. A change of tab, by the user or a synced
 * group, fires `gm-change` with the new index and label.
 */
export class GmTabs extends GlassElement {
    static glassDefaults = { radius: 999, bezel: 14, thickness: 28 };
    #list;
    #buttons = [];
    #panels = [];
    #droplet;
    #onSync = (e) => {
        const { key, label } = e.detail;
        if (key === this.getAttribute('sync'))
            this.selectLabel(label, false);
    };
    get glassTarget() {
        return this.#list ?? this;
    }
    setup() {
        const id = `gm-tabs-${++uid}`;
        this.#panels = [...this.querySelectorAll(':scope > gm-tab')];
        const list = document.createElement('div');
        list.className = 'gm-tablist';
        list.setAttribute('role', 'tablist');
        this.#buttons = this.#panels.map((panel, i) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'gm-tab-button';
            button.setAttribute('role', 'tab');
            button.id = `${id}-tab-${i}`;
            button.textContent = panel.getAttribute('label') ?? `Tab ${i + 1}`;
            button.setAttribute('aria-controls', `${id}-panel-${i}`);
            button.addEventListener('click', () => this.select(i));
            panel.id = `${id}-panel-${i}`;
            panel.setAttribute('role', 'tabpanel');
            panel.setAttribute('aria-labelledby', button.id);
            panel.tabIndex = 0;
            list.append(button);
            return button;
        });
        list.addEventListener('keydown', (e) => this.#onKey(e));
        this.prepend(list);
        this.#list = list;
        this.#droplet = new Droplet(list, () => {
            const b = this.#buttons[this.selected];
            return b ? boxOf(b) : undefined;
        });
        const key = this.getAttribute('sync');
        const remembered = key ? stored(key) : null;
        const first = this.#panels.findIndex((p) => p.getAttribute('label') === remembered);
        this.select(first >= 0 ? first : 0, false);
        addEventListener(SYNC_EVENT, this.#onSync);
    }
    teardown() {
        removeEventListener(SYNC_EVENT, this.#onSync);
        this.#droplet?.destroy();
        this.#list?.remove();
    }
    /** Index of the selected tab. */
    selected = 0;
    select(index, broadcast = true) {
        if (!this.#buttons[index])
            return;
        const changed = index !== this.selected;
        this.selected = index;
        this.#buttons.forEach((b, i) => {
            const on = i === index;
            b.setAttribute('aria-selected', String(on));
            b.tabIndex = on ? 0 : -1;
            this.#panels[i].hidden = !on;
        });
        this.#droplet?.follow();
        const key = this.getAttribute('sync');
        const label = this.#panels[index].getAttribute('label');
        if (key && label && broadcast) {
            try {
                localStorage.setItem(`gm-tabs:${key}`, label);
            }
            catch { }
            dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: { key, label } }));
        }
        if (changed) {
            this.dispatchEvent(new CustomEvent('gm-change', { detail: { index, label }, bubbles: true }));
        }
    }
    selectLabel(label, broadcast = true) {
        const i = this.#panels.findIndex((p) => p.getAttribute('label') === label);
        if (i >= 0 && i !== this.selected)
            this.select(i, broadcast);
    }
    #onKey(e) {
        const last = this.#buttons.length - 1;
        const moves = {
            ArrowRight: this.selected === last ? 0 : this.selected + 1,
            ArrowLeft: this.selected === 0 ? last : this.selected - 1,
            Home: 0,
            End: last,
        };
        const next = moves[e.key];
        if (next === undefined)
            return;
        e.preventDefault();
        this.select(next);
        this.#buttons[next].focus();
    }
}
define('gm-tabs', GmTabs);
//# sourceMappingURL=tabs.js.map