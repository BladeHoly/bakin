function browserEnv() {
    if (typeof window === 'undefined' || typeof CSS === 'undefined')
        return null;
    const brands = navigator.userAgentData?.brands;
    return {
        supports: (p, v) => CSS.supports(p, v),
        matchMedia: (q) => window.matchMedia(q),
        isChromium: brands?.some((b) => b.brand === 'Chromium') ?? false,
    };
}
/** Pass `null` to describe a non-browser environment (SSR). */
export function detectTier(env = browserEnv()) {
    if (!env)
        return 'solid';
    if (env.matchMedia('(prefers-reduced-transparency: reduce)').matches)
        return 'solid';
    const backdrop = env.supports('backdrop-filter', 'blur(1px)') ||
        env.supports('-webkit-backdrop-filter', 'blur(1px)');
    if (!backdrop)
        return 'solid';
    return env.isChromium ? 'refract' : 'blur';
}
//# sourceMappingURL=support.js.map