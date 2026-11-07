# Bakin 1.0 — el plan detallado

> Escrito el 2026-11-07 sobre `dev` (557a072). Es la ficha larga de la sección «1.0» de
> `docs/PLAN.md` §4, que ahora es un resumen y apunta acá. Se lee y se trabaja como dice
> `docs/PLAN.md` §2 (una ficha por ítem; el orden es el de este archivo; cada ítem se marca ✅ acá
> y en `docs/HANDOFF.md`). Todo lo que afirma sale de las investigaciones del 2026-11-07 en
> `docs/research/` y se cita por archivo y sección; **ningún número de acá se midió de nuevo**:
> los *(suposición)* de las investigaciones siguen siendo suposiciones hasta el paso cero.
>
> Investigaciones base: `2026-11-07-gpui-en-windows.md` (GPUI), `2026-11-07-optimizaciones-1.0.md`
> (OPT), `2026-11-07-paridad-1.0.md` (PAR), `2026-11-07-pruebas-sin-cdp.md` (TEST),
> `2026-11-07-rust-y-python.md` (RYP), `2026-11-07-seguridad-1.0.md` (SEC),
> `2026-11-07-refraccion-del-escritorio.md` (REF), `2026-11-07-novedades-1.0.md` (NOV). Abajo se
> citan con esas siglas: «OPT A1» = tabla A, fila 1 de optimizaciones; «PAR §7.2» = sección 7.2
> de paridad.

---

## 0. Cómo leer este plan

* **Ficha de ítem** = la de `docs/PLAN.md` §2: Qué / Cómo (archivos, crates) / Riesgos / Listo
  cuando / Chequeos / Tamaño. Costos en la escala de PLAN §2: **S** medio día, **M** 1–2 días,
  **L** 3–5 días, **XL** más de una semana (se parte). Las investigaciones usan otra escala (OPT:
  S horas, M días, L semanas; RYP: S ≤ 1 día, M 2–4 días, L 1–2 semanas): acá están convertidas.
* **Estado**: ⬜ pendiente · 🟡 en curso · ✅ hecho (con commit) · ⏸ postergado (con motivo).
* **Fases** en orden: «0.25 previa» (arreglos chicos, hechos en otra sesión salvo uno) → **1.0-α0 Paso
  cero** (prototipo y mediciones, con compuertas) → α1 Cimientos → α2 Mini → α3 Índice → α4
  Música → α5 Cola y modo juego → α6 Descargas y Convertir → α7 Imágenes, Configuración y
  Apariencia → **1.0-β Cierre** → **1.0.0**. Son hitos de trabajo, no publicaciones: **no hay
  ninguna publicación entre la 0.13.2 y la 1.0.0** (decisión del usuario).
* **Compuertas (go/no-go)**: cada fase termina con una tabla de condiciones medibles. Si una
  falla, la fase no cierra; lo que no llega se achica o se posterga, nunca se publica a medias.
* **Reglas nuevas de la 1.0** (además de las de `CLAUDE.md` y PLAN §1):
  1. Las pruebas **nunca tocan el mouse, el teclado ni el foco del usuario** (TEST §2.3): nada de
     `SendInput`, `SetForegroundWindow`, `click_input()`; ningún smoke ni ventana mientras el
     usuario juega.
  2. **Un escritor por archivo y por versión** (RYP §7): el lado que no es dueño lee y no escribe.
  3. **Medir siempre en release** (OPT A13): la copia de prueba se compila `--release --features
     probe`; nada de números de debug en `budgets.json` para la interfaz nativa.
  4. **Lo que hoy existe en la interfaz se conserva tal cual** salvo decisión escrita (PAR §0 y
     la lista de §6 de este archivo): textos entre comillas, atajos, clic derecho, estados
     recordados, deshacer, preguntas de una sola vez.

---

## 1. Lo decidido por el usuario (2026-11-07)

Estas decisiones mandan sobre cualquier ítem de abajo:

1. **1.0 = interfaz nativa con GPUI, sin WebView**, pantalla por pantalla. **La app actual
   (Tauri) sigue siendo la instalada hasta que la nueva la iguale; no se envía ningún híbrido.**
   Metas medibles: ventana abierta ≤ 100 MB, visible en < 1 s.
2. **No se publican la 0.14–0.24.** La próxima publicación es la 1.0, con un solo registro de
   Novedades por funciones (borrador: NOV). La prueba a mano del usuario, después de la 1.0.
   Publicaciones y pushes al repo público **solo con el OK explícito del usuario**, cada vez.
3. **Apariencia**: transparencia y vidrio a gusto, basados en glass-midas (preajustes +
   «Personalizado»); refracción sobre lo que Bakin dibuja; **«Refractar el escritorio»** opcional,
   apagado por defecto, con DXGI Desktop Duplication y los trucos de costo (REF «Los trucos»).
   **Cuál preajuste es el predeterminado (Sobrio opaco o Vidrio desenfocado) lo decide el usuario
   después de las mediciones del paso cero** (§11, decisión D1).
4. **«Mandar a Bakin»** desde el celular encola directo (ya hecho, C101).
5. **Antes del prototipo, arreglos chicos** (fase «0.25 previa», otra sesión en paralelo): anillo
   de audio lock-free + prioridad MMCSS, `player.json` por ids y solo al cambiar, restos de voseo,
   seguridad barata (código LAN de 12, aviso en la PC cuando el celular manda un enlace, vista
   previa del respaldo con nombres + lista blanca de ajustes, nombres de cookies de Google en el
   redactor, SECURITY.md al día), `cryptography` fuera del motor.
6. **Recomendaciones de las investigaciones que este plan adopta** (si se desvía, lo dice en el
   ítem): `gpui-pre` fijado con `=` + `gpui-kit` (GPUI «Estabilidad de la API»); binario nuevo y
   crate núcleo compartido sin Tauri (GPUI «Convivir con Tauri», RYP §8 paso 0); índice en Rust
   antes de la pantalla Música (OPT decisión 4); el motor no arranca al iniciar (OPT K1); Python
   solo para el catálogo de YouTube Music al final (RYP §9: ~20 MB, MIT); vidrio con imágenes
   pre-desenfocadas + capa DirectComposition aparte para el escritorio, **sin fork de gpui en la
   1.0** (OPT decisión 1 camino b, GPUI «Shaders propios» caminos b y c); modo juego automático
   (OPT C1); caché LRU de imágenes y miniaturas al tamaño (OPT A1, A2); tres capas de prueba y
   guardia sobre `activate_window` (TEST §3, §2.3); cadena de suministro (SEC §12.3); SignPath
   con la 1.0 (SEC §12.7); accesibilidad vía AccessKit si aguanta (GPUI «Lectores de pantalla»).

---

## 2. Metas y presupuestos de la 1.0

Los de PLAN §5 siguen; se suman (OPT N2) y se miden en release con `BAKIN_MEASURE=1` (OPT N1).
Van a `packaging/budgets.json` y `docs/QA.md` › Budgets (un test los compara).

| Clave | Objetivo (release) | Cómo se mide | Desde |
|---|---|---|---|
| `first_frame_ms` | ≤ 500 (meta del usuario: visible < 1 s) | `measure.json`: proceso lanzado → primer `draw` (`on_next_frame`) | α2 (mini), α4 (ventana) |
| `window_open_mb` | **≤ 100, duro** (deja de ser `warn_only`) | suma de procesos, 10 s después de la biblioteca visible, 5200 temas + 300 fotos | α4 aviso, β falla |
| `idle_frames_per_min` | ≤ 5 | cuadros dibujados en 60 s sin eventos | α2 |
| `scroll_frame_p95_ms` | ≤ 8 | scroll de 5000 filas, p95 por cuadro | α4 |
| `gpu_idle_pct` | < 1 % | PDH «GPU Engine» del proceso, 10 s | α2 |
| `mini_mb` | ≤ 45 (medido en 0.17.3: 46 sin LRU) | C116 | α2 |
| `tray_music_mb` | ≤ 20 (OPT C7: soltar caché y atlas al cerrar) | C9/C10 nativos | α3 |
| `tray_downloads_mb` | ≤ 100 **sin contar yt-dlp en espera** (RYP §8 paso 4) | motor falso | α5 |
| `engine_mb` | ≤ 30 (meta RYP §9: 20–25) | `bakin-catalog` corriendo | β |
| `installer_mb` | ≤ 40 | `release.ps1` imprime | β |
| «Impacto en el juego» | sin presupuesto numérico: **protocolo manual** con PresentMon (OPT N3) | juego fijo del usuario: sin Bakin / bandeja sonando / mini arriba / ventana con cada preajuste | α0, α5, β |

> Desvío: OPT N2 propone `installer_mb ≤ 20` suponiendo motor sin Python; como el catálogo queda
> en Python (RYP §9, ~18–22 MB en disco), el número honesto es ≤ 40 (hoy ≈ 20 + motor de 46). Se
> ajusta en β con el instalador real.

---

## 3. La arquitectura a la que vamos

```
desktop/
  core/        bakin-core   (lib, sin Tauri ni GPUI): paths, migrate, meta, index, tags(lofty),
                            thumbs, trash, music (plays/vistas/playlists/mixes/stats/dupes/
                            tagclean/tagfix/mirror), queue, scheduler, mediadl, tools, vault,
                            credstore, wallhaven, lyrics, musicbrainz, backup, settings, i18n,
                            gamemode, measure, log(redact)       ← lo usan los dos binarios
  ui/          bakin-ui     (lib, GPUI = gpui-pre fijado + gpui-kit): tokens de glass-midas,
                            componentes con test_id, pantallas, sonda (feature probe)
  app/         bakin        (bin nuevo): ventana principal + mini, tray-icon/muda, global-hotkey,
                            auto-launch, instancia única, bakin://, updater, SMTC, LAN, player
  src-tauri/   bakin-desktop (bin actual, Tauri): se mantiene como la app instalada; usa
                            bakin-core detrás de flags solo para verificar; se borra en β
engine/        bakin-catalog (Python: requests + ytmusicapi): catalog_*, spotify_*, read_text_file
```

* **Proceso**: la interfaz, el reproductor, el índice, la cola y el servidor de casa viven en el
  proceso del shell; **el motor no existe salvo mientras el usuario busca en el catálogo** (RYP
  §9). Entre procesos sigue el contrato de hoy: JSON Lines por stdin/stdout (`engine.rs` sin
  cambios) + archivos con **un dueño** (RYP §7, tabla de dueños).
* **GPUI**: `gpui = { package = "gpui-pre", version = "=0.3.x" }` + `gpui-kit` de la misma
  tanda; `default-features = false` (OPT A11); `[patch.crates-io]` listo para `gpui-pre-windows`
  pero **sin fork en la 1.0**. Lo que GPUI no trae se construye con `windows-rs` sobre el HWND
  (GPUI «Tabla por necesidad»): topmost, click-through, bandeja, deep link, instancia única,
  reducir movimiento, drop de enlaces.
* **Vidrio** (OPT decisión 1, B1–B6; GPUI «Shaders propios»): (b) fondo procedural estático y su
  versión desenfocada como texturas, cada superficie de vidrio = recorte + tinte + borde + brillo
  (el «frost» de glass-midas); lo que se mueve debajo va sólido/translúcido; (a) `Blurred`/Mica de
  DWM para la ventana; (c) capa DirectComposition propia **debajo** del target de GPUI para
  «Refractar el escritorio». Tokens y receta en el repo de glass-midas como crate
  `glass-midas-gpui` (ver D13).
* **Pruebas** (TEST §3): capa 1 `cargo test -p bakin-ui` (`TestAppContext`, sin GPU, CI siempre);
  capa 2 `ui-smoke.mjs` → sonda HTTP en loopback con token (`feature = "probe"`, nunca en
  release); capa 3 pocas capturas con tolerancia (`PrintWindow(PW_RENDERFULLCONTENT)`).

---

## 4. Las fases, de un vistazo

| Fase | Qué entrega | Compuerta principal | Decide el usuario |
|---|---|---|---|
| **0.25 previa** ✅ 8/8 | arreglos chicos antes del prototipo (C114, C115); `cryptography` fuera: motor de 46 a 36 MB | pytest/cargo/smoke en verde | — |
| **1.0-α0 Paso cero** | prototipo GPUI: vidrio en tres modos, ventana, input, lista, sistema, sin GPU, sonda, presupuestos; PresentMon con juego | las 8 pruebas de GPUI «Qué tiene que probar el prototipo» + protocolo del juego | **D1** preajuste por defecto; **D2** `gpui-pre` y capa (c); **D3** protocolo del juego |
| **1.0-α1 Cimientos** | workspace (`core`, `ui`, `app`), paths/migrate portados, tokens y componentes, LRU, sonda, `bring_to_front()`, medición, cadena de suministro | `cargo test` verde; ids y tags idénticos a Python; sonda ausente del release | D13 crate de glass-midas |
| **1.0-α2 Mini** | el modo mínimo en GPUI (bar/card/info), bandeja, reproductor en proceso | C116; ≤ 45 MB, < 250 ms, 0 % CPU; el usuario lo aprueba a la vista | **D4** |
| **1.0-α3 Índice** | biblioteca en Rust (0.19.1), arranque sin motor, bandeja liviana | smoke completo con `BAKIN_LIB=rust`; abrir desde la bandeja < 300 ms; sin motor | D5 SQLite (técnica, se informa) |
| **1.0-α4 Música** | núcleo de Música en Rust + la pantalla Música, reproductor, cola, letra, playlists, arreglar | tests portados; ≤ 100 MB y < 1 s con 5200 temas; **el usuario la usa una semana** | **D6** |
| **1.0-α5 Cola y modo juego** | cola y programador en Rust (0.19.2), motor stateless, modo juego automático | `test_background` portado; C12 → A; `queue.json` ida y vuelta; PresentMon con descarga | D7 límite de velocidad en juego |
| **1.0-α6 Descargas y Convertir** | yt-dlp/ffmpeg/herramientas/vault en Rust; pantallas Descargas, Convertir, hub, presets | tests portados; descarga real de prueba termina y se cancela matando el árbol | — |
| **1.0-α7 Imágenes, Configuración, Apariencia** | Wallhaven/LRCLIB/MusicBrainz/respaldo en Rust; Imágenes, visor, fondos, Configuración entera, Apariencia, «Refractar el escritorio» | tests portados; respaldo ida y vuelta; paridad §6 completa | **D8** video en el visor; **D9** refracción del escritorio va o cae a 1.0.x |
| **1.0-β Cierre** | chrome e integración sin Tauri, borrar WebView, motor `bakin-catalog`, seguridad con la 1.0, accesibilidad, H.3–H.6, Novedades, docs | todos los presupuestos en verde en release; QA sin «→ A» que dependa de WebView | **D10** AccessKit; **D11** SignPath; **D12** copia/rotación de la clave |
| **1.0.0** | publicación | OK explícito | **D14** OK de publicar y de pushear |

Las fases α2 y α3 pueden ir en paralelo (no comparten archivos); α5 puede empezar mientras α4 se
pule (RYP §8 último párrafo). Lo que es «pantalla» espera a lo que es «dato» (OPT decisión 4).

---

## 5. Fase por fase

### 5.0 «0.25 previa» — ✅ 8/8 (2026-11-07)

Items chicos que mejoran la app actual y dejan menos que portar. **No se publican**: quedan en
`dev` y entran a la 1.0. La sesión que los hizo tomó **C114** (los arreglos de seguridad) y
**C115** (el reproductor y el voseo) en `docs/QA.md`; este plan reserva desde C116 y R36 (§12).

| # | Qué | Dónde | Tamaño | Verificación | Estado |
|---|---|---|---|---|---|
| P1 | **Anillo de audio lock-free (SPSC) + prioridad del decodificador** en vez de `Mutex<VecDeque<f32>>` con `try_lock` que devuelve silencio (OPT D1, D2; `player.rs:488`) | `player.rs` | M | cargo test; C115 | ✅ |
| P2 | **`player.json` por ids, solo al cambiar la cola, en otro hilo** (OPT D4, K3: 2,3 MB → ~100 KB; hoy se reescribe cada 20 s) | `player.rs::save/restore` | S | cargo test; C20 sigue; C115 | ✅ |
| P3 | **Restos de voseo** al «tú» (PAR §0 y §28.31: encuadre, grabar atajo, Wallhaven «Probá»/«Elegí», `hotkeys.rs`) | `index.html`, `app.js`, `hotkeys.rs`, `i18n` | S | test de voseo; C115; R35 | ✅ |
| P4 | **Código LAN de 12 caracteres** + **aviso en la PC «El teléfono mandó un enlace»** (SEC §12.1; `service.py:1276`, `home.rs:116`) | motor + `home.rs`/`background.rs` | S | pytest; C114 | ✅ |
| P5 | **Respaldo: vista previa con nombres (carpetas, seguidos, presets) + ajustes que nunca se importan** (`lan_*`/`out_dir`/`musicbrainz`/`lyrics_online`); presets importados con cookies apagadas y `template` que no puede salir de la carpeta (SEC §12.2, §7, §5) | `backup.py`, `mediadl.py`, `app.js` | M | `test_backup.py`; C114 | ✅ |
| P6 | **Redactor con nombres de cookies de Google** + `register_secret` del código LAN (SEC §12.5) | `redact.py` | S | `test_redact.py`; C114 | ✅ |
| P7 | **SECURITY.md al día** (tabla SEC §9 + Deno + UA de Spotify + servidor de casa + «confiamos en los releases» + PATH) | `SECURITY.md` | S | lectura; `export-public.ps1`; C114 | ✅ |
| P8 | **`cryptography` fuera del motor** (OPT G1: 9,5 MB que nadie declara; `xref` de PyInstaller) | `packaging/build-engine.ps1` | S | tamaño del motor en `release.ps1`: 46 → 36 MB | ✅ |

Lo que **no** entró en la previa y sí en la 1.0: `redact` en `media_raw`/`error` (α6 D1), tope al
extraer ffmpeg (α6 D2), `wallpaper_set` solo dentro de la biblioteca (α7 P6); el test de nombres de
archivo hostiles con yt-dlp real (α6 D8).

---

### 5.1 1.0-α0 «Paso cero» — el prototipo con compuertas

**Qué**: una ventana GPUI del tamaño de la barra mini y una «principal» de prueba, en la copia
con `BAKIN_MUTE=1`, que demuestre lo que las investigaciones no pudieron verificar leyendo código
(GPUI «Qué tiene que probar el prototipo (fase cero)»). **Nada de esto es código de producto**:
vive en `desktop/proto/` y se tira después de anotar los resultados en `docs/QA.md` › Budgets y
en este archivo. Sin la compuerta Z-GO cerrada no se empieza α1.

| # | Qué se prueba | Cómo | Tamaño |
|---|---|---|---|
| Z1 | **`gpui-pre` fijado compila y abre** una ventana sin bordes, barra propia, los 8 bordes de resize, snap/flyout de Win11, `show: false` + mostrar tras el primer cuadro (sin destello: GPUI «Transparencia por píxel») | `gpui = { package = "gpui-pre", version = "=0.3.11" }` (o la última al empezar), `default-features = false`; `TitlebarOptions { appears_transparent }`, `on_hit_test_window_control` | S |
| Z2 | **Vidrio, tres modos medidos** (REF «Plan», GPUI prueba 1): `Blurred`/Mica de DWM; frost sobre imagen pre-desenfocada; **capa DirectComposition propia** (`CreateTargetForHwnd(hwnd, FALSE)` debajo del de GPUI) con captura DXGI Desktop Duplication + Dual Kawase a media resolución + rectángulos sucios + tope 30 fps + `WDA_EXCLUDEFROMCAPTURE` solo en la capa (REF «Los trucos» 1–8, «Cuidados»). Medir GPU, CPU, retraso, y que la interfaz transparente se compone encima sin artefactos. Probar `GPUI_DISABLE_DIRECT_COMPOSITION=1` para el modo opaco (OPT B5) | `desktop/proto/glass.rs`; PDH para % GPU; `measure.json` | L |
| Z3 | **PresentMon con el juego del usuario** (OPT N3, C3): sin Bakin / bandeja sonando / mini arriba (translúcido y opaco: ¿sale del Independent Flip?) / ventana grande con Sobrio, Vidrio, Cristal. **Lo corre el usuario** cuando quiera jugar; el protocolo escrito en `docs/QA.md` como escenario manual (R45) | protocolo + planilla en `docs/QA.md` | S (+ tiempo del usuario) |
| Z4 | **Ventana**: topmost en caliente (`SetWindowPos`), click-through por regiones (`SetWindowSubclass` + `HTTRANSPARENT` contra `WS_EX_TRANSPARENT` con `WS_EX_NOREDIRECTIONBITMAP`: GPUI «Click-through»), dos ventanas a la vez, dos monitores con 100 % y 150 %, `focus: false` no roba el primer plano (`GetForegroundWindow` antes/después, TEST §2.3) | `proto/window.rs` | M |
| Z5 | **Input**: `Input` de gpui-kit con teclado español (acentos, ñ, dead keys), IME chino/japonés, emoji, selección y pegado; Tab entre tres controles (`tab_index`) | `proto/input.rs` | S |
| Z6 | **Lista**: `list()` con 5000 filas de alto variable con miniaturas, rueda y touchpad (DirectManipulation de `gpui-pre`); memoria con 500 portadas visibles y **que la LRU libere** RAM y VRAM (atlas `remove`) (GPUI prueba 4; OPT A1, A3) | `proto/list.rs` con un `ImageCache` LRU de 32 MB | M |
| Z7 | **Sistema**: `tray-icon` + `muda` con menú nativo en el loop de GPUI; `global-hotkey` + SMTC (`smtc.rs` ya crea su HWND) desde el mismo proceso; `bakin://` por registro + instancia única por mutex + named pipe; una notificación con AUMID (`show_system_notification` de `gpui-pre`) | `proto/system.rs` | M |
| Z8 | **Sin GPU**: adaptador básico (driver deshabilitado o VM) y por RDP; `gpu_specs().is_software_emulated`; anotar si abre y a cuánto (GPUI «Backend GPU»; R36) | manual, anotado | S |
| Z9 | **Pruebas**: un test de `VisualTestContext` (clic + teclas), la sonda mínima por HTTP (`/tree`, `/screenshot` con `PrintWindow(PW_RENDERFULLCONTENT)` sobre DComp: ¿da imagen o negro?), `first_frame_ms`; Narrador/NVDA sobre la ventana (¿AccessKit paniquea? → `inaccessible()`) (TEST §7 paso 0; GPUI prueba 7) | `proto/tests.rs`, `proto/probe.rs` | M |
| Z10 | **Presupuestos del prototipo**: ventana «principal» con lista de 5000 + 500 portadas + vidrio: < 1 s visible y ≤ 100 MB (GPUI prueba 8); mini vacío ≤ 50 MB y < 300 ms (RYP §8 paso 0) | `measure.json` | S |

**Compuerta Z-GO** (todas; se anota el resultado en `docs/QA.md` › Budgets con fecha):

| Condición | Si falla |
|---|---|
| Z1 abre sin destello y con snap | probar la versión anterior/siguiente de `gpui-pre`; si ninguna, `gpui 0.2.2` + `show: false` y se anota lo que se pierde (a11y, touchpad) |
| Z2: el modo (b) frost se ve como el `material="frost"` de hoy (captura comparada a ojo por el usuario) | «Vidrio a gusto» se achica a acrílico de DWM + frost (GPUI «Bloqueantes» 1); H.1 se reescribe |
| Z2: capa (c) compone debajo de GPUI sin artefactos y **cuesta cero con el escritorio quieto** | «Refractar el escritorio» sale de la 1.0 (REF «Plan»: «queda solo en el modo mínimo o se descarta») |
| Z3: con la ventana en Sobrio y con el mini opaco el juego no pierde fps ni Independent Flip | en modo juego, mini opaco y ventana Sobrio **siempre** (OPT B3, C3); si aun así cuesta, el mini se oculta en modo juego |
| Z4: click-through por subclass funciona con DComp | `WS_EX_TRANSPARENT`; si tampoco, el modo gaming del mini usa una ventana `PopUp` opaca aparte |
| Z6: 5000 filas a 60 fps y la LRU baja la memoria al salir de pantalla | sin esto no hay ≤ 100 MB: se revisa el tamaño de miniatura (96 → 64 px) y el tope (32 → 16 MB) antes de seguir |
| Z7: los cinco mecanismos de sistema andan en el loop de GPUI | el que falle se hace con una ventana oculta propia (como `smtc.rs`) |
| Z8: con WARP abre | si no abre: aviso al arrancar «Bakin necesita una tarjeta de video compatible con DirectX 11» y se documenta como requisito (como Zed) |
| Z9: `PrintWindow` da imagen | `GPUI_DISABLE_DIRECT_COMPOSITION=1` en la copia de prueba; si tampoco, capturas solo en la PC del usuario (TEST §8) |
| Z10 en verde | se buscan los MB (OPT A1, A2, A11, A12) antes de α1; la meta de 100 MB no se negocia |
| **D1 tomada**: el usuario eligió el preajuste por defecto viendo Z2 y Z3 | — |

---

### 5.2 1.0-α1 «Cimientos»

| # | Ítem | Tamaño |
|---|---|---|
| A1 | **Workspace nuevo**: `desktop/core/` (`bakin-core`, sin Tauri ni GPUI), `desktop/ui/` (`bakin-ui`, GPUI), `desktop/app/` (bin `bakin`); `desktop/src-tauri/` queda como miembro y **sigue siendo la app instalada**. Perfil release por paquete (OPT A10, M4: `opt-level = "s"` global, `3` para `symphonia-*`, `rubato`, `gpui`, `taffy`, `image`, `zune-jpeg`, `fast_image_resize`; LTO fat, 1 unidad, `strip`). `mimalloc` solo si gana ≥ 3 MB o ≥ 10 % (OPT A12). *Listo cuando*: `cargo build --release -p bakin` abre una ventana vacía. | M |
| A2 | **`paths`, `migrate`, `meta` portados** a `bakin-core` (RYP §4 filas Paths/Migraciones; §7 regla 1): misma carpeta que Python (test cruzado como `covers.rs::the_cache_name_matches_the_engines`), marco de `migrate.py` con `_v`, `*.before-N.json`, atómico, «más nuevo → no tocar»; `.bakin-meta.json` (`schema` 1). *Chequeos*: `cargo test core::paths::*`, `core::migrate::*` con los fixtures de `tests/`. | M |
| A3 | **`lofty` sobre los mismos archivos de prueba que `test_library.py`** y un **script de solo lectura** que compara ids y tags de Python vs Rust sobre la biblioteca real del usuario (`R:\Cosas\Music`; RYP §8 paso 2, §10 «Paridad de datos»). Diferencias aceptadas y documentadas (WMA sin título, ß). *Chequeos*: `cargo test core::tags::*`; R42. | M |
| A4 | **Tokens y componentes de glass-midas en Rust**: `tokens.rs` generado desde `glass-midas/core/tokens.js` (una sola fuente, PLAN H.1; PAR §0 «Tokens»: grilla de 4 px, tipografía 10–32, radios, colores, `--hit-min` 24 px); componentes base (`Surface` frost/lite/sólido, `Button`, `Chip`, `Segmented`, `Menu`, `Dialog`, `Toast` sobre modales, `Label` con `clipped`, `Input` de gpui-kit envuelto) con **`test_id` obligatorio** (TEST §3.2) y `tab_index` en todo lo interactivo. `test_design.py` sigue chequeando los tokens. | L |
| A5 | **Sonda** (`feature = "probe"`, nunca en `default`; TEST §3.2): `/tree`, `/state`, `/action`, `/invoke`, `/mouse` (PostMessage), `/keys`, `/resize`, `/screenshot`, `/engine`, `/metrics`; token en `%BAKIN_HOME%\probe.json`; solo con `BAKIN_HOME` + `BAKIN_PROBE=1`; `release.ps1` verifica que el exe no contenga `bakin-probe/` y un pytest lo verifica sobre el instalador. `ui-smoke.mjs` gana el cliente `native` con la misma firma que `js()`/`engine()` (TEST §5). | L |
| A6 | **`bring_to_front()`**: el único sitio que llama `activate_window()` (que inyecta Alt y roba el foco: TEST §0.4, §2.3); solo tras gesto del usuario (bandeja, atajo, segunda instancia) y **nunca con `BAKIN_HOME`**. *Chequeos*: `cargo test` de uso único (grep del árbol) + R40. | S |
| A7 | **Caché de imágenes LRU** (`ImageCacheProvider` propio, tope 32 MB, libera atlas) y **miniaturas al tamaño en píxeles físicos** por DPI y uso (OPT A1, A2, H1: 96 lógicos, 320 reproductor/SMTC, 420 grilla); decodificar y reducir fuera del hilo de UI, pool de 2, cancelación (OPT A4); prefetch de 2 pantallas (OPT H2). | M |
| A8 | **Medición**: `measure.rs` con `BAKIN_MEASURE=1` → `measure.json` (OPT N1) y los presupuestos de §2 en `budgets.json`; `tracing` + Tracy solo en debug (OPT N4). | M |
| A9 | **Reproductor a prueba de juego, lo que falta** después de la previa: buffer adelantado por contexto (0,5 s escritorio, 2–3 s juego/batería: OPT D3), sin asignaciones por paquete (D5), `recv()` bloqueante en pausa (C4). | S |
| A10 | **Cadena de suministro antes de sumar gpui** (SEC §12.3, §11): `requirements-engine.txt` con hashes + `--require-hashes` en CI y `release.ps1`; `pip-audit` y `cargo audit`/`cargo deny` en el job `test`; acciones de GitHub por SHA; confirmar required reviewers del environment `release`. | M |
| A11 | **Reducir movimiento y modo sin animación**: `SPI_GETCLIENTAREAANIMATION` leído una vez (GPUI «Animación»), `BAKIN_NO_ANIM=1` para capturas; animaciones 150–250 ms, ninguna en bucle (OPT A6). | S |
| A12 | **Estado de la interfaz en archivo**: `ui-state.json` del shell reemplaza al `localStorage` del WebView (PAR §27.10: `bakin.view/tabs/side/hub/recent/skip/compact/lock/seenVersion/ly-asked/lang/wh.*/mini.*`); se importa **una vez** desde el WebView2 existente (leer su `localStorage` por CDP en la copia de prueba no sirve para el usuario: se exporta desde la app actual al cerrar, en un ítem de la previa si hay tiempo, o se acepta perder esas preferencias en la 1.0 y se anota en Novedades). | S |

**Compuerta α1**: `cargo test` del workspace en verde; ids y tags idénticos a Python sobre los
fixtures; la sonda responde en la copia y **no está** en el exe de release; `budgets.json` tiene
las claves nuevas (en aviso hasta que haya pantalla).

---

### 5.3 1.0-α2 «Mini» — la primera pantalla

La mejor primera pantalla (TEST §5 orden sugerido, RYP §8 paso 1): chica, sin listas largas, ya
medida (46 MB, 218 ms), y es la mayor queja histórica del usuario (0.24).

| # | Ítem | Tamaño |
|---|---|---|
| M1 | **Mini en GPUI con paridad total con PAR §11**: ventana transparente sin sombra, abre abajo a la derecha con 24 px o donde estaba (`mini.json`, `remember`); layouts `bar` 420×64, `card` 160–400 cuadrada con grip de esquina, `info` 340×56 (solo modo gaming); «Nada sonando»; portada del caché (nunca despierta al motor); ⏮/⏭ clic = ±N s (`skip` compartido), clic derecho = tema; Espacio; **rueda = volumen** con «Volumen {pct} %» 1,2 s; ⋯ con «Siempre encima», «Ver como tarjeta»/«Ver como barra», «Modo gaming», `hr`, «Cerrar y seguir en la bandeja» (último), menú en espacio que la ventana crea; **modo gaming** con la pregunta de una sola vez («Modo gaming — Los clics van a pasar a lo que está detrás. Para volver: Ctrl+Shift+G o el ícono de Bakin en la bandeja.»), click-through por subclass (Z4), borde punteado; atenuarse (`dim`); Esc / doble clic / ⤢ vuelven a la ventana grande; progreso a 1 Hz (OPT C2); opaco sobre juego si Z3 lo pidió. | L |
| M2 | **Letras locales en Rust** (`lyrics.find` + `parse_lrc`; RYP §4 «Letras locales») para la tarjeta. | S |
| M3 | **Estado del reproductor sin JSON** (OPT D6): la UI lee atómicos/`watch` del `player`; el evento `time` desaparece; SMTC igual. | M |
| M4 | **Bandeja desde el binario nuevo** (`tray-icon` + `muda` en el hilo del loop; GPUI «Bandeja»): menú de PAR §23 con los mismos textos y habilitaciones; click izquierdo abre; i18n desde `core::i18n`. | M |
| M5 | **Capa 1 + capa 2 + 3 goldens**: tests de tamaños, ⋯, transparencia al mouse, posición recordada (`clamp_to_monitors` de H.4); los 8 checks del mini (C36, C41, C60, C61, C67, C68, C77, C107) pasan al cliente `native` **el mismo día, con el mismo número**; goldens bar/card/info al 100 % con vidrio sólido (TEST §3.3). | M |

**Compuerta α2**: C116 en verde; `mini_mb ≤ 45`, `first_frame_ms < 250`, `idle_frames_per_min
≤ 5`, 0 % CPU; sin ningún `msedgewebview2` nuevo; letras en tarjeta; **D4: el usuario lo aprueba
a la vista** (lo prueba como copia aparte, con sonido, con su música apuntada en solo lectura; el
Bakin viejo cerrado mientras tanto, por la regla de un escritor).

---

### 5.4 1.0-α3 «Índice y arranque sin motor» (= 0.19.1)

| # | Ítem | Tamaño |
|---|---|---|
| I1 | **`core/index.rs`**: `Vec<Item>` en memoria + caché persistente; ids `dir:<sha1(path.lower())[:12]>` y `col\|rel` idénticos (RYP §7 regla 2); escaneo incremental por `(size, mtime)` con `walkdir`, 2 hilos a prioridad baja (OPT E2); rutas de red/USB en su hilo con timeout y «no disponible» (E7); paginado y filtros de `library_items`/`libFilters` (PAR §5.3: páginas de 60, orden, scope, kind, tags, color, orientación, resolución, `low_quality`). **D5 (técnica)**: si con 3500 archivos el arranque en memoria con caché `bincode` da < 300 ms, no hay SQLite (RYP §6 go/no-go); si no, `rusqlite` WAL con escrituras incrementales (OPT E1). | XL → I1a memoria + caché, I1b consultas y filtros, I1c `audio-tags.json`/`imageinfo.json` importados una vez |
| I2 | **`core/thumbs.rs`**: `zune-jpeg` con reducción por DCT + `fast_image_resize`, **mismo caché `thumbs/<sha1>`** (nada se regenera), 2 hilos; poda a ~300 MB y borrado de huérfanas (OPT E4, E5); `covers.rs` pasa a usarlo. | M |
| I3 | **`imageinfo` + color dominante** desde la miniatura, en el escaneo (OPT H4; RYP «Dimensiones»). | S |
| I4 | **`core/trash.rs`**: `IFileOperation` con `FOF_ALLOWUNDO`, índice `$I` del `$Recycle.Bin` portado con `BAKIN_FAKE_BIN`; «Eliminadas» con «Restaurar», «Quitar de la lista», «Volver a bajar» (PAR §5.5; esta última encola vía motor hasta α5). | M |
| I5 | **Favoritas, tags, eliminadas** desde `.bakin-meta.json` (la verdad sigue en el meta, RYP §6); `library_overview`/`music_overview` en Rust. | M |
| I6 | **Servidor de casa y rotación desde el índice** (OPT J1, J2; RYP `lan_*` «desaparecen»): `home.rs` no despierta al motor; portadas del caché; chunks de 64 KiB (J3). | S |
| I7 | **Arranque** (OPT K1–K4): settings + índice (< 20 ms) → ventana con la última vista desde el caché → tras el primer cuadro bandeja, atajos, SMTC, reproductor, LAN → a los 2 s escaneo → a los 30 s actualización, seguidos, herramientas; **`Engine::spawn` deja de correr al iniciar**; `--background` sin ventana ni renderer; nada de COM/monitores/DWM antes del primer cuadro; nada de red al arrancar (I4). | M |
| I8 | **Bandeja liviana**: soltar caché de imágenes y atlas al cerrar la ventana (OPT C7); abrir desde la bandeja < 300 ms (C8); planificador por tiempo (C6) y portapapeles por evento (C5). | S |
| I9 | **Vigilar carpetas** con `notify` (debounce 2 s, ignorar `.part`, solo unidades fijas; OPT E3). Reemplaza la «relectura cada 4 s» de PAR §5.3 sin perder su efecto (lo bajado aparece solo, sin parpadear). | M |
| I10 | **Verificación con la app actual**: comandos `lib_*` en `src-tauri` detrás de `BAKIN_LIB=rust` (PLAN 0.19.1) **solo para correr el smoke completo de hoy (75 checks) sobre el índice nuevo**; no se instala ni se envía así; el flag se borra en β. | S |

**Compuerta α3**: smoke completo en verde con `BAKIN_LIB=rust` y con `BAKIN_LIB=python` (R41: un
solo escritor por archivo en los dos valores); arranque con 3500 < 1 s; «abrir desde la bandeja
→ biblioteca» sin motor y < 300 ms; `tray_music_mb ≤ 20`; C5, C13, C26 → «A (Rust)»; C117.

---

### 5.5 1.0-α4 «Música»

La pantalla más grande. Se hace sobre el núcleo de Música en Rust (RYP §8 paso 3), y se
construye **con la lista de PAR como checklist** (§6 de este archivo).

| # | Ítem | Tamaño |
|---|---|---|
| U1 | **Núcleo de Música en Rust** (`core/music/`), portando **los tests antes que el código** (RYP §10 «Tests»): reproducciones/saltos/estado (`plays.json`, `plays-log.jsonl`, `skips.json`; los `*-pending.jsonl` se suman por última vez), vistas (`top/recent/never/artist/album/playlist/mix/queue/rules/added/radio`, `music_groups`, `music_overview`), playlists normales e inteligentes (reglas, `sort_items`, m3u8), mixes y radio (`StdRng` con hash de la fecha: los mixes del día de la migración cambian una vez, aceptado), estadísticas, duplicados (`unicode-normalization`), limpiar títulos (`regex`), escritura de tags + portada + `tags-backup.jsonl` + deshacer (`lofty`), espejo (plan/apply, manifiesto, hard link, Papelera). MusicBrainz y LRCLIB **siguen en el motor** hasta α7 (el shell lo despierta solo para «Buscar en MusicBrainz»/«Buscar en LRCLIB»). | XL → U1a plays+vistas, U1b playlists+mixes+stats, U1c dupes+tagclean+tagfix, U1d espejo |
| U2 | **Marco de la ventana principal y barra superior** (PAR §1, §2): 1280×840, mínimo 520×560, borde `DWMWA_BORDER_COLOR`, controles de ventana, bordes de resize, arranque frío sin «desarmado» (`show: false`); marca y ☰ (Ctrl B), pestañas `Música`/`Imágenes` + «+» con «Descargas»/«Convertir» (`menuitemcheckbox`, `ui-state.tabs`), búsqueda `Ctrl K`/`Ctrl F` con debounce 250 ms y «pegar un enlace abre el hub», anillo de progreso, «Descargar» (Ctrl D), ⚙, pastilla de actualización; responsive de PAR §27.9 (1100/1000/960/900/760/640/560, mínimo 520). | L |
| U3 | **Fondo de la ventana** (PAR §3): procedural estático (4 luces, grano determinista), redibujado solo por tamaño (350 ms) o portada con «Con el color de la portada»; fundido 0,8 s; **y su versión pre-desenfocada** para el frost (OPT B1). | M |
| U4 | **Barra lateral** (PAR §4): plegada a íconos de 58 px / cajón ≤ 900 px; tarjeta Música con las 11 vistas y contadores, Mixes, Playlists con «+»; Carpetas con «+» y ✕ (diálogo «¿Quitar esta carpeta de la biblioteca?»); el contador de la lista solo cuando la lateral no lo muestra (§4.1). | M |
| U5 | **Lista de temas** (PAR §5, §7.2): `list()` virtual, encabezado con tick «Seleccionar todo» y columnas ordenables en dos sentidos (▲/▼), fila con tick al hover + ♥ + portada + título/artista/álbum/Agregada/duración/formato (`low`/`low-go`/`lossless`), `.playing`, vista compacta, responsive de columnas; estados vacíos **con sus textos exactos** (PAR §5.4) y acciones; búsqueda sin resultado → «Buscar «q» para bajar». | L |
| U6 | **Vistas agrupadas y especiales** (PAR §7.1): Artistas/Álbumes (tarjetas, `back`), Estadísticas (chips de período, tiles, listas, 24 barras, texto «Cuenta desde el…»), Cola (desde el que suena, ✕/Supr, **arrastrar para reordenar**, «Vaciar lo que sigue»), Radio, playlist con sus botones. | L |
| U7 | **Acciones y menús** (PAR §7.3, §7.4, §8): «▶ Reproducir», «Aleatorio ⌄» con los 4 tipos, «Baja calidad», ⋯ (Vista compacta, Limpiar, Duplicados); menú contextual del tema (mismas etiquetas que la barra de selección: PAR §0 «Un nombre por cosa»); barra de selección que reemplaza los filtros, Ctrl A / Supr / Esc, «Seleccionar las {n} cargadas». Menús anclados a un punto de 1 px en el puntero (`anchored` + `deferred`). | M |
| U8 | **Barra del reproductor** (PAR §9, §9.1): tres zonas (dos filas ≤ 900), locate (C29), ⤨ con clic derecho (4 tipos, ✦/◉/●), ⏮/⏭ clic = ±N s / clic derecho = tema, ▶/⏸, 🔁 off/all/one, seek con hover-time y **A–B por clic derecho** (etiqueta «Repetir un tramo»), badge del temporizador, ♥, «Letra», «Cola (Q)», «Temporizador» (diálogo con chips), «Modo mínimo (N)», «Silenciar (M)», volumen; toasts de `notice`/`error` del player con los textos de hoy; `syncKeepAlive`. Atajos de PAR §21 (Espacio, Ctrl ←/→, ←/→, F, M, S, R, Q, L, N). | L |
| U9 | **Panel de letra** (PAR §10): a la derecha, la lista le hace lugar; línea actual centrada; clic = seek; vacíos con sus textos; «Buscar en LRCLIB» y la pregunta de una sola vez «¿Buscar la letra sola cada vez…?» → «Buscar siempre». | M |
| U10 | **Diálogos de Música** (PAR §7.6, §7.7, §5.6): playlist (normal/inteligente con reglas y vista previa en vivo 350 ms), «Agregar a playlist», etiquetar, «¿Eliminar?» (Papelera + «Deshacer» 8 s), «Limpiar títulos y artistas» (con MusicBrainz vía motor), «Duplicados», «Mejorar calidad» (encola vía motor hasta α5), carpeta espejo (menú «Espejo ✓», `ask` de adopción y de «¿Sacar un tema…?»), exportar `.m3u8` (diálogo nativo de GPUI). Todos con el borde superior a la misma altura; Esc cierra; toasts por encima (PAR §0 «Diálogos», «Toasts»). | L |
| U11 | **i18n en Rust**: `core/i18n` con español como clave y la tabla de `ui/i18n/en.js` portada; `tr`/`tn` con `{vars}`; «Automático» sigue a Windows; cambiar idioma redibuja (ya no «recarga la página»); bandeja incluida. Textos **idénticos** a los de hoy (los chequea el smoke). | M |
| U12 | **Pruebas de Música**: capa 1 con `iterations` para C38/C69 (lista virtual, carreras), C6/C7/C35/C54/C112 como estado y acciones (TEST §3.1); los checks de Música (C6–C8, C35, C38, C59, C69, C73, C81, C82, C90, C109, C110, C112, C94, «vistas de música», «reproductor») pasan al cliente `native` con su número; C105 (anchos) y C103 (accesibilidad: reglas sobre `/tree`, Tab, un modal, Esc) nativos para esta pantalla; `scroll_frame_p95_ms` en `/metrics`. | L |

**Compuerta α4**: `test_music_views.py`, `test_mixes.py`, `test_dupes.py`, `test_tagclean.py`,
`test_tagfix.py`, `test_mirror.py` portados y en verde (contados en HANDOFF: los que se van de
Python y los que llegan a Rust); `plays.json` escrito por Rust lo lee Python sin cambios; ventana
Música abierta **≤ 100 MB y visible < 1 s** con los 5200 temas del usuario; C118; **D6: el usuario
usa la pantalla Música una semana con su música real sin volver a la vieja** (con el binario nuevo
como su Bakin de esa semana, respaldo previo del `BakinData`, el viejo cerrado).

---

### 5.6 1.0-α5 «Cola, programador y modo juego» (= 0.19.2)

| # | Ítem | Tamaño |
|---|---|---|
| Q1 | **`core/queue.rs`**: `queue.json` **mismo formato** (`items` serializados como `service._serialize` + `holds` por carril, `QUEUE_LIMIT 2000`); worker en el shell; carriles (Wallhaven y conversiones por delante de la música, PAR §13); `requeue`, `stopped`, `paused`; `library_upgrade_audio`/`_finish_upgrade`/`move_plays` (RYP «Mejorar copias»). El motor pasa a **stateless**: `run_media(item)`, `run_expand`, `run_wallhaven`, `run_convert`, `analyze`, `list_entries` reciben el ítem, emiten progreso, devuelven `outcome` + `stats`. Flag `BAKIN_QUEUE` una versión, **un dueño** del `queue.json` (RYP §7). | XL → Q1a persistencia e ida y vuelta, Q1b worker y carriles, Q1c motor stateless |
| Q2 | **Programador en Rust** (`core/scheduler.rs` sobre el reloj de `scheduler.rs`): horarios, límite diario (`media.json`), pausas 429 (YouTube 6 h, Wallhaven 1 h), «mañana 00:05», seguidos (`follows.json`, historial de 30), revisión diaria de yt-dlp (nunca al arrancar ni en modo juego: OPT F8, I4), medianoche cruzada y pushback de `test_background.py`; `sleep_if_idle`/`resume_due` desaparecen. | L |
| Q3 | **Ciclo de vida del motor**: `engine.rs` arranca por trabajo y cierra al minuto sin pedidos **aunque la ventana esté abierta** (RYP §9); `Consumo` muestra «Catálogo: dormido»; `engine-exit` ya no borra la cola (PAR §28.29). | S |
| Q4 | **Modo juego** (`core/gamemode.rs`; OPT C1, F3, F4, B3, A6, D3): `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT)` + `SHQueryUserNotificationState` (sin inyección ni sondeo de procesos: OPT X19); mientras dura: preajuste **Sobrio**, sin animaciones, mini a 1 Hz y opaco si Z3 lo pidió, motor/yt-dlp/ffmpeg a `BELOW_NORMAL` + `PROCESS_MODE_BACKGROUND_BEGIN`, **Job Object** con `KILL_ON_JOB_CLOSE` y tope de CPU, captura del escritorio apagada, sin chequeos de actualización ni seguidos, buffer de audio 2–3 s, **el audio nunca baja de prioridad** (OPT X5); visible como ícono en bandeja/mini; **D7**: «Límite de velocidad en modo juego» (opción, apagada). | L |
| Q5 | **«Mandar a Bakin» y `bakin://` sobre la cola nueva**: `home.rs::bakinLink` encola en Rust; el aviso «El teléfono mandó un enlace» (previa P4) se emite desde el shell. | S |
| Q6 | **Pruebas**: `test_background.py` portado a `cargo test core::scheduler::*` (persistencia, tic, límite, medianoche, pushback 6 h; R19/R20/R23 a Rust); smoke C12 → **A** con `BAKIN_FAKE_ENGINE=1` (un `run_media` que «baja» un WAV generado en 2 s); una cola guardada por Python la retoma Rust y al revés (fixture). | M |

**Compuerta α5**: todo lo anterior en verde; `tray_downloads_mb ≤ 100` entre trabajos (solo el
shell: RYP §5); C119; C120 (modo juego); **PresentMon con el juego y una descarga corriendo**
(protocolo Z3, lo corre el usuario cuando juegue).

---

### 5.7 1.0-α6 «Descargas, Convertir y el hub»

| # | Ítem | Tamaño |
|---|---|---|
| D1 | **`core/mediadl.rs`** (RYP §4 «yt-dlp, integración», §5): `build_args`, `cookie_args`, `Runner` leyendo `[bakin-p]/[bakin-s]/[bakin-d]`, `friendly_error` con los regexes y **los textos idénticos** (`AUTH/BLOCKED/DISK_FULL/OFFLINE/GONE/COOKIE_DB`), `analyze -J`, `list_ids/list_entries`, `archive.txt`, `scan_existing` con ffprobe, `leftover_videos`; **Job Object** en vez de `taskkill /T` (el árbol muere con Bakin); presets (`media.json` v1, `clean_preset` **sin `..` ni separadores en `template`**, `folder` relativo para importados: SEC §5, §12.5), `classify_media`/`suggest_preset`; varios enlaces sueltos del mismo preset en un solo proceso (OPT F2); `--concurrent-fragments 3` solo video (F5); `PYINSTALLER_RESET_ENVIRONMENT` ya no hace falta. | XL → D1a argumentos + presets, D1b Runner + errores + Job Object, D1c analyze/listas/archive |
| D2 | **Vault DPAPI y Credential Manager en Rust** (`CryptProtectData`, `CredReadW`; mismo blob y target: RYP «Vault», «Credential Manager»): `scratch`/`wipe` con ceros/`sweep` al arrancar, `_vault_leave` (cookies renovadas vuelven si siguen con sesión), `cookies_*` y `cookies-health.json`. **Herramientas** (`tools.rs`): releases oficiales, SHA-256 del mismo release, staging + rename, **tope de 500 MB por miembro al extraer** (SEC §5), `.old`, «desde PATH» visible. | L |
| D3 | **Convertir** (`core/convert.rs`): ffmpeg con `-progress`; imágenes con `image` (WebP con calidad vía ffmpeg: RYP «Convertir»); «Conservar metadatos». **Loudness**: ffmpeg `ebur128` portado; medir el crate `ebur128` en proceso y elegir por CPU (OPT D7). **Mover descargas / deshacer** (`relocate.rs`, SHA-256 entre discos) y **mover la carpeta de datos** (`redirect.txt`). | L |
| D4 | **Pantalla Descargas** (PAR §13): lateral con resumen y «Configurar descargas…»; cabecera «Hoy: {today} de {limit}», «Pausar»/«Reanudar» (oculto sin nada), «Limpiar las listas»; banner `role=status` con el motivo y «Siguen {hoy\|mañana\|jueves} a las {hh:mm}»; «Bajando ahora» con las fases textuales; «Siguiente (n)»; «Con problemas (n)» (no se van solas; «Reintentar» no tras 429/bot); «Listas · hoy (n)» + «Abrir carpeta»; vacío; Seguidos (cada 5 min, «Revisar todos ahora», historial); «Ver el registro» (400 líneas, `aria-live`); avisos de Windows (desde el shell siempre, ya sin página). | L |
| D5 | **El hub «Descargar»** (PAR §12): nav «Buscar música» (catálogo vía `bakin-catalog`, miniaturas en lote **bajadas por el shell con caché en disco** ≤ 4 en paralelo: OPT I2), «Fondos de Wallhaven» (vía motor hasta α7), «Un enlace» (`LINK_MODES`, recientes, `actionsFor` por tipo incluidos «Me gusta» y varios enlaces); diálogo «Descargar música o video» (analiza, «Detectado», preset por sitio, playlist con rangos, tramo, capítulos, subtítulos, formato/calidad/resolución, resumen); «Seguir»; «Desde Spotify» (+ CSV por diálogo nativo); «Mejorar calidad»; presets (`#preset-dialog` completo, importar del script, «Registrar lo que ya tengo»); Herramientas; «Videos que quedaron en MP4». | XL → D5a hub + Un enlace + media dialog, D5b catálogo + Spotify, D5c presets + herramientas + seguir |
| D6 | **Convertir** (PAR §14): lateral con chips Imagen/Audio/Video y formatos con sus pistas; lista con progreso; **drag & drop desde el Explorador** (`IDropTarget` de GPUI con `CF_HDROP`; GPUI «Drag & drop»); «Convertir…» desde la selección. | M |
| D7 | **Enlaces desde afuera** (PAR §24): `bakin://` por `HKCU\Software\Classes\bakin` + `argv` de la segunda instancia (instancia única: mutex + named pipe, patrón de Zed); portapapeles por `AddClipboardFormatListener` (OPT C5; opt-in, un solo enlace ≤ 2048, texto del interruptor «Bakin mira lo que copias solo para ver si es un enlace; no guarda nada»: SEC §2); extensión (diálogo §24.3); **nada se encola sin confirmar** (PAR §28.36). Enlaces arrastrados desde el navegador: `IDropTarget` propio con `CF_UNICODETEXT` o se deja «pegar» (decisión técnica, barata). | M |
| D8 | **Pruebas**: `test_mediadl.py`, `test_cookies.py`, `test_vault.py`, `test_convert.py`, `test_loudness.py`, `test_relocate.py` portados; **test de nombres de archivo hostiles con yt-dlp real** (`..`, `..\..\x`, `CON`, `a:b`; SEC §5); una descarga real en la copia (`BAKIN_HOME`, nunca la cuenta del usuario) termina, se cancela y mata el árbol; «YouTube frenó» pausa 6 h y fuerza la revisión de yt-dlp; `usage` muestra yt-dlp y ffmpeg como hijos; checks de Descargas/Convertir/hub (C4, C12, C14, C33, C40, C42, C54, C64, C90–C93, C97, C106, C108, C111, C113) al cliente `native`; C121. | L |

**Compuerta α6**: lo anterior en verde; el motor **no tiene** `_run_media`/`_run_convert`;
`cookies` nunca en logs ni en `/state` de la sonda (grep del smoke).

---

### 5.8 1.0-α7 «Imágenes, Wallhaven, Configuración y Apariencia»

| # | Ítem | Tamaño |
|---|---|---|
| P1 | **Wallhaven en Rust** (`reqwest` rustls con pool: OPT I1; cadencia 1,5 s, 429 → 1 h, `clean_filters` con sus tests, colecciones, seguir, descarga en streaming a `.part` + rename (I3), tags → `.bakin-meta.json`; miniaturas por el shell con caché en disco (I2). Es la última razón para que Python escriba meta (RYP §7). | L |
| P2 | **LRCLIB y MusicBrainz + CAA en Rust** (1 req/s, 503 → pausa, Retry-After; query Lucene y ranking con tests). | M |
| P3 | **Respaldo / importar en Rust** (`zip`; misma allowlist y topes; merge de favoritas/plays/playlists; la vista previa con nombres y la lista blanca de la previa P5; **round-trip** con un zip exportado por Python). **Logs del shell** con redacción (`tracing` + filtro portado de `redact.py`; nivel por módulo, sin progreso línea a línea: OPT L3). | L |
| P4 | **Pantalla Imágenes** (PAR §4.2, §4.4, §4.5, §5.2, §6.1): scopes Todo/Favoritas/Eliminadas, Colecciones con «+» y ✕, Tags (nube, «Quitar filtro», botón `tags-fold` plegada), filtros Tipo/Fondos (orientación, resolución, **popover de 29 colores**, mismo color lo quita, × sin abrir) y Ordenar; grilla por filas con tiles (badges de tamaño, ♥, «Fondo N», `data-tone`, tick al hover), `L.square` para listas 100 % audio; **tiles de video**: fotograma a 0,5 s por ffmpeg al caché de miniaturas (GPUI no tiene `<video>`). | L |
| P5 | **Visor** (PAR §6.2): miniatura → archivo completo decodificado **reducido a la pantalla** (OPT A2), ♥/ⓘ/✕, ←/→ con carga de páginas, «{n} de {total}», controles que se desvanecen a 2,2 s, panel lateral (meta, «Abrir con…», «Poner de fondo…», «Mostrar en carpeta», «Eliminar», tags), audio pausa el player. **D8: video en el visor**: (a) «Abrir con…» + fotograma (S) o (b) reproducir dentro con Media Foundation a textura (L). Recomendación: (a) para la 1.0, (b) a 1.x. | M (+L si D8 = b) |
| P6 | **Fondos** (PAR §6.3, §6.4; `wallpaper.rs` ya es Rust): menú «Poner de fondo» construido en cada apertura (monitores consultados, R28), «Todos los monitores», «Ajustar encuadre» (`role=slider`, flechas, «Centrar»), «Pantalla de bloqueo» con rechazo recordado; **rutas solo dentro de la biblioteca** (SEC §8); fondo por monitor en JPEG q95 salvo bordes duros (OPT H3). Configuración › Fondos de pantalla (rotación por monitor, fuente, cadencia, orden, orientación, día/noche, «Que siga la portada», «Siguiente», «Pausar la rotación»). | M |
| P7 | **Configuración entera** (PAR §15): General (idioma, segundo plano, avisos, **Iniciar con Windows** vía `auto-launch`, extensión, portapapeles, copia de seguridad, datos de Bakin + «Mover…» + R30), Música (⏮⏭, fundido, volumen parejo con estado, EQ de 10 bandas dentro de su panel, salida, letras, MusicBrainz, modo mínimo), Atajos (grabación con `global-hotkey` pausado, «Sin atajo», conflictos, listas estáticas), Descargas (carpeta + «¿Mover tus descargas?», preset, límite diario, presets, herramientas, salud de cookies con la línea ámbar, tope horario, horario), Cuentas (cookies: estados y acciones; Wallhaven: usuario, clave que **se vacía siempre**), En tu casa (QR, código, «Cambiar el código», estado; el evento `lan` con error **ahora sí se muestra**, PAR §28.30), Consumo (+ % GPU y cuadros por minuto: OPT N5). Abre en la sección pedida y la enfoca. | XL → P7a General+Música+Atajos, P7b Descargas+Cuentas+En tu casa+Consumo |
| P8 | **Apariencia** (PLAN H.1, decisión 3 de §1): preajustes **Sobrio** (opaca, sin DWM, «el más liviano»), **Vidrio** (desenfoque de Windows `Blurred`/Mica), **Cristal** (+ refractar el escritorio), y **«Personalizado»** que abre: **Ventana** Opaca / Transparente / Desenfocada y cuánto (0–100 %); **Vidrio** niveles de glass-midas (sólido / esmerilado / refracción) e intensidad (`blur` sm–lg, `thickness` thin–thick, `tint` claro/oscuro, `saturation`); «Con el color de la portada» sigue. Preajuste por defecto = **D1**. En modo juego, siempre Sobrio (OPT B3). Opaca ⇒ `DISABLE_DIRECT_COMPOSITION` y «cambiar pide reabrir la ventana» (B5). Parámetros en un constant buffer, nunca compilar nada al mover un control (B6). Vidrio dinámico solo donde se mueve algo debajo (B2). | L |
| P9 | **«Refractar el escritorio»** (REF; OPT B4; SEC §11 como **contrato con test**): interruptor apagado por defecto con el texto «Bakin mira lo que hay detrás de su ventana para curvarlo»; DXGI Desktop Duplication en la capa (c) de Z2; solo cuando lo de atrás cambia (rectángulos sucios, eco descartado), espejo en GPU, media resolución, 30 fps (60 arrastrando), una captura para ventana y mini; se suelta oculta/minimizada/bandeja/tapada (`DWMWA_CLOAKED`)/bloqueo/UAC (`DXGI_ERROR_ACCESS_LOST` → reintento a los 2 s, no en bucle); no capturar con app a pantalla completa adelante (`SHQueryUserNotificationState`); RDP → sin refracción sin error; **la textura no se lee nunca a CPU** (test en CI: ningún `D3D11_USAGE_STAGING` en el módulo); **el smoke corre con el interruptor apagado**; `WDA_EXCLUDEFROMCAPTURE` solo en la capa de fondo (la interfaz se ve en OBS/Discord); se apaga solo en actualización mayor; párrafo en SECURITY.md. **D9**: si Z2 no dio «cero quieto, barato con juego», esto va a 1.0.x. | L |
| P10 | **Novedades, actualización, primer arranque, extensión** (PAR §19, §24.3): `#news-dialog` desde `changelog.json`, pastilla y diálogo de actualización (nunca sola, bloqueada con descarga en curso), `#welcome-dialog` en tres pasos **+ el aviso de privacidad del primer arranque** (PLAN 0.22.1, SEC §12.4: la tabla de qué sale de la PC), extensión. | M |
| P11 | **Pruebas**: `test_wallhaven.py`, `test_lyrics.py`, `test_musicbrainz.py`, `test_backup.py` portados; round-trip de respaldo Python↔Rust; checks de Imágenes/fondos/Configuración (C13, C21, C22, C27, C34, C44–C48, C57, C62, C65, C66, C72, C79, C83, C95, C100–C105, C111, C112) al cliente `native`; C122, C123, C124; R38. | L |

**Compuerta α7**: lo anterior en verde; el motor es solo `catalog_*` + `spotify_*` +
`read_text_file`; instalador medido; paridad §6 completa para Imágenes y Configuración.

---

### 5.9 1.0-β «Cierre»

| # | Ítem | Tamaño |
|---|---|---|
| B1 | **Chrome y ciclo de vida sin Tauri** (PAR §1): cerrar con trabajo y «Seguir con la ventana cerrada» apagado → «Hay una descarga en curso»; con KeepAlive o rotación: la ventana se oculta (sin WebView no hace falta destruirla a los 20 s; se sueltan caché y atlas: I8); reapertura por bandeja/segunda instancia/enlace/atajo con `bring_to_front()`; `--background`; «Salir de Bakin» guarda y cierra el motor; `app_restart`. | M |
| B2 | **Integración de sistema sin Tauri** (OPT decisión 3; GPUI «Integración con el sistema»): `tray-icon`+`muda` (α2), `global-hotkey` (sin `tauri-plugin-global-shortcut`; copias de prueba nunca registran), `auto-launch`, notificaciones con AUMID (`gpui-pre`) o `tauri-winrt-notification` suelto, instancia única (α6), `bakin://` (α6), **updater propio**: `latest.json` + `minisign-verify` + NSIS `/S`, «bajar en segundo plano, instalar al cerrar» (OPT M2), nunca con descarga en curso, misma clave pública; SMTC igual. Sin tao/wry/webview2-com: **desaparece el runtime WebView2 del instalador**. | L |
| B3 | **Borrar lo viejo**: `desktop/src-tauri` (menos lo que migró a `core`/`app`), `desktop/ui/*.html/js/css` (el `vendor/glass-midas` queda como fuente de tokens hasta que D13 lo reemplace), cliente `web` del smoke, `axe-core`, `WEBVIEW2_USER_DATA_FOLDER`, flags `BAKIN_LIB`/`BAKIN_QUEUE`; `window_open_mb` deja de ser `warn_only`; `docs/QA.md` sin ningún «→ A» que dependa de WebView; H.4 revisado (lo que ya no existe se quita, lo que sigue se testea). | M |
| B4 | **Motor `bakin-catalog`** (RYP §9): venv limpia y reproducible (OPT G2), sin Pillow/mutagen/`cryptography`, PyInstaller onedir con el bootloader propio, `METHODS` reducido, `catalog_hourly`/`catalog_covers` por parámetro, `migrate.run()` fuera; vuelve a **MIT**; `packaging/notices.py` simplificado; `THIRD-PARTY-NOTICES.md` con Apache-2.0 de gpui/gpui-kit. | M |
| B5 | **Seguridad con la 1.0** (SEC §12 «Con la 1.0»): contrato de captura (P9) verificado; **panic aislado en el decodificador** (R39: hilo con `catch_unwind` y `panic = "unwind"` o «saltar tema»); `resolve`/`is_inside`/`check_new_folder`/allowlists portados con tests; `cargo deny`; **D11 SignPath** (Authenticode: con captura a bordo la firma deja de ser reputación); **D12 clave del updater** (H.5: dónde está la copia, plan de rotación «un release firmado con la vieja que trae la nueva»); `pre-push` que impida empujar `bak` a un remoto que no sea el privado (SEC §10). | M |
| B6 | **Accesibilidad** (GPUI «Lectores de pantalla»; TEST §3.2): **D10**: AccessKit de `gpui-pre` si no paniquea con Narrador/NVDA, si no `Application::inaccessible()` y «límite conocido» en Novedades; reglas propias sobre `/tree` (nombres, Tab, un modal, Esc, contraste, reduce-motion) como C103 nativo en todas las pantallas; pasada manual con Accessibility Insights FastPass; teclado completo (PAR §21, §27.1). C125. | M |
| B7 | **H.3 prueba de actualización** (`packaging/update-test.ps1`: instala N−1 en copia, `latest.json` local, actualiza a N, cola y reproductor siguen; C19, R25 → A; con `-WithUpdateTest`). **H.6**: Python 3.14 + bootloader; `gpui-pre` se sube **a propósito** cada tanto con el OK, nunca `git = main`. | M |
| B8 | **Presupuestos en verde en release** (§2 + PLAN §5): `cargo bloat --release` una vez (OPT M3); instalador ≤ 40 MB; Defender limpio; CI del repo público mide cada release con la biblioteca generada y publica la tabla (OPT M5). | S |
| B9 | **Lo P2/P3 de OPT que entre en el tiempo** (no bloquean): `tokio current_thread` para el servidor (J4), JSON compacto (L2), respaldo en streaming (L4), eventos del motor en lotes (G6), prefetch del catálogo. | S cada uno |
| B10 | **Novedades 1.0**: `CHANGELOG.md` › `## 1.0.0 — <fecha>` desde NOV + lo que trae la interfaz nativa (una línea por cosa, «tú», sin jerga): «Bakin es ahora una app nativa: abre en menos de un segundo y usa menos de 100 MB», «Apariencia: Sobrio, Vidrio o Cristal, y «Personalizado»», «Refractar el escritorio (opcional)», «Modo juego automático», «Modo mínimo que pesa menos de 50 MB», «Ya no necesita WebView2», «Límites conocidos» (video en el visor según D8; lectores de pantalla según D10). Versión 1.0.0 en los lugares que queden (`bakin/__init__.py`, `pyproject.toml`, `desktop/app/Cargo.toml`, `desktop/core`, `desktop/ui`; el test de «versión en 5 lugares» se actualiza). | S |
| B11 | **Docs al día**: `README.md`, `SECURITY.md` (párrafo de captura), `docs/ARCHITECTURE-BACKGROUND.md`, `docs/DESIGN.md` (GPUI y Apariencia), `docs/QA.md` (columna «nativo»), `docs/PLAN.md`, `docs/HANDOFF.md` › Cómo seguir (release sin Tauri), `export-public.ps1` (excluye `docs/research/`). | S |
| B12 | **CI**: `cargo test -p bakin-ui --features test-support` en el job `test`; job `smoke-native` experimental con `continue-on-error` (WARP; `GPUI_DISABLE_DIRECT_COMPOSITION=1` de fallback; sube `diff.png` y `/tree`) (TEST §6); `cargo audit`/`pip-audit`; acciones por SHA (A10). | S |

**Compuerta β**: todos los presupuestos de §2 y PLAN §5 en verde en release; smoke nativo
completo (los 75 checks de hoy + los nuevos de §12) en verde en la PC del usuario; pytest del
`bakin-catalog` y `cargo test` del workspace en verde; checklist de paridad (§6) sin casillas
vacías o con decisión escrita; `docs/QA.md` sin «→ A» evitables; Defender limpio; instalador
firmado (minisign y, si D11 = sí, Authenticode).

---

### 5.10 1.0.0 «Publicación»

1. **D14: OK explícito del usuario** para publicar y para pushear al repo público.
2. `release.ps1 -Publish` desde **pwsh** (HANDOFF › Cómo seguir, actualizado en B11); release en
   borrador → publicación a mano; `latest.json`; página de descarga (`bakin-releases`) sin tocar.
3. `export-public.ps1` → clon `%TEMP%\bakin-public` → push con el OK.
4. HANDOFF: «1.0.0 publicada»; la prueba a mano del usuario viene **después** (decisión 2); lo
   que encuentre va a `docs/PLAN.md` §7 y sale en 1.0.x.

---

## 6. Paridad por pantalla (la checklist)

Regla: **cuando una pantalla pasa a GPUI, sus checks pasan al cliente `native` el mismo día, con
el mismo nombre y número**, y los checks web de esa pantalla se borran (TEST §5). Cada fila se
tilda cuando la sección de PAR está entera en la pantalla nueva (los textos, los estados
recordados 💾, las confirmaciones, el deshacer, las preguntas de una sola vez, lo angosto).

| Pantalla / área | Secciones de PAR | Fase | Checks que migran | Nuevo |
|---|---|---|---|---|
| Modo mínimo | §11, §21 «Mini», §28.23–24 | α2 | C36, C41, C60, C61, C67, C68, C77, C107 | C116 |
| Bandeja | §23, §1 «Reapertura», «Salida» | α2/β | C9, C10, C43 (sin motor), R1, R13, R14 | — |
| Ventana y chrome | §1, §27.9 (anchos), §27.8 (vidrio) | α4 (U2), β (B1) | C34, C39, C74, C76, C78, C105 | C124 |
| Barra superior | §2 | α4 (U2) | C33, C74, C92, C111 | — |
| Fondo de la ventana | §3 | α4 (U3) | C37 | — |
| Barra lateral | §4 | α4 (U4), α7 (P4) | C31, C32, C76 | — |
| Lista y estados vacíos | §5.1–§5.4, §28.4–5 | α4 (U5) | C5, C21, C38, C69, C81, C82, C112 | C117 |
| Eliminadas | §5.5 | α3 (I4) | C26, C111 | — |
| Acciones de ítem | §5.6, §28.6 | α4 (U10) | C13 | — |
| Música: vistas | §7.1 | α4 (U6) | C8, C28, C86, C87, C88, C99 | C118 |
| Música: lista | §7.2 | α4 (U5) | C73, C109, C110, C111 | — |
| Música: acciones y menús | §7.3, §7.4, §7.5 | α4 (U7) | C6, C35, C111 | — |
| Playlists | §7.6 | α4 (U10) | C15, C16, C98, R24 | — |
| Arreglar la biblioteca | §7.7 | α4 (U10) | C84, C85, C89 | — |
| Selección | §8, §28.13 | α4 (U7) | C30, C58, C110 | — |
| Reproductor | §9, §9.1, §28.1–2 | α4 (U8) | C17, C20, C23, C24, C29, C59, C94, R26 | — |
| Letra | §10 | α4 (U9) | C94, C111 | — |
| Descargar: hub | §12, §12.1–§12.6, §28.17–19 | α6 (D5) | C4, C33, C40, C42, C47, C49, C52, C53, C64, C90, C91, C97, C113 | C121 |
| Descargas | §13, §28.28–29 | α6 (D4) | C12, C51, C54, C92, C106, C108, C112 | C119 |
| Convertir | §14, §22 | α6 (D6) | C14 | — |
| Imágenes: tiles y filtros | §4.2, §4.4, §4.5, §5.2, §6.1, §6.5, §28.3, §28.14 | α7 (P4) | C21, C25, C46, C57, C66 | C122 |
| Visor | §6.2 | α7 (P5) | C22 | — |
| Fondos | §6.3, §6.4, §28.15 | α7 (P6) | C44, C45, C48, C55, C62, R28 | — |
| Configuración | §15 (todas las secciones) | α7 (P7, P8) | C3, C27, C63, C72, C79, C83, C93, C95, C100, C101, C104, C111, C112, R30, R31, R32 | C123 |
| Apariencia | PLAN H.1 | α7 (P8, P9) | — | C123, C124 (refracción), R38 |
| Diálogos y menús | §16, §17, §0 «Diálogos» | cada pantalla | C56, C80, C111 | — |
| Toasts | §18, §28.9–10 | cada pantalla | C53, C80, C92 | — |
| Novedades, actualización, primer arranque | §19 | α7 (P10), β (B7) | C19, C102, R25 | — |
| Eventos del motor | §20 | α5/α6 | C106 | — |
| Atajos | §21, §15 «Atajos» | α4, α7 | C63, C75 | — |
| Enlaces desde afuera | §24 | α6 (D7) | R15, C64 | — |
| Página del teléfono y servidor | §25 | sin cambios (α3 I6, α5 Q5) | C100, C101, C114, R31 | — |
| SMTC | §26 | sin cambios | R5 | — |
| Transversales | §27.1–§27.7 | β (B6) | C103, C104, C105 | C125 |
| Estado persistido | §27.10 | α1 (A12) | C20, C111 | — |
| «Fácil de olvidar» | §28 (40 puntos) | β: se repasa uno por uno antes de cerrar | — | — |

Dos huecos que PAR no cubre y GPUI no trae: **video** (`<video>` en tiles y visor → P4/P5 y
**D8**) y **texto estático seleccionable** (no hace falta salvo en campos: GPUI «Selección»).

---

## 7. Seguridad: dónde cae cada arreglo

| Prioridad (SEC §12) | Arreglo | Fase / ítem |
|---|---|---|
| 1 (media) | Código LAN de 12; aviso «El teléfono mandó un enlace» (✅ previa P4, C114); `s` nuevo por request en la PWA; texto «red propia; cualquiera con la Wi-Fi y el código» | pendiente: α7 P7 «En tu casa»; TLS con huella en el QR → 1.x |
| 2 (media) | Respaldo: vista previa con nombres, ajustes nunca importados, presets con cookies apagadas, plantilla sin salir de la carpeta (✅ previa P5, C114) | se porta tal cual en α7 P3 |
| 3 (media) | Hashes de Python, `pip-audit`/`cargo audit`/`cargo deny`, acciones por SHA, required reviewers, clave del updater documentada y rotación | α1 A10; β B5, B7 (D12) |
| 4 (media) | SECURITY.md al día + aviso de privacidad del primer arranque | previa P7; α7 P10 |
| 5 (baja) | Redactor con cookies de Google y código LAN; `redact` en `media_raw`/`error`; `clean_preset` sin `..`; test de nombres hostiles; tope al extraer ffmpeg; `wallpaper_set` solo biblioteca | previa P6; α6 D1, D2, D8; α7 P6 |
| 6 (con 1.0) | Captura del escritorio como contrato con test | α7 P9 |
| 7 (con 1.0) | SignPath | β B5 (D11) |
| 8 (con 1.0) | Invariantes de rutas portadas con tests; panic aislado del decodificador; `cargo deny` antes de gpui | α3 I1, α6 D1, β B5; A10 |
| 9 (con 1.0) | Asset scope y `img-src` | **se cae solo** sin WebView (SEC §12.9) |
| 10–14 (después) | TLS LAN, ACL al mover datos, UA de Spotify, Dependabot `glib`, `pre-push` | 1.x salvo `pre-push` (β B5, cuesta nada) |

Lo que **se conserva tal cual** (SEC §13): lista blanca de métodos del motor (ahora la llama el
shell directo), `bakin://` y portapapeles con confirmación, servidor solo en IPs privadas con
comparación constante y rate limit, bóveda DPAPI con copia efímera + wipe + sweep, redactor,
allowlist del respaldo, Papelera obligatoria, tags con backup y deshacer, updater firmado sin
instalación automática, `Cargo.lock` + `--locked`, bootloader propio con hash, release en
borrador, export público que falla si se filtra una ruta real o el e-mail.

---

## 8. Pruebas: las tres capas y las reglas

* **Capa 1** (`cargo test -p bakin-ui`, `#[gpui::test]`, `TestAppContext`/`VisualTestContext`;
  TEST §3.1): estado y transiciones, acciones y atajos, foco y Tab, `simulate_resize` a
  1280/980/720 con `debug_bounds` (nada fuera del viewport, ≥ 24 px), diálogos, determinismo con
  `iterations = 50` donde hay carreras. Cada pantalla recibe un `trait EngineClient` (`FakeEngine`
  con los JSON de `BAKIN_FAKE_*`) y un reloj inyectado. **No** prueba texto cortado (fuentes Noop),
  colores, vidrio ni rendimiento.
* **Capa 2** (la sonda, A5; TEST §3.2): el `ui-smoke.mjs` de hoy con cliente `native`; mismos
  nombres y números; «sin errores de JavaScript» → «sin pánicos ni errores» (`problems[]` del panic
  hook); presupuestos con el mismo PowerShell + `/metrics`; **C103 sin axe**: reglas propias
  (nombres, Tab, un modal, Esc, contraste por tokens, reduce-motion); **C105**: ningún nodo con
  `bounds.right > viewport.width` fuera de un scroll, `clipped` por `shape_line`, `min(w,h) < 24`.
* **Capa 3** (TEST §3.3): `/screenshot` por `PrintWindow`; datos de muestra, `BAKIN_FAKE_CLOCK`,
  `BAKIN_NO_ANIM=1`, 100 %, fuente embebida, vidrio sólido; ΔE ≤ 16 y ≤ 0,5 % de píxeles; pocas
  (mini ×3, barra del reproductor, un diálogo); `warn_only` en CI con WARP.
* **Reglas**: nada toca mouse/teclado/foco del usuario (A6, R40); `PostMessage` solo para
  hit-test, drags en capa 1; la sonda nunca en release (A5); capturas solo con imágenes generadas;
  ningún smoke ni ventana mientras el usuario juega; medir en release.
* **CI** (TEST §6): capa 1 en el job `test`; `smoke-native` experimental; el smoke completo sigue
  siendo local en `release.ps1`.

---

## 9. Mejor NO hacerlo (no-objetivos de la 1.0)

De OPT «Parte 2» (X1–X30), de GPUI, REF y SEC, y de las decisiones del usuario:

| # | La tentación | Por qué no | Alternativa |
|---|---|---|---|
| X1 | Enganchar DWM (OpenFluidGlass) | API no documentada, GPL, se rompe en silencio | B1/B3, capa (c) |
| X2 | Shaders compilados en tiempo de ejecución por combinación de controles | trabas, dxc/fxc, caché | constant buffer (B6) |
| X3 | Fork de gpui «para tenerlo bajo control» (grande **o chico**, en la 1.0) | deuda permanente; cada rebase cuesta días | `gpui-pre` fijado; `[patch]` listo pero vacío; contribuir upstream en 1.x si hace falta |
| X4 | Refractar el escritorio prendido por defecto, 60 fps, resolución completa, o WGC sin borde | GPU continua; consentimiento MSIX; privacidad | P9 |
| X5 | Bajar la prioridad de todo Bakin | el audio se corta | F3 solo hijos; audio alto |
| X6 | `EmptyWorkingSet` | cosmético | achicar de verdad (A1, A2) |
| X7 | WASAPI exclusivo / bit-perfect | roba el dispositivo a Discord y al juego | D1–D3 |
| X8 | Anillo de audio de pocos ms | más cortes | nada |
| X9 | Pre-generar miniaturas o escanear con todos los núcleos al arrancar | CPU a fondo jugando | a demanda, 2 hilos |
| X10 | Recodificar o convertir la biblioteca a WebP/AVIF | toca archivos del usuario | miniaturas propias |
| X11 | yt-dlp como módulo dentro del motor | se rompe cada semana | proceso propio (D1) |
| X12 | Acelerar YouTube | 429, «no eres un bot» | regla de la casa |
| X13 | Prefetch de Wallhaven / caché agresiva | Cloudflare | I1 + I2 |
| X14 | Transcodificar para el celular | CPU jugando | J1–J3 |
| X15 | PyInstaller onefile, UPX | 3–4 s en frío; falsos positivos | onedir |
| X16 | MSIX | cambia instalación, firma, sandbox | Consumo |
| X17 | Actualizaciones diferenciales | infra para ~10 MB | instalador chico |
| X18 | `target-cpu=native`, PGO | se cae en otra PC | A10 |
| X19 | Detectar juegos sondeando procesos o con inyección | anticheat | C1 |
| X20 | Motor siempre despierto | 28–50 MB para siempre | F1 + bakin-catalog |
| X21 | Precargar portadas de toda la cola | memoria invisible | LRU + prefetch |
| X22 | Todo el shell async con tokio | el reproductor es un hilo dedicado | dejar los hilos |
| X23 | Motor de texto propio | se pierden ClearType y fallback | DirectWrite |
| X24 | Atlas de portadas en disco (DDS) | prematuro | — |
| X25 | SQLite para settings/hotkeys/mini | archivos de 100 bytes | JSON |
| X26 | Otro motor web para YouTube Music/Wallhaven | decenas de MB | I2 |
| X27 | Flags de WebView2 durante la transición | la WebView se va igual | nada |
| X28 | Mini dibujado por CPU para bajar a 10 MB | dos UIs | C2 |
| X29 | `notify` sobre red o USB | poco fiable | solo unidades fijas |
| X30 | Apagar el hilo VSync o parchear `Present` | rompe el ritmo | A5 |
| X31 | WebView2 como HWND hijo dentro de GPUI («pantalla a pantalla en una sola ventana») | apaga DComp, tapa todo, foco e IME a mano (GPUI «Convivir con Tauri») | binario nuevo, pantalla a pantalla como orden de trabajo |
| X32 | Tauri + GPUI en el mismo proceso | dos loops, dos manifiestos, dos `windows` | binario nuevo |
| X33 | Enviar un híbrido o publicar la 0.14–0.24 | decisión del usuario | 1.0 entera |
| X34 | Cliente InnerTube propio en Rust o `rustypipe` (GPL-3) dentro del shell | frágil / licencia | `bakin-catalog` en Python (D15 al final, si hace falta) |
| X35 | Temas de terceros con shaders | ejecución en GPU con datos ajenos (SEC §11) | tokens y preajustes propios |
| X36 | Linux/macOS, modelos locales (fase G), app nativa de celular, TLS LAN, gestor de descargas general | 1.x | — |

---

## 10. Riesgos y mitigaciones

| Riesgo | Qué puede pasar | Mitigación |
|---|---|---|
| **Shaders propios no existen en GPUI** (GPUI «Bloqueantes» 1) | el vidrio con refracción no se ve como el de hoy | Z2 lo decide antes de escribir una línea de producto; si (b) se ve pobre, H.1 se achica a acrílico + frost y se dice |
| **Memoria con imágenes** (GPUI «Bloqueantes» 2) | una grilla de wallpapers rompe los 100 MB | LRU + miniaturas al tamaño (A7), Z6 lo mide, `window_open_mb` duro |
| **`gpui-pre` es de un tercero** y cambia cada ~2 semanas (GPUI «Estabilidad»); RYP §2 desaconsejaba snapshots de terceros | API rota al subir; proyecto abandonado | fijado con `=`; subir a propósito con el OK (B7); `[patch.crates-io]` listo; si longbridge deja de publicar, `gpui 0.2.2`/upstream con lo que se pierde anotado |
| **Acrílico de DWM en Win10** (arrastre trabado, blur que no aplica, texto borroso) | Vidrio se ve mal en algunas PCs | Z2 en la PC del usuario; R43: fallback automático a Sobrio si `SetWindowCompositionAttribute` falla; Sobrio siempre disponible |
| **Click-through sobre DComp** no documentado | el modo gaming del mini no deja pasar clics | Z4 (subclass → `WS_EX_TRANSPARENT` → ventana aparte) |
| **Sin GPU / RDP / VM** | la app no abre | Z8 + R36: aviso y requisito documentado como Zed |
| **Accesibilidad** joven o apagada | regresión frente a WebView2 | D10 + reglas propias + Narrador a mano + límite conocido en Novedades |
| **Paridad de datos** (ids, tags, mixes) | biblioteca «distinta» al cambiar | tests cruzados; script de solo lectura sobre la biblioteca real (A3); un dueño por archivo (R41); respaldo antes de D6 |
| **Dos escritores** durante un flag | un lado pisa al otro | regla 2 de §0; smoke con el flag en los dos valores (R41) |
| **520 tests de Python** que se mudan | agujeros al portar | portar los tests **antes** que el código; contarlos en HANDOFF por fase |
| **yt-dlp**: árbol huérfano, cookies en claro, errores no reconocidos | — | Job Object; wipe + sweep; fixtures reales de salida; test de nombres hostiles |
| **`panic = "abort"`** con archivo malformado | un tema tira toda la app con el servidor y la cola | R39 en β B5 (decidir unwind en hilo o «saltar tema» antes de β) |
| **Captura del escritorio** sin indicador | privacidad; anticheats | contrato con test (P9), apagado por defecto, se apaga en actualización mayor, SECURITY.md |
| **Tamaño y arranque del shell** con `lofty`, `reqwest`, `zip`, `image` | exe grande | perfil por paquete (A1), `cargo bloat` (B8), `rusqlite` solo si D5 lo pide |
| **Defender** con un binario Rust nuevo con red y procesos hijos | falso positivo | `MpCmdRun` en `release.ps1`; SignPath (D11) |
| **Tiempo**: el PLAN lo llamaba L+ (meses) | se demora | fases con entregas que ya sirven (mini liviano, biblioteca sin motor, bandeja sin motor); nada se publica a medias; cada compuerta puede achicar (refracción, video, a11y) sin frenar |
| **Tests que «pasan» con texto cortado** (NoopTextSystem) | — | `clipped` solo en capa 2; anchos en las dos |
| **Goldens frágiles** | ruido | pocos, vidrio sólido, tolerancia, `warn_only` en CI |
| **La sonda se cuela en release** | superficie de ataque local | feature no default + `cfg` + verificación del exe + test del instalador |
| **`activate_window` roba el foco jugando** (TEST §8) | Alt inyectado en el juego | `bring_to_front()` único con guardia (A6, R40) |
| **El usuario pierde preferencias del `localStorage`** al pasar a la 1.0 | molestia | A12: exportar desde la app actual si hay tiempo; si no, decirlo en Novedades |

---

## 11. Lo que decide el usuario, y cuándo

| # | Decisión | Cuándo | Con qué en la mano |
|---|---|---|---|
| **D1** | **Preajuste por defecto: Sobrio (opaco) o Vidrio (desenfocado por Windows)** | al cerrar α0 | Z2 (GPU/CPU por modo) + Z3 (PresentMon con su juego) + capturas comparadas |
| D2 | Aceptar `gpui-pre` fijado (o volver a `gpui 0.2.2`) y si la capa (c) sigue en la 1.0 | al cerrar α0 | Z1, Z2, Z9 |
| D3 | Qué juego y cuándo corre el protocolo PresentMon (lo corre él) | α0, de nuevo en α5 y β | protocolo escrito (R45) |
| D4 | Aprobar el mini nativo a la vista | al cerrar α2 | la copia aparte con sonido |
| D5 | (técnica, se informa) SQLite o caché binaria para el índice | α3 I1 | medición con 3500 |
| **D6** | **Usar la pantalla Música una semana con su música real** y aprobarla | al cerrar α4 | binario nuevo como su Bakin de esa semana, respaldo previo |
| D7 | Si existe «Límite de velocidad en modo juego» (opción apagada) | α5 Q4 | — |
| **D8** | **Video en el visor**: «Abrir con…» + fotograma (1.0) o reproducir dentro con Media Foundation (L) | α7 P5 | costo |
| **D9** | **«Refractar el escritorio»** entra en la 1.0 o cae a 1.0.x | α7 P9, según Z2 | GPU/CPU quieto y con juego |
| D10 | Accesibilidad: AccessKit (si no paniquea) o `inaccessible()` + límite conocido | β B6 | prueba con Narrador/NVDA |
| D11 | **SignPath** (Authenticode) con la 1.0: pedir la cuenta OSS (proyecto público) | empezar en α6 (tarda); cierra en β | SEC §12.7, PLAN §6.11 |
| D12 | Dónde está la copia de la clave del updater y si se planea rotación | β B5 | H.5 |
| D13 | Dónde vive `glass-midas-gpui` (crate en el repo de glass-midas, consumido por tag de git fijado; publicado a crates.io con el OK) | α1 A4 | memoria «glass-midas» |
| **D14** | **OK de publicar la 1.0.0 y de pushear al repo público** | 1.0.0 | compuerta β cerrada |
| D15 | Si `ytmusicapi` muere: sidecar Rust `rustypipe` (GPL-3, proceso aparte) o quitar el catálogo | solo si pasa | RYP §9 |
| D16 | Textos finales de Apariencia (nombres «Sobrio / Vidrio / Cristal / Personalizado» y el del interruptor de captura) | α7 P8 | capturas |

---

## 12. Números de QA reservados

Continúan después de los que ya tomó la previa: **C114** (seguridad barata: código LAN de 12,
aviso del enlace del celular, vista previa del respaldo + ajustes nunca importados + presets con
cookies apagadas, plantilla que no sale de la carpeta, cookies de Google redactadas, SECURITY.md)
y **C115** (anillo de audio lock-free + prioridad del decodificador + cola escrita solo al cambiar
en otro hilo + test de voseo). Este plan empieza en **C116** y **R36**. Cada fila se agrega a
`docs/QA.md` cuando su ítem se hace, en inglés como las demás.

| # | Fase | Qué | Cómo |
|---|---|---|---|
| C116 | α2 | Modo mínimo nativo: bar/card/info, ⋯ con sus ítems y orden, siempre encima, modo gaming con la pregunta de una vez y click-through, rueda = volumen, posición recordada, letras en tarjeta; sin `msedgewebview2`; ≤ 45 MB, < 250 ms, 0 % CPU | capa 1 + sonda + 3 goldens |
| C117 | α3 | Biblioteca en Rust: abre sin motor, incremental, Papelera/restaurar (`BAKIN_FAKE_BIN`), 3500 < 1 s, abrir desde la bandeja < 300 ms, `tray_music_mb ≤ 20` | cargo test `core::index::*` + smoke con `BAKIN_LIB=rust` |
| C118 | α4 | Música nativa: lista de 5000 a p95 ≤ 8 ms, columnas, selección, menús, reproductor, cola arrastrable, letra, playlists, arreglar; ≤ 100 MB y < 1 s con 5200 temas | capa 1 (`iterations`) + sonda + `/metrics` |
| C119 | α5 | Cola y programador en Rust: `queue.json` de Python la retoma Rust y al revés; motor stateless por trabajo; bandeja con descargas ≤ 100 MB entre trabajos; horarios, límite, 429, seguidos | cargo test `core::scheduler::*` + smoke C12 con `BAKIN_FAKE_ENGINE=1` |
| C120 | α5 | Modo juego automático: con un juego al frente Bakin pasa a Sobrio, sin animaciones, mini a 1 Hz, hijos a prioridad baja, captura apagada, sin chequeos; ícono en bandeja; vuelve al salir | cargo test (`gamemode` con foreground simulado) + sonda (`/state.game_mode`) + M (PresentMon) |
| C121 | α6 | Descargas, Convertir y el hub nativos; yt-dlp en Job Object (cancelar mata el árbol, también si Bakin muere); nombres de archivo hostiles no salen de la carpeta | cargo test + descarga real en la copia + smoke |
| C122 | α7 | Imágenes nativa: grilla, filtros (29 colores), visor, menú de fondo por monitor, encuadre, «Fondo N», tiles de video con fotograma | capa 1 + sonda (`BAKIN_DRY_WALLPAPER=1`) |
| C123 | α7 | Configuración nativa entera + Apariencia: Sobrio/Vidrio/Cristal + «Personalizado», cambio en caliente (opaca pide reabrir), modo juego fuerza Sobrio; `lan` con error se muestra | capa 1 + sonda («cada ajuste en un solo lugar») |
| C124 | α7 | Refractar el escritorio: apagado por defecto, texto exacto, se suelta oculta/bandeja/bloqueo/UAC/tapada, nunca a CPU (CI), smoke con el interruptor apagado, la capa de fondo excluida de la captura y la interfaz visible en OBS | cargo test + grep en CI + M |
| C125 | β | Accesibilidad nativa: Tab recorre todo lo visible, cada interactivo con nombre, un modal a la vez y Esc, reduce-motion sin deslizamientos; Narrador lee (si D10 = AccessKit) | sonda (reglas sobre `/tree`) + M (Narrador, FastPass) |
| C126 | β | Arranque de la 1.0: `first_frame_ms ≤ 500`, ≤ 100 MB, sin red ni motor al arrancar, `--background` sin ventana ni renderer; `ui-state.json` con la última vista | `measure.json` + smoke |
| C127 | β | Sistema sin Tauri: bandeja, instancia única (segunda instancia abre y entrega `bakin://`), atajos globales (nunca en copias), autoarranque, notificación con AUMID, updater propio (descarga en segundo plano, instala al cerrar, nunca con descarga en curso) | cargo test + smoke + H.3 |
| R36 | α0/β | Sin GPU (adaptador básico/WARP), en VM o por RDP: Bakin abre con aviso «sin aceleración» y efectos bajos; si no puede, dice el requisito | M (anotado en Budgets) |
| R37 | β | Device lost (driver que se cae, suspensión): la ventana se recrea sin perder cola ni vista | M |
| R38 | α7 | La captura pierde acceso (UAC, bloqueo, RDP, pantalla completa exclusiva): vidrio sin refracción, reintento a los 2 s, nunca en bucle ni error visible | cargo test (simulado) + M |
| R39 | β | Un archivo de audio malformado no tira la app: se salta el tema y se avisa | cargo test con fixture corrupto |
| R40 | α1 | Abrir Bakin desde la bandeja, el atajo o una segunda instancia es lo único que trae la ventana al frente; con `BAKIN_HOME` nunca; ninguna prueba inyecta teclas ni mueve el mouse | cargo test (uso único de `activate_window`) + revisión del smoke |
| R41 | α3/α5 | Con `BAKIN_LIB`/`BAKIN_QUEUE` en cualquier valor, cada archivo tiene un solo escritor; el smoke pasa en los dos | smoke ×2 + test de dueños |
| R42 | α1 | Un archivo que `lofty` no lee (WMA, MP3 raro) se lista sin título; nunca cae al motor | cargo test + script sobre la biblioteca real |
| R43 | α0/α7 | Win10 con acrílico que traba el arrastre o no aplica: Bakin cae a Sobrio solo y lo dice en Apariencia | M + cargo test del fallback |
| R44 | α0/α4 | Dos monitores a 100 % y 150 %: la ventana y el mini se ven nítidos al cruzar; el visor y el encuadre respetan la escala | M |
| R45 | α0/α5/β | Protocolo PresentMon: sin Bakin / bandeja sonando / mini arriba (translúcido y opaco) / ventana en cada preajuste; el juego no pierde fps ni Independent Flip con Sobrio y mini opaco | M (planilla en Budgets) |

---

## 13. Qué cambia en `docs/PLAN.md` y dónde sigue

* PLAN §4 › 1.0 es ahora un resumen que apunta acá; H.1 se conserva allá en su forma corta.
* H.3–H.6 viven en β B5–B7 de este archivo.
* 0.19.1 y 0.19.2 de PLAN se hacen como α3 y α5; 0.19.3 (Wallhaven) como α7 P1.
* 0.17.3 (mini nativo) se cierra con α2; su C58 queda reemplazado por C116.
* `docs/HANDOFF.md` › «Cómo seguir» se reescribe en β B11 (release sin Tauri).
